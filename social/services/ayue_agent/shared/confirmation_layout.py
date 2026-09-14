"""Pure confirmation-card layout validation shared by public runtimes."""
from __future__ import annotations

from dataclasses import dataclass
import re

from services.ayue_agent.shared.public_reply import validate_public_reply
from .model_context_text import strip_model_time_annotations


CONFIRMATION_MARKER = "[[confirmation]]"
CONFIRMATION_SLOT = "primary_write_confirmation"
_COMPLETION_CLAIM = re.compile(
    r"(?:已經|已经|已).{0,12}(?:新增|寫入|写入|送出|寄出|取消|刪除|删除|修改|完成|建立)"
)
_INTERNAL_CONTEXT_LABEL = re.compile(r"(?:訊息時間|时区|時區)\s*[：:]")


@dataclass(frozen=True)
class ConfirmationLayout:
    messages: list[str]
    interaction_blocks_v1: list[dict[str, object]]
    used_fallback: bool


def _safe_message(value: str) -> str | None:
    if _COMPLETION_CLAIM.search(value) or _INTERNAL_CONTEXT_LABEL.search(value):
        return None
    validation = validate_public_reply(
        value,
        preserve_details=True,
        reject_internal_identifiers=True,
        reject_structured_output=True,
        max_chars=1400,
    )
    return validation.reply or None


def confirmation_layout(model_text: str, *, fallback_preview: str) -> ConfirmationLayout:
    """Convert one harmless marker to UI blocks; malformed prose fails closed."""
    raw = strip_model_time_annotations(model_text)
    fallback = _safe_message(strip_model_time_annotations(fallback_preview)) or "請確認是否套用這次行事曆變更。"
    repeats_preview = bool(fallback and fallback in raw)
    if raw.count(CONFIRMATION_MARKER) == 1 and not repeats_preview:
        before, after = (part.strip() for part in raw.split(CONFIRMATION_MARKER, 1))
        safe_before = _safe_message(before) if before else None
        safe_after = _safe_message(after) if after else None
        if safe_before:
            messages = [safe_before] + ([safe_after] if safe_after else [])
            blocks: list[dict[str, object]] = [
                {"type": "text", "message_index": 0},
                {"type": "confirmation", "slot": CONFIRMATION_SLOT},
            ]
            if safe_after:
                blocks.append({"type": "text", "message_index": 1})
            return ConfirmationLayout(messages, blocks, False)
    return ConfirmationLayout(
        [fallback],
        [
            {"type": "text", "message_index": 0},
            {"type": "confirmation", "slot": CONFIRMATION_SLOT},
        ],
        True,
    )
