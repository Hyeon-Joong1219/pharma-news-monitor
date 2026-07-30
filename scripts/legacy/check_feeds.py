import sqlite3

conn = sqlite3.connect("news.db")
conn.row_factory = sqlite3.Row

new_feeds = [
    "CNBC Health", "Axios Healthcare", "Forbes Healthcare",
    "Business Insider Health", "Barron's Biotech", "MarketWatch Pharma",
]

print("=== 신규 메이저 경제지 수집 결과 ===")
for f in new_feeds:
    rows = conn.execute(
        "SELECT COUNT(*) as cnt FROM articles WHERE source=? AND fetched_at >= datetime('now','-2 hours')", (f,)
    ).fetchone()
    total = conn.execute("SELECT COUNT(*) as cnt FROM articles WHERE source=?", (f,)).fetchone()
    print(f"  {f:30s}: 신규 {rows['cnt']:3d}건 / 전체 {total['cnt']:3d}건")

print()
row = conn.execute("SELECT COUNT(*) as cnt FROM articles WHERE fetched_at >= datetime('now','-2 hours')").fetchone()
print(f"최근 2시간 전체 수집: {row['cnt']}건")

print()
print("=== CNBC Health 최근 기사 샘플 ===")
rows = conn.execute(
    "SELECT title, published_dt FROM articles WHERE source='CNBC Health' ORDER BY fetched_at DESC LIMIT 5"
).fetchall()
for r in rows:
    dt = (r["published_dt"] or "")[:10]
    print(f"  [{dt}] {r['title'][:80]}")

print()
print("=== Axios Healthcare 최근 기사 샘플 ===")
rows = conn.execute(
    "SELECT title, published_dt FROM articles WHERE source='Axios Healthcare' ORDER BY fetched_at DESC LIMIT 5"
).fetchall()
for r in rows:
    dt = (r["published_dt"] or "")[:10]
    print(f"  [{dt}] {r['title'][:80]}")

conn.close()
