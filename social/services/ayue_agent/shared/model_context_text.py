"""Remove internal model-context annotations without changing event dates."""
from __future__ import annotations

import re


_MESSAGE_TIME_LABEL = r"(?:訊息時間|讯息时间|消息時間|消息时间)"
_MESSAGE_TIME_START = re.compile(r"[\[［【]\s*" + _MESSAGE_TIME_LABEL + r"\s*[：:]")
_MESSAGE_TIME_ANNOTATION = re.compile(
    r"[\[［【]\s*" + _MESSAGE_TIME_LABEL + r"\s*[：:]\s*"
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?\s*[;；]\s*(?:時區|时区)\s*[：:]\s*"
    r"[A-Za-z0-9_+./:-]{1,64}\s*[\]］】]"
    # Some providers echo the separator as literal JSON escapes. Only remove
    # those escapes immediately after the annotation, never throughout prose.
    r"(?:[ \t]*(?:\\r\\n|\\n|\\r))*"
)


def strip_model_time_annotations(value: str | None) -> str:
    """Strip our tagged timestamps; incomplete tags cannot become a reply.

    Plain event dates, clock times, timezone explanations and other bracketed
    prose are preserved. This is output projection, not a history migration.
    """
    text = _MESSAGE_TIME_ANNOTATION.sub("", str(value or ""))
    if _MESSAGE_TIME_START.search(text):
        return ""
    return text.strip()
