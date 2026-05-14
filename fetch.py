import os
import yaml
import sqlite3
import hashlib
import feedparser
import datetime
import logging
import sys
import re
import requests
import urllib3
from email.utils import parsedate_to_datetime

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

_DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(_DATA_DIR, "news.db")

_KO_SOURCES = {
    "히트뉴스", "팜뉴스", "메디파나뉴스", "의약뉴스", "청년의사", "의학신문",
    "약사공론", "약업신문", "바이오스펙테이터", "메디게이트뉴스",
    "연합뉴스", "연합뉴스 건강", "뉴스1", "뉴스1 헬스", "뉴시스", "뉴시스 헬스",
    "헬스조선",
}

_TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"


# ── 번역 ────────────────────────────────────────────────────────

def translate_to_ko(text: str, timeout: int = 8) -> str:
    """영문 텍스트를 한국어로 번역. 실패 시 빈 문자열 반환."""
    if not text or not text.strip():
        return ""
    try:
        params = {"client": "gtx", "sl": "en", "tl": "ko", "dt": "t", "q": text[:800]}
        r = requests.get(_TRANSLATE_URL, params=params, verify=False, timeout=timeout)
        r.raise_for_status()
        return "".join(seg[0] for seg in r.json()[0] if seg[0])
    except Exception as e:
        logger.debug(f"번역 실패: {e}")
        return ""


# ── 유틸 ─────────────────────────────────────────────────────────

def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    return re.sub(r"\s+", " ", text).strip()


def compute_hash(title: str, link: str) -> str:
    return hashlib.md5(f"{title}|{link}".encode("utf-8")).hexdigest()


def normalize_date(raw: str) -> str:
    """RSS/이메일 등 다양한 날짜 문자열 → 'YYYY-MM-DDTHH:MM:SS' ISO 형식으로 통일."""
    if not raw:
        return ""
    raw = raw.strip()
    # RFC 2822 (RSS 기본 형식: "Thu, 07 May 2026 13:17:55 +0000")
    try:
        dt = parsedate_to_datetime(raw)
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        pass
    # ISO 계열
    for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"]:
        try:
            return datetime.datetime.strptime(raw[:len(fmt)], fmt).strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:
            continue
    return ""


# ── DB 초기화 / 마이그레이션 ─────────────────────────────────────

def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            title        TEXT,
            link         TEXT,
            source       TEXT,
            published    TEXT,
            published_dt TEXT DEFAULT '',
            summary      TEXT,
            keywords     TEXT,
            hash         TEXT UNIQUE,
            fetched_at   TEXT,
            lang         TEXT DEFAULT '',
            title_ko     TEXT DEFAULT '',
            summary_ko   TEXT DEFAULT ''
        )
    """)
    for col, default in [("lang", "''"), ("title_ko", "''"), ("summary_ko", "''"),
                         ("published_dt", "''"), ("hidden", "0")]:
        try:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {col} TEXT DEFAULT {default}")
            logger.info(f"DB 컬럼 추가: {col}")
        except sqlite3.OperationalError:
            pass

    # 기존 행 lang 채우기
    for src in _KO_SOURCES:
        conn.execute(
            "UPDATE articles SET lang='ko' WHERE source=? AND (lang IS NULL OR lang='')", (src,)
        )
    conn.execute(
        "UPDATE articles SET lang='en' WHERE (lang IS NULL OR lang='') AND source NOT IN (%s)"
        % ",".join("?" * len(_KO_SOURCES)),
        list(_KO_SOURCES),
    )
    conn.commit()


# ── 설정 로드 ────────────────────────────────────────────────────

def load_config():
    with open("feeds.yaml", encoding="utf-8") as f:
        feeds = yaml.safe_load(f)["feeds"]
    with open("keywords.yaml", encoding="utf-8") as f:
        kw = yaml.safe_load(f)
    topic_kws  = [k.lower() for k in kw.get("topic_keywords", [])]
    entity_kws = [k.lower() for k in kw.get("entity_keywords", [])]
    return feeds, topic_kws, entity_kws


def match_keywords(text: str, keywords: list) -> list:
    t = text.lower()
    return [kw for kw in keywords if kw in t]


# ── 메인 수집 ────────────────────────────────────────────────────

def fetch_feeds():
    feeds, topic_kws, entity_kws = load_config()
    all_kws = topic_kws + entity_kws

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    c = conn.cursor()

    total_saved = 0
    results = []

    for feed in feeds:
        name      = feed["name"]
        url       = feed["url"]
        lang      = feed.get("lang", "")
        dedicated = feed.get("dedicated", False)

        try:
            parsed = feedparser.parse(url)
            if parsed.bozo and not parsed.entries:
                raise Exception(str(parsed.get("bozo_exception", "empty feed")))

            saved = 0
            for entry in parsed.entries:
                title     = strip_html((entry.get("title") or "").strip())
                link      = (entry.get("link") or "").strip()
                summary   = strip_html((entry.get("summary") or entry.get("description") or "").strip())
                published = (entry.get("published") or entry.get("updated") or "").strip()

                if not title or not link:
                    continue

                body = f"{title} {summary}"

                if dedicated:
                    matched     = match_keywords(body, all_kws)
                    should_save = True
                else:
                    matched_t   = match_keywords(body, topic_kws)
                    matched_e   = match_keywords(body, entity_kws)
                    matched     = matched_t + matched_e
                    should_save = len(matched_t) > 0

                if not should_save:
                    continue

                # 발행일 기준 30일 초과 기사 제외 (RSS 피드에 섞인 오래된 기사 차단)
                published_dt = normalize_date(published)
                if published_dt:
                    try:
                        pub_date = datetime.datetime.fromisoformat(published_dt)
                        if (datetime.datetime.utcnow() - pub_date).days > 30:
                            continue
                    except Exception:
                        pass

                # 영문 기사 → 번역
                title_ko = summary_ko = ""
                if lang == "en":
                    title_ko   = translate_to_ko(title)
                    summary_ko = translate_to_ko(summary[:500]) if summary else ""

                h = compute_hash(title, link)
                try:
                    c.execute(
                        """INSERT INTO articles
                           (title, link, source, published, published_dt, summary, keywords,
                            hash, fetched_at, lang, title_ko, summary_ko)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            title, link, name, published, published_dt,
                            summary[:2000],
                            ", ".join(matched) if matched else "",
                            h,
                            datetime.datetime.utcnow().isoformat(),
                            lang, title_ko, summary_ko,
                        ),
                    )
                    saved += 1
                except sqlite3.IntegrityError:
                    pass  # 중복

            conn.commit()
            total_saved += saved
            results.append({"name": name, "status": "ok", "saved": saved,
                            "total": len(parsed.entries), "dedicated": dedicated})
            marker = "[전문]" if dedicated else "[필터]"
            logger.info(f"[OK] {marker} {name}: {saved}개 저장 / {len(parsed.entries)}개 수신")

        except Exception as e:
            results.append({"name": name, "status": "error", "error": str(e)})
            logger.error(f"[FAIL] {name}: {e}")

    conn.close()
    return results, total_saved


if __name__ == "__main__":
    results, total_saved = fetch_feeds()

    # 수집 완료 후 스코어링·클러스터링 실행
    if total_saved > 0:
        try:
            from scoring import run_scoring
            logger.info("스코어링 시작...")
            run_scoring()
        except Exception as e:
            logger.warning(f"스코어링 실패 (무시): {e}")

        # AI 관련성 분류 (비전문 매체 기사 필터링)
        try:
            from relevance_ai import run_relevance_classification
            logger.info("AI 관련성 분류 시작...")
            hidden = run_relevance_classification(days=1)
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
