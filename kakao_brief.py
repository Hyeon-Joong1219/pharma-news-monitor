"""평일 아침 영업 브리핑을 카카오톡 '나에게 보내기'로 발송.

흐름: 직전 발송 이후 기사 조회 → 고객사(미팅 상대 포함)·경쟁사·관심 주제 / 일반 상위 기사로 분류 →
      Groq로 '내 타겟'·'업계 주요 뉴스' 각 최대 10개 요약 → 카카오 토큰 갱신 →
      리스트 메시지(3항목씩, 묶음별 최대 4통) 발송
      (각 항목 클릭 → 대시보드 /go/<id> → 원문 기사)

필요 환경변수
  DATABASE_URL, GROQ_API_KEY
  KAKAO_REST_API_KEY      카카오 디벨로퍼스 앱의 REST API 키
  KAKAO_REFRESH_TOKEN     scripts/kakao_auth.py로 최초 1회 발급한 refresh token
  KAKAO_CLIENT_SECRET     (선택) 앱에서 Client Secret을 '사용함'으로 켠 경우
  DASHBOARD_URL           (선택) 대시보드 주소. 카카오 앱 [플랫폼 > Web]에 도메인 등록 필요.
                          없으면 링크 버튼 없이 발송

사용법: python kakao_brief.py [--dry-run] [--if-due]
  --dry-run  발송 없이 콘솔 출력만
  --if-due   평일 06시 이후 + 오늘 미발송일 때만 발송 (예약 실행용)
"""
import datetime
import json
import os
import re
import sys

import requests

from db import get_db
from kwmatch import contains_keyword, contains_name

KAKAO_TOKEN_URL = "https://kauth.kakao.com/oauth/token"
KAKAO_MEMO_URL  = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
TEXT_LIMIT      = 200   # 카카오 텍스트 템플릿 최대 글자 수 (기사 없음 안내용)
PER_MESSAGE     = 3     # 카카오 리스트 템플릿 항목 상한
MAX_ITEMS       = 10    # 묶음(내 타겟 / 업계 주요 뉴스)별 최대 항목 수 → 리스트 메시지 최대 4통씩
TITLE_LIMIT     = 45    # 리스트 항목 제목/설명은 카톡에서 2줄까지만 보임
DESC_LIMIT      = 60
MODEL           = "openai/gpt-oss-120b"

# AI 분류 완료 기사는 관련성×키워드점수, 미분류는 키워드점수 (server.py와 동일 지표)
_EFF_SCORE = (
    "CASE WHEN ai_classified=1 "
    "THEN (COALESCE(relevance_score,50)*COALESCE(score,0)/100.0) "
    "ELSE COALESCE(score,0) END"
)


# ── 1. 기사 선별 ────────────────────────────────────────────────────

CUST_MAX    = 20   # LLM에 넘길 고객사 기사 상한 (프롬프트 길이 보호용 — 평소 하루 0~6건)
COMP_MAX    = 6    # 경쟁사 기사
TOPIC_MAX   = 12   # 관심 주제 기사 (ADC 기사가 하루 10건 안팎이라 다른 주제 몫을 남김)
GENERAL_MAX = 20   # 업계 주요 뉴스 후보 (LLM이 이 중 최대 10개를 고름)
MAX_WINDOW  = datetime.timedelta(days=5)   # 발송이 오래 끊겼을 때 다룰 최대 기간 (연휴 대비)

# 인사·부고·채용 공고는 회사명이 제목에 있어도 영업 소식이 아님
_SKIP_RE    = re.compile(r"\[(인사|부고|채용|경력채용|신입채용)\]|채용 공고")
# 증시 시황처럼 회사명이 수십 개 나열되는 기사 — 본문 매칭은 무시하고 제목 매칭만 인정
_ROUNDUP_RE = re.compile(r"\[(주가|주식|증시|특징주|마감시황)\]|변동 현황|맥짚기")
# Google News 기사는 제목 끝에 ' - 매체명'이 붙어 같은 기사도 제목이 달라짐
_SOURCE_SUFFIX_RE = re.compile(r"\s+-\s+[^-]{1,30}$")


def _ensure_log_table(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS kakao_brief_log (sent_at TIMESTAMP NOT NULL DEFAULT NOW())")
    conn.commit()


def brief_since(conn) -> datetime.datetime:
    """이번 브리핑이 다룰 기사 시작 시점(naive UTC) = 직전 발송 시각.
    평일에만 발송하므로 월요일엔 자동으로 금요일 아침 이후(주말분)가 포함된다.
    (과거엔 항상 '최근 24시간'이라 금 07:30~일 07:30 기사가 어느 브리핑에도 안 들어갔음)"""
    _ensure_log_table(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(sent_at) AS t FROM kakao_brief_log")
        last = cur.fetchone()["t"]
    now = datetime.datetime.utcnow()
    if last is None:
        monday = (now + datetime.timedelta(hours=9)).weekday() == 0
        return now - datetime.timedelta(hours=72 if monday else 24)
    return max(last, now - MAX_WINDOW)


def is_due(conn) -> bool:
    """--if-due 실행 시 발송 여부: 평일(KST) 06시 이후이고 오늘 아직 안 보냈을 때만.
    GitHub 예약 실행은 1~2시간씩 밀리거나 건너뛰어져(2026-10-19 첫 예약 발송 누락)
    브리핑 예약과 3시간마다 도는 수집 작업 양쪽에서 이 조건으로 호출해 하루 한 번은 반드시 보낸다."""
    now_kst = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    if now_kst.weekday() >= 5 or now_kst.hour < 6:
        return False
    _ensure_log_table(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(sent_at) AS t FROM kakao_brief_log")
        last = cur.fetchone()["t"]
    return last is None or (last + datetime.timedelta(hours=9)).date() < now_kst.date()


def record_sent(conn):
    _ensure_log_table(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO kakao_brief_log (sent_at) VALUES (NOW())")
    conn.commit()


def _company_hits(r: dict, companies: list) -> list:
    """기사에 등장하는 담당 고객사 (제목 매칭 우선, 나열형 기사는 제목만)."""
    title = " ".join(filter(None, [r.get("title"), r.get("title_ko")])).lower()
    if _SKIP_RE.search(title):
        return []
    hits = [c for c in companies if contains_name(title, c.lower())]
    if hits or _ROUNDUP_RE.search(title):
        return hits
    body = " ".join(filter(None, [r.get("summary"), r.get("summary_ko")])).lower()
    return [c for c in companies if contains_name(body, c.lower())]


def _load_met_accounts(conn) -> list:
    """내가 미팅한 상대(met_accounts — scripts/sync_met_accounts.py가 Obsidian 미팅록에서 동기화)."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('met_accounts') AS t")
        if not cur.fetchone()["t"]:
            return []
        cur.execute("SELECT account, aliases, last_meeting, meeting_count FROM met_accounts "
                    "ORDER BY last_meeting DESC")
        accounts = [dict(r) for r in cur.fetchall()]
    for acc in accounts:
        acc["aliases"] = [a for a in acc["aliases"].split("|") if a]
    return accounts


def load_articles(conn, since: datetime.datetime) -> tuple:
    """({'C': 고객사, 'X': 경쟁사, 'T': 관심 주제, 'G': 일반 상위} 기사, 워치리스트 개수) 반환.

    고객사 기사는 점수와 무관하게 전부 LLM에 넘긴다. 과거엔 'ADC'·'비만치료제' 같은
    주제 키워드와 한 칸(상위 15개)을 나눠 써서, 점수가 낮은 EB 고객사 기사의 65%가
    LLM에 도달하기 전에 잘렸음 (2026-10 실측: 큐로셀 CAR-T 임상 진입 등).
    미팅한 상대는 워치리스트에 없어도 고객사로 보고, 기사에 'met'(미팅 이력)를 붙인다."""
    with conn.cursor() as cur:
        cur.execute("SELECT keyword, COALESCE(kind, 'company') AS kind FROM watchlist ORDER BY keyword")
        watchlist = cur.fetchall()
        cur.execute(
            f"SELECT id, title, title_ko, summary, summary_ko, source, link, cluster_id, "
            f"{_EFF_SCORE} AS eff "
            f"FROM articles "
            f"WHERE (hidden IS NULL OR hidden = 0) "
            f"AND fetched_at >= %s "
            f"AND COALESCE(published_dt, fetched_at) >= %s - INTERVAL '1 day' "
            # LIMIT 없음: 하루 600건 안팎이라 점수 상위 N건만 가져오면 점수가 낮은 고객사 기사가 잘림
            f"ORDER BY eff DESC, fetched_at DESC",
            (since, since),
        )
        rows = [dict(r) for r in cur.fetchall()]
    met = _load_met_accounts(conn)
    companies   = [w["keyword"] for w in watchlist if w["kind"] == "company"]
    companies  += [a for acc in met for a in acc["aliases"] if a not in companies]
    competitors = [w["keyword"] for w in watchlist if w["kind"] == "competitor"]
    topics      = [w["keyword"] for w in watchlist if w["kind"] == "topic"]

    # 같은 사건을 여러 매체가 보도한 경우 대표 기사 1건만 남김
    # (cluster_id는 단독 기사에도 채워지므로 정규화한 제목으로도 한 번 더 거른다)
    seen_cluster, seen_title, uniq = set(), set(), []
    for r in rows:
        tkey = _SOURCE_SUFFIX_RE.sub("", r["title"] or "")
        if r["cluster_id"] in seen_cluster or tkey in seen_title:
            continue
        if r["cluster_id"]:
            seen_cluster.add(r["cluster_id"])
        seen_title.add(tkey)
        uniq.append(r)

    cust, comp, topic, general = [], [], [], []
    for r in uniq:
        hits = _company_hits(r, companies)
        if hits:
            r["tag"] = f"고객사:{', '.join(hits)}"
            hit_low = {h.lower() for h in hits}
            for acc in met:   # 최근 미팅 순이므로 첫 매칭이 가장 최근 미팅
                if hit_low & {a.lower() for a in acc["aliases"]}:
                    r["met"] = f"미팅 {acc['last_meeting']:%m/%d}"
                    break
            cust.append(r)
            continue
        hits = _company_hits(r, competitors)
        if hits:
            r["tag"] = f"경쟁사:{', '.join(hits)}"
            comp.append(r)
            continue
        text = " ".join(filter(None, [r["title"], r["title_ko"], r["summary"], r["summary_ko"]])).lower()
        hits = [t for t in topics if contains_keyword(text, t.lower())]
        if hits:
            r["tag"] = f"주제:{', '.join(hits)}"
            topic.append(r)
        else:
            general.append(r)
    # 미팅한 고객사 기사를 앞으로 (상한에 걸려 잘리지 않도록)
    cust.sort(key=lambda r: "met" not in r)
    buckets = {"C": cust[:CUST_MAX], "X": comp[:COMP_MAX],
               "T": topic[:TOPIC_MAX], "G": general[:GENERAL_MAX]}
    return buckets, len(watchlist)


# ── 2. 영업 관점 요약 ───────────────────────────────────────────────

# LLM이 막연한 시사점("배지·시약 제안 필요")을 쓰지 않도록 우리 제품과 영업 신호를 구체적으로 알려준다.
# ※ 이 저장소는 공개이고 프롬프트는 외부 API(Groq)로 전송되므로 특정 고객의 정보는 넣지 않는다 —
#   아래 내용은 영업 현장에서 반복되는 패턴을 일반화한 것.
_CONTEXT = (
    "당신은 Merck Life Science Korea Process Solutions(바이오의약품 생산 공정 솔루션) 영업팀 "
    "Emerging Biotech(EB) 담당자를 돕는 애널리스트입니다.\n"
    "[우리가 파는 것] 세포배양 배지·피드와 CHO 세포주 플랫폼(CHOZN, 연구용·상업용 라이선스), 곤충세포 배지, "
    "정제 레진(Eshmuno)·멤브레인 크로마토그래피(Natrix), depth·멸균·바이러스 제거 필터와 TFF(Pellicon), "
    "세포 용해·핵산 분해 시약(Benzonase), 일회용 백·바이오리액터(Mobius), ADC 접합 CDMO 서비스, "
    "바이러스 안전성 시험(BioReliance), 공정 분석(Raman), 교육·기술 지원 프로그램\n"
    "[우리가 하지 않는 것] 신약 후보물질 발굴, 임상 대행(CRO), 투자, 완제의약품 판매 — 시사점에 쓰지 않는다\n"
    "[고객] 국내 EB(전임상~임상 2상 바이오텍), CDMO, 정부·공공 바이오 지원기관(허브·재단)\n\n"
)

# '내 고객·경쟁사' 묶음 전용 — 신호별로 영업이 실제로 하는 일을 연결해 준다
_PLAYBOOK = (
    "[신호 → 영업이 할 일]\n"
    "- 투자 유치·유상증자 → 공정개발 예산이 생김: 세포주·배지 스크리닝, 정제 레진 스크리닝 제안 시점\n"
    "- 임상 진입·다음 상 진입 → GMP 원부자재 확정·scale-up: 배지·필터 공급 계획, 바이러스 안전성 시험, "
    "후기 임상이면 연속 배양(perfusion) 공정\n"
    "- IND·BLA·품목허가 신청 → 원부자재를 바꾸기 어려워지는 시점: 공급 안정성·규제 문서 지원\n"
    "- 기술수출(L/O) → 공정이 파트너사로 넘어감: 세포주 상업 라이선스, 파트너사 공급 연결\n"
    "- CDMO 선정·계약·기술이전 → 발주 주체가 CDMO로 바뀜: CDMO 쪽 공급·3자 협의\n"
    "- 공장 착공·증설·GMP 인증 → 일회용 시스템·필터·TFF 대량 수요\n"
    "- 모달리티별: ADC=접합 공정 장비·멤브레인·ADC CDMO, AAV·세포유전자치료=업스트림 배지·용해/핵산 분해·"
    "affinity 레진·바이러스 클리어런스, mRNA=capture 레진, 이중항체·Fc 융합=세포주·배지·정제\n"
    "- 공공기관·허브의 사업 공고·착공·입주사 모집 → 입주사 대상 교육 프로그램, 단가계약(입찰), 신규 고객 발굴\n"
    "- 임상 실패·중단·파이프라인 축소·구조조정 → 발주 지연·감소 위험: 진행 중 건 일정 확인\n"
    "- 경쟁사의 국내 투자·신제품·가격 정책 → 고객 이탈 위험: 비교 데이터·대응 자료 준비\n"
    "[insight 예시 — 회사명은 가상]\n"
    "- 좋음: (A바이오 200억 시리즈B 기사) '세포주·배지 스크리닝 제안 시점'\n"
    "- 좋음: (B테라퓨틱스 2상 진입 기사) 'GMP 배지·필터 공급 계획 확인'\n"
    "- 좋음: (C사 글로벌 기술수출 기사) '세포주 상업 라이선스 조건 점검'\n"
    "- 나쁨: '배지·필터 수요 증가'(어느 기사에나 붙는 일반론), '후보 물질 지원'(우리가 하지 않는 일), "
    "'투자 유치 신호 →'(신호 이름만 반복)\n\n"
)

_RULES = (
    "작성 규칙:\n"
    "- 기사 제목·요약에 적힌 사실만 쓴다. 없는 수치·일정·계획·인과관계를 지어내지 않는다\n"
    f"- headline: 기사 제목을 복사하지 말고 '회사명: 핵심 사건' 형식으로 {TITLE_LIMIT - 5}자 이내로 압축 "
    "(회사명·제품명 철자만 기사 표기 그대로)\n"
    f"- insight: {DESC_LIMIT - 10}자 이내 (내용은 아래 [과제]의 지시를 따른다)\n"
    "- level: red=이번 주 안에 연락·확인할 일이 생긴 경우(담당 고객사 기사만 가능), "
    "yellow=관련은 있으나 당장 할 일은 없음, white=참고. 행사 개최·수상·학회 발표만으로는 red가 아니다\n"
    "- 같은 회사의 같은 사건(예: 같은 허가·같은 학회 발표·같은 증자를 다룬 여러 매체 기사)은 "
    "가장 구체적인 기사 하나만 고른다. 중요한 순서대로, 쉬운 한국어\n"
    "- ref는 기사 앞에 붙은 번호를 그대로 쓴다\n"
    '- 출력은 JSON만: {"items": [{"ref": "<기사 번호>", "level": "yellow", "headline": "...", "insight": "..."}]}\n\n'
)

_LEVEL_EMOJI = {"red": "🔴", "yellow": "🟡", "white": "⚪"}
_LEVEL_RANK  = {"white": 0, "yellow": 1, "red": 2}


def _fmt(ref: str, a: dict) -> str:
    title   = (a.get("title_ko") or a.get("title") or "")[:120]
    summary = (a.get("summary_ko") or a.get("summary") or "")[:180]
    tag     = f" <{a['tag']}>" if a.get("tag") else ""
    return f"{ref}. [{(a.get('source') or '')[:20]}] {title}{tag}" + (f" | {summary}" if summary else "")


def _section(prefix: str, articles: list) -> str:
    return "\n".join(_fmt(f"{prefix}{i+1}", a) for i, a in enumerate(articles)) or "(없음)"


def _ask(client, prompt: str) -> list:
    """LLM 응답(JSON)의 항목 목록. gpt-oss는 추론 모델이라 max_tokens가 작으면 빈 응답이
    올 수 있음 → 넉넉히 + 1회 재시도."""
    for _ in range(2):
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=3000, temperature=0.2, reasoning_effort="low",
            response_format={"type": "json_object"},
        )
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            continue
        try:
            return json.loads(content).get("items") or []
        except (json.JSONDecodeError, AttributeError):
            continue
    raise RuntimeError("Groq가 빈 응답 또는 잘못된 JSON을 반환했습니다.")


def _to_items(raw: list, refs: dict, allowed: str) -> list:
    """LLM 항목 → [(제목, 설명, 기사)]. 이번 묶음의 번호(allowed 접두어)가 아니거나 없는 번호는
    링크가 엉뚱한 기사로 걸리므로 제외 (일반 기사에 예시 번호 'C1'을 붙인 사례가 있었음).
    🔴는 담당 고객사(C) 기사에만 허용 — 그 외는 LLM이 red를 줘도 🟡로 낮춘다."""
    items, used = [], set()
    for it in raw:
        ref = str(it.get("ref", "")).strip("[] ")
        a = refs.get(ref) if ref[:1] in allowed else None
        if not a or a["id"] in used:
            continue
        used.add(a["id"])
        level = it.get("level") if it.get("level") in _LEVEL_RANK else "white"
        if ref[0] != "C" and _LEVEL_RANK[level] > _LEVEL_RANK["yellow"]:
            level = "yellow"
        mark  = _LEVEL_EMOJI[level]
        mark += "🤝" if a.get("met") else ""   # 미팅 이력은 LLM이 아니라 코드가 표시 (누락·환각 방지)
        title = f"{mark} {str(it.get('headline', '')).strip()}"
        desc  = str(it.get("insight", "")).strip()
        if a.get("met"):
            desc = f"[{a['met']}] {desc}"
        items.append((_clip(title, TITLE_LIMIT), _clip(desc, DESC_LIMIT), a))
    return items[:MAX_ITEMS]


def generate_brief(buckets: dict) -> tuple:
    """(내 타겟 항목, 업계 주요 뉴스 항목) 반환. 각 [(제목, 설명, 기사), ...]."""
    from groq import Groq
    # Groq 무료 한도가 분당 8천 토큰이라 두 번째 호출이 429를 받을 수 있음 → SDK가 retry-after만큼 기다렸다 재시도
    client = Groq(api_key=os.environ["GROQ_API_KEY"], timeout=90, max_retries=4)
    refs = {f"{p}{i+1}": a for p, group in buckets.items() for i, a in enumerate(group)}

    target = []
    if buckets["C"] or buckets["X"] or buckets["T"]:
        target = _to_items(_ask(client, _CONTEXT + _PLAYBOOK + _RULES + (
            f"[과제] 아래 기사로 '내 고객·경쟁사 브리핑' 항목을 최대 {MAX_ITEMS}개 고른다.\n"
            "- insight는 위 [신호 → 영업이 할 일]에서 해당하는 것을 찾아 할 일만 쓴다(headline 내용 반복 금지). "
            "해당하는 신호가 없으면 억지로 연결하지 말고 '동향 참고'로 쓰고 level은 white\n"
            "- 공공기관·허브 기사는 행사 자체보다 입주사·지원사업과 연결되는 할 일을 쓴다\n"
            "- 담당 고객사(C)는 단순 주가 등락·인사·채용처럼 영업과 무관한 경우만 빼고 모두 포함, 맨 앞에\n"
            "- 경쟁사(X)는 국내 영업에 영향이 큰 것(국내 투자·공급 이슈·신제품·가격·인수합병)만 최대 2개, "
            "insight에 머크의 대응 포인트\n"
            "- 남는 자리는 관심 주제(T) 중 국내 바이오텍·CDMO의 사업 소식(임상 진입·투자·계약·시설)만. "
            "해외 제약사 제품·보험 급여·정책 기사는 제외하고, 해당이 없으면 비워 둔다(억지로 채우지 않음)\n\n"
            "[담당 고객사 기사]\n" + _section("C", buckets["C"]) +
            "\n\n[경쟁사 기사]\n" + _section("X", buckets["X"]) +
            "\n\n[관심 주제 기사]\n" + _section("T", buckets["T"])
        )), refs, "CXT")
        # 담당 고객사 기사는 LLM이 빠뜨려도 반드시 전달 (테스트에서 KIMCo 기사를 누락한 사례가 있었음).
        # 같은 사건의 다른 매체 기사일 수 있어 이미 다룬 고객사의 추가 기사는 보충하지 않는다
        covered = {a["tag"] for _, _, a in target if a.get("tag", "").startswith("고객사:")}
        for a in buckets["C"]:
            if a["tag"] not in covered and len(target) < MAX_ITEMS:
                covered.add(a["tag"])
                target += _to_items([{"ref": f"C{buckets['C'].index(a) + 1}", "level": "white",
                                      "headline": a.get("title_ko") or a.get("title") or "",
                                      "insight": "동향 참고"}], refs, "C")

    general = []
    if buckets["G"]:
        general = _to_items(_ask(client, _CONTEXT + _RULES + (
            f"[과제] 아래 기사로 '업계 주요 뉴스' 항목을 최대 {MAX_ITEMS}개 고른다.\n"
            "- 국내외 제약·바이오 업계 사람이 오늘 알아야 할 뉴스: 규제(FDA·식약처 승인·정책), 대형 거래"
            "(인수합병·기술수출), 주요 임상 결과, 바이오 생산·CDMO 산업 동향을 우선\n"
            "- 건강 상식·병원 행사·인사·단순 주가·학회 참가 홍보 기사는 제외\n"
            "- insight는 이 뉴스가 왜 중요한지 한 문장(예: '국내 첫 4차 치료제', '키트루다 시밀러 경쟁 본격화'). "
            "이 묶음에서는 '동향 참고'를 쓰지 않는다. 머크 영업과 직접 연결될 때만 그 내용을 적고, "
            "'배지·필터 수요 증가'처럼 어느 기사에나 붙일 수 있는 일반론은 쓰지 않는다\n"
            "- 출력 전에 같은 제품·같은 회사의 같은 사건이 두 번 들어갔는지 확인하고 하나만 남긴다\n\n"
            "[기사]\n" + _section("G", buckets["G"])
        )), refs, "G")
    return target, general


def _clip(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def _link(url: str) -> dict:
    return {"web_url": url, "mobile_web_url": url}


def build_messages(header: str, items: list, base_url: str) -> list:
    """3개씩 묶어 카카오 리스트 템플릿 목록으로 변환. 항목을 누르면 원문 기사로 이동.
    원문 URL은 200~400자로 길고 도메인도 제각각이라(카카오는 등록 도메인만 링크 허용)
    대시보드 /go/<id>를 거쳐 리다이렉트한다."""
    # 리스트 템플릿은 항목이 2~3개여야 함 → 마지막 통에 1개만 남지 않도록 고르게 분배
    # (예: 7개 → 3·2·2). 1개뿐이면 호출부에서 텍스트 템플릿으로 보낸다.
    n_chunks = -(-len(items) // PER_MESSAGE)
    sizes    = [len(items) // n_chunks + (1 if i < len(items) % n_chunks else 0) for i in range(n_chunks)]
    chunks, pos = [], 0
    for size in sizes:
        chunks.append(items[pos:pos + size])
        pos += size
    templates = []
    for n, chunk in enumerate(chunks, 1):
        page = f" ({n}/{len(chunks)})" if len(chunks) > 1 else ""
        templates.append({
            "object_type":  "list",
            "header_title": f"{header}{page}",
            "header_link":  _link(base_url),
            "contents": [
                {"title": title, "description": desc,
                 "link": _link(f"{base_url}/go/{a['id']}" if (base_url and a) else base_url)}
                for title, desc, a in chunk
            ],
            "buttons": [{"title": "대시보드 전체 보기", "link": _link(base_url)}] if base_url else [],
        })
    return templates


def to_templates(header: str, items: list, base_url: str) -> list:
    """리스트 템플릿은 링크 필수·항목 2개 이상 → 조건이 안 되면 텍스트 1통으로 대체."""
    if base_url and len(items) >= 2:
        return build_messages(header, items, base_url)
    link = f"{base_url}/go/{items[0][2]['id']}" if (base_url and items[0][2]) else base_url
    lines = [f"{t}\n{d}" if d else t for t, d, _ in items[:3]]
    return [text_template(f"{header}\n\n" + "\n\n".join(lines), link)]


def text_template(text: str, url: str) -> dict:
    t = {"object_type": "text", "text": _clip(text, TEXT_LIMIT), "link": _link(url)}
    if url:
        t["button_title"] = "대시보드 열기"
    return t


def collect_warnings(conn) -> list:
    """수집 파이프라인 이상 징후. 피드가 죽거나 AI 분류가 밀려도 브리핑은 평소처럼 와서
    기사가 빠지는 걸 알 수 없었음 (2026-10 확인: 더벨 등 7개 피드가 '정상' 상태로 7일간 0건)."""
    import yaml
    with open("feeds.yaml", encoding="utf-8") as f:
        names = [fd["name"] for fd in yaml.safe_load(f)["feeds"]]
    warnings = []
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(last_run_at) AS t FROM feed_health")
        last_run = cur.fetchone()["t"]
        if last_run:
            hours = (datetime.datetime.utcnow() - last_run).total_seconds() / 3600
            if hours > 7:   # fetch는 3시간 주기 → 두 번 연속 누락
                warnings.append(f"뉴스 수집이 {hours:.0f}시간째 멈춤")
        cur.execute("SELECT COUNT(*) AS n FROM articles "
                    "WHERE fetched_at >= NOW() - INTERVAL '24 hours' AND COALESCE(ai_classified, 0) = 0")
        pending = cur.fetchone()["n"]
        if pending >= 30:
            warnings.append(f"AI 분류 밀림 {pending}건 (Groq 한도 의심)")
        cur.execute(
            "SELECT h.source FROM feed_health h WHERE h.source = ANY(%s) AND ("
            "  h.consecutive_failures >= 8 OR NOT EXISTS ("
            "    SELECT 1 FROM articles a WHERE a.source = h.source "
            "    AND a.fetched_at >= NOW() - INTERVAL '7 days')) "
            "ORDER BY h.source",
            (names,),
        )
        dead = [r["source"] for r in cur.fetchall()]
        if dead:
            warnings.append(f"피드 {len(dead)}곳 7일째 수집 0건: {', '.join(dead)}")
    return warnings


def _describe(t: dict) -> str:
    if t["object_type"] == "text":
        return t["text"]
    rows = [f"■ {t['header_title']}"]
    for c in t["contents"]:
        rows.append(f"  {c['title']}\n     {c['description']}\n     → {c['link']['web_url'] or '(링크 없음)'}")
    return "\n".join(rows)


# ── 3. 카카오 발송 ──────────────────────────────────────────────────

def _ensure_token_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS kakao_token (
                id            INTEGER PRIMARY KEY DEFAULT 1,
                refresh_token TEXT NOT NULL,
                updated_at    TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)
    conn.commit()


def get_access_token(conn) -> str:
    """refresh token으로 access token 발급.
    카카오는 refresh token 만료 1개월 전부터 갱신 시 새 refresh token을 내려주므로
    DB에 저장해 두고 다음 실행에서 사용 → 매일 실행되는 한 재로그인 불필요."""
    _ensure_token_table(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT refresh_token FROM kakao_token WHERE id = 1")
        row = cur.fetchone()
    refresh = row["refresh_token"] if row else os.environ.get("KAKAO_REFRESH_TOKEN", "").strip()
    if not refresh:
        raise RuntimeError("refresh token이 없습니다. scripts/kakao_auth.py를 먼저 실행하세요.")

    data = {
        "grant_type":    "refresh_token",
        "client_id":     os.environ["KAKAO_REST_API_KEY"].strip(),
        "refresh_token": refresh,
    }
    secret = os.environ.get("KAKAO_CLIENT_SECRET", "").strip()
    if secret:
        data["client_secret"] = secret
    resp = requests.post(KAKAO_TOKEN_URL, data=data, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"카카오 토큰 갱신 실패 {resp.status_code}: {resp.text}")
    tok = resp.json()

    new_refresh = tok.get("refresh_token") or refresh
    if new_refresh != refresh or not row:
        save_refresh_token(conn, new_refresh)
    return tok["access_token"]


def save_refresh_token(conn, refresh: str):
    _ensure_token_table(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO kakao_token (id, refresh_token, updated_at) VALUES (1, %s, NOW()) "
            "ON CONFLICT (id) DO UPDATE SET refresh_token = EXCLUDED.refresh_token, updated_at = NOW()",
            (refresh,),
        )
    conn.commit()


def send_kakao(access_token: str, template: dict):
    resp = requests.post(
        KAKAO_MEMO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        data={"template_object": json.dumps(template, ensure_ascii=False)},
        timeout=15,
    )
    if resp.status_code != 200 or resp.json().get("result_code") != 0:
        raise RuntimeError(f"카카오 발송 실패 {resp.status_code}: {resp.text}")


# ── main ───────────────────────────────────────────────────────────

def main():
    dry_run = "--dry-run" in sys.argv
    today   = (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).strftime("%m/%d")
    url     = os.environ.get("DASHBOARD_URL", "").strip().rstrip("/")

    conn = get_db()
    try:
        if "--if-due" in sys.argv and not is_due(conn):
            print("발송 대상 아님 (주말·06시 이전이거나 오늘 이미 발송) — 종료")
            return
        since = brief_since(conn)
        span  = datetime.datetime.utcnow() - since
        # 주말·연휴를 건너뛴 날은 다룬 기간을 헤더에 표시
        period = f" ({(since + datetime.timedelta(hours=9)).strftime('%m/%d')}~)" if span > datetime.timedelta(hours=30) else ""
        print(f"대상 기간: {since:%Y-%m-%d %H:%M} UTC 이후 ({span.total_seconds() / 3600:.0f}시간)")

        buckets, n_watch = load_articles(conn, since)
        print(f"고객사 {len(buckets['C'])}건 / 경쟁사 {len(buckets['X'])}건 / 관심 주제 {len(buckets['T'])}건 "
              f"/ 기타 {len(buckets['G'])}건 (워치리스트 {n_watch}개)")

        # '내 타겟'(고객사·미팅 상대·경쟁사·관심 주제)과 '업계 주요 뉴스'를 따로 보낸다
        # (미팅 고객사 소식은 별도 메시지였으나 고객사 기사와 겹쳐 🤝 표시로 통합)
        target, general = generate_brief(buckets) if any(buckets.values()) else ([], [])
        messages = []
        if target:
            messages += to_templates(f"🎯 {today} 내 고객·경쟁사{period}", target, url)
        if general:
            messages += to_templates(f"📰 {today} 업계 주요 뉴스{period}", general, url)
        if not messages:
            messages = [text_template(f"📰 {today} 영업 브리핑{period}\n\n직전 브리핑 이후 주요 기사가 없습니다.", url)]

        # 수집 이상이 있는 날만 경고 1통 (브리핑에서 기사가 빠졌을 수 있음을 알림)
        warnings = collect_warnings(conn)
        if warnings:
            messages.append(text_template(f"⚠ {today} 수집 점검 필요\n" + "\n".join(f"· {w}" for w in warnings), url))

        for m in messages:
            print("-" * 40 + "\n" + _describe(m))
        if dry_run:
            print("\n[dry-run] 발송하지 않음")
            return

        token = get_access_token(conn)
        for m in messages:
            send_kakao(token, m)
        record_sent(conn)
        print(f"\n카카오톡 {len(messages)}건 발송 완료")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
