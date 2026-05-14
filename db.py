import os
import psycopg2
from psycopg2.extras import RealDictCursor

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
                source_count    INTEGER DEFAULT 1
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_fetched_at ON articles(fetched_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lang     ON articles(lang)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_score    ON articles(score DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_source   ON articles(source)")
    conn.commit()
    conn.close()
