"""Deterministic, bounded identity rules for owner-grounded Concepts.

The alias table is intentionally small. Unknown concepts are still stable,
but are never semantically equated by heuristics or an LLM-provided key.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import re
import unicodedata


DEFAULT_DURABLE_MEMORY_LIMIT = 6
HARD_DURABLE_MEMORY_LIMIT = 8
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,50}$")
_PREFIX_RE = re.compile(
    r"^(?:我)?(?:很|非常|也)?(?:喜歡|偏好|愛聽|愛|不喜歡|討厭|避免|需要|興趣)"
    r"\s*[:：,，、]?\s*",
    re.IGNORECASE,
)
_EXPLICIT_LIST_RE = re.compile(
    r"^(?:我在註冊表單填寫的平常興趣是|平常興趣是|"
    r"(?:我|本人)?\s*(?:很|非常|也)?\s*"
    r"(?:喜歡|偏好|愛聽|愛|常聽|不喜歡|討厭|避免|需要))"
    r"\s*[:：]?\s*(?P<body>.+?)[。.!！]?\s*$",
    re.IGNORECASE,
)
_UNSAFE_ATOM_RE = re.compile(
    r"(?:適合|一起|想要|想|可以|會|不會|喜歡|偏好|愛聽|常聽|討厭|避免|需要|"
    r"但是|因為|所以|如果|而且|又|跟|和|的)"
)
_ALIASES = {
    "kpop": ("k_pop", "K-pop"),
    "jpop": ("j_pop", "J-pop"),
}
_ALIAS_EVIDENCE = {
    "k_pop": re.compile(r"(?<![A-Za-z0-9])K[\s\-_.‐‑‒–—]*pop(?![A-Za-z0-9])", re.I),
    "j_pop": re.compile(r"(?<![A-Za-z0-9])J[\s\-_.‐‑‒–—]*pop(?![A-Za-z0-9])", re.I),
}
_NEGATIVE_PREFERENCE_CUE_RE = re.compile(
    r"(?:不\s*(?:太|很|怎麼)?\s*(?:喜歡|愛)|討厭|避免)", re.I,
)
_POSITIVE_PREFERENCE_CUE_RE = re.compile(
    r"(?:喜歡|偏好|愛聽|常聽|很愛)", re.I,
)


@dataclass(frozen=True)
class CanonicalConcept:
    key: str
    label: str
    alias: str = ""


def durable_memory_limit() -> int:
    try:
        value = int(os.getenv(
            "DURABLE_MEMORY_MAX_CANDIDATES_PER_MESSAGE",
            str(DEFAULT_DURABLE_MEMORY_LIMIT),
        ))
    except (TypeError, ValueError):
        value = DEFAULT_DURABLE_MEMORY_LIMIT
    return max(1, min(value, HARD_DURABLE_MEMORY_LIMIT))


def _clean_label(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip(" \t\r\n\"'：:,，。")
    while text and _PREFIX_RE.match(text):
        text = _PREFIX_RE.sub("", text, count=1).strip()
    return text[:40]


def _alias_token(label: str) -> str:
    return re.sub(r"[\s\-_.‐‑‒–—]+", "", label.casefold())


def canonicalize_concept(label: object, suggested_key: object = "") -> CanonicalConcept | None:
    """Return a server-owned Concept identity; never trust the proposed key."""
    del suggested_key
    clean = _clean_label(label)
    if not clean:
        return None
    alias = _ALIASES.get(_alias_token(clean))
    if alias:
        return CanonicalConcept(key=alias[0], label=alias[1], alias=_alias_token(clean))
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._+&/\-]{0,39}", clean):
        slug = re.sub(r"[^a-z0-9]+", "_", clean.casefold()).strip("_")[:50]
        if len(slug) >= 2 and _KEY_RE.fullmatch(slug):
            return CanonicalConcept(key=slug, label=clean)
    normalized = unicodedata.normalize("NFKC", clean).casefold()
    normalized = re.sub(r"[\s\-_.‐‑‒–—]+", "", normalized)
    key = "concept_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return CanonicalConcept(key=key, label=clean)


def canonical_evidence_span(text: object, concept: CanonicalConcept) -> str:
    """Return an exact owner substring supporting one canonical concept."""
    source = unicodedata.normalize("NFKC", str(text or ""))
    pattern = _ALIAS_EVIDENCE.get(concept.key)
    if pattern:
        matched = pattern.search(source)
        return matched.group(0) if matched else ""
    return concept.label if concept.label and concept.label in source else ""


def has_mixed_preference_polarity(value: object) -> bool:
    """Reject one candidate that contains both explicit like and avoid cues."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    if not _NEGATIVE_PREFERENCE_CUE_RE.search(text):
        return False
    without_negative = _NEGATIVE_PREFERENCE_CUE_RE.sub("", text)
    return bool(_POSITIVE_PREFERENCE_CUE_RE.search(without_negative))


def split_explicit_preference_enumeration(
    value: object, *, limit: int | None = None,
) -> list[str]:
    """Split only a closed, explicit list of short noun-like preferences."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    matched = _EXPLICIT_LIST_RE.fullmatch(text)
    if not matched:
        return []
    return split_compound_concept_label(matched.group("body"), limit=limit)


def split_compound_concept_label(
    value: object, *, limit: int | None = None,
) -> list[str]:
    """Propose a split for an old bare label only when every item is simple."""
    body = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not re.search(r"[、,，]", body):
        return []
    items = [
        re.sub(r"\s+", " ", item).strip(" \t\r\n\"'。.!?！？")
        for item in re.split(r"[、,，]", body)
    ]
    bounded_limit = durable_memory_limit() if limit is None else max(
        1, min(int(limit), HARD_DURABLE_MEMORY_LIMIT),
    )
    if not 2 <= len(items) <= bounded_limit:
        return []
    for item in items:
        if (
            not item or len(item) > 24
            or _UNSAFE_ATOM_RE.search(item)
            or re.search(r"[。.!?！？；;:]", item)
            or not re.search(r"[A-Za-z0-9\u4e00-\u9fff]", item)
        ):
            return []
    return items
