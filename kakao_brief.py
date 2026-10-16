"""평일 아침 영업 브리핑을 카카오톡 '나에게 보내기'로 발송.

흐름: 최근 24시간 기사 조회 → 워치리스트 매칭 기사 우선 선별 →
      Groq로 영업 관점 요약 → 카카오 토큰 갱신 → 리스트 메시지(3항목씩, 최대 3통) 발송
      (각 항목 클릭 → 대시보드 /go/<id> → 원문 기사)

필요 환경변수
  DATABASE_URL, GROQ_API_KEY
  KAKAO_REST_API_KEY      카카오 디벨로퍼스 앱의 REST API 키
  KAKAO_REFRESH_TOKEN     scripts/kakao_auth.py로 최초 1회 발급한 refresh token
  KAKAO_CLIENT_SECRET     (선택) 앱에서 Client Secret을 '사용함'으로 켠 경우
  DASHBOARD_URL           (선택) 대시보드 주소. 카카오 앱 [플랫폼 > Web]에 도메인 등록 필요.
                          없으면 링크 버튼 없이 발송

사용법: python kakao_brief.py [--dry-run]   (--dry-run: 발송 없이 콘솔 출력만)
"""
import datetime
import json
import os
import re
import sys

import requests

from db import get_db
from kwmatch import contains_keyword

KAKAO_TOKEN_URL = "https://kauth.kakao.com/oauth/token"
KAKAO_MEMO_URL  = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
TEXT_LIMIT      = 200   # 카카오 텍스트 템플릿 최대 글자 수 (기사 없음 안내용)
PER_MESSAGE     = 3     # 카카오 리스트 템플릿 항목 상한
MAX_ITEMS       = 9     # 하루 최대 항목 수 → 리스트 메시지 3통
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

def load_articles(conn) -> tuple:
    """(워치리스트 매칭 기사, 일반 상위 기사, 워치리스트 키워드) 반환."""
    with conn.cursor() as cur:
        cur.execute("SELECT keyword FROM watchlist ORDER BY keyword")
        keywords = [r["keyword"] for r in cur.fetchall()]
        cur.execute(
            f"SELECT id, title, title_ko, summary, summary_ko, source, link, cluster_id, "
            f"{_EFF_SCORE} AS eff "
            f"FROM articles "
            f"WHERE (hidden IS NULL OR hidden = 0) "
            f"AND fetched_at >= NOW() - INTERVAL '24 hours' "
            f"AND COALESCE(published_dt, fetched_at) >= NOW() - INTERVAL '2 days' "
            f"ORDER BY eff DESC, fetched_at DESC LIMIT 300"
        )
        rows = [dict(r) for r in cur.fetchall()]

    # 같은 사건을 여러 매체가 보도한 경우 대표 기사 1건만 남김
    seen, uniq = set(), []
    for r in rows:
        key = r["cluster_id"] or r["title"]
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)

    kws_lower = [k.lower() for k in keywords]
    watch, general = [], []
    for r in uniq:
        text = " ".join(filter(None, [r["title"], r["title_ko"], r["summary"], r["summary_ko"]])).lower()
        hits = [keywords[i] for i, k in enumerate(kws_lower) if contains_keyword(text, k)]
        if hits:
            r["hits"] = hits
            watch.append(r)
        else:
            general.append(r)
    return watch[:15], general[:10], keywords


# ── 2. 영업 관점 요약 ───────────────────────────────────────────────

def _fmt(ref: str, a: dict) -> str:
    title   = (a.get("title_ko") or a.get("title") or "")[:100]
    summary = (a.get("summary_ko") or a.get("summary") or "")[:150]
    tag     = f" <관심:{', '.join(a['hits'])}>" if a.get("hits") else ""
    return f"{ref}. [{(a.get('source') or '')[:20]}] {title}{tag}" + (f" | {summary}" if summary else "")


def generate_brief(watch: list, general: list) -> list:
    """[(항목 텍스트, 근거 기사 dict 또는 None), ...] 반환."""
    from groq import Groq
    refs = {f"W{i+1}": a for i, a in enumerate(watch)}
    refs.update({f"G{i+1}": a for i, a in enumerate(general)})

    client = Groq(api_key=os.environ["GROQ_API_KEY"], timeout=60, max_retries=1)
    prompt = (
        "당신은 Merck Life Science Korea(바이오공정 소모품·장비·배지·레진·필터 공급사)의 "
        "영업 담당자를 돕는 애널리스트입니다. 담당 고객은 국내 Emerging Biotech와 CDMO입니다.\n"
        "아래 기사로 오늘 아침 영업 브리핑을 한국어로 작성하세요.\n\n"
        "형식 규칙:\n"
        f"- 항목 {MAX_ITEMS - 3}~{MAX_ITEMS}개 (기사가 부족하면 그보다 적게), 한 줄에 한 항목, 항목 사이 빈 줄 없음\n"
        "- 각 줄 형식: '[W3] 🔴 회사명: 핵심 사건 || 머크 영업 기회/시사점'\n"
        "  · 반드시 근거 기사 번호로 시작하고, 사건과 시사점은 '||'로 구분\n"
        f"  · '||' 앞(이모지·회사명·사건)은 {TITLE_LIMIT - 5}자 이내, 뒤(시사점)는 {DESC_LIMIT - 10}자 이내로 짧게\n"
        "  · 같은 사건은 한 번만, 중요한 순서대로 정렬\n"
        "- 🔴=즉시 영업 액션 필요(투자유치, 임상 진입, 공장 증설, CDMO 계약), "
        "🟡=모니터링, ⚪=업계 동향\n"
        "- 관심 기업(W) 기사를 우선 배치하고, 영업과 무관한 기사는 생략\n"
        "- 머리말·맺음말·마크다운(**, #) 금지, 한자어 대신 쉬운 한국어 사용\n\n"
        "[관심 기업 기사]\n" + ("\n".join(_fmt(f"W{i+1}", a) for i, a in enumerate(watch)) or "(없음)") +
        "\n\n[기타 주요 기사]\n" + ("\n".join(_fmt(f"G{i+1}", a) for i, a in enumerate(general)) or "(없음)")
    )
    # gpt-oss는 추론 모델이라 max_tokens가 작으면 빈 응답이 올 수 있음 → 넉넉히 + 1회 재시도
    for _ in range(2):
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000, temperature=0.3, reasoning_effort="low",
        )
        content = (resp.choices[0].message.content or "").strip().replace("**", "")
        if not content:
            continue
        items = []
        for line in content.splitlines():
            m = re.match(r"^\[?([WG]\d+)\]?[.:)]?\s*(.+)$", line.strip())
            if not m:
                continue   # 근거 기사 없는 줄(머리말 등)은 링크를 걸 수 없으므로 제외
            title, _, desc = m.group(2).partition("||")
            items.append((_clip(title, TITLE_LIMIT), _clip(desc, DESC_LIMIT), refs.get(m.group(1))))
        return items[:MAX_ITEMS]
    raise RuntimeError("Groq가 빈 응답을 반환했습니다.")


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


def text_template(text: str, url: str) -> dict:
    t = {"object_type": "text", "text": _clip(text, TEXT_LIMIT), "link": _link(url)}
    if url:
        t["button_title"] = "대시보드 열기"
    return t


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
        watch, general, keywords = load_articles(conn)
        print(f"관심 기업 매칭 {len(watch)}건 / 기타 {len(general)}건 (워치리스트 {len(keywords)}개)")

        items = generate_brief(watch, general) if (watch or general) else []
        if not items:
            messages = [text_template(f"📰 {today} 영업 브리핑\n\n최근 24시간 동안 영업 관련 주요 기사가 없습니다.", url)]
        elif not url or len(items) == 1:
            # 리스트 템플릿은 링크 필수·항목 2개 이상 → 조건이 안 되면 텍스트로 대체
            link = f"{url}/go/{items[0][2]['id']}" if (url and items[0][2]) else url
            messages = [text_template(f"📰 {today} 영업 브리핑\n\n" + "\n".join(t for t, _, _ in items[:4]), link)]
        else:
            messages = build_messages(f"📰 {today} 영업 브리핑", items, url)

        for m in messages:
            print("-" * 40 + "\n" + _describe(m))
        if dry_run:
            print("\n[dry-run] 발송하지 않음")
            return

        token = get_access_token(conn)
        for m in messages:
            send_kakao(token, m)
        print(f"\n카카오톡 {len(messages)}건 발송 완료")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
