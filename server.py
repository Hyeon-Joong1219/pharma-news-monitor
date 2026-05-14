import sqlite3
import threading
import time
import webbrowser
import requests
import urllib3
import json
import os
import datetime

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from flask import Flask, jsonify, render_template, request

_TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"

# ── 시장 브리프 캐시 (언어별, 1시간 유효) ─────────────────────────
_brief_cache: dict = {}   # key: lang ("ko"|"en"|"") → {brief, generated_at, ts}


def _load_groq_key() -> str:
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


def _generate_brief(articles: list, lang: str) -> str:
    """Groq LLM으로 오늘의 제약/바이오 시장 브리프 생성."""
    try:
        from groq import Groq
    except ImportError:
        return "groq 패키지가 설치되지 않았습니다."

    api_key = _load_groq_key()
    if not api_key:
        return "GROQ_API_KEY가 설정되지 않았습니다."

    client = Groq(api_key=api_key)

    lines = []
    for i, a in enumerate(articles[:20]):
        title = (a.get("title_ko") or a.get("title") or "")[:100]
        source = (a.get("source") or "")[:20]
        summary = (a.get("summary_ko") or a.get("summary") or "")[:150]
        lines.append(f"{i+1}. [{source}] {title}" + (f" | {summary}" if summary else ""))

    prompt = (
        "You are a pharmaceutical/biotech industry analyst writing a concise daily market brief in Korean.\n"
        "Based on today's top pharma/biotech news articles below, write a brief of 3-5 key insights.\n\n"
        "Format requirements:\n"
        "- Write entirely in Korean\n"
        "- Each insight on a new line, starting with a relevant emoji and a bold keyword (e.g. **FDA 승인**, **M&A**, **임상 결과**)\n"
        "- Focus on MARKET IMPLICATIONS, not just news facts (what does this mean for investors/industry?)\n"
        "- Mention specific company names, drug names, or deal sizes when relevant\n"
        "- Keep each line concise (1-2 sentences max)\n"
        "- Do NOT include headers or intro/outro text — just the bullet insights\n\n"
        "Today's articles:\n" + "\n".join(lines)
    )

    try:
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
            temperature=0.4,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"브리프 생성 실패: {e}"


def _translate(text: str) -> str:
    if not text or not text.strip():
        return ""
    try:
        params = {"client": "gtx", "sl": "en", "tl": "ko", "dt": "t", "q": text[:800]}
        r = requests.get(_TRANSLATE_URL, params=params, verify=False, timeout=8)
        r.raise_for_status()
        return "".join(seg[0] for seg in r.json()[0] if seg[0])
    except Exception:
        return ""

app = Flask(__name__)
_DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(_DATA_DIR, "news.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@app.route("/")
def index():
    try:
        conn = get_db()
        sources = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT source FROM articles ORDER BY source"
            ).fetchall()
        ]
        conn.close()
    except Exception:
        sources = []
    return render_template("index.html", sources=sources)


@app.route("/api/articles")
def api_articles():
    q      = request.args.get("q", "").strip()
    source = request.args.get("source", "").strip()
    period = request.args.get("period", "").strip()
    lang   = request.args.get("lang", "").strip()
    sort   = request.args.get("sort", "date")   # "date" | "score"
    page   = max(1, int(request.args.get("page", 1)))
    per_page = 50

    where_parts, params = ["(hidden IS NULL OR hidden = 0)"], []

    if q:
        where_parts.append("(title LIKE ? OR title_ko LIKE ? OR summary LIKE ? OR keywords LIKE ?)")
        params += [f"%{q}%"] * 4
    if source:
        where_parts.append("source = ?")
        params.append(source)
    if lang:
        where_parts.append("lang = ?")
        params.append(lang)
    date_from = request.args.get("date_from", "").strip()
    date_to   = request.args.get("date_to",   "").strip()

    # datetime() 로 감싸서 'T'/'공백' 구분자 차이를 정규화 (Python isoformat vs SQLite)
    DATE_FILTER = "COALESCE(NULLIF(datetime(published_dt),''), datetime(fetched_at))"
    FA = "datetime(fetched_at)"   # fetched_at 정규화 단축어

    # 발행일 기준 신선도: 3일 이내이거나 발행일 미상인 경우 통과
    FRESHNESS = f"({DATE_FILTER} >= datetime('now', '-3 days'))"

    if period == "today":
        # fetched_at UTC 저장 → KST 자정 = UTC 전날 15:00
        where_parts.append(f"{FA} >= datetime('now', 'start of day', '-9 hours')")
        # "오늘" 뷰는 발행일도 2일 이내여야 함 (RSS가 오래된 기사 포함하는 경우 차단)
        # DATE_FILTER가 이미 빈 published_dt → fetched_at으로 대체하므로 별도 bypass 불필요
        FRESHNESS_TODAY = f"({DATE_FILTER} >= datetime('now', '-2 days'))"
        where_parts.append(FRESHNESS_TODAY)
    elif period == "24h":
        where_parts.append(f"{FA} >= datetime('now', '-1 day')")
        where_parts.append(FRESHNESS)
    elif period == "7d":
        where_parts.append(f"{FA} >= datetime('now', '-7 days')")
        where_parts.append(f"({DATE_FILTER} >= datetime('now', '-7 days'))")
    elif period == "30d":
        where_parts.append(f"{FA} >= datetime('now', '-30 days')")
        where_parts.append(f"({DATE_FILTER} >= datetime('now', '-30 days'))")
    elif date_from or date_to:
        if date_from:
            where_parts.append(f"{DATE_FILTER} >= datetime(?)")
            params.append(date_from)
        if date_to:
            where_parts.append(f"{DATE_FILTER} <= datetime(?)")
            params.append(date_to + "T23:59:59")
    elif not period:
        # 기간 필터 미선택 시: 중요도순이면 7일, 날짜순이면 30일 기본 적용
        default_days = "7" if sort == "score" else "30"
        where_parts.append(f"{FA} >= datetime('now', '-{default_days} days')")

    # 정렬 기준: published_dt 우선, 없으면 fetched_at
    DATE_COL = "COALESCE(NULLIF(datetime(published_dt),''), datetime(fetched_at))"

    where_clause = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
    order_clause = f"ORDER BY score DESC, {DATE_COL} DESC" if sort == "score" \
                   else f"ORDER BY {DATE_COL} DESC"

    conn = get_db()
    total = conn.execute(
        f"SELECT COUNT(*) FROM articles{where_clause}", params
    ).fetchone()[0]

    rows = conn.execute(
        f"SELECT * FROM articles{where_clause} {order_clause} LIMIT ? OFFSET ?",
        params + [per_page, (page - 1) * per_page],
    ).fetchall()
    conn.close()

    return jsonify({
        "articles": [dict(r) for r in rows],
        "total": total, "page": page, "per_page": per_page,
    })


@app.route("/api/top")
def api_top():
    """당일 주요 뉴스 TOP N (score 기준)."""
    lang  = request.args.get("lang", "").strip()
    limit = min(int(request.args.get("limit", 10)), 20)

    where_parts = ["datetime(fetched_at) >= datetime('now', '-1 day')", "score > 0", "(hidden IS NULL OR hidden = 0)"]
    params = []
    if lang:
        where_parts.append("lang = ?")
        params.append(lang)

    where_clause = " WHERE " + " AND ".join(where_parts)
    conn = get_db()
    rows = conn.execute(
        f"SELECT * FROM articles{where_clause} ORDER BY score DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/translate/<int:article_id>", methods=["POST"])
def translate_article(article_id):
    """단건 온디맨드 번역 (UI에서 번역 버튼 클릭 시 호출)."""
    conn = get_db()
    row = conn.execute(
        "SELECT title, summary, title_ko FROM articles WHERE id=?", (article_id,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not found"}), 404

    if row["title_ko"]:
        conn.close()
        return jsonify({"title_ko": row["title_ko"]})

    title_ko   = _translate(row["title"])
    summary_ko = _translate((row["summary"] or "")[:500])

    conn.execute(
        "UPDATE articles SET title_ko=?, summary_ko=? WHERE id=?",
        (title_ko, summary_ko, article_id),
    )
    conn.commit()
    conn.close()
    return jsonify({"title_ko": title_ko, "summary_ko": summary_ko})


@app.route("/api/daily-brief")
def api_daily_brief():
    """오늘 기사 기반 시장 브리프 (1시간 캐시, ?refresh=1 로 강제 재생성)."""
    lang    = request.args.get("lang", "").strip()
    refresh = request.args.get("refresh", "0") == "1"

    cache = _brief_cache.get(lang)
    if cache and not refresh:
        age = time.time() - cache["ts"]
        if age < 3600:
            return jsonify(cache)

    # 오늘 기사 수집 (fetched 기준 KST 오늘, score 내림차순 상위 25건)
    conn = get_db()
    FA = "datetime(fetched_at)"
    DATE_FILTER = "COALESCE(NULLIF(datetime(published_dt),''), datetime(fetched_at))"
    where = (
        f"(hidden IS NULL OR hidden = 0) "
        f"AND {FA} >= datetime('now', 'start of day', '-9 hours') "
        f"AND ({DATE_FILTER} >= datetime('now', '-2 days'))"
    )
    if lang:
        where += f" AND lang = '{lang}'"

    rows = conn.execute(
        f"SELECT title, title_ko, summary, summary_ko, source, lang, score "
        f"FROM articles WHERE {where} ORDER BY score DESC, {FA} DESC LIMIT 25"
    ).fetchall()
    conn.close()

    if not rows:
        return jsonify({"brief": None, "generated_at": None, "article_count": 0})

    articles = [dict(r) for r in rows]
    brief = _generate_brief(articles, lang)

    now_kst = (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).strftime("%H:%M")
    payload = {
        "brief": brief,
        "generated_at": now_kst,
        "article_count": len(articles),
        "ts": time.time(),
    }
    _brief_cache[lang] = payload
    result = {k: v for k, v in payload.items() if k != "ts"}
    return jsonify(result)


@app.route("/api/sources")
def api_sources():
    lang = request.args.get("lang", "").strip()
    conn = get_db()
    if lang:
        rows = conn.execute(
            "SELECT DISTINCT source FROM articles WHERE lang=? ORDER BY source", (lang,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT DISTINCT source FROM articles ORDER BY source"
        ).fetchall()
    conn.close()
    return jsonify([r[0] for r in rows])


def _open_browser():
    time.sleep(1.5)
    webbrowser.open("http://127.0.0.1:5000")


import logging as _logging
_sched_logger = _logging.getLogger("scheduler")

def _scheduler_loop():
    """백그라운드 스케줄러: 3시간마다 뉴스 수집 → 스코어링 → AI 분류."""
    time.sleep(60)  # 앱 완전 기동 후 시작
    while True:
        try:
            _sched_logger.info("[Scheduler] 뉴스 수집 시작...")
            # 스크립트 디렉터리를 작업 경로로 설정 (YAML 파일 위치)
            import os as _os
            _os.chdir(os.path.dirname(os.path.abspath(__file__)))
            from fetch import fetch_feeds
            _, saved = fetch_feeds()
            _sched_logger.info(f"[Scheduler] 수집 완료: {saved}건")
        except Exception as e:
            _sched_logger.error(f"[Scheduler] 수집 실패: {e}")
        time.sleep(3 * 3600)  # 3시간 대기


def start_scheduler():
    t = threading.Thread(target=_scheduler_loop, daemon=True, name="news-scheduler")
    t.start()


# 프로덕션(gunicorn)과 개발 모두에서 스케줄러 자동 시작
# gunicorn 환경변수가 있을 때만 실행 (로컬 개발 시 수동 fetch 유지 가능)
if os.environ.get("ENABLE_SCHEDULER", "0") == "1":
    start_scheduler()


if __name__ == "__main__":
    threading.Thread(target=_open_browser, daemon=True).start()
    print("대시보드 시작 중... http://127.0.0.1:5000")
    app.run(debug=False, host="127.0.0.1", port=5000)
