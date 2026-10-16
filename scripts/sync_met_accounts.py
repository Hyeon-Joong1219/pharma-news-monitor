"""Obsidian 미팅록에서 '내가 미팅한 고객사' 목록을 DB(met_accounts)로 동기화.

kakao_brief.py는 GitHub Actions에서 돌기 때문에 로컬 Obsidian 볼트를 읽을 수 없다.
그래서 이 PC에서 주기적으로 실행해 회사명·별칭·최근 미팅일·횟수만 DB에 올린다.
※ 미팅 내용(논의사항·참석자 등)은 올리지 않는다 — 고객 정보가 외부로 나가지 않도록.

읽는 곳 (OBSIDIAN_VAULT, 기본 C:\\김현중)
  Emerging Biotech/Meetings/*.md   frontmatter의 date, account
  2. Areas/<account>.md            '영문명' 행 → 영문 별칭

실행: venv\\Scripts\\python.exe scripts\\sync_met_accounts.py [--dry-run]
"""
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import get_db  # noqa: E402

VAULT = os.environ.get("OBSIDIAN_VAULT", r"C:\김현중")

# 다른 단어에 섞여 오탐이 나는 별칭 (예: '테스' → '테스트')
DENY_ALIASES = {"테스", "tes", "dsm", "icm", "progen", "gbc", "kobia", "pcmo"}


def _frontmatter(path: str) -> dict:
    text = open(path, encoding="utf-8").read()
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    fm = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line and not line.startswith((" ", "\t", "-")):
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip().strip("'\"")
    fm["_body"] = text
    return fm


def _english_name(account: str) -> str:
    path = os.path.join(VAULT, "2. Areas", f"{account}.md")
    if not os.path.exists(path):
        return ""
    m = re.search(r"\|\s*영문명\s*\|\s*([^|]+)\|", open(path, encoding="utf-8").read())
    if not m:
        return ""
    return re.sub(r"\(.*?\)", "", m.group(1)).strip()   # 'GBC (글로벌...)' → 'GBC'


def _aliases(account: str) -> list:
    cands = [account, account.replace(" ", ""), _english_name(account)]
    out = []
    for a in cands:
        a = a.strip()
        if not a or a.lower() in DENY_ALIASES or a in out:
            continue
        if a.isascii() and len(a) <= 4:      # 짧은 영문 약어는 오탐이 심함
            continue
        out.append(a)
    return out


def collect() -> list:
    meetings = {}
    for path in glob.glob(os.path.join(VAULT, "Emerging Biotech", "Meetings", "*.md")):
        fm = _frontmatter(path)
        acc, date = fm.get("account", ""), fm.get("date", "")
        if not acc or not re.match(r"\d{4}-\d{2}-\d{2}$", date):
            continue
        rec = meetings.setdefault(acc, {"last": date, "count": 0})
        rec["count"] += 1
        rec["last"] = max(rec["last"], date)
    return [
        {"account": acc, "aliases": _aliases(acc), "last_meeting": r["last"], "meeting_count": r["count"]}
        for acc, r in sorted(meetings.items(), key=lambda x: x[1]["last"], reverse=True)
    ]


def save(rows: list):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS met_accounts (
                    account       TEXT PRIMARY KEY,
                    aliases       TEXT NOT NULL,
                    last_meeting  DATE NOT NULL,
                    meeting_count INTEGER NOT NULL,
                    updated_at    TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            # 미팅록이 삭제·이름 변경된 경우를 반영하도록 전체 교체
            cur.execute("DELETE FROM met_accounts")
            for r in rows:
                cur.execute(
                    "INSERT INTO met_accounts (account, aliases, last_meeting, meeting_count) "
                    "VALUES (%s, %s, %s, %s)",
                    (r["account"], "|".join(r["aliases"]), r["last_meeting"], r["meeting_count"]),
                )
        conn.commit()
    finally:
        conn.close()


def main():
    rows = collect()
    for r in rows:
        print(f"{r['last_meeting']}  {r['meeting_count']}회  {r['account']:<16} 별칭: {', '.join(r['aliases'])}")
    if "--dry-run" in sys.argv:
        print("\n[dry-run] DB 저장 안 함")
        return
    if not rows:
        sys.exit("미팅록을 찾지 못했습니다 — DB를 비우지 않고 종료합니다. OBSIDIAN_VAULT 경로를 확인하세요.")
    save(rows)
    print(f"\nmet_accounts {len(rows)}개 동기화 완료")


if __name__ == "__main__":
    main()
