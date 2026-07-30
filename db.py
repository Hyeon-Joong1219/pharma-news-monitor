import os
import psycopg2
from psycopg2.extras import RealDictCursor

def _load_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        for line in open(env_path, encoding="utf-8"):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

_load_env()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()


def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn


def init_db():
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                id              SERIAL PRIMARY KEY,
                title           TEXT,
                link            TEXT,
                source          TEXT,
                published       TEXT,
                published_dt    TIMESTAMP,
                summary         TEXT,
                keywords        TEXT DEFAULT '',
                hash            TEXT UNIQUE,
                fetched_at      TIMESTAMP NOT NULL DEFAULT NOW(),
                lang            TEXT DEFAULT '',
                title_ko        TEXT DEFAULT '',
                summary_ko      TEXT DEFAULT '',
                hidden          INTEGER DEFAULT 0,
                score           REAL DEFAULT 0,
                relevance_score REAL,
                ai_classified   INTEGER DEFAULT 0,
                source_count    INTEGER DEFAULT 1,
                cluster_id      TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_fetched_at ON articles(fetched_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lang     ON articles(lang)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_score    ON articles(score DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_source   ON articles(source)")
        # 저장 시 제목+소스 중복 방어 조회용
        cur.execute("CREATE INDEX IF NOT EXISTS idx_source_title ON articles(source, title)")
        # 기존 테이블에 컬럼 추가 (이미 존재하면 무시)
        cur.execute("ALTER TABLE articles ADD COLUMN IF NOT EXISTS cluster_id TEXT")
        # 사용자 피드백: 0=없음, -1=사용자가 '관련 없음' 처리, 1=사용자가 복구
        # user_feedback != 0 인 기사의 hidden은 자동 파이프라인이 덮어쓰지 않음
        cur.execute("ALTER TABLE articles ADD COLUMN IF NOT EXISTS user_feedback INTEGER DEFAULT 0")
        # 사용자 관심 워치리스트 키워드
        cur.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                id         SERIAL PRIMARY KEY,
                keyword    TEXT UNIQUE NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)
        # 피드별 수집 상태 (fetch.py가 매 실행 upsert)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS feed_health (
                source               TEXT PRIMARY KEY,
                last_status          TEXT,
                last_error           TEXT,
                last_run_at          TIMESTAMP,
                last_success_at      TIMESTAMP,
                consecutive_failures INTEGER DEFAULT 0,
                last_saved           INTEGER
            )
        """)
    conn.commit()
    conn.close()
