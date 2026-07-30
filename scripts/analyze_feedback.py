# -*- coding: utf-8 -*-
"""사용자 피드백(🚫 관련없음 / ♻️ 복구) 분석 리포트.

대시보드에서 쌓인 user_feedback 데이터를 분석해:
  1. 사용자가 숨긴 기사(user_feedback=-1)의 소스/키워드 패턴 통계
  2. 사용자가 복구한 기사(user_feedback=1) — AI 오탐 사례
  3. (--ai) Groq에게 패턴을 보내 하드제외 규칙/프롬프트 개선안 제안받기

사용법:
  python scripts/analyze_feedback.py          # 통계만
  python scripts/analyze_feedback.py --ai     # 통계 + AI 개선 제안
피드백이 30건 이상 쌓인 뒤 실행하는 것을 권장.
"""
import sys
import os
import re
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from db import get_db                     # noqa: E402
from scoring import significant_words     # noqa: E402


def load_feedback():
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, title, title_ko, summary, source, lang, relevance_score, "
            "       user_feedback, keywords "
            "FROM articles WHERE COALESCE(user_feedback,0) <> 0 "
            "ORDER BY fetched_at DESC"
        )
        rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    hidden   = [r for r in rows if r["user_feedback"] == -1]
    restored = [r for r in rows if r["user_feedback"] == 1]
    return hidden, restored


def _title(r):
    return (r.get("title_ko") or r.get("title") or "").strip()


def print_stats(hidden, restored):
    print("=" * 62)
    print(f"  사용자 피드백 분석  (숨김 {len(hidden)}건 / 복구 {len(restored)}건)")
    print("=" * 62)

    if hidden:
        print("\n[1] 사용자가 '관련 없음' 처리한 기사")
        src_cnt = Counter(r["source"] for r in hidden)
        print("  ── 소스별 분포 (상위 10):")
        for src, n in src_cnt.most_common(10):
            print(f"     {src:<20} {n}건")

        word_cnt = Counter()
        for r in hidden:
            word_cnt.update(significant_words(_title(r)))
        print("  ── 제목 빈출 단어 (상위 20 — 하드제외 규칙 후보):")
        for w, n in word_cnt.most_common(20):
            if n >= 2:
                print(f"     {w:<20} {n}회")

        print("  ── 최근 숨김 기사 샘플 (10건):")
        for r in hidden[:10]:
            rel = r.get("relevance_score")
            rel_s = f"AI {rel:.0f}점" if rel is not None else "AI 미분류"
            print(f"     [{r['source']}/{rel_s}] {_title(r)[:52]}")

    if restored:
        print("\n[2] 사용자가 복구한 기사 (AI가 잘못 숨긴 오탐 사례)")
        for r in restored[:15]:
            rel = r.get("relevance_score")
            rel_s = f"AI {rel:.0f}점" if rel is not None else "AI 미분류"
            print(f"     [{r['source']}/{rel_s}] {_title(r)[:52]}")
        print("  → 이 유형의 기사가 반복되면 relevance_ai.py 프롬프트의")
        print("    few-shot 예시에 추가하거나 threshold를 조정하세요.")

    if not hidden and not restored:
        print("\n  아직 피드백 데이터가 없습니다.")
        print("  대시보드에서 🚫(관련없음)/♻️(복구) 버튼을 사용하면 쌓입니다.")


def ai_suggestions(hidden, restored):
    """Groq에게 피드백 패턴을 보내 필터 개선안을 제안받는다."""
    try:
        from relevance_ai import _get_client
        client = _get_client()
    except Exception as e:
        print(f"\n[AI 제안 생략] {e}")
        return

    lines = []
    for r in hidden[:40]:
        lines.append(f"HIDDEN: [{r['source']}] {_title(r)[:80]}")
    for r in restored[:20]:
        lines.append(f"RESTORED: [{r['source']}] {_title(r)[:80]}")

    prompt = (
        "You maintain a Korean pharma/biotech news filter. Below is user feedback:\n"
        "HIDDEN = user manually marked as irrelevant (filter missed them)\n"
        "RESTORED = user un-hid them (AI wrongly filtered them out)\n\n"
        + "\n".join(lines) +
        "\n\nBased on these patterns, suggest in Korean:\n"
        "1. New hard-exclude keyword rules (exclude terms + rescue terms) for recurring "
        "irrelevant topics in HIDDEN\n"
        "2. Few-shot examples to add to the relevance-scoring prompt so RESTORED-type "
        "articles score higher\n"
        "Be specific and concise. Only suggest rules with 2+ supporting examples."
    )
    try:
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=900,
            temperature=0.2,
        )
        print("\n" + "=" * 62)
        print("  [AI 개선 제안]  (검토 후 수동 반영 권장)")
        print("=" * 62)
        print(resp.choices[0].message.content.strip())
    except Exception as e:
        print(f"\n[AI 제안 실패] {e}")


if __name__ == "__main__":
    hidden, restored = load_feedback()
    print_stats(hidden, restored)
    if "--ai" in sys.argv and (hidden or restored):
        ai_suggestions(hidden, restored)
