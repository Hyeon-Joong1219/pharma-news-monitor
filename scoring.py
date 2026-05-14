"""
스코어링 엔진
  - 키워드 가중치로 기사별 base_score 계산
  - 48시간 내 다수 매체가 같은 이슈를 다루면 source_boost 적용
  - 최종 score = base_score * (1 + 0.5 * (source_count - 1))
"""
import os
import sqlite3
import yaml
import re
import logging
from collections import defaultdict
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)
DB_PATH = os.path.join(
    os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__))),
    "news.db"
)

# 클러스터링에서 무시할 영문 불용어
_EN_STOPWORDS = {
    "the","a","an","in","on","at","to","for","of","and","or","is","are",
    "was","were","be","been","have","has","had","will","would","could",
    "should","may","might","do","does","did","this","that","with","from",
    "by","as","its","it","he","she","they","we","new","says","said",
    "report","reports","after","over","amid","plan","plans",
}
# 클러스터링에서 무시할 한국어 불용어 (공백 기준 분리 후)
_KO_STOPWORDS = {
    "이","가","을","를","은","는","의","에","에서","로","으로","과","와",
    "도","만","보다","하다","있다","없다","위해","대해","관련","통해","위한",
    "대한","따른","따라","등","및","더","약","약품","회사","기업","뉴스",
}
STOPWORDS = _EN_STOPWORDS | _KO_STOPWORDS


def load_config():
    with open("scoring.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def init_db_columns(conn: sqlite3.Connection):
    for col, typ, default in [
        ("score",           "REAL",    "0"),
        ("source_count",    "INTEGER", "1"),
        ("cluster_id",      "TEXT",    "''"),
        ("relevance_score", "REAL",    "0"),
        ("hidden",          "INTEGER", "0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {col} {typ} DEFAULT {default}")
        except sqlite3.OperationalError:
            pass
    conn.commit()


# ── 키워드 스코어 ────────────────────────────────────────────────

def compute_base_score(text: str, weights: dict) -> float:
    t = text.lower()
    return sum(w for kw, w in weights.items() if kw.lower() in t)


def compute_relevance_score(text: str, source: str,
                             rel_weights: dict,
                             dedicated_sources: set,
                             ded_bonus: float) -> float:
    """
    제약/바이오 관련성 점수.
    양수 신호(제약 용어/기업)와 음수 신호(비제약 업종)를 합산.
    전문 제약 매체는 기본 보너스를 부여해 항상 threshold 이상이 되도록 함.
    """
    t = text.lower()
    score = sum(w for kw, w in rel_weights.items() if kw.lower() in t)
    if source in dedicated_sources:
        score += ded_bonus
    return score


# ── 클러스터링 유틸 ──────────────────────────────────────────────

def significant_words(text: str, min_len: int = 4) -> set:
    """제목에서 의미있는 단어 집합 추출."""
    if not text:
        return set()
    tokens = re.split(r'[\s,.\-_:;!?/|&()\[\]"\'·]+', text.lower())
    return {t for t in tokens if len(t) >= min_len and t not in STOPWORDS}


class UnionFind:
    def __init__(self, ids):
        self.parent = {i: i for i in ids}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, x, y):
        self.parent[self.find(x)] = self.find(y)


def cluster_articles(articles: list, cfg: dict) -> dict:
    """
    같은 이슈를 다루는 기사들을 묶어 {cluster_root: [articles]} 반환.
    단어 역색인(inverted index)으로 O(n·k) 수준으로 처리.
    """
    window_sec  = cfg.get("time_window_hours", 48) * 3600
    min_shared  = cfg.get("min_shared_words", 3)
    min_len     = cfg.get("min_word_length", 4)

    # 기사별 메타 준비
    meta = {}
    for a in articles:
        try:
            t = datetime.fromisoformat(a["fetched_at"])
        except Exception:
            t = datetime.utcnow()
        # 영문 + 한국어 번역 제목 모두 활용
        words = significant_words(a.get("title") or "", min_len)
        words |= significant_words(a.get("title_ko") or "", min_len)
        meta[a["id"]] = {"time": t, "words": words, "source": a["source"]}

    # 단어 역색인
    word_idx: dict[str, set] = defaultdict(set)
    for aid, m in meta.items():
        for w in m["words"]:
            word_idx[w].add(aid)

    uf = UnionFind(meta.keys())
    checked: set = set()

    for aid, m in meta.items():
        # 이 기사와 최소 1개 단어를 공유하는 후보 수집
        candidates: set = set()
        for w in m["words"]:
            candidates |= word_idx[w]
        candidates.discard(aid)

        for cid in candidates:
            pair = (min(aid, cid), max(aid, cid))
            if pair in checked:
                continue
            checked.add(pair)

            cm = meta[cid]
            if m["source"] == cm["source"]:       # 같은 매체는 클러스터 제외
                continue
            if abs((m["time"] - cm["time"]).total_seconds()) > window_sec:
                continue
            if len(m["words"] & cm["words"]) >= min_shared:
                uf.union(aid, cid)

    id_map = {a["id"]: a for a in articles}
    clusters: dict = defaultdict(list)
    for aid in meta:
        clusters[uf.find(aid)].append(id_map[aid])

    return dict(clusters)


# ── 메인 ────────────────────────────────────────────────────────

def load_dedicated_sources() -> set:
    """feeds.yaml에서 dedicated=true 소스명 집합 반환."""
    try:
        import yaml as _yaml
        with open("feeds.yaml", encoding="utf-8") as f:
            feeds = _yaml.safe_load(f).get("feeds", [])
        return {fd["name"] for fd in feeds if fd.get("dedicated")}
    except Exception:
        return set()


def run_scoring(days: int = 30):
    """
    최근 `days`일 기사를 스코어링·클러스터링·관련성 평가.
    fetch.py 실행 후 자동으로 호출됨.
    """
    cfg         = load_config()
    weights     = cfg.get("keyword_weights", {})
    rel_weights = cfg.get("relevance_weights", {})
    threshold   = cfg.get("relevance_threshold", 0)
    ded_bonus   = cfg.get("dedicated_relevance_bonus", 8)
    c_cfg       = cfg.get("cluster_settings", {})
    boost       = c_cfg.get("source_boost_per_source", 0.5)

    dedicated_sources = load_dedicated_sources()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_db_columns(conn)

    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    articles = [
        dict(r) for r in conn.execute(
            "SELECT id, title, title_ko, summary, source, fetched_at FROM articles WHERE fetched_at >= ?",
            (cutoff,),
        ).fetchall()
    ]

    if not articles:
        conn.close()
        return

    logger.info(f"스코어링 대상: {len(articles)}건 (최근 {days}일)")

    # 1단계: 키워드 base_score + 관련성 점수
    for a in articles:
        text = " ".join(filter(None, [a.get("title"), a.get("title_ko"), a.get("summary")]))
        a["base_score"]      = compute_base_score(text, weights)
        a["relevance_score"] = compute_relevance_score(
            text, a["source"], rel_weights, dedicated_sources, ded_bonus
        )

    # 2단계: 클러스터링 → source_boost
    clusters = cluster_articles(articles, c_cfg)

    multi = [(root, grp) for root, grp in clusters.items() if len(grp) > 1]
    logger.info(f"멀티소스 클러스터: {len(multi)}개")
    for root, grp in sorted(multi, key=lambda x: -len(x[1]))[:5]:
        sources = ", ".join({a["source"] for a in grp})
        logger.info(f"  [{len(grp)}개 매체] {grp[0]['title'][:60]}  →  {sources}")

    # 3단계: 최종 점수 계산 & hidden 판정 & DB 업데이트
    updates = []
    hidden_cnt = 0
    for root, grp in clusters.items():
        sources      = {a["source"] for a in grp}
        source_count = len(sources)
        multiplier   = 1 + boost * (source_count - 1)
        for a in grp:
            final   = round(a["base_score"] * multiplier, 2)
            rel     = round(a["relevance_score"], 2)
            hidden  = 1 if rel < threshold else 0
            if hidden:
                hidden_cnt += 1
            updates.append((final, source_count, str(root), rel, hidden, a["id"]))

    conn.executemany(
        "UPDATE articles SET score=?, source_count=?, cluster_id=?, relevance_score=?, hidden=? WHERE id=?",
        updates,
    )
    conn.commit()
    conn.close()
    logger.info(f"스코어링 완료 - hidden 처리: {hidden_cnt}건 (관련성 threshold={threshold})")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    run_scoring(days=days)
