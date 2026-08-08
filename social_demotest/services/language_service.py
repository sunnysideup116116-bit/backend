"""Small, deterministic language normalization at model-output boundaries."""

from __future__ import annotations

import re
import unicodedata
import json

try:  # OpenCC is optional during a rolling deploy; the fallback covers UI-critical terms.
    from opencc import OpenCC

    _OPENCC = OpenCC("s2twp")
except Exception:  # pragma: no cover - exercised on hosts without the optional wheel
    _OPENCC = None


_FALLBACK_S2T = str.maketrans({
    "户": "戶", "台": "臺", "与": "與", "为": "為", "个": "個", "这": "這",
    "么": "麼", "时": "時", "间": "間", "里": "裡", "后": "後", "会": "會",
    "对": "對", "来": "來", "现": "現", "动": "動", "约": "約", "历": "曆",
    "复": "覆", "应": "應", "话": "話", "记": "記", "认": "認", "请": "請",
    "设": "設", "计": "計", "调": "調", "查": "查", "帮": "幫", "开": "開",
    "关": "關", "发": "發", "现": "現", "处": "處", "资": "資", "讯": "訊",
    "实": "實", "际": "際", "还": "還", "没": "沒", "点": "點", "种": "種",
    "从": "從", "经": "經", "过": "過", "谈": "談", "题": "題", "问": "問",
    "想": "想", "国": "國", "门": "門", "书": "書", "乐": "樂", "观": "觀",
    "爱": "愛", "觉": "覺", "习": "習", "习": "習", "绪": "緒", "长": "長",
})


def normalize_zh_tw(value: str | None, *, max_length: int | None = None) -> str:
    """Normalize model-produced display text; never use this for IDs or keys."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = _OPENCC.convert(text) if _OPENCC else text.translate(_FALLBACK_S2T)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_length].rstrip() if max_length else text


def normalize_model_text(value):
    """Normalize human-facing values from a typed model payload.

    JSON keys and non-text values are deliberately retained exactly as supplied.
    This helper belongs only at model-output boundaries; identifiers and evidence
    spans must never be passed to it.
    """
    if isinstance(value, str):
        return normalize_zh_tw(value)
    if isinstance(value, list):
        return [normalize_model_text(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_model_text(item) for key, item in value.items()}
    return value


_PUBLIC_REPLY_PROTECTED = re.compile(
    r"```[\s\S]*?```|`[^`\n]+`|https?://[^\s<>]+|\{[^{}\n]*\}|\[[^\[\]\n]*\]",
)


def normalize_public_reply(value: str | None, *, max_length: int | None = None) -> str:
    """Normalize human-facing model text without rewriting opaque payloads.

    Public V3 replies are prose, but they may contain a URL or a code/JSON
    fragment that must remain byte-for-byte stable.  OpenCC owns the language
    conversion; this boundary only protects those opaque fragments and never
    touches tool arguments, IDs, revisions, or stored JSON.
    """
    text = str(value or "")
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            json.loads(stripped)
        except (TypeError, ValueError):
            pass
        else:
            return text[:max_length].rstrip() if max_length else text

    protected: list[str] = []

    def hold(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return f"__AYUE_REPLY_PROTECTED_{len(protected) - 1}__"

    matches = list(_PUBLIC_REPLY_PROTECTED.finditer(text))
    if not matches:
        return normalize_zh_tw(text, max_length=max_length)
    output: list[str] = []
    cursor = 0
    for match in matches:
        output.append(normalize_zh_tw(text[cursor:match.start()]))
        output.append(hold(match))
        cursor = match.end()
    output.append(normalize_zh_tw(text[cursor:]))
    normalized = "".join(output)
    for index, original in enumerate(protected):
        normalized = normalized.replace(f"__AYUE_REPLY_PROTECTED_{index}__", original)
    return normalized[:max_length].rstrip() if max_length else normalized
