"""Deterministic, bounded identity rules for owner-grounded Concepts.

The alias table is intentionally small. Unknown concepts are still stable,
but are never semantically equated by heuristics or an LLM-provided key.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
from importlib import metadata
import os
import re
import unicodedata


DEFAULT_DURABLE_MEMORY_LIMIT = 6
HARD_DURABLE_MEMORY_LIMIT = 8
MAX_PREFERENCE_TEXT_CHARS = 500
MAX_PREFERENCE_DISPLAY_CHARS = 40
MAX_PREFERENCE_KEY_CHARS = 51
MAX_PROFILE_SOURCE_CHARS = 800
MAX_PREFERENCE_EVIDENCE_CHARS = 160
MAX_REGISTRATION_INTEREST_CHARS = 120
MAX_OWNER_MEMORY_QUERY_CHARS = 120
CANONICALIZATION_VERSION = "v2"
FRESH_PREFERENCE_CONVERTER_DISTRIBUTION = "OpenCC"
FRESH_PREFERENCE_CONVERTER_VERSION = "1.4.1"
FRESH_PREFERENCE_CONVERTER_CONFIG = "s2twp"
FRESH_PREFERENCE_NORMALIZATION_POLICY = "opencc-1.4.1-s2twp-nfkc-whitespace-v1"
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


def is_v2_preference_key(value: object) -> bool:
    """Recognize the reserved digest format, not legacy slugs starting v2_."""
    return isinstance(value, str) and re.fullmatch(r"v2_[0-9a-f]{48}", value) is not None


@dataclass(frozen=True)
class CanonicalConcept:
    key: str
    # Compatibility spelling: label always means complete semantic text here.
    # UI shortening is available only through the explicit display property.
    label: str
    alias: str = ""
    canonicalization_version: str = CANONICALIZATION_VERSION

    @property
    def semantic_text(self) -> str:
        return self.label

    @property
    def display_label(self) -> str:
        return display_preference_label(self.label)

    @property
    def semantic_input_hash(self) -> str:
        return hashlib.sha256(self.semantic_text.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, str]:
        return {
            "key": self.key, "canonical_key": self.key,
            "label": self.semantic_text, "semantic_text": self.semantic_text,
            "display_label": self.display_label,
            "canonicalization_version": self.canonicalization_version,
            "semantic_input_hash": self.semantic_input_hash,
            "fidelity_status": "complete" if self.canonicalization_version == "v2" else "legacy_unknown",
        }


class PreferenceTextError(ValueError):
    """Typed, non-sensitive permanent validation failure; never contains input."""

    def __init__(self, code: str = "preference_text_invalid"):
        self.code = code
        super().__init__(code)


def normalize_preference_text(value: object, *, max_length: int = MAX_PREFERENCE_TEXT_CHARS) -> str:
    """Validate the entire input before and after deterministic normalization.

    The 500-character concept ceiling retains the pre-existing embedding wrapper
    resource envelope. Callers with a distinct source/evidence budget can supply
    that explicit bound. No prefix or normalization expansion is silently lost.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PreferenceTextError("preference_text_type_invalid")
    if len(value) > max_length:
        raise PreferenceTextError("preference_text_too_long")
    text = unicodedata.normalize("NFKC", value)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_length:
        raise PreferenceTextError("preference_text_too_long")
    try:
        text.encode("utf-8", errors="strict")
    except UnicodeError:
        raise PreferenceTextError("preference_text_unicode_invalid") from None
    if any(unicodedata.category(char) == "Cc" for char in text):
        raise PreferenceTextError("preference_text_control_invalid")
    return text


@lru_cache(maxsize=1)
def _fresh_preference_converter():
    """One pinned fresh-input converter. Never use an optional UI fallback.

    Loaded lazily: existing v2/legacy reads and digest verification do not
    depend on a converter and must never convert persisted semantic source.
    """
    try:
        version = metadata.version(FRESH_PREFERENCE_CONVERTER_DISTRIBUTION)
    except metadata.PackageNotFoundError:
        raise PreferenceTextError("preference_normalizer_unavailable") from None
    if version != FRESH_PREFERENCE_CONVERTER_VERSION:
        raise PreferenceTextError("preference_normalizer_version_mismatch")
    try:
        from opencc import OpenCC
        return OpenCC(FRESH_PREFERENCE_CONVERTER_CONFIG)
    except Exception:
        raise PreferenceTextError("preference_normalizer_unavailable") from None


def normalize_fresh_preference_text(
    value: object, *, max_length: int = MAX_PREFERENCE_TEXT_CHARS,
) -> str:
    """Fresh owner input only; not a stored-record reader or identity rewrite.

    Length means Unicode codepoints, not UTF-16 code units or grapheme clusters.
    Check the entire raw and normalized/converted input. A conversion expansion
    over the bound rejects the operation instead of salvaging a prefix.
    """
    source = normalize_preference_text(value, max_length=max_length)
    converter = _fresh_preference_converter()
    try:
        converted = converter.convert(source)
    except Exception:
        raise PreferenceTextError("preference_normalization_failed") from None
    return normalize_preference_text(converted, max_length=max_length)


def canonicalize_fresh_concept(label: object, suggested_key: object = "") -> CanonicalConcept | None:
    """Convert fresh input once before the frozen deterministic identity stage."""
    return canonicalize_concept(normalize_fresh_preference_text(label), suggested_key)


def check_fresh_preference_normalizer() -> None:
    """Startup preflight without dotenv, provider clients, Graph or writes."""
    _fresh_preference_converter()


def display_preference_label(value: object, limit: int = MAX_PREFERENCE_DISPLAY_CHARS) -> str:
    """Presentation only. This result must never be fed back into identity."""
    text = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip()
    bound = max(1, min(int(limit), MAX_PREFERENCE_TEXT_CHARS))
    return text if len(text) <= bound else text[:max(0, bound - 1)] + "…"


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
    text = normalize_preference_text(value).strip(" \t\r\n\"'：:,，。")
    while text and _PREFIX_RE.match(text):
        text = _PREFIX_RE.sub("", text, count=1).strip()
    return text


def _alias_token(label: str) -> str:
    return re.sub(r"[\s\-_.‐‑‒–—]+", "", label.casefold())


def canonicalize_concept(label: object, suggested_key: object = "") -> CanonicalConcept | None:
    """v2 identity from the COMPLETE semantic normal form, never display text.

    The 192-bit digest representation fits the existing 2–51 key contract. Only
    the digest representation is shortened, never its input. Registry aliases
    are deterministic; no model key or semantic similarity creates identity.
    """
    del suggested_key
    source = normalize_preference_text(label)
    if has_mixed_preference_polarity(source):
        raise PreferenceTextError("mixed_preference_polarity")
    clean = _clean_label(source)
    if not clean:
        return None
    alias_token = _alias_token(clean)
    alias = _ALIASES.get(alias_token)
    semantic = alias[1] if alias else clean
    # Preserve role/constraint punctuation. Only established case/spacing and
    # hyphen/underscore separator normalization is generic, not synonym inference.
    normal_form = re.sub(r"[\s_\-‐‑‒–—]+", " ", semantic.casefold()).strip()
    key = "v2_" + hashlib.sha256(("preference:v2\0" + normal_form).encode("utf-8")).hexdigest()[:48]
    return CanonicalConcept(key=key, label=semantic, alias=alias_token if alias else "")


def canonicalize_concept_v1(label: object, suggested_key: object = "") -> CanonicalConcept | None:
    """Frozen legacy READ-key derivation/oracle, never a new-write contract.

    A derived legacy key is not evidence of equality. Only an independently
    verified owner assertion may bridge it to v2. This deliberately reproduces
    the historical prefix loss so legacy IDs need not be rewritten.
    """
    del suggested_key
    clean = unicodedata.normalize("NFKC", str(label or ""))
    clean = re.sub(r"\s+", " ", clean).strip(" \t\r\n\"'：:,，。")
    while clean and _PREFIX_RE.match(clean):
        clean = _PREFIX_RE.sub("", clean, count=1).strip()
    clean = clean[:40]
    if not clean:
        return None
    alias = _ALIASES.get(_alias_token(clean))
    if alias:
        return CanonicalConcept(key=alias[0], label=alias[1], alias=_alias_token(clean), canonicalization_version="v1")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._+&/\-]{0,39}", clean):
        slug = re.sub(r"[^a-z0-9]+", "_", clean.casefold()).strip("_")[:50]
        if len(slug) >= 2 and _KEY_RE.fullmatch(slug):
            return CanonicalConcept(key=slug, label=clean, canonicalization_version="v1")
    normalized = unicodedata.normalize("NFKC", clean).casefold()
    normalized = re.sub(r"[\s\-_.‐‑‒–—]+", "", normalized)
    key = "concept_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return CanonicalConcept(key=key, label=clean, canonicalization_version="v1")


def stored_concept_identity(record: object) -> CanonicalConcept | None:
    """Read verified v2 source without trusting display labels or legacy keys."""
    if not isinstance(record, dict) or record.get("canonicalization_version") != "v2":
        return None
    try:
        identity = canonicalize_concept(record.get("semantic_text"))
    except PreferenceTextError:
        return None
    if not identity:
        return None
    keys = [record[name] for name in ("key", "canonical_key", "concept_key") if record.get(name)]
    if (not keys or any(key != identity.key for key in keys)
            or record.get("semantic_text") != identity.semantic_text
            or record.get("semantic_input_hash") != identity.semantic_input_hash):
        return None
    if record.get("fidelity_status", "complete") != "complete":
        return None
    return identity


def verified_legacy_identity(record: object) -> CanonicalConcept | None:
    """Derive comparison-only v2 identity from an explicit OWNER assertion proof.

    These fields must come from the matched owner's relation, not a shared
    Concept label. This reader never writes proof, changes a key or moves edges.
    Unversioned data alone is legacy_unknown even when its label is short.
    """
    if (not isinstance(record, dict)
            or record.get("canonicalization_version") not in (None, "", "v1", "legacy_unknown")):
        return None
    if (record.get("legacy_evidence_scope") != "owner_assertion"
            or record.get("legacy_fidelity_status") != "complete"):
        return None
    source = record.get("legacy_semantic_text")
    try:
        identity = canonicalize_concept(source)
        legacy = canonicalize_concept_v1(source)
    except PreferenceTextError:
        return None
    if not identity or not legacy:
        return None
    if (record.get("key") or record.get("concept_key")) != legacy.key:
        return None
    if record.get("legacy_semantic_input_hash") != identity.semantic_input_hash:
        return None
    return identity


def canonical_query_provenance(
    label: object, concept: CanonicalConcept | None = None,
) -> str:
    """Classify how a query reached one deterministic Concept identity.

    This is retrieval metadata only.  It never changes the canonical key and
    must not be used to create aliases or weaken exact-match qualification.
    """
    identity = concept or canonicalize_concept(label)
    clean = _clean_label(label)
    if not identity or not clean:
        return ""
    if identity.alias and clean.casefold() != identity.label.casefold():
        return "deterministic_alias"
    return "exact_canonical"


def canonical_evidence_span(text: object, concept: CanonicalConcept) -> str:
    """Return an exact owner substring supporting one canonical concept."""
    source = unicodedata.normalize("NFKC", str(text or ""))
    registry = _ALIASES.get(concept.alias)
    pattern = _ALIAS_EVIDENCE.get(registry[0] if registry else concept.key)
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
    text = normalize_preference_text(value, max_length=MAX_PROFILE_SOURCE_CHARS)
    matched = _EXPLICIT_LIST_RE.fullmatch(text)
    if not matched:
        return []
    return split_compound_concept_label(matched.group("body"), limit=limit)


def split_compound_concept_label(
    value: object, *, limit: int | None = None,
) -> list[str]:
    """Propose a split for an old bare label only when every item is simple."""
    body = normalize_preference_text(value, max_length=MAX_PROFILE_SOURCE_CHARS)
    if not re.search(r"[、,，]", body):
        return []
    items = [
        re.sub(r"\s+", " ", item).strip(" \t\r\n\"'。.!?！？")
        for item in re.split(r"[、,，]", body)
    ]
    bounded_limit = durable_memory_limit() if limit is None else max(
        1, min(int(limit), HARD_DURABLE_MEMORY_LIMIT),
    )
    if len(items) < 2:
        return []
    for item in items:
        if (
            not item or len(item) > 24
            or _UNSAFE_ATOM_RE.search(item)
            or re.search(r"[。.!?！？；;:]", item)
            or not re.search(r"[A-Za-z0-9\u4e00-\u9fff]", item)
        ):
            return []
    if len(items) > bounded_limit:
        raise PreferenceTextError("memory_limit_exceeded")
    return items
