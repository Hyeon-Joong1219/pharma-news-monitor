# -*- coding: utf-8 -*-
"""DB 백업 — articles/feed_health/watchlist 테이블을 CSV.gz로 덤프.

- backups/ 폴더에 backup_YYYYMMDD_HHMM_<table>.csv.gz 형태로 저장
- 마지막 백업이 6일 이내면 건너뜀 (run_all.bat에서 매일 호출해도 주 1회만 실행)
  → 강제 실행: python scripts/backup_db.py --force
- 오래된 백업은 세트 8개까지만 보관 (약 2개월치)

복원 방법 (예: articles):
  gzip으로 압축 해제 후 psql에서
  \\copy articles FROM 'backup_..._articles.csv' WITH CSV HEADER
"""
import sys
import os
import gzip
import glob
import io
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import psycopg2  # noqa: E402
from db import get_db  # noqa: E402

BACKUP_DIR   = os.path.join(ROOT, "backups")
TABLES       = ["articles", "feed_health", "watchlist"]
MIN_AGE_DAYS = 6
KEEP_SETS    = 8


def last_backup_age_days() -> float:
    files = glob.glob(os.path.join(BACKUP_DIR, "backup_*_articles.csv.gz"))
    if not files:
        return 1e9
    newest = max(os.path.getmtime(f) for f in files)
    return (datetime.datetime.now().timestamp() - newest) / 86400


def prune_old():
    """가장 오래된 백업 세트부터 삭제해 KEEP_SETS개 유지."""
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "backup_*_articles.csv.gz")))
    excess = len(files) - KEEP_SETS
    for f in files[:max(0, excess)]:
        prefix = f[: -len("_articles.csv.gz")]
        for old in glob.glob(prefix + "_*.csv.gz"):
            os.remove(old)
            print(f"  오래된 백업 삭제: {os.path.basename(old)}")


CHUNK_ROWS = 5000   # articles는 id 순으로 이만큼씩 나눠 받음
RETRIES    = 3
ID_TABLES  = {"articles", "watchlist"}   # 정수 id가 있어 구간 분할 가능 (feed_health는 작아서 통째로)


def _copy_chunked(table: str, f):
    """테이블을 id 구간별 COPY로 나눠 받아 f(바이너리)에 이어 쓴다.
    한 번에 COPY하면(약 100MB, 4~5분) 회사망에서 연결이 간헐적으로 끊겨
    'PGRES_COPY_OUT ... no message from the libpq'로 실패했음 (2026-07 이후 백업 실패).
    구간마다 새 연결로 받고, 끊기면 그 구간만 재시도한다.
    연결은 autocommit — 트랜잭션 안에서 끊기면 Supavisor가 서버 세션을 'idle in transaction'으로
    붙잡아 articles 잠금이 남고, fetch.py의 ALTER TABLE이 대기하다 시간 초과됨 (2026-10-21)."""
    if table not in ID_TABLES:
        conn = get_db()
        conn.autocommit = True
        try:
            buf = io.BytesIO()
            with conn.cursor() as cur:
                cur.copy_expert(f"COPY {table} TO STDOUT WITH CSV HEADER", buf)
            f.write(buf.getvalue())
        finally:
            conn.close()
        return
    last_id, first = 0, True
    while True:
        for attempt in range(1, RETRIES + 1):
            buf = io.BytesIO()
            conn = get_db()
            conn.autocommit = True
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT MAX(id) AS m FROM (SELECT id FROM {table} WHERE id > %s "
                        f"ORDER BY id LIMIT %s) t", (last_id, CHUNK_ROWS))
                    upto = cur.fetchone()["m"]
                    if upto is None:
                        return
                    cur.copy_expert(
                        f"COPY (SELECT * FROM {table} WHERE id > {int(last_id)} AND id <= {int(upto)} "
                        f"ORDER BY id) TO STDOUT WITH CSV{' HEADER' if first else ''}", buf)
                break
            except psycopg2.Error:
                if attempt == RETRIES:
                    raise
                print(f"  {table} id>{last_id} 구간 재시도 ({attempt}/{RETRIES})")
            finally:
                conn.close()
        f.write(buf.getvalue())
        last_id, first = upto, False


def run(force: bool = False):
    os.makedirs(BACKUP_DIR, exist_ok=True)

    age = last_backup_age_days()
    if not force and age < MIN_AGE_DAYS:
        print(f"백업 건너뜀 — 마지막 백업이 {age:.1f}일 전 ({MIN_AGE_DAYS}일 주기, --force로 강제 실행)")
        return

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    for table in TABLES:
        path = os.path.join(BACKUP_DIR, f"backup_{stamp}_{table}.csv.gz")
        try:
            with gzip.open(path, "wb") as f:
                _copy_chunked(table, f)
        except Exception:
            # 반쪽 파일이 남으면 '최근 백업 있음'으로 보여 6일간 백업을 건너뛰므로 삭제
            if os.path.exists(path):
                os.remove(path)
            raise
        size_mb = os.path.getsize(path) / 1024 / 1024
        print(f"  {table:<12} → {os.path.basename(path)} ({size_mb:.1f} MB)")

    prune_old()
    print("백업 완료")


if __name__ == "__main__":
    run(force="--force" in sys.argv)
