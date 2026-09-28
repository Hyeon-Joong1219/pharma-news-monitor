import threading
import time
import webbrowser
import urllib3
import os
import datetime
import logging as _logging

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import psycopg2
from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException
from db import get_db, init_db
from kwmatch import contains_keyword

_brief_cache: dict = {}

# 워치리스트 키워드 캐시 (60초 TTL — articles/counts 연속 호출 시 중복 쿼리 방지)
_watchlist_cache = {"kws": [], "ts": 0.0}


def _load_watchlist_keywords() -> list:
    if time.time() - _watchlist_cache["ts"] < 60:
        return _watchlist_cache["kws"]
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT keyword FROM watchlist ORDER BY keyword")
            kws = [r["keyword"] for r in cur.fetchall()]
        conn.close()
    except Exception:
        kws = _watchlist_cache["kws"]
    _watchlist_cache.update({"kws": kws, "ts": time.time()})
    return kws


def _invalidate_watchlist_cache():
    _watchlist_cache["ts"] = 0.0


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
    client = Groq(api_key=api_key, timeout=45, max_retries=1)
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
    # gpt-oss 계열은 추론(reasoning) 모델이라 답변 전에 보이지 않는 "추론 토큰"을
    # 먼저 소비한다. max_tokens가 낮으면 추론만 하다 끝나 content가 빈 문자열로
    # 돌아오는 경우가 있어(finish_reason=length), reasoning_effort를 낮추고
    # max_tokens를 넉넉히 준다. 그래도 비어 있으면 한 번 재시도한다.
    for attempt in range(2):
        try:
            resp = client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1500, temperature=0.4,
                reasoning_effort="low",
            )
            content = (resp.choices[0].message.content or "").strip()
            if content:
                return content
        except Exception as e:
            return f"브리프 생성 실패: {e}"
    return "브리프 생성 실패: 모델이 빈 응답을 반환했습니다. 잠시 후 다시 시도해주세요."


# 번역은 fetch.py 구현을 재사용 (중복 제거)
from fetch import translate_to_ko as _translate

app = Flask(__name__)
_log = _logging.getLogger("server")


# ── 전역 에러 처리 ─────────────────────────────────────────────────
# API가 HTML 500 페이지 대신 항상 JSON을 돌려주도록 해서 프론트엔드가
# 원인을 사용자에게 표시할 수 있게 한다.

_DB_DOWN_MSG = ("데이터베이스에 연결할 수 없습니다. Supabase 프로젝트가 일시중지(pause)"
                "되었거나 DATABASE_URL이 올바르지 않을 수 있습니다.")


@app.errorhandler(psycopg2.OperationalError)
def _handle_db_down(e):
    _log.error(f"DB 연결 실패: {e}")
    return jsonify({"error": _DB_DOWN_MSG, "code": "db_unavailable"}), 503


@app.errorhandler(psycopg2.DataError)
def _handle_bad_input(e):
    return jsonify({"error": "잘못된 요청 값입니다.", "code": "bad_request"}), 400


@app.errorhandler(Exception)
def _handle_unexpected(e):
    if isinstance(e, HTTPException):
        if request.path.startswith("/api/"):
            return jsonify({"error": e.description, "code": e.name}), e.code
        return e
    _log.exception(f"처리되지 않은 오류: {request.path}")
    return jsonify({"error": f"서버 오류: {type(e).__name__}", "code": "internal"}), 500


# 스키마 보장 — gunicorn 배포에서는 __main__ 블록이 실행되지 않아 init_db가
# 호출되지 않으므로, 첫 API 요청 시 한 번 실행한다(실패 시 다음 요청에서 재시도).
_schema_ready    = False
_schema_tried_at = 0.0
_schema_lock     = threading.Lock()


@app.before_request
def _ensure_schema():
    global _schema_ready, _schema_tried_at
    if _schema_ready or not request.path.startswith("/api/"):
        return
    # DB 장애 중에는 매 요청마다 재시도하지 않도록 60초 간격으로만 시도.
    # 실패해도 요청은 그대로 진행 — DB가 필요한 엔드포인트만 503을 반환한다.
    if time.time() - _schema_tried_at < 60:
        return
    with _schema_lock:
        if _schema_ready or time.time() - _schema_tried_at < 60:
            return
        _schema_tried_at = time.time()
        try:
            init_db()
            _schema_ready = True
        except psycopg2.OperationalError as e:
            _log.warning(f"init_db: DB 연결 실패 — 60초 후 재시도: {e}")
        except Exception as e:
            # 권한 문제 등으로 DDL이 실패해도 조회 기능은 계속 동작하도록 함
            _log.warning(f"init_db 실패 (무시): {e}")
            _schema_ready = True


def _int_arg(name: str, default: int, lo: int = 1, hi: int = 10_000) -> int:
    try:
        return min(hi, max(lo, int(request.args.get(name, default))))
    except (TypeError, ValueError):
        return default


def _date_arg(value: str) -> str:
    """YYYY-MM-DD 형식만 허용 (잘못된 값은 무시)."""
    try:
        return datetime.date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        return ""


@app.route("/api/health")
def api_health():
    """DB 연결 상태 확인용 (모니터링/대시보드 배너)."""
    try:
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(fetched_at) AS last FROM articles")
                last = cur.fetchone()["last"]
        finally:
            conn.close()
    except psycopg2.Error as e:
        _log.error(f"health check 실패: {e}")
        return jsonify({"ok": False, "db": False, "error": _DB_DOWN_MSG}), 503
    stale_hours = None
    if isinstance(last, datetime.datetime):
        stale_hours = round((datetime.datetime.utcnow() - last).total_seconds() / 3600, 1)
    return jsonify({
        "ok": True, "db": True,
        "last_fetched_at": last.isoformat() if isinstance(last, datetime.datetime) else None,
        "hours_since_last_fetch": stale_hours,
    })


@app.route("/")
def index():
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT source FROM articles ORDER BY source")
            sources = [r["source"] for r in cur.fetchall()]
        conn.close()
    except Exception as e:
        _log.warning(f"index: 매체 목록 조회 실패: {e}")
        sources = []
    return render_template("index.html", sources=sources)


def _build_article_filters(args, include_lang=True):
    """공용 WHERE 절 빌더 — /api/articles 와 /api/counts 가 공유.
    반환: (where_parts, params)"""
    q           = args.get("q", "").strip()
    source      = args.get("source", "").strip()
    period      = args.get("period", "").strip()
    lang        = args.get("lang", "").strip()
    sort        = args.get("sort", "date")
    show_hidden = args.get("show_hidden", "0") == "1"

    # 스크랩 보기: 사용자가 저장한 기사 ID 목록만 직접 조회
    # (사용자가 명시적으로 저장한 기사이므로 hidden/기간 필터 미적용)
    ids_raw = args.get("ids", "").strip()
    if ids_raw:
        id_list = [int(x) for x in ids_raw.split(",") if x.strip().isdigit()][:300]
        return ["id = ANY(%s)"], [id_list]

    # show_hidden=1: 숨김 기사만 표시 (AI 필터 감사용)
    where_parts = ["hidden = 1"] if show_hidden else ["(hidden IS NULL OR hidden = 0)"]
    params = []

    # 워치리스트만 보기 (SQL은 ILIKE 광역 매칭 — 정밀 배지는 kwmatch로 별도 계산)
    if args.get("watch", "0") == "1":
        watch_kws = _load_watchlist_keywords()
        if watch_kws:
            ors = []
            for kw in watch_kws:
                like = f"%{kw}%"
                ors.append("(title ILIKE %s OR title_ko ILIKE %s OR summary ILIKE %s OR keywords ILIKE %s)")
                params += [like] * 4
            where_parts.append("(" + " OR ".join(ors) + ")")
        else:
            where_parts.append("FALSE")   # 워치리스트 비어있으면 결과 없음

    if q:
        where_parts.append(
            "(title ILIKE %s OR title_ko ILIKE %s OR summary ILIKE %s OR keywords ILIKE %s)"
        )
        params += [f"%{q}%"] * 4
    if source:
        where_parts.append("source = %s")
        params.append(source)
    if include_lang and lang:
        where_parts.append("lang = %s")
        params.append(lang)

    date_from = _date_arg(args.get("date_from", "").strip())
    date_to   = _date_arg(args.get("date_to",   "").strip())

    # published_dt 없으면 fetched_at으로 대체
    DATE_FILTER = "COALESCE(published_dt, fetched_at)"
    FA          = "fetched_at"

    FRESHNESS = f"({DATE_FILTER} >= NOW() - INTERVAL '3 days')"

    if period == "today":
        # 오늘 수집(fetched_at)된 기사 중 최근 3일 내 발행된 것
        # → 해외 소스는 오늘 RSS에서 처음 수집돼도 published_dt가 2~3일 전일 수 있음
        where_parts.append(f"{FA} >= NOW() - INTERVAL '24 hours'")
        where_parts.append(f"({DATE_FILTER} >= NOW() - INTERVAL '3 days')")
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

    return where_parts, params


@app.route("/api/articles")
def api_articles():
    sort     = request.args.get("sort", "date")
    page     = _int_arg("page", 1)
    per_page = 50

    where_parts, params = _build_article_filters(request.args)

    DATE_COL     = "COALESCE(published_dt, fetched_at)"
    where_clause = " WHERE " + " AND ".join(where_parts)

    # 중요도순 정렬: AI 분류 완료 기사는 AI관련성×키워드점수 복합 지표 사용
    # ai_classified=1: relevance_score(0-100) × score / 100  → AI가 낮게 평가한 기사 하위
    # ai_classified=0: score 그대로 (AI 분류 전 임시)
    _EFF_SCORE = (
        "CASE WHEN ai_classified=1 "
        "THEN (COALESCE(relevance_score,50)*COALESCE(score,0)/100.0) "
        "ELSE COALESCE(score,0) END"
    )

    conn = get_db()
    try:
        with conn.cursor() as cur:
            if sort == "score":
                # 중요도순: 같은 cluster_id 중 복합점수 최고 기사 1개만 표시.
                # 여러 매체가 동시에 다룬 이슈(source_count)를 최우선 기준으로 삼고,
                # 그 안에서 AI관련성×키워드점수 복합 지표로 2차 정렬한다.
                dedup_sql = (
                    f"SELECT DISTINCT ON (COALESCE(cluster_id, id::text)) * "
                    f"FROM articles{where_clause} "
                    f"ORDER BY COALESCE(cluster_id, id::text), "
                    f"COALESCE(source_count,1) DESC, {_EFF_SCORE} DESC, {DATE_COL} DESC"
                )
                cur.execute(
                    f"SELECT COUNT(*) FROM ({dedup_sql}) sub", params
                )
                total = cur.fetchone()["count"]
                cur.execute(
                    f"SELECT * FROM ({dedup_sql}) sub "
                    f"ORDER BY COALESCE(source_count,1) DESC, {_EFF_SCORE} DESC, {DATE_COL} DESC "
                    f"LIMIT %s OFFSET %s",
                    params + [per_page, (page - 1) * per_page],
                )
            else:
                # 최신순도 같은 cluster_id 중 가장 최신 기사 1개만 표시
                dedup_sql = (
                    f"SELECT DISTINCT ON (COALESCE(cluster_id, id::text)) * "
                    f"FROM articles{where_clause} "
                    f"ORDER BY COALESCE(cluster_id, id::text), {DATE_COL} DESC"
                )
                cur.execute(
                    f"SELECT COUNT(*) FROM ({dedup_sql}) sub", params
                )
                total = cur.fetchone()["count"]
                cur.execute(
                    f"SELECT * FROM ({dedup_sql}) sub "
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

    # 워치리스트 매칭 어노테이션 (정밀 단어경계 매칭 — ⭐ 배지용)
    watch_kws = _load_watchlist_keywords()
    if watch_kws:
        for r in rows:
            text = " ".join(filter(None, [r.get("title"), r.get("title_ko"),
                                          r.get("summary"), r.get("keywords")])).lower()
            hits = [kw for kw in watch_kws if contains_keyword(text, kw.lower())]
            if hits:
                r["watch_hits"] = hits

    return jsonify({"articles": rows, "total": total, "page": page, "per_page": per_page})


@app.route("/api/counts")
def api_counts():
    """탭 카운트 전용 경량 엔드포인트 — GROUPING SETS로 한 번의 쿼리에
    전체/언어별 dedup 카운트를 모두 계산 (기존에는 /api/articles 3회 호출)."""
    where_parts, params = _build_article_filters(request.args, include_lang=False)
    where_clause = " WHERE " + " AND ".join(where_parts)

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT lang, COUNT(DISTINCT COALESCE(cluster_id, id::text)) AS cnt "
                f"FROM articles{where_clause} "
                f"GROUP BY GROUPING SETS ((lang), ())",
                params,
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    counts = {"all": 0, "ko": 0, "en": 0}
    for r in rows:
        key = r["lang"] if r["lang"] else "all"
        if r["lang"] is None:
            counts["all"] = r["cnt"]
        elif key in counts:
            counts[key] = r["cnt"]
    return jsonify(counts)


@app.route("/api/feedback/<int:article_id>", methods=["POST"])
def api_feedback(article_id):
    """사용자 피드백: '관련 없음'(hide) / '복구'(restore).
    user_feedback이 설정된 기사는 스코어링·AI 분류가 hidden을 덮어쓰지 않는다.
    피드백 이력은 추후 AI 프롬프트/하드 제외 규칙 개선의 학습 데이터가 된다."""
    action = (request.get_json(silent=True) or {}).get("action", "")
    if action not in ("hide", "restore"):
        return jsonify({"error": "action must be 'hide' or 'restore'"}), 400

    hidden, feedback = (1, -1) if action == "hide" else (0, 1)
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE articles SET hidden=%s, user_feedback=%s WHERE id=%s",
                (hidden, feedback, article_id),
            )
            if cur.rowcount == 0:
                return jsonify({"error": "not found"}), 404
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "action": action})


@app.route("/api/watchlist", methods=["GET", "POST"])
def api_watchlist():
    """워치리스트 키워드 조회/추가."""
    conn = get_db()
    try:
        if request.method == "POST":
            kw = ((request.get_json(silent=True) or {}).get("keyword") or "").strip()
            if not kw or len(kw) > 60:
                return jsonify({"error": "keyword must be 1-60 chars"}), 400
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO watchlist (keyword) VALUES (%s) "
                    "ON CONFLICT (keyword) DO NOTHING",
                    (kw,),
                )
            conn.commit()
            _invalidate_watchlist_cache()
        with conn.cursor() as cur:
            cur.execute("SELECT id, keyword FROM watchlist ORDER BY keyword")
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return jsonify(rows)


@app.route("/api/watchlist/<int:wid>", methods=["DELETE"])
def api_watchlist_delete(wid):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM watchlist WHERE id=%s", (wid,))
            deleted = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    _invalidate_watchlist_cache()
    return jsonify({"ok": bool(deleted)})


@app.route("/api/feed-health")
def api_feed_health():
    """피드별 수집 상태. failing = 마지막 실행이 실패한 피드 목록."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM feed_health ORDER BY consecutive_failures DESC, source"
            )
            rows = [dict(r) for r in cur.fetchall()]
    except psycopg2.errors.UndefinedTable:
        rows = []
    finally:
        conn.close()

    for r in rows:
        for k in ("last_run_at", "last_success_at"):
            if isinstance(r.get(k), datetime.datetime):
                r[k] = r[k].isoformat()
    failing = [r for r in rows if r.get("last_status") == "error"]
    return jsonify({
        "total": len(rows),
        "failing_count": len(failing),
        "failing": failing,
        "ai_error": _fetch_state.get("last_ai_error"),
    })


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

            title_ko   = _translate(row["title"] or "")
            summary_ko = _translate((row["summary"] or "")[:500])
            cur.execute(
                "UPDATE articles SET title_ko = %s, summary_ko = %s WHERE id = %s",
                (title_ko, summary_ko, article_id),
            )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"title_ko": title_ko, "summary_ko": summary_ko})


@app.route("/api/ai-summary/<int:article_id>", methods=["POST"])
def ai_summary(article_id):
    """영어 기사를 Groq 8b로 한국어 AI 요약 생성 (캐시 우선)."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, summary, summary_ko, lang FROM articles WHERE id = %s",
                (article_id,),
            )
            row = cur.fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        if row["lang"] != "en":
            return jsonify({"error": "only for English articles"}), 400

        # 이미 AI 요약이 존재하고 충분히 길면 캐시 반환
        existing = (row["summary_ko"] or "").strip()
        if len(existing) > 120 and existing.startswith("【AI】"):
            return jsonify({"summary_ko": existing, "cached": True})

        api_key = _load_groq_key()
        if not api_key:
            return jsonify({"error": "GROQ_API_KEY not set"}), 503

        try:
            from groq import Groq
        except ImportError:
            return jsonify({"error": "groq package not installed"}), 503

        title   = (row["title"] or "")[:300]
        summary = (row["summary"] or "")[:1200]
        prompt  = (
            "You are a pharmaceutical/biotech industry analyst writing for a Korean-speaking audience.\n"
            "Summarize the following English article in Korean in 2-3 concise sentences.\n"
            "Focus on: drug/therapy name, company, key clinical data (e.g., OS improvement, ORR), "
            "regulatory status (FDA approval/CRL/IND), deal size, or market implication.\n"
            "Avoid vague language. Use specific numbers and names when available.\n"
            "Write in natural, modern Korean. No honorifics (반말/존댓말 모두 불필요). No intro phrase.\n\n"
            f"Title: {title}\n"
            f"Content: {summary}\n\n"
            "Korean summary (2-3 sentences only):"
        )
        try:
            client  = Groq(api_key=api_key, timeout=45, max_retries=1)
            resp    = client.chat.completions.create(
                model="openai/gpt-oss-20b",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=900,
                temperature=0.15,
                reasoning_effort="low",
            )
            content = (resp.choices[0].message.content or "").strip()
            if not content:
                return jsonify({"error": "모델이 빈 응답을 반환했습니다. 다시 시도해주세요."}), 502
            ko = "【AI】" + content
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE articles SET summary_ko = %s WHERE id = %s", (ko, article_id)
                )
            conn.commit()
            return jsonify({"summary_ko": ko, "cached": False})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/daily-brief")
def api_daily_brief():
    lang    = request.args.get("lang", "").strip()
    refresh = request.args.get("refresh", "0") == "1"

    cache = _brief_cache.get(lang)
    if cache and not refresh and (time.time() - cache["ts"]) < 3600:
        return jsonify({k: v for k, v in cache.items() if k != "ts"})

    where_parts = [
        "(hidden IS NULL OR hidden = 0)",
        # KST 자정 계산: NOW()를 먼저 +9h 시프트한 뒤 자정으로 절삭해야 정확함.
        # (절삭 후 -9h를 하면 UTC 15~24시(=KST 00~09시) 구간에서 하루 전 자정으로
        #  잘못 계산되어 "오늘" 브리프에 어제 기사가 섞이는 버그가 있었음)
        "fetched_at >= DATE_TRUNC('day', NOW() + INTERVAL '9 hours') - INTERVAL '9 hours'",
        "COALESCE(published_dt, fetched_at) >= NOW() - INTERVAL '2 days'",
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
                f"SELECT title, title_ko, summary, summary_ko, source, lang, score "
                f"FROM articles{where_clause} ORDER BY score DESC, fetched_at DESC LIMIT 25",
                params,
            )
            articles = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    if not articles:
        return jsonify({"brief": None, "generated_at": None, "article_count": 0})

    brief   = _generate_brief(articles, lang)
    now_kst = (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).strftime("%H:%M")
    payload = {"brief": brief, "generated_at": now_kst, "article_count": len(articles), "ts": time.time()}
    # 생성 실패 시에는 캐시하지 않음 — 실패한 응답이 1시간 동안 고정 노출되는 것을 방지
    if brief and "생성 실패" not in brief:
        _brief_cache[lang] = payload
    return jsonify({k: v for k, v in payload.items() if k != "ts"})


@app.route("/api/debug/overseas")
def api_debug_overseas():
    """진단용: 해외 기사 현황 (hidden 포함 전체)"""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            # 시간대별 분포
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE COALESCE(published_dt, fetched_at) >= NOW() - INTERVAL '24 hours') AS today_all,
                    COUNT(*) FILTER (WHERE COALESCE(published_dt, fetched_at) >= NOW() - INTERVAL '24 hours'
                                     AND (hidden IS NULL OR hidden = 0)) AS today_visible,
                    COUNT(*) FILTER (WHERE COALESCE(published_dt, fetched_at) >= NOW() - INTERVAL '24 hours'
                                     AND hidden = 1) AS today_hidden,
                    COUNT(*) FILTER (WHERE fetched_at >= NOW() - INTERVAL '24 hours') AS fetched_today,
                    COUNT(*) FILTER (WHERE fetched_at >= NOW() - INTERVAL '7 days') AS fetched_7d,
                    COUNT(*) FILTER (WHERE published_dt IS NULL) AS no_pub_dt,
                    COUNT(*) AS total_en
                FROM articles WHERE lang = 'en'
            """)
            summary = dict(cur.fetchone())

            # 소스별 오늘 기사 수 (hidden 포함)
            cur.execute("""
                SELECT source,
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE hidden IS NULL OR hidden = 0) AS visible,
                    COUNT(*) FILTER (WHERE hidden = 1) AS hidden_cnt,
                    MAX(COALESCE(published_dt, fetched_at)) AS latest
                FROM articles
                WHERE lang = 'en'
                  AND COALESCE(published_dt, fetched_at) >= NOW() - INTERVAL '48 hours'
                GROUP BY source
                ORDER BY total DESC
            """)
            by_source = []
            for r in cur.fetchall():
                row = dict(r)
                if isinstance(row.get("latest"), datetime.datetime):
                    row["latest"] = row["latest"].isoformat()
                by_source.append(row)
    finally:
        conn.close()
    return jsonify({"summary": summary, "by_source_48h": by_source})


_fetch_state = {"running": False, "last_run": None, "last_saved": None,
                "last_error": None, "last_ai_error": None}


@app.route("/api/fetch-status")
def api_fetch_status():
    return jsonify(_fetch_state)


@app.route("/api/trigger-fetch", methods=["POST"])
def api_trigger_fetch():
    """수동으로 뉴스 수집 + 스코어링 + AI 분류를 백그라운드 실행."""
    if _fetch_state["running"]:
        return jsonify({"status": "already_running"}), 409

    def _run():
        _fetch_state["running"]    = True
        _fetch_state["last_error"] = None
        try:
            os.chdir(os.path.dirname(os.path.abspath(__file__)))
            from fetch import fetch_feeds
            _, saved = fetch_feeds()
            _fetch_state["last_saved"] = saved

            try:
                from scoring import run_scoring
                run_scoring()
            except Exception as e:
                _logging.warning(f"[Trigger] 스코어링 실패: {e}")

            try:
                from relevance_ai import run_relevance_classification
                run_relevance_classification(days=3)
                _fetch_state["last_ai_error"] = None
            except Exception as e:
                _fetch_state["last_ai_error"] = str(e)[:200]
                _logging.warning(f"[Trigger] AI 분류 실패: {e}")

            now_kst = (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).strftime("%Y-%m-%d %H:%M KST")
            _fetch_state["last_run"] = now_kst
        except Exception as e:
            _fetch_state["last_error"] = str(e)
            _logging.error(f"[Trigger] 수집 실패: {e}")
        finally:
            _fetch_state["running"] = False

    threading.Thread(target=_run, daemon=True, name="manual-fetch").start()
    return jsonify({"status": "started"})


@app.route("/api/cluster/<cluster_id>")
def api_cluster(cluster_id):
    """같은 cluster_id를 가진 기사 전체 반환 (클러스터 뷰용)."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, title, title_ko, summary, summary_ko, source, lang,
                          score, published_dt, fetched_at, link, keywords
                   FROM articles
                   WHERE cluster_id = %s AND (hidden IS NULL OR hidden = 0)
                   ORDER BY score DESC, COALESCE(published_dt, fetched_at) DESC""",
                (cluster_id,),
            )
            rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            for k in ("published_dt", "fetched_at"):
                if isinstance(r.get(k), datetime.datetime):
                    r[k] = r[k].isoformat()
    finally:
        conn.close()
    return jsonify(rows)


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

            try:
                from scoring import run_scoring
                run_scoring()
                _sched_logger.info("[Scheduler] 스코어링 완료")
            except Exception as e:
                _sched_logger.warning(f"[Scheduler] 스코어링 실패: {e}")

            try:
                from relevance_ai import run_relevance_classification
                hidden = run_relevance_classification(days=3)
                _fetch_state["last_ai_error"] = None
                _sched_logger.info(f"[Scheduler] AI 분류 완료 - {hidden}건 필터링")
            except Exception as e:
                _fetch_state["last_ai_error"] = str(e)[:200]
                _sched_logger.warning(f"[Scheduler] AI 분류 실패: {e}")

            now_kst = (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).strftime("%Y-%m-%d %H:%M KST")
            _fetch_state.update({"last_run": now_kst, "last_saved": saved, "running": False, "last_error": None})
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
