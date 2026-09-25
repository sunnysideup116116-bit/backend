"""Bounded bootstrap text/auth contracts. No DB, provider or credential copies."""
import base64
import hashlib
import hmac
import json
import math
import os
import time
import uuid

from agent_quota.signing_config import quota_signing_key
from .concept_identity import (
    canonicalize_concept, durable_memory_limit, has_mixed_preference_polarity,
    normalize_fresh_preference_text, split_compound_concept_label,
    split_explicit_preference_enumeration, FRESH_PREFERENCE_NORMALIZATION_POLICY,
)

POLICY = "preference-bootstrap-v1"
MAX_SNAPSHOT = 100
MAX_BYTES = 262144
PREVIEW_TTL = 600
PROTECTED = r"(?:黑人|白人|黃種人|種族|族裔|宗教|信仰|穆斯林|基督教|同性戀|性傾向|性別認同|跨性別|殘障|身心障礙|疾病|政治立場|國籍|公民身分)"


class BootstrapError(ValueError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def require(value, code, status=409):
    if not value:
        raise BootstrapError(code, status)


def encoded(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError):
        raise BootstrapError("unsupported_snapshot_property", 422) from None
    require(len(raw) <= MAX_BYTES, "bootstrap_payload_too_large", 413)
    return raw


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def enabled():
    return os.getenv("PREFERENCE_BOOTSTRAP_ENABLED", "off").lower() == "on"


def owner_id(value):
    require(isinstance(value, str) and 1 <= len(value) <= 128 and value == value.strip(), "invalid_owner", 422)
    return value


def operation_id(value):
    try:
        require(str(uuid.UUID(value)) == value, "invalid_operation", 422)
    except (ValueError, TypeError, AttributeError):
        raise BootstrapError("invalid_operation", 422) from None
    return value


def normalize_items(prefers, avoids):
    import re
    require(isinstance(prefers, list) and isinstance(avoids, list), "both_polarities_required", 422)
    require(1 <= len(prefers) + len(avoids) <= min(5, durable_memory_limit()), "bootstrap_item_limit", 422)
    items = {}
    for relation, values in (("PREFERS", prefers), ("AVOIDS", avoids)):
        for raw in values:
            text = normalize_fresh_preference_text(raw)
            require(not has_mixed_preference_polarity(text), "mixed_polarity", 422)
            require(not split_explicit_preference_enumeration(text)
                    and not split_compound_concept_label(text), "atomic_item_required", 422)
            identity = canonicalize_concept(text)
            require(identity and not re.search(PROTECTED, identity.semantic_text, re.I), "preference_not_admissible", 422)
            require(identity.key not in items, "duplicate_or_conflicting_identity", 422)
            items[identity.key] = {**identity.as_dict(), "relation": relation}
    return sorted(items.values(), key=lambda item: item["key"])


def _mac(domain, payload):
    return hmac.new(quota_signing_key(), domain.encode() + b"\0" + payload, hashlib.sha256).hexdigest()


def seal_preview(payload):
    raw = encoded(payload)
    return base64.urlsafe_b64encode(raw).decode() + "." + _mac("preference-bootstrap-preview-v1", raw)


def open_preview(token, owner, *, allow_expired=False):
    try:
        require(isinstance(token, str) and len(token) <= MAX_BYTES * 2, "invalid_preview", 403)
        value, signature = token.rsplit(".", 1)
        raw = base64.b64decode(value, altchars=b"-_", validate=True)
        require(hmac.compare_digest(signature, _mac("preference-bootstrap-preview-v1", raw)), "invalid_preview", 403)
        payload = json.loads(raw)
        require(payload["owner"] == owner and payload["policy"] == POLICY
                and payload["normalization_policy"] == FRESH_PREFERENCE_NORMALIZATION_POLICY, "invalid_preview", 403)
        operation_id(payload["preview_id"])
        if not allow_expired:
            require(math.isfinite(payload["expires_at"]) and time.time() < payload["expires_at"], "preview_expired")
        return payload
    except BootstrapError:
        raise
    except Exception:
        raise BootstrapError("invalid_preview", 403) from None


def internal_headers(path, body, *, now=None):
    stamp = str(int(time.time() if now is None else now))
    bound = path.encode() + b"\0" + stamp.encode() + b"\0" + encoded(body)
    return {"X-Preference-Bootstrap": stamp + "." + _mac("preference-bootstrap-internal-v1", bound)}


def verify_internal(path, body, header):
    try:
        stamp, signature = header.split(".", 1)
        require(abs(time.time() - int(stamp)) <= 120, "invalid_bootstrap_context", 403)
        expected = internal_headers(path, body, now=int(stamp))["X-Preference-Bootstrap"]
        require(hmac.compare_digest(expected, header), "invalid_bootstrap_context", 403)
    except Exception:
        raise BootstrapError("invalid_bootstrap_context", 403) from None
