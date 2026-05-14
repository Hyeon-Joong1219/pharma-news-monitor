"""
Groq API 기반 제약/바이오 관련성 자동 분류기 (무료).

동작 방식:
  - 비전문 매체(dedicated=false) 기사만 분류 대상 (전문 제약 매체는 항상 관련)
  - 기사 20개씩 묶어 Groq LLM에게 관련성 0~10 점수를 요청
  - 점수 < threshold(기본 3) → hidden=1 처리
  - 분류 결과는 relevance_score 컬럼에 저장

API 키 발급 (무료, 카드 불필요):
  1. https://console.groq.com 회원가입
  2. API Keys 메뉴에서 키 생성
  3. .env 파일에 GROQ_API_KEY=gsk_... 추가
"""

import os
import json
import sqlite3
import logging
import time
import yaml
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH      = os.path.join(
    os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__))),
    "news.db"
)
BATCH_SIZE   = 20
AI_THRESHOLD = 3   # 이 점수 미만 → hidden
MODEL        = "llama-3.1-8b-instant"   # Groq 무료 모델


def _load_api_key() -> str:
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        if os.path.exists(env_path):
            for line in open(env_path, encoding="utf-8"):
                line = line.strip()
                if line.startswith("GROQ_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    return key


def _get_client():
    try:
        from groq import Groq
    except ImportError:
        raise RuntimeError("groq 패키지 미설치: pip install groq")

    api_key = _load_api_key()
    if not api_key or api_key == "여기에_GROQ_API_키_입력":
        raise RuntimeError(
            "GROQ_API_KEY가 설정되지 않았습니다.\n"
            "  1. https://console.groq.com 에서 무료 회원가입\n"
            "  2. API Keys 메뉴에서 키 생성\n"
            "  3. .env 파일에 GROQ_API_KEY=gsk_... 추가"
        )
    return Groq(api_key=api_key)


def _load_dedicated_sources() -> set:
    try:
        with open("feeds.yaml", encoding="utf-8") as f:
            feeds = yaml.safe_load(f).get("feeds", [])
        return {fd["name"] for fd in feeds if fd.get("dedicated")}
    except Exception:
        return set()


def _classify_batch(client, articles: list) -> dict:
    """Groq LLM에게 기사 목록을 보내 관련성 점수(0~10)를 받아옴."""
    lines = []
    for i, a in enumerate(articles):
        title_orig = (a.get("title") or "")[:120]
        title_ko   = (a.get("title_ko") or "")[:80]
        summary    = (a.get("summary") or "")[:200]
        lang       = a.get("lang", "")

        if lang == "en":
            title_str = title_orig
            if title_ko:
                title_str += f" ({title_ko})"
        else:
            title_str = title_ko or title_orig

        lines.append(f'{i+1}. [{a["source"]}] {title_str} | {summary[:150]}')

    prompt = (
        "You are evaluating news articles for a pharmaceutical/biotech industry monitoring system.\n"
        "Rate each article's relevance to pharma/biotech/healthcare on a scale of 0-10.\n\n"
        "Scoring guide:\n"
        "  10 : Clinical trial results, FDA/EMA/MFDS approvals, new drug development, "
        "biosimilar launches, pharma company M&A or licensing deals\n"
        "   7 : Pharma/biotech company investments, IPO, healthcare policy, medical devices\n"
        "   4 : General health/medical information loosely related to pharma\n"
        "   1 : Gaming, real estate, automotive, EV batteries, entertainment, "
        "finance unrelated to pharma, energy sector\n"
        "   0 : Completely unrelated to pharma/biotech\n\n"
        "Key rules:\n"
        "- M&A/IPO of a pharma or biotech company -> high score (7-10)\n"
        "- M&A/IPO of a gaming, energy, or real estate company -> low score (0-2)\n"
        "- Articles in Korean or English are both valid - judge by content, not language\n\n"
        "Articles:\n"
        + "\n".join(lines)
        + "\n\n"
        "Reply with a JSON array ONLY (no explanation): "
        '[{"id":1,"score":8},{"id":2,"score":2},...]'
    )

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=512,
            temperature=0,
        )
        raw = resp.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
        return {articles[item["id"] - 1]["id"]: float(item["score"]) for item in data}
    except Exception as e:
        logger.warning(f"Groq 분류 실패 (배치): {e}")
        return {}


def run_relevance_classification(days: int = 2, force: bool = False) -> int:
    """
    최근 `days`일 기사 중 비전문 매체 기사를 Groq으로 분류.
    force=True 이면 이미 분류된 기사도 재분류.
    반환: hidden 처리된 기사 수
    """
    try:
        client = _get_client()
    except RuntimeError as e:
        logger.error(str(e))
        return 0

    dedicated = _load_dedicated_sources()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

    try:
        conn.execute("ALTER TABLE articles ADD COLUMN ai_classified INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass

    cond = "" if force else "AND (ai_classified IS NULL OR ai_classified = 0)"
    rows = conn.execute(
        f"SELECT id, title, title_ko, summary, source, lang FROM articles "
        f"WHERE fetched_at >= ? {cond}",
        (cutoff,),
    ).fetchall()

    dedicated_ids = [r["id"] for r in rows if r["source"] in dedicated]
    non_ded = [dict(r) for r in rows if r["source"] not in dedicated]

    if dedicated_ids:
        conn.executemany(
            "UPDATE articles SET ai_classified=1, hidden=0 WHERE id=?",
            [(i,) for i in dedicated_ids],
        )
        conn.commit()

    if not non_ded:
        conn.close()
        logger.info("AI 분류 대상 없음 (비전문 매체 신규 기사 없음)")
        return 0

    logger.info(f"AI 관련성 분류 시작: {len(non_ded)}건 (배치 {BATCH_SIZE}개씩)")

    hidden_cnt = 0
    for i in range(0, len(non_ded), BATCH_SIZE):
        batch  = non_ded[i: i + BATCH_SIZE]
        scores = _classify_batch(client, batch)
        updates = []
        for a in batch:
            score  = scores.get(a["id"], 5.0)
            hidden = 1 if score < AI_THRESHOLD else 0
            if hidden:
                hidden_cnt += 1
            updates.append((score * 10, hidden, 1, a["id"]))

        conn.executemany(
            "UPDATE articles SET relevance_score=?, hidden=?, ai_classified=? WHERE id=?",
            updates,
        )
        conn.commit()
        logger.info(f"  배치 {i//BATCH_SIZE + 1} 완료 ({len(batch)}건)")
        time.sleep(2.5)  # Groq 무료 30 RPM 제한 준수 (2초 이상 필요)

    conn.close()
    logger.info(f"AI 분류 완료 - hidden: {hidden_cnt}건 / 전체: {len(non_ded)}건")
    return hidden_cnt


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    force = "--force" in sys.argv
    days  = 2
    for arg in sys.argv[1:]:
        if arg.isdigit():
            days = int(arg)
    run_relevance_classification(days=days, force=force)
