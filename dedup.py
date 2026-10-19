"""같은 사건을 다룬 다른 매체 기사 판별 (제목 기준).

scoring.py의 클러스터링은 제목 공유 단어 4개 이상을 요구해(거대 클러스터 방지) 짧은 제목의
같은 사건 기사를 자주 놓친다 — 예: '식약처, …희귀의약품 '퀸락정' 허가' / '오노약품, …'퀸락' 국내 허가',
비티젠 135억 위탁생산 계약 기사 4건이 모두 따로 남았음 (2026-10-19 브리핑).
브리핑 중복 제거와 AI 분류 대표 기사 선정에 쓴다.

같은 사건 판정 (하나라도 해당)
  1. 따옴표 고유명(제품·행사명)이 같거나 접두어 관계('퀸락'↔'퀸락정') + 다른 단어 1개 이상 공유
  2. 금액·비율 단어('135억', '30%')를 공유 + 다른 단어 1개 이상 공유
  3. 주어(첫 단어)가 같고 단어 2개 이상, 짧은 제목 대비 40% 이상 공유
  4. 주어가 달라도 단어 3개 이상, 60% 이상 공유
실데이터 검토로 걸러낸 오탐: ''이것''·''의외의 음식'' 같은 따옴표 상투어, 같은 제품의 다른 소식('나보타'),
같은 지원사업의 다른 회사('스케일업 팁스'), 다른 기관의 같은 종류 행사(산학협력 MOU·유방암 캠페인).
"""
import re

from scoring import STOPWORDS

_QUOTED_RE  = re.compile(r"[‘'\"“「『<《]([^‘’'\"“”「」『』<>《》]{2,20})[’'\"”」』>》]")
_TAG_RE     = re.compile(r"^\s*\[[^\]]{1,15}\]\s*")          # [HIT알공], [단독] 같은 머리말
_SUFFIX_RE  = re.compile(r"\s+-\s+[^-]{1,30}$")              # Google News ' - 매체명'
_SPLIT_RE   = re.compile(r"[\s,.\-_:;!?/|&()\[\]\"'‘’“”·…]+")
_HANGUL_RE  = re.compile(r"[가-힣]")
_AMOUNT_RE  = re.compile(r"\d[\d,.]*(억|조|만원|천만|달러|원|%)")   # 금액·비율만 (25년·1회 같은 수치는 흔해서 제외)
_GENERIC_QUOTED = {"이것", "이거", "이것만", "의외의 음식", "진짜 비결", "꿀팁"}


def signature(title: str) -> tuple:
    """(단어 집합, 따옴표 고유명 집합, 첫 단어)."""
    t = _SUFFIX_RE.sub("", _TAG_RE.sub("", title or "")).lower()
    quoted = {q.strip() for q in _QUOTED_RE.findall(t)
              if len(q.strip()) >= 2 and q.strip() not in _GENERIC_QUOTED}
    words, first = set(), ""
    for w in _SPLIT_RE.split(t):
        if not w or w in STOPWORDS:
            continue
        # 한글 2자 이상, 영문·숫자 3자 이상 ('adc', 'ckd', '135억' 포함)
        if (_HANGUL_RE.search(w) and len(w) >= 2) or len(w) >= 3:
            words.add(w)
            first = first or w
    return words, quoted, first


def is_same_event(a: tuple, b: tuple) -> bool:
    (wa, qa, fa), (wb, qb, fb) = a, b
    shared  = wa & wb
    shorter = min(len(wa), len(wb)) or 1
    # 따옴표 고유명 자체도 단어 집합에 들어가므로 '그 외' 공유 단어를 따로 센다
    quoted_words = {w for q in qa | qb for w in _SPLIT_RE.split(q)}
    others = shared - quoted_words
    if others and any(x.startswith(y) or y.startswith(x) for x in qa for y in qb):
        return True
    numbers = {w for w in shared if _AMOUNT_RE.search(w)}
    if numbers and len(shared - numbers) >= 1:
        return True
    if fa and fa == fb and len(shared) >= 2 and len(shared) / shorter >= 0.4:
        return True
    return len(shared) >= 3 and len(shared) / shorter >= 0.6
