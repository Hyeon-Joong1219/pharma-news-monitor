"""
기존 영문 기사를 일괄 번역합니다.
  python translate_batch.py          # 전체 미번역 영문 기사
  python translate_batch.py --limit 200   # 최대 200건
"""
import sqlite3
import sys
import time
import re
import requests
import urllib3
import argparse

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DB_PATH = "news.db"
_TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    return re.sub(r"\s+", " ", text).strip()


def translate_to_ko(text: str, timeout: int = 8) -> str:
    if not text or not text.strip():
        return ""
    try:
        params = {"client": "gtx", "sl": "en", "tl": "ko", "dt": "t", "q": text[:800]}
        r = requests.get(_TRANSLATE_URL, params=params, verify=False, timeout=timeout)
        r.raise_for_status()
        return "".join(seg[0] for seg in r.json()[0] if seg[0])
    except Exception as e:
        return ""


def run(limit: int = 0):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 최신 기사부터 번역 (UI 첫 페이지가 최신순이므로)
    query = "SELECT id, title, summary FROM articles WHERE lang='en' AND (title_ko IS NULL OR title_ko='') ORDER BY fetched_at DESC"
    if limit:
        query += f" LIMIT {limit}"

    rows = conn.execute(query).fetchall()
    total = len(rows)
    print(f"번역 대상: {total}건")

    ok = fail = 0
    for i, row in enumerate(rows, 1):
        t_ko = translate_to_ko(strip_html(row["title"]))
        s_ko = translate_to_ko(strip_html(row["summary"] or "")[:500])

        if t_ko:
            conn.execute(
                "UPDATE articles SET title_ko=?, summary_ko=? WHERE id=?",
                (t_ko, s_ko, row["id"]),
            )
            conn.commit()
            ok += 1
        else:
            fail += 1

        if i % 10 == 0 or i == total:
            pct = i / total * 100
            print(f"  진행: {i}/{total} ({pct:.0f}%)  성공 {ok} / 실패 {fail}")

        time.sleep(0.05)  # Google 요청 제한 회피

    conn.close()
    print(f"\n완료: 성공 {ok}건 / 실패 {fail}건")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="최대 번역 건수 (0=전체)")
    args = parser.parse_args()
    run(limit=args.limit)
