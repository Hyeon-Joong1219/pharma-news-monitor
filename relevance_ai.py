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
import json
import logging
import time
import yaml
from db import get_db

logger = logging.getLogger(__name__)

BATCH_SIZE            = 15
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
BACKFILL_LIMIT        = 200  # 미분류(ai_classified=0) 기사 한 번에 재처리 최대 건수
MODEL                 = "llama-3.1-8b-instant"


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
    return Groq(api_key=api_key)


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
        summary    = (a.get("summary") or "")[:250]   # 150 → 250으로 확대
        lang       = a.get("lang", "")
        source     = (a.get("source") or "")[:25]

        if lang == "en":
            title_str = title_orig
            if title_ko:
                title_str += f" ({title_ko})"
        else:
            title_str = title_ko or title_orig

        lines.append(f'{i+1}. [{source}] {title_str} | {summary[:220]}')

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
        "EXAMPLES (사용 금지 — 참고용):\n"
        "  '홈플러스 인수합병' → 0  |  '이마트 실적' → 0  |  'STX 조선 합병' → 0\n"
        "  'SK하이닉스 HBM 투자' → 0  |  '현대차 전기차 판매' → 0  |  '크래프톤 게임' → 0\n"
        "  '사모펀드 PEF 비제약 M&A' → 0  |  '한약제제 한방치료 한의원' → 0\n"
        "  '랜섬웨어 해킹 사이버보안 침해' → 0  |  '챗GPT LLM 생성형AI 서비스' → 0\n"
        "  '포스코 탄소섬유 소재산업' → 0  |  '배터리 양극재 음극재' → 0\n"
        "  'FDA approves Pfizer cancer drug' → 10  |  '한미약품 기술이전 계약' → 10\n"
        "  '셀트리온 바이오시밀러 유럽 승인' → 10  |  '알테오젠 ADC 플랫폼 계약' → 9\n"
        "  '연합뉴스 경기 침체 우려' → 0  |  '부동산 청약 열기' → 0\n"
        "  'AI 신약개발 플랫폼 임상' → 7  |  'AI 보안 솔루션 출시' → 0\n\n"
        "Articles:\n" + "\n".join(lines) + "\n\n"
        "Reply with JSON array ONLY (no explanation, no markdown):\n"
        '[{"id":1,"score":8},{"id":2,"score":2},...]'
    )

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
            temperature=0,
        )
        raw = resp.choices[0].message.content.strip()
        # 마크다운 코드블록 제거
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:])
            raw = raw.rsplit("```", 1)[0].strip()
        data = json.loads(raw)
        return {articles[item["id"] - 1]["id"]: float(item["score"]) for item in data}
    except Exception as e:
        logger.warning(f"Groq 분류 실패 (배치): {e}")
        return {}


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
        with conn.cursor() as cur:
            for aid in blocked_ids:
                cur.execute(
                    "UPDATE articles SET hidden=1, ai_classified=1, relevance_score=0 WHERE id=%s",
                    (aid,),
                )
        conn.commit()
        logger.info(f"  [하드제외] {len(blocked_ids)}건 즉시 차단 (AI 생략)")

    return pass_through


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
                "ORDER BY fetched_at DESC LIMIT %s",
                (BACKFILL_LIMIT,),
            )
            rows = cur.fetchall()

        if not rows:
            return 0

        articles = [dict(r) for r in rows]
        logger.info(f"[Backfill] 미분류 기사 재처리: {len(articles)}건")

        # 하드 제외 먼저
        articles = _apply_hard_exclude(conn, articles)
        hard_blocked = len(rows) - len(articles)

        hidden_cnt = hard_blocked
        for i in range(0, len(articles), BATCH_SIZE):
            batch  = articles[i: i + BATCH_SIZE]
            scores = _classify_batch(client, batch)
            if not scores:
                logger.warning(f"[Backfill] 배치 {i//BATCH_SIZE+1} API 실패 — 건너뜀")
                time.sleep(2.0)
                continue
            with conn.cursor() as cur:
                for a in batch:
                    if a["id"] not in scores:
                        continue
                    score  = scores[a["id"]]
                    thresh = DEDICATED_THRESHOLD if a["source"] in dedicated else NON_DED_THRESHOLD
                    hidden = 1 if score < thresh else 0
                    if hidden:
                        hidden_cnt += 1
                    cur.execute(
                        "UPDATE articles SET relevance_score=%s, hidden=%s, ai_classified=1 WHERE id=%s",
                        (score * 10, hidden, a["id"]),
                    )
            conn.commit()
            time.sleep(2.0)

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

    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(days=days)
    cond   = "" if force else "AND (ai_classified IS NULL OR ai_classified = 0)"

    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT id, title, title_ko, summary, source, lang FROM articles "
            f"WHERE fetched_at >= %s {cond} ORDER BY fetched_at DESC",
            (cutoff,),
        )
        rows = cur.fetchall()

    all_articles = [dict(r) for r in rows]
    conn.close()

    if not all_articles:
        logger.info("AI 분류 대상 없음")
        # 대상이 없어도 백필 실행
        _run_backfill(client, dedicated)
        return 0

    logger.info(
        f"AI 관련성 분류 시작: {len(all_articles)}건 "
        f"(전체 분류 — dedicated 포함, 배치 {BATCH_SIZE}개씩)"
    )

    conn = get_db()
    hidden_cnt = 0
    try:
        # 1단계: 하드 제외 필터 (AI 생략, 즉시 차단)
        to_classify = _apply_hard_exclude(conn, all_articles)
        hard_blocked = len(all_articles) - len(to_classify)
        hidden_cnt += hard_blocked

        # 2단계: 나머지는 AI 분류
        for i in range(0, len(to_classify), BATCH_SIZE):
            batch  = to_classify[i: i + BATCH_SIZE]
            scores = _classify_batch(client, batch)
            batch_num = i // BATCH_SIZE + 1
            if not scores:
                logger.warning(f"  배치 {batch_num} API 실패 — 미분류 유지 (나중에 백필)")
                time.sleep(2.0)
                continue
            with conn.cursor() as cur:
                for a in batch:
                    if a["id"] not in scores:
                        continue
                    score  = scores[a["id"]]
                    thresh = DEDICATED_THRESHOLD if a["source"] in dedicated else NON_DED_THRESHOLD
                    hidden = 1 if score < thresh else 0
                    if hidden:
                        hidden_cnt += 1
                    cur.execute(
                        "UPDATE articles SET relevance_score=%s, hidden=%s, ai_classified=1 WHERE id=%s",
                        (score * 10, hidden, a["id"]),
                    )
            conn.commit()
            logger.info(f"  배치 {batch_num} 완료 ({len(batch)}건)")
            time.sleep(2.0)
    finally:
        conn.close()

    logger.info(f"AI 분류 완료 — hidden: {hidden_cnt}건 / {len(all_articles)}건 (하드제외 {hard_blocked}건 포함)")

    # 미분류 백필 (이번 run에서 처리 못한 기간 외 기사)
    backfill_hidden = _run_backfill(client, dedicated)
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
