import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import get_db
conn = get_db()
cur = conn.cursor()
cur.execute("SELECT id, source, title, cluster_id, fetched_at FROM articles WHERE cluster_id IN (%s, %s, %s, %s) ORDER BY id", ("312423", "358995", "122608", "311369"))
rows = cur.fetchall()
for r in rows:
    print(r["id"], r["source"][:15], r["cluster_id"], r["title"][:60])
conn.close()
