"""키워드 매칭 공통 모듈.

짧은 영문 약어(4자 이하 순수 ASCII)는 substring 매칭 시 오탐이 심각해
단어 경계 매칭을 강제한다:
  - "ema"  → "email", "cinema" 에 매칭되던 문제
  - "ind"  → "india", "find", "industry"
  - "nda"  → "agenda", "honda"
  - "orr"  → "tomorrow"
5자 이상 영문 키워드는 복수형("biosimilars" 등) 매칭을 위해
기존 substring 방식을 유지하고, 한국어 키워드도 substring 유지
(조사가 붙는 한국어 특성상 경계 매칭이 오히려 누락을 만듦).

모든 함수는 소문자화된 키워드를 전제로 한다 (기존 호출부와 동일).
"""
import re
from functools import lru_cache

_SHORT_ASCII_MAX = 4


@lru_cache(maxsize=4096)
def _boundary_pattern(kw: str):
    # \b 대신 영숫자 lookaround 사용: "phase 3", "q3" 같은 숫자 인접도 경계로 처리
    return re.compile(r"(?<![0-9a-z])" + re.escape(kw) + r"(?![0-9a-z])")


def _needs_boundary(kw: str) -> bool:
    return len(kw) <= _SHORT_ASCII_MAX and kw.isascii()


def contains_keyword(text_lower: str, kw_lower: str) -> bool:
    """text_lower(소문자화된 본문)에 kw_lower가 등장하는지 판정."""
    if _needs_boundary(kw_lower):
        return _boundary_pattern(kw_lower).search(text_lower) is not None
    return kw_lower in text_lower


@lru_cache(maxsize=4096)
def _name_pattern(name: str):
    if name.isascii():
        # 영문 회사명은 길이와 무관하게 단어 경계 필수 ('EnCell' ⊂ 'ViGenCell' 오탐)
        return re.compile(r"(?<![0-9a-z])" + re.escape(name) + r"(?![0-9a-z])")
    # 한글 회사명은 앞쪽만 경계 검사 ('프로젠' ⊂ '에이프로젠' 오탐) — 뒤는 조사가 붙으므로 허용
    return re.compile(r"(?<![0-9a-z가-힣])" + re.escape(name))


def contains_name(text_lower: str, name_lower: str) -> bool:
    """회사명 매칭 — contains_keyword보다 엄격 (다른 회사명 안에 포함된 경우 제외)."""
    return _name_pattern(name_lower).search(text_lower) is not None


def match_keywords(text: str, keywords: list) -> list:
    """본문에 매칭된 키워드 목록 반환 (키워드는 이미 소문자)."""
    t = text.lower()
    return [kw for kw in keywords if contains_keyword(t, kw)]


def weighted_score(text: str, weights: dict) -> float:
    """{키워드: 가중치} 딕셔너리로 본문 점수 합산 (키워드 대소문자 무관)."""
    t = text.lower()
    return sum(w for kw, w in weights.items() if contains_keyword(t, kw.lower()))
