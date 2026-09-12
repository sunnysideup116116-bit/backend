"""Input hints and presentation normalization, not language authorization."""
from social.services.language_service import normalize_public_reply


def input_language_codes(preference: str) -> list[str]:
    return {
        "zh-TW": ["zh-TW"],
        "en-US": ["en-US"],
    }.get(preference, ["zh-TW", "en-US"])


def display_transcript(text: str, *, traditional: bool = True) -> str:
    # Normalize only the accumulated display text, never individual deltas:
    # this preserves Latin word spacing at the streaming boundary.
    return normalize_public_reply(text, max_length=2000) if traditional else text[:2000]
