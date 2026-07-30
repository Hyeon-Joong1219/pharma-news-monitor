"""published_dt 타임존 정규화 마이그레이션 (1회성, 재실행 안전).

과거 fetch.py는 RSS 발행일의 타임존(+0900 등)을 무시하고 벽시계 시각을
그대로 저장해 UTC 기준 fetched_at과 최대 9시간 어긋났다.
이 스크립트는 저장된 원본 문자열(published)을 수정된 normalize_date로
다시 파싱해 published_dt를 naive-UTC로 통일한다.
"""
import sys
import os
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from db import get_db                  # noqa: E402
from fetch import normalize_date       # noqa: E402

BATCH = 500

conn = get_db()
with conn.cursor() as cur:
    cur.execute(
        "SELECT id, published, published_dt FROM articles "
        "WHERE published IS NOT NULL AND published <> ''"
    )
    rows = cur.fetchall()

print(f"검사 대상: {len(rows)}건")

updates = []
for r in rows:
    iso = normalize_date(r["published"])
    if not iso:
        continue
    try:
        new_dt = datetime.datetime.fromisoformat(iso)
    except ValueError:
        continue
    old_dt = r["published_dt"]
    # 1분 이상 차이나는 경우만 갱신 (이미 UTC인 행은 그대로)
    if old_dt is None or abs((old_dt - new_dt).total_seconds()) >= 60:
        updates.append((new_dt, r["id"]))

print(f"갱신 대상: {len(updates)}건")
updates.sort(key=lambda x: x[1])   # ID 순 갱신 — 동시 실행 시 데드락 방지

for i in range(0, len(updates), BATCH):
    batch = updates[i: i + BATCH]
    with conn.cursor() as cur:
        for row in batch:
            cur.execute("UPDATE articles SET published_dt=%s WHERE id=%s", row)
    conn.commit()
    print(f"  배치 {i // BATCH + 1} 커밋 ({len(batch)}건)")

conn.close()
print("완료")
