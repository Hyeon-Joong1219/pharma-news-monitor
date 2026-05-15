import threading
import time
import webbrowser
import requests
import urllib3
import os
import datetime
import logging as _logging

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from flask import Flask, jsonify, render_template, request
from db import get_db, init_db

_TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
_brief_cache: dict = {}


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
        title   = (a.get("title_ko") or a.get("title") or "")[:100]
        source  = (a.get("source") or "")[:20]
        summary = (a.get("summary_ko") or a.get("summary") or "")[:150]
        lines.append(f"{i+1}. [{source}] {title}" + (f" | {summary}" if summary else ""))
    prompt = (
        "You are a pharmaceutical/biotech industry analyst writing a concise daily market brief in Korean.\n"
        "Based on today's top pharma/biotech news articles below, write a brief of 3-5 key insights.\n\n"
        "Format requirements:\n"
        "- Write entirely in Korean\n"
        "- Each insight on a new line, starting with a relevant emoji and a bold keyword\n"
        "- Focus on MARKET IMPLICATIONS, not just news facts\n"
        "- Mention specific company names, drug names, or deal sizes when relevant\n"
        "- Keep each line concise (1-2 sentences max)\n"
        "- Do NOT include headers or intro/outro text\n"
        "- Write in natural, modern Korean — avoid Chinese characters (漢字) or archaic Sino-Korean terms like 里程碑, 契機, 趨勢. Use plain Korean equivalents instead (e.g. 이정표→중요한 발걸음, 계기→기회, 추세→흐름)\n\n"
        "Today's articles:\n" + "\n".join(lines)
    )
    try:
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600, temperature=0.4,
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


@app.route("/")
def index():
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT source FROM articles ORDER BY source")
            sources = [r["source"] for r in cur.fetchall()]
        conn.close()
    except Exception:
        sources = []
    return render_template("index.html", sources=sources)


@app.route("/api/articles")
def api_articles():
    q        = request.args.get("q", "").strip()
    source   = request.args.get("source", "").strip()
    period   = request.args.get("period", "").strip()
    lang     = request.args.get("lang", "").strip()
    sort     = request.args.get("sort", "date")
    page     = max(1, int(request.args.get("page", 1)))
    per_page = 50

    where_parts, params = ["(hidden IS NULL OR hidden = 0)"], []

    if q:
        where_parts.append(
            "(title ILIKE %s OR title_ko ILIKE %s OR summary ILIKE %s OR keywords ILIKE %s)"
        )
        params += [f"%{q}%"] * 4
    if source:
        where_parts.append("source = %s")
        params.append(source)
    if lang:
        where_parts.append("lang = %s")
        params.append(lang)

    date_from = request.args.get("date_from", "").strip()
    date_to   = request.args.get("date_to",   "").strip()

    # published_dt 없으면 fetched_at으로 대체
    DATE_FILTER = "COALESCE(published_dt, fetched_at)"
    FA          = "fetched_at"

    FRESHNESS = f"({DATE_FILTER} >= NOW() - INTERVAL '3 days')"

    if period == "today":
        # KST 오늘 자정 = UTC 전날 15:00
        where_parts.append(f"{FA} >= DATE_TRUNC('day', NOW()) - INTERVAL '9 hours'")
        where_parts.append(f"({DATE_FILTER} >= NOW() - INTERVAL '2 days')")
    elif period == "24h":
        where_parts.append(f"{FA} >= NOW() - INTERVAL '1 day'")
        where_parts.append(FRESHNESS)
    elif period == "7d":
        where_parts.append(f"{FA} >= NOW() - INTERVAL '7 days'")
        where_parts.append(f"({DATE_FILTER} >= NOW() - INTERVAL '7 days')")
    elif period == "30d":
        where_parts.append(f"{FA} >= NOW() - INTERVAL '30 days'")
        where_parts.append(f"({DATE_FILTER} >= NOW() - INTERVAL '30 days')")
    elif date_from or date_to:
        if date_from:
            where_parts.append(f"{DATE_FILTER} >= %s::timestamp")
            params.append(date_from)
        if date_to:
            where_parts.append(f"{DATE_FILTER} <= %s::timestamp")
            params.append(date_to + "T23:59:59")
    elif not period:
        default_interval = "7 days" if sort == "score" else "30 days"
        where_parts.append(f"{FA} >= NOW() - INTERVAL '{default_interval}'")

    DATE_COL     = "COALESCE(published_dt, fetched_at)"
    where_clause = " WHERE " + " AND ".join(where_parts)

    conn = get_db()
    try:
        with conn.cursor() as cur:
            if sort == "score":
                # 중요도순: 같은 cluster_id 중 점수 최고 기사 1개만 표시
                dedup_sql = (
                    f"SELECT DISTINCT ON (COALESCE(cluster_id, id::text)) * "
                    f"FROM articles{where_clause} "
                    f"ORDER BY COALESCE(cluster_id, id::text), score DESC, {DATE_COL} DESC"
                )
                cur.execute(
                    f"SELECT COUNT(*) FROM ({dedup_sql}) sub", params
                )
                total = cur.fetchone()["count"]
                cur.execute(
                    f"SELECT * FROM ({dedup_sql}) sub "
                    f"ORDER BY score DESC, {DATE_COL} DESC LIMIT %s OFFSET %s",
                    params + params + [per_page, (page - 1) * per_page],
                )
            else:
                cur.execute(f"SELECT COUNT(*) FROM articles{where_clause}", params)
                total = cur.fetchone()["count"]
                cur.execute(
                    f"SELECT * FROM articles{where_clause} "
                    f"ORDER BY {DATE_COL} DESC LIMIT %s OFFSET %s",
                    params + [per_page, (page - 1) * per_page],
                )
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    # datetime 객체 → ISO 문자열 변환 (JSON 직렬화)
    for r in rows:
        for k in ("published_dt", "fetched_at"):
            if isinstance(r.get(k), datetime.datetime):
                r[k] = r[k].isoformat()

    return jsonify({"articles": rows, "total": total, "page": page, "per_page": per_page})


@app.route("/api/top")
def api_top():
    lang  = request.args.get("lang", "").strip()
    limit = min(int(request.args.get("limit", 10)), 20)

    where_parts = [
        "fetched_at >= NOW() - INTERVAL '1 day'",
        "score > 0",
        "(hidden IS NULL OR hidden = 0)",
    ]
    params = []
    if lang:
        where_parts.append("lang = %s")
        params.append(lang)

    where_clause = " WHERE " + " AND ".join(where_parts)
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM articles{where_clause} ORDER BY score DESC LIMIT %s",
                params + [limit],
            )
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    for r in rows:
        for k in ("published_dt", "fetched_at"):
            if isinstance(r.get(k), datetime.datetime):
                r[k] = r[k].isoformat()

    return jsonify(rows)


@app.route("/api/translate/<int:article_id>", methods=["POST"])
def translate_article(article_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT title, summary, title_ko FROM articles WHERE id = %s", (article_id,)
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "not found"}), 404
            if row["title_ko"]:
                return jsonify({"title_ko": row["title_ko"]})

            title_ko   = _translate(row["title"])
            summary_ko = _translate((row["summary"] or "")[:500])
            cur.execute(
                "UPDATE articles SET title_ko = %s, summary_ko = %s WHERE id = %s",
                (title_ko, summary_ko, article_id),
            )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"title_ko": title_ko, "summary_ko": summary_ko})


@app.route("/api/daily-brief")
def api_daily_brief():
    lang    = request.args.get("lang", "").strip()
    refresh = request.args.get("refresh", "0") == "1"

    cache = _brief_cache.get(lang)
    if cache and not refresh and (time.time() - cache["ts"]) < 3600:
        return jsonify({k: v for k, v in cache.items() if k != "ts"})

    where_parts = [
        "(hidden IS NULL OR hidden = 0)",
        "fetched_at >= DATE_TRUNC('day', NOW()) - INTERVAL '9 hours'",
        "COALESCE(published_dt, fetched_at) >= NOW() - INTERVAL '2 days'",
    ]
    if lang:
        where_parts.append(f"lang = '{lang}'")

    where_clause = " WHERE " + " AND ".join(where_parts)
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT title, title_ko, summary, summary_ko, source, lang, score "
                f"FROM articles{where_clause} ORDER BY score DESC, fetched_at DESC LIMIT 25"
            )
            articles = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    if not articles:
        return jsonify({"brief": None, "generated_at": None, "article_count": 0})

    brief   = _generate_brief(articles, lang)
    now_kst = (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).strftime("%H:%M")
    payload = {"brief": brief, "generated_at": now_kst, "article_count": len(articles), "ts": time.time()}
    _brief_cache[lang] = payload
    return jsonify({k: v for k, v in payload.items() if k != "ts"})


@app.route("/api/sources")
def api_sources():
    lang = request.args.get("lang", "").strip()
    conn = get_db()
    try:
        with conn.cursor() as cur:
            if lang:
                cur.execute(
                    "SELECT DISTINCT source FROM articles WHERE lang = %s ORDER BY source", (lang,)
                )
            else:
                cur.execute("SELECT DISTINCT source FROM articles ORDER BY source")
            sources = [r["source"] for r in cur.fetchall()]
    finally:
        conn.close()
    return jsonify(sources)


def _open_browser():
    time.sleep(1.5)
    webbrowser.open("http://127.0.0.1:5000")


_sched_logger = _logging.getLogger("scheduler")

def _scheduler_loop():
    time.sleep(60)
    while True:
        try:
            _sched_logger.info("[Scheduler] 뉴스 수집 시작...")
            os.chdir(os.path.dirname(os.path.abspath(__file__)))
            from fetch import fetch_feeds
            _, saved = fetch_feeds()
            _sched_logger.info(f"[Scheduler] 수집 완료: {saved}건")
        except Exception as e:
            _sched_logger.error(f"[Scheduler] 수집 실패: {e}")
        time.sleep(3 * 3600)


if os.environ.get("ENABLE_SCHEDULER", "0") == "1":
    threading.Thread(target=_scheduler_loop, daemon=True, name="news-scheduler").start()


if __name__ == "__main__":
    init_db()
    threading.Thread(target=_open_browser, daemon=True).start()
    print("대시보드 시작 중... http://127.0.0.1:5000")
    app.run(debug=False, host="127.0.0.1", port=5000)
