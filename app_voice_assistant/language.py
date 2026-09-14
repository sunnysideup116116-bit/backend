"""Input hints and presentation normalization, not language authorization."""
import re

from social.services.language_service import normalize_public_reply


_CJK_OR_PUNCTUATION = (
    r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    r"，。！？；：、（）《》〈〉「」『』【】,.!?;:"
)
_SPACED_CJK = re.compile(
    rf"(?<=[{_CJK_OR_PUNCTUATION}])[ \t]+(?=[{_CJK_OR_PUNCTUATION}])"
)


def input_language_codes(preference: str) -> list[str]:
    return {
        "zh-TW": ["zh-TW"],
        "en-US": ["en-US"],
    }.get(preference, ["zh-TW", "en-US"])


def display_transcript(text: str, *, traditional: bool = True) -> str:
    # Normalize only the accumulated display text, never individual deltas:
    # this preserves Latin word spacing at the streaming boundary.
    normalized = (
        normalize_public_reply(text, max_length=2000)
        if traditional
        else text[:2000]
    )
    # Gemini Live sometimes emits a single space between every Mandarin token.
    # Remove spacing only when both sides are Han characters or punctuation;
    # Latin words and mixed phrases such as "嗨 Candy" retain their spaces.
    return _SPACED_CJK.sub("", normalized)
