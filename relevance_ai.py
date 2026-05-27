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
import logging
import time
import yaml
from db import get_db

logger = logging.getLogger(__name__)

BATCH_SIZE   = 20
AI_THRESHOLD = 4   # 이 점수 미만 → hidden (3→4로 상향: 비제약 기사 차단 강화)
# llama-3.1-8b-instant: ~500 tokens/batch → 무료 100k TPD 내에서 200배치(4,000건) 처리 가능
# llama-3.3-70b-versatile: ~33,000 tokens/batch → 무료 TPD 3배치만에 소진됨 (사용 금지)
MODEL        = "llama-3.1-8b-instant"


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
        "You are a strict filter for a Korean pharmaceutical/biotech industry news monitoring system.\n"
        "Rate each article's relevance to pharma/biotech/life sciences on a scale of 0-10.\n\n"
        "Scoring guide:\n"
        "  9-10: FDA/EMA/식약처 approval, clinical trial results (phase 1-3), new drug launch,\n"
        "         biosimilar/ADC/CAR-T/gene therapy news, pharma company M&A or licensing deal\n"
        "   7-8: Pharma/biotech company earnings, IPO, pipeline update, healthcare policy\n"
        "         directly affecting drugs, medical device approval\n"
        "   5-6: General biotech investment, hospital/insurance news with pharma angle,\n"
        "         scientific research with clear drug development implication\n"
        "   3-4: Loosely health-related but minimal pharma relevance\n"
        "   0-2: NON-PHARMA — must score 0-2 if the article is primarily about:\n"
        "         supermarkets (홈플러스/이마트/코스트코), shipbuilding (STX/현대중공업/조선소),\n"
        "         semiconductors (SK하이닉스/삼성전자), automobiles (현대차/기아/GM),\n"
        "         steel (포스코/현대제철), telecom (SKT/KT/LG유플러스),\n"
        "         gaming (크래프톤/넥슨/NC소프트), crypto/finance/real estate,\n"
        "         entertainment, sports, politics, or general economy unrelated to pharma\n\n"
        "CRITICAL RULES:\n"
        "- Judge by the MAIN SUBJECT of the article, not just keywords\n"
        "- M&A of a PHARMA/BIOTECH company → 8-10  |  M&A of a retailer/shipper → 0-1\n"
        "- A conglomerate article (e.g. SK, 롯데, 삼성) with no pharma division angle → 0-2\n"
        "- Korean and English articles equally valid — judge by content only\n"
        "- When in doubt about a Korean company, ask: is their PRIMARY business pharma/biotech?\n\n"
        "Examples:\n"
        "  '홈플러스 인수합병 추진' → 0 (supermarket, not pharma)\n"
        "  'STX 조선 합병' → 0 (shipbuilding)\n"
        "  'SK하이닉스 반도체 투자' → 0 (semiconductor)\n"
        "  '삼성바이오로직스 임상 결과' → 10 (pharma)\n"
        "  '한미약품 기술이전 계약' → 10 (pharma licensing)\n"
        "  'FDA approves Pfizer cancer drug' → 10\n"
        "  '연합뉴스 경기 침체 우려' → 0 (general economy)\n\n"
        "Articles:\n"
        + "\n".join(lines)
        + "\n\n"
        "Reply with a JSON array ONLY (no explanation, no markdown): "
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

    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(days=days)

    conn = get_db()
    cond = "" if force else "AND (ai_classified IS NULL OR ai_classified = 0)"
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT id, title, title_ko, summary, source, lang FROM articles "
            f"WHERE fetched_at >= %s {cond}",
            (cutoff,),
        )
        rows = cur.fetchall()

    dedicated_ids = [r["id"] for r in rows if r["source"] in dedicated]
    non_ded = [dict(r) for r in rows if r["source"] not in dedicated]

    if dedicated_ids:
        with conn.cursor() as cur:
            for did in dedicated_ids:
                cur.execute(
                    "UPDATE articles SET ai_classified=1, hidden=0 WHERE id=%s", (did,)
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
        with conn.cursor() as cur:
            for a in batch:
                score  = scores.get(a["id"], 5.0)
                hidden = 1 if score < AI_THRESHOLD else 0
                if hidden:
                    hidden_cnt += 1
                cur.execute(
                    "UPDATE articles SET relevance_score=%s, hidden=%s, ai_classified=%s WHERE id=%s",
                    (score * 10, hidden, 1, a["id"]),
                )
        conn.commit()
        logger.info(f"  배치 {i//BATCH_SIZE + 1} 완료 ({len(batch)}건)")
        time.sleep(2.5)

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
