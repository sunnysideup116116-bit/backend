"""Small, deterministic presentation rules for Pi-authored chat prose."""

from __future__ import annotations

import re


# Covers ordinary color emoji, flags, dingbats, and their invisible joiners.
# This is deliberately applied only at Pi's final visible-reply boundaries.
_DISPLAY_EMOJI = re.compile(
    r"[\U0001F1E6-\U0001F1FF\U0001F300-\U0001FAFF\u2600-\u27BF]"
)
_EMOJI_REMAINDERS = re.compile(r"[\u200d\ufe0e\ufe0f\u20e3]")


def strip_display_emoji(value: str | None) -> str:
    """Keep Pi replies text-first without altering URLs, IDs, or tool data."""
    text = _DISPLAY_EMOJI.sub("", str(value or ""))
    text = _EMOJI_REMAINDERS.sub("", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()
