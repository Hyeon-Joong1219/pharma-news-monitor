import os
import yaml
import base64
import hashlib
import feedparser
import datetime
import logging
import sys
import re
import socket
import requests
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from psycopg2.extras import execute_values
from db import get_db, init_db
from kwmatch import match_keywords, weighted_score, contains_name

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Windows 콘솔/리다이렉트가 CP949로 잡혀 로그가 깨지는 문제 방지
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

_TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
_MAX_WORKERS   = 12
_FEED_TIMEOUT  = 20
_USER_AGENT    = "Mozilla/5.0 (compatible; pharma-news-monitor)"

# ── 수집 단계 필터 상수 ────────────────────────────────────────────
# dedicated 피드: 관련성 점수가 이 이하면 명백한 비제약 기사로 판단 → 저장 안 함
_DEDICATED_REL_THRESHOLD = -5
# 제목 + 요약 합계가 이보다 짧으면 내용 없는 쓰레기 엔트리로 판단 → 저장 안 함
_MIN_BODY_LEN = 30


# ── 번역 ────────────────────────────────────────────────────────

def _translate_groq(text: str) -> str:
    """Groq 폴백 번역 — Google 비공식 endpoint 장애 시 2중화."""
    try:
        from groq import Groq
        from relevance_ai import _load_api_key
        key = _load_api_key()
        if not key:
            return ""
        resp = Groq(api_key=key, timeout=20, max_retries=1).chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[{
                "role": "user",
                "content": (
                    "Translate the following pharma/biotech news text into natural Korean. "
                    "Output ONLY the Korean translation, nothing else.\n\n" + text[:800]
                ),
            }],
            max_tokens=900,
            temperature=0,
            reasoning_effort="low",
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.debug(f"Groq 폴백 번역 실패: {e}")
        return ""


def translate_to_ko(text: str, timeout: int = 8) -> str:
    if not text or not text.strip():
        return ""
    try:
        params = {"client": "gtx", "sl": "en", "tl": "ko", "dt": "t", "q": text[:800]}
        r = requests.get(_TRANSLATE_URL, params=params, verify=False, timeout=timeout)
        r.raise_for_status()
        result = "".join(seg[0] for seg in r.json()[0] if seg[0])
        if result.strip():
            return result
    except Exception as e:
        logger.debug(f"Google 번역 실패, Groq 폴백 시도: {e}")
    return _translate_groq(text)


# ── 유틸 ─────────────────────────────────────────────────────────

def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    return re.sub(r"\s+", " ", text).strip()


def compute_hash(title: str, link: str) -> str:
    return hashlib.md5(f"{title}|{link}".encode("utf-8")).hexdigest()


def normalize_date(raw: str) -> str:
    """발행일 문자열을 naive-UTC ISO 형식으로 정규화.

    RSS 날짜에 타임존(+0900 등)이 있으면 UTC로 변환 후 tzinfo를 제거한다.
    DB의 published_dt/fetched_at이 모두 naive-UTC로 통일되어야
    30일 컷오프·시간감쇠·'오늘' 필터가 정확해진다.
    (과거에는 KST 벽시계 시각이 그대로 저장돼 최대 9시간 오차가 있었음)
    """
    if not raw:
        return ""
    raw = raw.strip()
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is not None:
            dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        pass
    # ISO 8601 (타임존 오프셋/Z 포함) — 기존 raw[:len(fmt)] 방식은 %Y가
    # 값(4자)보다 짧아 항상 잘못 잘려 ISO 날짜 파싱이 전부 실패하던 버그가 있었음
    try:
        dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        pass
    for fmt, ln in [("%Y-%m-%d %H:%M:%S", 19), ("%Y/%m/%d %H:%M:%S", 19),
                    ("%Y-%m-%d", 10), ("%Y/%m/%d", 10)]:
        try:
            return datetime.datetime.strptime(raw[:ln], fmt).strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:
            continue
    return ""


# ── Google News 링크 디코딩 ──────────────────────────────────────
# Google News RSS 링크(news.google.com/rss/articles/...)의 article id를
# base64 디코딩하면 구형 포맷은 원문 URL이 그대로 들어 있다.
# 신형 포맷(AU_yqL...)은 정적 디코딩이 불가능하므로 원본 링크를 유지한다
# (실패해도 무해한 best-effort).
_GNEWS_RE = re.compile(r"news\.google\.com/(?:rss/articles|articles)/([^?/]+)")
_URL_IN_BYTES_RE = re.compile(rb"https?://[\x21-\x7e]+")


def decode_gnews_url(url: str) -> str:
    m = _GNEWS_RE.search(url or "")
    if not m:
        return url
    try:
        data = base64.urlsafe_b64decode(m.group(1) + "===")
        m2 = _URL_IN_BYTES_RE.search(data)
        if m2:
            decoded = m2.group(0).decode("ascii", "ignore")
            # protobuf 잔여 바이트가 URL 끝에 붙는 경우 방어
            if decoded.startswith("http") and "." in decoded[:40]:
                return decoded
    except Exception:
        pass
    return url


# ── 설정 로드 ────────────────────────────────────────────────────

# Google News 검색은 기간 지정이 없으면 관련도순이라 결과 100개 대부분이 몇 달~몇 년 전
# 기사였음 (2026-10 확인: 한국경제 최신 결과가 3주 전, 헬스코리아뉴스는 2018년).
# fetch가 3시간 주기이므로 최근 2일로 제한하면 누락 없이 최신 기사만 받는다.
_GNEWS_RECENCY = "when:2d"
_GNEWS_SEARCH  = {
    "ko": "https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko",
    "en": "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en",
}
_WATCH_CHUNK   = 8     # Google News 검색 1회에 OR로 묶을 이름 수
# Google News 제목 끝의 ' - 매체명' (직접 수집한 같은 기사와 중복 판정하려면 제거해야 함)
_GNEWS_SUFFIX_RE = re.compile(r"\s+-\s+[^-]{1,30}$")


def with_recency(url: str) -> str:
    if "news.google.com/rss/search?q=" not in url or "when%3A" in url or "when:" in url:
        return url
    return url.replace("search?q=", f"search?q={quote(_GNEWS_RECENCY)}+", 1)


def load_watchlist() -> dict:
    """워치리스트 이름을 종류별로 반환 {'company': [...], 'competitor': [...], 'topic': [...]}."""
    try:
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT keyword, COALESCE(kind, 'company') AS kind FROM watchlist")
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"워치리스트 로드 실패 (고객사·경쟁사 검색 생략): {e}")
        return {}
    by_kind: dict = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r["keyword"])
    return by_kind


def watch_search_urls(names: list, lang: str = "ko", context: str = "") -> list:
    """이름(고객사·경쟁사)으로 Google News를 검색하는 URL 목록 — 피드 목록에 없는
    매체의 기사도 수집한다. 국내 검색은 한글명만, 해외 검색은 영문명만 사용
    (국내 매체는 한글명으로 보도하므로 영문명은 오탐·중복만 늘림).
    context: 검색어에 AND로 붙일 조건 (예: 해외 경쟁사는 주가·실적 기사가 대부분이라 공정 키워드로 한정)."""
    names = [n for n in names if n.isascii() == (lang == "en")]
    urls = []
    for i in range(0, len(names), _WATCH_CHUNK):
        q = " OR ".join(f'"{n}"' for n in names[i:i + _WATCH_CHUNK])
        urls.append(_GNEWS_SEARCH[lang].format(q=quote(f"({q}) {context}".strip())))
    return urls


def load_config():
    with open("feeds.yaml", encoding="utf-8") as f:
        feeds = yaml.safe_load(f)["feeds"]
    with open("keywords.yaml", encoding="utf-8") as f:
        kw = yaml.safe_load(f)
    with open("scoring.yaml", encoding="utf-8") as f:
        sc = yaml.safe_load(f)
    topic_kws   = [k.lower() for k in kw.get("topic_keywords", [])]
    entity_kws  = [k.lower() for k in kw.get("entity_keywords", [])]
    rel_weights = sc.get("relevance_weights", {})
    return feeds, topic_kws, entity_kws, rel_weights


def _quick_relevance(text: str, rel_weights: dict) -> float:
    """scoring.yaml relevance_weights로 빠른 관련성 점수 계산.
    Dedicated 피드 pre-filter 전용 — 강한 음수(-5 이하)이면 비제약 기사."""
    return weighted_score(text, rel_weights)


# ── 단일 피드 수집 (스레드 단위) ─────────────────────────────────

def _parse_entries(urls: list) -> list:
    """URL들의 엔트리를 합쳐 반환. 전부 실패했을 때만 예외."""
    entries, errors = [], []
    for u in urls:
        # feedparser 기본 User-Agent를 차단하고 HTML을 돌려주는 매체가 있음 (헬스코리아뉴스)
        parsed = feedparser.parse(with_recency(u), agent=_USER_AGENT)
        if parsed.bozo and not parsed.entries:
            errors.append(str(parsed.get("bozo_exception", "empty feed")))
            continue
        entries.extend(parsed.entries)
    if errors and not entries:
        raise Exception(errors[0])
    return entries


def fetch_single_feed(feed: dict, topic_kws: list, entity_kws: list,
                      rel_weights: dict, companies: list = ()) -> tuple:
    """
    피드 1개를 수집해 DB에 저장.
    반환: (result_dict, saved_count)
    각 스레드가 독립적인 DB 연결을 사용한다.

    수집 단계 2중 필터:
      [고객사검색] 제목에 고객사명 없으면 → 저장 안 함 (Google 검색 오탐)
      [Dedicated]  quick_relevance < -5  → 저장 안 함 (명백 비제약)
      [Non-ded]    topic 키워드·고객사명 없으면 → 저장 안 함 (entity만으로는 부족)
      [공통]       body 길이 < 30      → 저장 안 함 (내용 없는 엔트리)
    """
    all_kws   = topic_kws + entity_kws
    name      = feed["name"]
    urls      = feed.get("urls") or [feed["url"]]
    lang      = feed.get("lang", "")
    dedicated = feed.get("dedicated", False)
    watch     = bool(feed.get("watchlist_search"))
    names     = feed.get("names", [])   # 검색 피드가 찾는 이름 (고객사 또는 경쟁사)
    comp_low  = [c.lower() for c in companies]

    try:
        entries = _parse_entries(urls)

        skipped_short  = 0
        skipped_filter = 0
        candidates     = []
        seen_hashes: set = set()
        seen_titles: set = set()

        for entry in entries:
            title     = strip_html((entry.get("title") or "").strip())
            link      = decode_gnews_url((entry.get("link") or "").strip())
            summary   = strip_html((entry.get("summary") or entry.get("description") or "").strip())
            published = (entry.get("published") or entry.get("updated") or "").strip()

            if watch:
                title = _GNEWS_SUFFIX_RE.sub("", title)
            if not title or not link:
                continue

            body = f"{title} {summary}"

            # ── 공통: 최소 본문 길이 체크 ─────────────────────────
            if len(body.strip()) < _MIN_BODY_LEN:
                skipped_short += 1
                continue

            # ── 피드 타입별 필터 ──────────────────────────────────
            if watch:
                # Google은 본문·관련 키워드까지 넓게 매칭하므로 제목에 회사명이 있는 기사만
                title_low = title.lower()
                matched   = [n for n in names if contains_name(title_low, n.lower())]
                if not matched:
                    skipped_filter += 1
                    continue
                should_save = True
            elif dedicated:
                # 전문 매체도 명백한 비제약 콘텐츠는 quick-filter
                quick_rel = _quick_relevance(body, rel_weights)
                if quick_rel < _DEDICATED_REL_THRESHOLD:
                    skipped_filter += 1
                    continue
                matched     = match_keywords(body, all_kws)
                should_save = True
            else:
                matched_t_title = match_keywords(title, topic_kws)   # 제목에서만
                matched_t_body  = match_keywords(body, topic_kws)    # 제목+요약 전체
                matched_e       = match_keywords(body, entity_kws)
                body_low        = body.lower()
                matched_c       = [c for c, cl in zip(companies, comp_low) if contains_name(body_low, cl)]
                matched         = matched_t_body + matched_e + matched_c
                # 저장 조건: 제목에 topic kw 1개 이상  OR  전체에 topic kw 2개 이상
                # → "신약"이 요약에 1번만 등장하는 비제약 기사 차단
                # 담당 고객사가 언급된 기사는 topic 키워드가 없어도 저장
                should_save = (len(matched_t_title) >= 1 or len(matched_t_body) >= 2
                               or bool(matched_c))

            if not should_save:
                skipped_filter += 1
                continue

            # ── 30일 초과 기사 제외 ────────────────────────────────
            published_dt = normalize_date(published)
            if published_dt:
                try:
                    pub_date = datetime.datetime.fromisoformat(published_dt)
                    if (datetime.datetime.utcnow() - pub_date).days > 30:
                        continue
                except Exception:
                    pass

            pub_dt_obj = None
            if published_dt:
                try:
                    pub_dt_obj = datetime.datetime.fromisoformat(published_dt)
                except Exception:
                    pass

            # non-dedicated 기사는 AI 승인 전까지 숨김 (hidden=1)
            initial_hidden = 0 if dedicated else 1
            h = compute_hash(title, link)
            if h in seen_hashes or title in seen_titles:
                continue
            seen_hashes.add(h)
            seen_titles.add(title)
            candidates.append({
                "title": title, "link": link, "published": published,
                "pub_dt": pub_dt_obj, "summary": summary[:2000],
                "keywords": ", ".join(matched) if matched else "",
                "hash": h, "hidden": initial_hidden,
            })

        # ── DB 저장 (스레드별 독립 연결) ──────────────────────────
        # 이미 저장된 기사를 먼저 한 번에 걸러낸 뒤 새 기사만 번역·저장한다.
        # (과거에는 매 실행마다 피드의 모든 영문 기사를 번역하고 건별로
        #  SELECT/INSERT 해서 수집 작업이 CI 타임아웃을 넘기는 원인이 됐음)
        saved = 0
        if candidates:
            conn = get_db()
            try:
                with conn.cursor() as cur:
                    # hash는 title|link 기반이라 링크 형식이 바뀌면(예: Google News
                    # 디코딩 도입) 같은 기사가 재저장될 수 있음 → 제목+소스로 2차 방어
                    # 고객사 검색 기사는 다른 피드가 이미 같은 제목으로 수집했으면 건너뜀
                    cur.execute(
                        "SELECT hash, title FROM articles "
                        "WHERE hash = ANY(%s) OR ((source = %s OR %s) AND title = ANY(%s))",
                        ([c["hash"] for c in candidates], name, watch,
                         [c["title"] for c in candidates]),
                    )
                    existing = cur.fetchall()
                known_hashes = {r["hash"] for r in existing}
                known_titles = {r["title"] for r in existing}
                new_items = [c for c in candidates
                             if c["hash"] not in known_hashes and c["title"] not in known_titles]

                now = datetime.datetime.utcnow()
                rows_to_insert = []
                for c in new_items:
                    # ── 영문 기사 번역 (신규 기사만) ──────────────────
                    title_ko = summary_ko = ""
                    if lang == "en":
                        title_ko   = translate_to_ko(c["title"])
                        summary_ko = translate_to_ko(c["summary"][:800]) if c["summary"] else ""
                    rows_to_insert.append((
                        c["title"], c["link"], name, c["published"], c["pub_dt"],
                        c["summary"], c["keywords"], c["hash"], now,
                        lang, title_ko, summary_ko, c["hidden"],
                    ))

                if rows_to_insert:
                    with conn.cursor() as cur:
                        inserted = execute_values(
                            cur,
                            """INSERT INTO articles
                               (title, link, source, published, published_dt, summary, keywords,
                                hash, fetched_at, lang, title_ko, summary_ko, hidden)
                               VALUES %s
                               ON CONFLICT (hash) DO NOTHING
                               RETURNING id""",
                            rows_to_insert,
                            fetch=True,
                        )
                        saved = len(inserted)
                    conn.commit()
            finally:
                conn.close()

        marker = "[전문]" if dedicated else "[필터]"
        detail = ""
        if skipped_short:
            detail += f" (짧은기사 {skipped_short}건 제외)"
        if skipped_filter:
            detail += f" (비제약 {skipped_filter}건 제외)"
        logger.info(f"[OK] {marker} {name}: {saved}개 저장 / {len(entries)}개 수신{detail}")
        return (
            {"name": name, "status": "ok", "saved": saved,
             "total": len(entries), "dedicated": dedicated},
            saved,
        )

    except Exception as e:
        logger.error(f"[FAIL] {name}: {e}")
        return ({"name": name, "status": "error", "error": str(e)}, 0)


# ── 피드 헬스 기록 ────────────────────────────────────────────────

def _record_feed_health(results: list):
    """피드별 수집 성공/실패 이력을 feed_health 테이블에 upsert.
    연속 실패 카운트로 죽은 피드를 대시보드에서 감지할 수 있게 한다."""
    try:
        conn = get_db()
        now = datetime.datetime.utcnow()
        with conn.cursor() as cur:
            for r in results:
                ok = r.get("status") == "ok"
                cur.execute(
                    """INSERT INTO feed_health
                         (source, last_status, last_error, last_run_at,
                          last_success_at, consecutive_failures, last_saved)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (source) DO UPDATE SET
                         last_status          = EXCLUDED.last_status,
                         last_error           = EXCLUDED.last_error,
                         last_run_at          = EXCLUDED.last_run_at,
                         last_success_at      = COALESCE(EXCLUDED.last_success_at,
                                                         feed_health.last_success_at),
                         consecutive_failures = CASE WHEN EXCLUDED.last_status = 'ok'
                                                     THEN 0
                                                     ELSE feed_health.consecutive_failures + 1 END,
                         last_saved           = COALESCE(EXCLUDED.last_saved,
                                                         feed_health.last_saved)""",
                    (r["name"], "ok" if ok else "error",
                     (r.get("error") or "")[:500] or None, now,
                     now if ok else None, 0 if ok else 1,
                     r.get("saved") if ok else None),
                )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"feed_health 기록 실패 (무시): {e}")


# ── 메인 수집 ────────────────────────────────────────────────────

def fetch_feeds():
    feeds, topic_kws, entity_kws, rel_weights = load_config()

    init_db()
    socket.setdefaulttimeout(_FEED_TIMEOUT)
    watchlist = load_watchlist()
    companies = watchlist.get("company", [])

    # 고객사·경쟁사 검색 피드는 URL을 워치리스트에서 생성 (이름이 없으면 생략)
    expanded = []
    for feed in feeds:
        kind = feed.get("watchlist_search")
        if kind:
            names = watchlist.get("company" if kind is True else kind, [])
            urls  = watch_search_urls(names, feed.get("lang", "ko"), feed.get("search_context", ""))
            if not urls:
                continue
            feed = {**feed, "urls": urls, "names": names}
        expanded.append(feed)
    feeds = expanded

    results     = []
    total_saved = 0

    logger.info(f"피드 수집 시작: {len(feeds)}개 피드, 최대 {_MAX_WORKERS}개 병렬 (고객사 {len(companies)}개)")

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="feed") as pool:
        futures = {
            pool.submit(fetch_single_feed, feed, topic_kws, entity_kws, rel_weights, companies): feed["name"]
            for feed in feeds
        }
        for future in as_completed(futures):
            try:
                result, saved = future.result()
            except Exception as e:
                name = futures[future]
                result = {"name": name, "status": "error", "error": str(e)}
                saved  = 0
            results.append(result)
            total_saved += saved

    _record_feed_health(results)
    return results, total_saved


if __name__ == "__main__":
    results, total_saved = fetch_feeds()

    try:
        from scoring import run_scoring
        logger.info("스코어링 시작...")
        run_scoring()
    except Exception as e:
        logger.warning(f"스코어링 실패 (무시): {e}")

    force_ai = "--force" in sys.argv
    try:
        from relevance_ai import run_relevance_classification
        logger.info(f"AI 관련성 분류 시작 (force={force_ai})...")
        hidden = run_relevance_classification(days=7 if force_ai else 3, force=force_ai)
        logger.info(f"AI 관련성 분류 완료 - {hidden}건 필터링")
    except Exception as e:
        logger.warning(f"AI 분류 실패 (무시): {e}")

    ok   = [r for r in results if r["status"] == "ok"]
    fail = [r for r in results if r["status"] == "error"]
    ded  = [r for r in ok if r.get("dedicated")]
    flt  = [r for r in ok if not r.get("dedicated")]

    print("\n" + "=" * 58)
    print(f"  수집 완료: {len(results)}개 피드 시도")
    print(f"  성공: {len(ok)}개  (전문매체 {len(ded)} | 필터매체 {len(flt)})")
    print(f"  실패: {len(fail)}개")
    print(f"  저장된 새 기사: {total_saved}개")
    if fail:
        print("\n  [실패 목록]")
        for r in fail:
            print(f"    - {r['name']}: {r['error']}")
    print("=" * 58)
