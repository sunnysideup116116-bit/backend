"""Shared deterministic helpers for public Ayue reply validation."""

from __future__ import annotations

import re


def _compact(message: str) -> str:
    return re.sub(r"\s+", "", message or "").lower()


def _v2_yes(message: str) -> bool:
    if _compact(message) in {"對", "是", "沒錯"}:
        return True
    return _compact(message) in {"好", "好的", "可以", "確認", "確定", "要", "yes", "ok"}


def _v2_no(message: str) -> bool:
    return _compact(message) in {"不要", "不用", "取消", "先不要", "no"}


def confirmation_choice(message: str) -> str:
    """Interpret only the small, explicit confirmation protocol."""
    if _v2_yes(message):
        return "confirm"
    if _v2_no(message):
        return "cancel"
    return "none"


_INTERNAL_META_REPLY_RE = re.compile(
    r"(?:沒有|無|缺少|目前沒有).{0,10}(?:工具|函式|功能)|"
    r"(?:工具|函式).{0,10}(?:無法|不能|限制)|"
    r"visible_tools|tool_call|系統限制|內部能力|"
    r"無法安全地|暫時不會執行任何操作|需要再確認一下你的意思",
    re.IGNORECASE,
)


def _concise_public_reply(
    reply: str,
    *,
    preserve_details: bool = False,
    max_chars: int | None = None,
    max_sentences: int | None = None,
) -> str:
    """Bound public prose without imposing a fixed conversational template."""
    text = re.sub(r"[ \t]+", " ", str(reply or "")).strip()
    limit = max_chars if max_chars is not None else 3_600
    sentence_limit = max_sentences
    sentences = [
        part.strip()
        for part in re.split(r"(?<=[。！？!?])", text)
        if part.strip()
    ]
    suffix = "……"
    if len(text) <= limit and (
        sentence_limit is None or len(sentences) <= sentence_limit
    ):
        return text
    selected: list[str] = []
    for sentence in sentences:
        candidate = "".join(selected) + sentence
        if len(candidate) > limit or (
            sentence_limit is not None and len(selected) >= sentence_limit
        ):
            break
        selected.append(sentence)
    if selected:
        selected_text = "".join(selected)
        if selected_text != text:
            return selected_text[: max(0, limit - len(suffix))].rstrip() + suffix
        return selected_text
    shortened = text[:limit]
    for marker in ("。", "！", "？", "；", "，", ","):
        position = shortened.rfind(marker)
        if position >= max(24, limit // 2):
            shortened = shortened[:position]
            break
    return shortened[: max(0, limit - len(suffix))].rstrip("，,；;：: ") + suffix
