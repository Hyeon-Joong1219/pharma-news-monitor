"""
Groq API 기반 제약/바이오 관련성 자동 분류기.

변경 이력:
  v2 — dedicated 소스 자동면제 제거, 이원 임계값, 미분류 백필, 프롬프트 강화
  v3 — 하드 제외 필터 추가: AI 전에 명백 비제약 카테고리 즉시 hidden 처리

동작 방식:
  1. 하드 제외 필터(_hard_exclude): AI 호출 없이 즉시 hidden=1
       — 한약/한방, 사이버보안/해킹, 배터리소재/소재산업, 일반AI(신약개발 제외)
       — 사모펀드(단, 제약·바이오 기업 대상이면 통과)
  2. Groq LLM 분류:
       — Dedicated 소스: score < 2 → hidden
       — Non-dedicated:  score < 5 → hidden  (4→5로 상향)
  3. 미분류 백필 처리

Groq 무료 계정 키 발급:
  1. https://console.groq.com 회원가입
  2. API Keys 메뉴에서 키 생성
  3. .env 파일에 GROQ_API_KEY=gsk_... 추가
"""

import os
import re
import json
import logging
import time
import yaml
from db import get_db, execute_batch_update

logger = logging.getLogger(__name__)

# 프롬프트 지시문(약 1.1천 토큰)이 배치마다 반복되므로 배치를 키워 일일 토큰을 아낀다 (15→25)
BATCH_SIZE            = 25
BATCH_INTERVAL        = 8.0  # 배치 사이 대기(초) — 분당 토큰 한도(8천)를 덜 두드리도록
DEDICATED_THRESHOLD   = 2    # dedicated 소스: 이 미만만 hidden
NON_DED_THRESHOLD     = 5    # non-dedicated: 이 미만 hidden (4→5로 강화)

# ── 하드 제외 필터 ────────────────────────────────────────────────────────────
# AI 호출 없이 즉시 hidden=1 처리.
# 각 규칙: exclude 중 하나라도 매칭 AND rescue가 하나도 없으면 → 즉시 차단.
_HARD_EXCLUDE_RULES = [
    # 한약·한방 — 제약과 완전히 다른 카테고리, rescue 없음
    {
        "exclude": [
            "한약", "한방", "한의원", "한의사", "한의학",
            "한약제제", "한약재", "침술", "침뜸", "뜸치료",
        ],
        "rescue": [],
    },
    # 사이버보안·해킹 — rescue 없음
    {
        "exclude": [
            "랜섬웨어", "사이버보안", "사이버 보안", "해킹", "보안취약점",
            "사이버공격", "악성코드", "정보보호", "사이버위협",
            "cybersecurity", "ransomware", "hacking", "malware", "cyberattack",
        ],
        "rescue": [],
    },
    # 배터리 소재·산업소재 — rescue 없음
    {
        "exclude": [
            "탄소섬유", "소재산업", "화학소재", "반도체 소재",
            "디스플레이 소재", "양극재", "음극재", "배터리셀",
            "이차전지 소재", "전구체",
        ],
        "rescue": [],
    },
    # 사모펀드·PEF — 제약/바이오 기업이 직접 대상이면 통과
    {
        "exclude": ["사모펀드", "사모투자", "PEF", "사모투자펀드"],
        "rescue": [
            "제약", "바이오", "의약품", "신약", "임상", "백신",
            "셀트리온", "한미약품", "유한양행", "대웅", "종근당", "녹십자",
            "HK이노엔", "보령", "동아ST", "동아제약", "롯데바이오", "SK바이오",
            "알테오젠", "휴온스", "메디톡스", "휴젤", "파마리서치",
        ],
    },
    # 일반 AI/LLM 뉴스 — 신약개발·의료 AI는 통과
    {
        "exclude": [
            "챗GPT", "ChatGPT", "생성형 AI", "생성형AI",
            "LLM 서비스", "AI 서비스", "AI 보안", "AI 반도체",
        ],
        "rescue": [
            "신약", "의약품", "임상", "제약", "바이오", "drug discovery",
            "drug design", "의료 AI", "의료AI", "신약개발 AI", "신약개발AI",
        ],
    },
]


def _hard_exclude(article: dict) -> bool:
    """True 반환 시 AI 분류 없이 즉시 hidden=1 처리."""
    text = " ".join(filter(None, [
        article.get("title") or "",
        article.get("title_ko") or "",
        article.get("summary") or "",
    ])).lower()

    for rule in _HARD_EXCLUDE_RULES:
        if any(kw.lower() in text for kw in rule["exclude"]):
            if not rule["rescue"] or not any(kw.lower() in text for kw in rule["rescue"]):
                return True
    return False
# 백필은 최근 3일·최대 60건만 — 예전엔 날짜 제한 없이 매 실행(하루 8회) 200건씩 재시도해
# gpt-oss-20b 일일 한도(20만 토큰)를 소진, 분류가 밀리면 백필이 더 늘어나는 악순환이 있었음 (2026-10-19)
BACKFILL_LIMIT        = 60
BACKFILL_DAYS         = 3
MAX_CONSECUTIVE_FAILS = 3    # 연속 API 실패 시 조기 중단 (Groq 장애·한도 초과 대비)  # 미분류(ai_classified=0) 기사 한 번에 재처리 최대 건수
MODEL                 = "openai/gpt-oss-20b"
# 일일 한도 소진 시 대체 모델 (Groq는 모델별로 한도가 따로라 분류가 멈추지 않게 함)
FALLBACK_MODEL        = "qwen/qwen3.8-27b"
_active_model         = [MODEL]   # 실행 중 한도 소진이 확인되면 FALLBACK_MODEL로 교체
_usage                = [0]       # 이번 실행의 누적 토큰 (로그로 일일 한도 대비 사용량 확인용)


def _load_api_key() -> str:
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        if os.path.exists(env_path):
            for line in open(env_path, encoding="utf-8"):
                line = line.strip()
                if line.startswith("GROQ_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    return key


def _get_client():
    try:
        from groq import Groq
    except ImportError:
        raise RuntimeError("groq 패키지 미설치: pip install groq")
    api_key = _load_api_key()
    if not api_key or api_key == "여기에_GROQ_API_키_입력":
        raise RuntimeError(
            "GROQ_API_KEY가 설정되지 않았습니다.\n"
            "  1. https://console.groq.com 에서 무료 회원가입\n"
            "  2. API Keys 메뉴에서 키 생성\n"
            "  3. .env 파일에 GROQ_API_KEY=gsk_... 추가"
        )
    # timeout: 응답 지연 시 배치 하나가 수 분씩 멈추지 않도록 함
    # max_retries=5: 무료 한도가 분당 8천 토큰이라 배치(약 3.5천 토큰)가 2~3개만 연달아 가도 429가 남.
    #   SDK가 retry-after만큼 기다렸다 재시도하므로, 예전(2회)처럼 연속 실패로 분류가 중단되지 않게 한다
    #   (2026-10 확인: 이 때문에 Daily Cleanup이 매일 실패하고 미분류 기사가 쌓였음)
    return Groq(api_key=api_key, timeout=45, max_retries=5)


def _load_dedicated_sources() -> set:
    try:
        with open("feeds.yaml", encoding="utf-8") as f:
            feeds = yaml.safe_load(f).get("feeds", [])
        return {fd["name"] for fd in feeds if fd.get("dedicated")}
    except Exception:
        return set()


def _classify_batch(client, articles: list) -> dict:
    """
    Groq LLM에게 기사 목록을 보내 관련성 점수(0~10)를 받아온다.
    반환: {article_id: score}
    """
    lines = []
    for i, a in enumerate(articles):
        title_orig = (a.get("title") or "")[:150]
        title_ko   = (a.get("title_ko") or "")[:100]
        # 관련성 판정엔 제목+요약 앞부분이면 충분 — 기사당 입력이 일일 토큰의 대부분이라 120자로 제한
        summary    = (a.get("summary") or "")[:120]
        lang       = a.get("lang", "")
        source     = (a.get("source") or "")[:25]

        if lang == "en":
            title_str = title_orig
            if title_ko:
                title_str += f" ({title_ko})"
        else:
            title_str = title_ko or title_orig

        lines.append(f'{i+1}. [{source}] {title_str} | {summary}')

    prompt = (
        "You are a strict gatekeeper for a Korean pharmaceutical/biotech industry intelligence system.\n"
        "Rate each article 0-10 for relevance to pharma/biotech/life sciences.\n\n"
        "SCORING GUIDE:\n"
        "  9-10 · FDA/EMA/식약처 drug approval or rejection\n"
        "        · Phase 1-3 clinical trial results or enrollment\n"
        "        · New drug/therapy launch or pipeline update\n"
        "        · Biosimilar, ADC, CAR-T, gene therapy, mRNA news\n"
        "        · Pharma/biotech company M&A, licensing deal, or IPO\n"
        "  7-8  · Pharma company earnings, strategic partnership\n"
        "        · Healthcare policy directly affecting drug access or pricing\n"
        "        · Medical device approval with clear pharma angle\n"
        "  5-6  · Biotech venture funding or spin-out\n"
        "        · Basic research with direct drug development implication\n"
        "        · Hospital/insurance news tied to specific drug treatment\n"
        "  3-4  · General health or wellness, loosely related to pharma\n"
        "  0-2  · NON-PHARMA — rate 0-2 when the article is PRIMARILY about:\n"
        "          retail/supermarkets (홈플러스/이마트/코스트코/쿠팡)\n"
        "          shipbuilding/heavy industry (STX/현대중공업/한화오션/조선소)\n"
        "          semiconductors (SK하이닉스/삼성전자 반도체/마이크론/인텔)\n"
        "          automobiles (현대차/기아/GM/테슬라/전기차)\n"
        "          steel/materials (포스코/현대제철/고려아연/소재산업/탄소섬유)\n"
        "          telecom (SKT/KT/LG유플러스/통신)\n"
        "          gaming (크래프톤/넥슨/엔씨소프트/배틀그라운드)\n"
        "          crypto/finance/real estate (비트코인/아파트 분양/재건축)\n"
        "          entertainment, sports, politics, general economy\n"
        "          private equity / PEF (사모펀드/사모투자/PEF) NOT targeting pharma\n"
        "          traditional/herbal medicine (한약/한방/한의원/한약제제/침술/뜸)\n"
        "          cybersecurity / IT security (사이버보안/랜섬웨어/해킹/보안취약점)\n"
        "          general AI/LLM news with no drug discovery angle (챗GPT/ChatGPT/LLM 단독)\n\n"
        "CRITICAL RULES:\n"
        "  · Judge by MAIN SUBJECT, not incidental keyword mentions\n"
        "  · Korean conglomerates (SK, 롯데, 삼성): rate HIGH only if the article\n"
        "    is specifically about their PHARMA/BIOTECH SUBSIDIARY, not the group overall\n"
        "    Example: '삼성바이오로직스 임상' → 10  |  '삼성전자 반도체 공장' → 0\n"
        "    Example: '롯데바이오로직스 위탁생산' → 9  |  '롯데쇼핑 실적' → 0\n"
        "    Example: 'SK바이오사이언스 백신' → 9   |  'SK하이닉스 HBM' → 0\n"
        "  · M&A: pharma/biotech target → 8-10  |  unrelated sector target → 0-1\n"
        "  · 'drug', 'hospital', 'health' alone do NOT guarantee high score — context matters\n"
        "  · When AI article mentions drug discovery/development angle → 5+\n"
        "  · General economic or political news even mentioning pharma in passing → 2-3\n\n"
        # 0점 예시는 위 0-2 목록과 같은 내용이라 생략 (배치마다 반복되는 토큰 절감)
        "EXAMPLES: 'FDA approves Pfizer cancer drug' → 10 | '한미약품 기술이전 계약' → 10 | "
        "'셀트리온 바이오시밀러 유럽 승인' → 10 | 'AI 신약개발 플랫폼 임상' → 7 | 'AI 보안 솔루션 출시' → 0\n\n"
        "Articles:\n" + "\n".join(lines) + "\n\n"
        "Reply with JSON array ONLY (no explanation, no markdown):\n"
        '[{"id":1,"score":8},{"id":2,"score":2},...]'
    )

    try:
        resp = _create(client, prompt)
        raw = (resp.choices[0].message.content or "").strip()
        if not raw:
            logger.warning("Groq 분류 실패 (배치): 빈 응답")
            return {}
        # 추론 블록(<think>)·마크다운 코드블록·앞뒤 설명문 제거 (대체 모델은 JSON 앞에 문장을 붙이기도 함)
        raw = raw.split("</think>")[-1]
        start, end = raw.find("["), raw.rfind("]")
        try:
            data = json.loads(raw[start:end + 1] if start >= 0 and end > start else raw)
        except json.JSONDecodeError:
            # gpt-oss가 마지막 항목을 '{"id":25,"score":10]}'처럼 괄호 순서를 틀리게 끝내는 일이 잦아
            # 배치 전체(25건)가 버려지고 매 실행 재전송돼 일일 한도를 소진했음 (2026-10-21 확인).
            # JSON이 깨져도 id·score 쌍은 온전하므로 정규식으로 건진다.
            data = [{"id": i, "score": s} for i, s in
                    re.findall(r'"id"\s*:\s*(\d+)\s*,\s*"score"\s*:\s*(\d+(?:\.\d+)?)', raw)]
            if not data:
                raise
        # LLM이 잘못된 id를 반환해도 배치 전체가 무효화되지 않도록 개별 검증
        result = {}
        for item in data:
            try:
                idx = int(item["id"]) - 1
                if 0 <= idx < len(articles):
                    result[articles[idx]["id"]] = float(item["score"])
            except (KeyError, ValueError, TypeError):
                continue
        return result
    except Exception as e:
        logger.warning(f"Groq 분류 실패 (배치): {e}")
        return {}


def _create(client, prompt: str):
    """분류 호출. 기본 모델의 일일 토큰 한도(TPD)가 소진되면 대체 모델로 바꿔 한 번 더 시도."""
    from groq import RateLimitError
    for _ in range(2):
        model = _active_model[0]
        kwargs = dict(model=model, messages=[{"role": "user", "content": prompt}], temperature=0)
        if model.startswith("openai/gpt-oss"):
            # gpt-oss는 추론 모델 — max_tokens가 낮으면 보이지 않는 추론 토큰만 소비하고
            # content가 빈 문자열로 반환되어 배치 전체가 미분류로 남는 문제가 있었음
            kwargs.update(max_tokens=1500, reasoning_effort="low")
        else:
            # qwen은 분당 출력 토큰 한도가 1천이라 max_tokens를 그 아래로, 추론은 끈다
            # (25건 점수 JSON은 실제로 300토큰 안팎)
            kwargs.update(max_tokens=900, reasoning_effort="none")
        try:
            resp = client.chat.completions.create(**kwargs)
            _usage[0] += getattr(resp.usage, "total_tokens", 0) or 0
            return resp
        except RateLimitError as e:
            if "tokens per day" in str(e) and model != FALLBACK_MODEL:
                logger.warning(f"{model} 일일 한도 소진 → {FALLBACK_MODEL}로 전환")
                _active_model[0] = FALLBACK_MODEL
                continue
            raise


def _apply_hard_exclude(conn, articles: list) -> list:
    """
    하드 제외 필터를 적용해 즉시 hidden=1 처리하고,
    AI 분류가 필요한 나머지 기사 목록을 반환한다.
    """
    pass_through = []
    blocked_ids  = []
    for a in articles:
        if _hard_exclude(a):
            blocked_ids.append(a["id"])
        else:
            pass_through.append(a)

    if blocked_ids:
        # 사용자가 직접 복구한 기사(user_feedback≠0)는 재차 숨기지 않음
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE articles SET "
                "hidden=CASE WHEN COALESCE(user_feedback,0)=0 THEN 1 ELSE hidden END, "
                "ai_classified=1, relevance_score=0 WHERE id = ANY(%s)",
                (blocked_ids,),
            )
        conn.commit()
        logger.info(f"  [하드제외] {len(blocked_ids)}건 즉시 차단 (AI 생략)")

    return pass_through


def _save_scores(conn, updates: list):
    """(relevance_score, hidden, id) 목록을 한 번의 왕복으로 반영."""
    execute_batch_update(
        conn,
        "UPDATE articles AS a SET relevance_score=v.rel, "
        "hidden=CASE WHEN COALESCE(a.user_feedback,0)=0 THEN v.hidden ELSE a.hidden END, "
        "ai_classified=1 "
        "FROM (VALUES %s) AS v(rel, hidden, id) WHERE a.id = v.id",
        updates,
        template="(%s::real, %s::int, %s::int)",
    )


REUSE_DAYS = 2   # 이 기간 안에 이미 분류된 같은 사건 기사가 있으면 점수를 그대로 쓴다


def _plan(conn, articles: list) -> tuple:
    """AI에 보낼 대표 기사와 점수를 재사용할 기사를 나눈다.
    같은 사건을 여러 매체가 쓰는 경우가 많아(하루 기사의 약 10%) 매번 전부 보내면 토큰 낭비.
      reused  : {article_id: score(0~10)} — 최근 이미 분류된 같은 사건 기사의 점수
      reps    : AI에 보낼 기사 목록
      members : {대표 id: [같은 사건 기사, ...]} — 대표 점수를 그대로 적용"""
    from dedup import signature, is_same_event
    with conn.cursor() as cur:
        cur.execute(
            "SELECT title, relevance_score FROM articles WHERE ai_classified = 1 "
            "AND relevance_score IS NOT NULL AND fetched_at >= NOW() - make_interval(days => %s) "
            "AND id <> ALL(%s)",
            (REUSE_DAYS, [a["id"] for a in articles]),
        )
        known = [(signature(r["title"]), r["relevance_score"] / 10) for r in cur.fetchall()]
    reused, reps, members, rep_sigs = {}, [], {}, []
    for a in articles:
        sig = signature(a.get("title"))
        hit = next((score for ks, score in known if is_same_event(ks, sig)), None)
        if hit is not None:
            reused[a["id"]] = hit
            continue
        rep = next((r for rs, r in rep_sigs if is_same_event(rs, sig)), None)
        if rep is not None:
            members[rep["id"]].append(a)
            continue
        rep_sigs.append((sig, a))
        reps.append(a)
        members[a["id"]] = []
    return reused, reps, members


def _classify_all(conn, client, articles: list, dedicated: set, label: str, abort_on_fail: bool) -> int:
    """기사 목록을 분류·저장하고 hidden 처리 건수를 반환 (본 분류·백필 공용)."""
    def _update(a, score):
        thresh = DEDICATED_THRESHOLD if a["source"] in dedicated else NON_DED_THRESHOLD
        return (score * 10, 1 if score < thresh else 0, a["id"])

    reused, reps, members = _plan(conn, articles)
    by_id = {a["id"]: a for a in articles}
    updates = [_update(by_id[i], s) for i, s in reused.items()]
    _save_scores(conn, updates)
    hidden_cnt = sum(u[1] for u in updates)
    n_members = sum(len(m) for m in members.values())
    logger.info(f"{label} 대상 {len(articles)}건 → 점수 재사용 {len(reused)}건, 같은 사건 묶음 {n_members}건, "
                f"AI 호출 {len(reps)}건 (배치 {BATCH_SIZE}개씩)")

    fails = 0
    for i in range(0, len(reps), BATCH_SIZE):
        batch     = reps[i: i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        scores    = _classify_batch(client, batch)
        if not scores:
            logger.warning(f"{label} 배치 {batch_num} API 실패 — 미분류 유지 (다음 실행에서 재시도)")
            fails += 1
            if fails >= MAX_CONSECUTIVE_FAILS:
                if abort_on_fail:
                    # Groq 장애/한도 초과 시 남은 배치를 계속 두드리며 CI 시간을 소진하지 않음
                    raise RuntimeError(f"Groq API {fails}회 연속 실패 — AI 분류 중단")
                logger.error(f"{label} API {fails}회 연속 실패 — 중단 (다음 실행에서 재시도)")
                break
            time.sleep(BATCH_INTERVAL)
            continue
        fails = 0
        updates = []
        for a in batch:
            if a["id"] in scores:
                for x in [a] + members[a["id"]]:
                    updates.append(_update(x, scores[a["id"]]))
        _save_scores(conn, updates)
        hidden_cnt += sum(u[1] for u in updates)
        logger.info(f"{label} 배치 {batch_num} 완료 ({len(batch)}건)")
        time.sleep(BATCH_INTERVAL)
    return hidden_cnt


def _run_backfill(client, dedicated: set) -> int:
    """
    ai_classified=0 인 기사를 최대 BACKFILL_LIMIT 건 재분류.
    fetch 후 AI 분류가 실패했거나 이전 미처리 기사를 소급 처리한다.
    """
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, title_ko, summary, source, lang FROM articles "
                "WHERE (ai_classified IS NULL OR ai_classified = 0) "
                "AND fetched_at >= NOW() - make_interval(days => %s) "
                "ORDER BY fetched_at DESC LIMIT %s",
                (BACKFILL_DAYS, BACKFILL_LIMIT),
            )
            rows = cur.fetchall()

        if not rows:
            return 0

        articles = [dict(r) for r in rows]
        # 하드 제외 먼저
        articles = _apply_hard_exclude(conn, articles)
        hidden_cnt = len(rows) - len(articles)
        hidden_cnt += _classify_all(conn, client, articles, dedicated, "[Backfill]", abort_on_fail=False)
        logger.info(f"[Backfill] 완료 — hidden: {hidden_cnt}건 / {len(rows)}건")
        return hidden_cnt
    finally:
        conn.close()


def run_relevance_classification(days: int = 3, force: bool = False) -> int:
    """
    최근 `days`일 기사 전체(dedicated 포함)를 Groq으로 분류.
    force=True 이면 이미 분류된 기사도 재분류.
    분류 후 ai_classified=0 미처리 기사를 추가 백필.
    반환: hidden 처리된 기사 수
    """
    try:
        client = _get_client()
    except RuntimeError as e:
        logger.error(str(e))
        return 0

    dedicated = _load_dedicated_sources()
    _usage[0] = 0

    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(days=days)
    cond   = "" if force else "AND (ai_classified IS NULL OR ai_classified = 0)"

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, title, title_ko, summary, source, lang FROM articles "
                f"WHERE fetched_at >= %s {cond} ORDER BY fetched_at DESC",
                (cutoff,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    all_articles = [dict(r) for r in rows]

    if not all_articles:
        logger.info("AI 분류 대상 없음")
        # 대상이 없어도 백필 실행
        _run_backfill(client, dedicated)
        return 0

    conn = get_db()
    try:
        # 1단계: 하드 제외 필터 (AI 생략, 즉시 차단)
        to_classify  = _apply_hard_exclude(conn, all_articles)
        hard_blocked = len(all_articles) - len(to_classify)
        # 2단계: 점수 재사용·같은 사건 묶음 후 나머지만 AI 분류
        hidden_cnt = hard_blocked + _classify_all(conn, client, to_classify, dedicated,
                                                  "AI 분류", abort_on_fail=True)
    finally:
        conn.close()

    logger.info(f"AI 분류 완료 — hidden: {hidden_cnt}건 / {len(all_articles)}건 (하드제외 {hard_blocked}건 포함)")

    # 미분류 백필 (이번 run에서 처리 못한 기간 외 기사)
    backfill_hidden = _run_backfill(client, dedicated)
    logger.info(f"AI 분류 토큰 사용: {_usage[0]:,} (모델 {_active_model[0]})")
    return hidden_cnt + backfill_hidden


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    force = "--force" in sys.argv
    days  = 3
    for arg in sys.argv[1:]:
        if arg.isdigit():
            days = int(arg)
    run_relevance_classification(days=days, force=force)
