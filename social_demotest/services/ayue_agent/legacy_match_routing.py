"""Small deterministic helpers retained exclusively for public V1 rollback."""

from __future__ import annotations

import re


def _compact(message: str) -> str:
    return re.sub(r"\s+", "", message or "").lower()


def is_match_outcome_followup(message: str) -> bool:
    """Recognise a short reaction to a just-reported match outcome."""
    compact = _compact(message).strip("!！?？。.")
    if compact in {"為什麼", "怎麼回事", "什麼情況", "發生什麼事", "怎麼會這樣"}:
        return True
    return bool(re.fullmatch(r"(?:為什麼|怎麼).{0,12}(?:拒絕|婉拒|沒接受|不接受).{0,6}", compact))


def should_answer_match_outcome_followup(message: str, previous_assistant_message: str = "") -> bool:
    """Only bind an ambiguous reaction to a decline when rollback context proves it."""
    if not is_match_outcome_followup(message):
        return False
    compact = _compact(message)
    if any(word in compact for word in ("拒絕", "婉拒", "沒接受", "不接受")):
        return True
    previous = _compact(previous_assistant_message)
    return any(marker in previous for marker in ("婉拒", "拒絕", "提案已經結束", "提案已經收起", "提案已收起"))


def is_explicit_match_request(message: str) -> bool:
    """Rollback-only check before the legacy path starts a search."""
    compact = _compact(message)
    if is_match_outcome_followup(message):
        return False
    phrases = (
        "幫我找人", "幫我配對", "開始找", "開始配", "找下一位", "幫我介紹",
        "重新找", "重新配", "再找一個", "換一個", "換下一位",
    )
    if any(phrase in compact for phrase in phrases):
        return True
    return bool(re.search(r"(?:幫我|替我|再|重新).{0,4}(?:找|介紹|配).{0,4}(?:人|一個|對象|下一位)", compact))
