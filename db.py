import os

# 회사 프록시(SSL 검사)가 자체 루트 인증서로 트래픽을 가로채 Python의 certifi
# 인증서 저장소로는 검증에 실패함 (CERTIFICATE_VERIFY_FAILED: self-signed
# certificate in certificate chain). truststore로 OS(Windows) 인증서 저장소를
# 사용하도록 전역 패치 — Groq/번역 등 모든 외부 HTTPS 호출에 적용됨.
# db 모듈이 가장 먼저 임포트되므로 여기서 한 번만 적용한다.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

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
    # connect_timeout: DB가 응답하지 않을 때 요청이 무한 대기하지 않도록 함
    # keepalives: 긴 배치 작업 중 유휴 연결이 중간 장비에 의해 끊기는 것 방지
    conn = psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor,
        connect_timeout=10,
        keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
    )
    return conn


def execute_batch_update(conn, sql: str, rows: list, template: str = None, page_size: int = 500):
    """UPDATE ... FROM (VALUES %s) 형태의 다건 갱신을 소수의 왕복으로 처리.
    원격 DB(Supabase)에서 행 단위 execute는 건당 수백 ms가 걸려 수천 건이면
    CI 타임아웃(60분)을 넘기므로, 반드시 이 함수로 묶어서 보낸다."""
    from psycopg2.extras import execute_values
    if not rows:
        return
    with conn.cursor() as cur:
        execute_values(cur, sql, rows, template=template, page_size=page_size)
    conn.commit()


def init_db():
    conn = get_db()
    try:
        _create_schema(conn)
    finally:
        conn.close()


def _create_schema(conn):
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
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cluster_id ON articles(cluster_id)")
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
