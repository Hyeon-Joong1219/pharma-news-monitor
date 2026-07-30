"""기존 articles 테이블의 published 날짜를 ISO 형식으로 정규화."""
import sqlite3
import datetime
from email.utils import parsedate_to_datetime


def normalize_date(raw: str) -> str:
    if not raw:
        return ""
    raw = raw.strip()
    try:
        dt = parsedate_to_datetime(raw)
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        pass
    for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"]:
        try:
            return datetime.datetime.strptime(raw[:len(fmt)], fmt).strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:
            continue
    return ""


conn = sqlite3.connect("news.db")

# 컬럼 추가 (이미 있으면 무시)
try:
    conn.execute("ALTER TABLE articles ADD COLUMN published_dt TEXT DEFAULT ''")
    print("published_dt 컬럼 추가 완료")
except Exception:
    print("published_dt 컬럼 이미 존재")

rows = conn.execute(
    "SELECT id, published FROM articles WHERE published_dt IS NULL OR published_dt = ''"
).fetchall()
print(f"정규화 대상: {len(rows)}건")

updated = 0
failed = 0
batch = []
for row_id, raw in rows:
    dt = normalize_date(raw or "")
    if dt:
        batch.append((dt, row_id))
        updated += 1
    else:
        # 정규화 실패 시 fetched_at 값으로 대체
        batch.append(("", row_id))
        failed += 1
    if len(batch) >= 500:
        conn.executemany("UPDATE articles SET published_dt=? WHERE id=?", batch)
        conn.commit()
        batch = []

if batch:
    conn.executemany("UPDATE articles SET published_dt=? WHERE id=?", batch)
    conn.commit()

# 정규화 실패한 건은 fetched_at 으로 채우기
conn.execute(
    "UPDATE articles SET published_dt=fetched_at WHERE published_dt IS NULL OR published_dt = ''"
)
conn.commit()
conn.close()

print(f"완료: {updated}건 정규화, {failed}건 fetched_at 대체")
