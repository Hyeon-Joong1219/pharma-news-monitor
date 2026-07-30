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
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

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


def run(force: bool = False):
    os.makedirs(BACKUP_DIR, exist_ok=True)

    age = last_backup_age_days()
    if not force and age < MIN_AGE_DAYS:
        print(f"백업 건너뜀 — 마지막 백업이 {age:.1f}일 전 ({MIN_AGE_DAYS}일 주기, --force로 강제 실행)")
        return

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    conn = get_db()
    try:
        for table in TABLES:
            path = os.path.join(BACKUP_DIR, f"backup_{stamp}_{table}.csv.gz")
            with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
                with conn.cursor() as cur:
                    cur.copy_expert(
                        f"COPY {table} TO STDOUT WITH CSV HEADER", f
                    )
            size_mb = os.path.getsize(path) / 1024 / 1024
            print(f"  {table:<12} → {os.path.basename(path)} ({size_mb:.1f} MB)")
    finally:
        conn.close()

    prune_old()
    print("백업 완료")


if __name__ == "__main__":
    run(force="--force" in sys.argv)
