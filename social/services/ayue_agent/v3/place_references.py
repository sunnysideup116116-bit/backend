"""Short-lived server-owned references for presented Places candidates."""

from __future__ import annotations

import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit


PLACE_REFERENCE_TTL_SECONDS = 10 * 60
MAX_PLACE_REFERENCES = 8
_REFERENCE_RE = re.compile(r"place_ref_[a-f0-9]{24}")
_ORDINAL_RE = re.compile(
    r"第\s*([一二三四五六七八九十]|[1-9]|10)\s*"
    r"(?:個(?!\s*(?:星期|禮拜|週|周|月|年|天))|家|間)"
)
_LAST_RE = re.compile(r"最後(?:一)?(?:個|家|間)")
_DEICTIC_RE = re.compile(r"(?:那一間|那間|那一家|那家)")
_GOOGLE_PLACE_ID_RE = re.compile(r"[A-Za-z0-9_-]{3,180}")
_CHINESE_ORDINALS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

_COLLECTION = None
_LOCK = threading.RLock()
_MEMORY: dict[tuple[str, str], dict[str, Any]] = {}
_LOGGER = logging.getLogger(__name__)
_FALLBACK_WARNING_EMITTED = False


def _warn_memory_fallback(reason: str) -> None:
    global _FALLBACK_WARNING_EMITTED
    if _FALLBACK_WARNING_EMITTED:
        return
    _FALLBACK_WARNING_EMITTED = True
    _LOGGER.warning(
        "V3 place reference persistence is using process-local memory fallback: %s; "
        "multi-worker follow-up state is not durable",
        reason,
    )


def clear_runtime_state() -> None:
    """Clear the process-local fallback used by tests and demo mode."""
    with _LOCK:
        _MEMORY.clear()


def _collection() -> Any:
    global _COLLECTION
    if os.getenv("AYUE_TEST_MODE", "off").strip().lower() in {"1", "true", "on"}:
        return None
    if os.getenv("AYUE_PLACE_REFERENCE_MONGO", "on").strip().lower() not in {"1", "true", "on"}:
        return None
    if _COLLECTION is None:
        try:
            from database import db
            _COLLECTION = db["v3_place_candidate_sets"]
        except Exception as exc:
            _warn_memory_fallback(f"Mongo collection unavailable ({type(exc).__name__})")
            return None
    return _COLLECTION


def ensure_indexes() -> None:
    collection = _collection()
    if collection is None:
        return
    try:
        collection.create_index([("user_id", 1), ("room_id", 1)], unique=True)
        collection.create_index("expires_at", expireAfterSeconds=0)
    except Exception:
        pass


def _safe_text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _safe_ordinal(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, float) and not value.is_integer():
        return 0
    try:
        ordinal = int(value)
    except (TypeError, ValueError):
        return 0
    return ordinal if 1 <= ordinal <= MAX_PLACE_REFERENCES else 0


def _safe_map_url(value: Any, provider: str) -> str:
    url = str(value or "").strip()
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        return ""
    if parsed.scheme != "https" or not parsed.hostname:
        return ""
    host = parsed.hostname.lower()
    if provider == "google" and (host == "google.com" or host.endswith(".google.com")):
        return url[:600]
    if provider == "openstreetmap" and host in {"openstreetmap.org", "www.openstreetmap.org"}:
        return url[:600]
    return ""


def _candidate_record(card: dict[str, Any], ordinal: int) -> dict[str, Any] | None:
    label = _safe_text(card.get("name"), 80)
    provider = str(card.get("provider") or "openstreetmap").strip().lower()
    place_id = str(card.get("place_id") or "").strip()
    map_url = _safe_map_url(card.get("map_url"), provider)
    if not label or provider not in {"google", "openstreetmap"}:
        return None
    if provider == "google" and not _GOOGLE_PLACE_ID_RE.fullmatch(place_id):
        return None
    if provider == "openstreetmap" and not map_url:
        return None
    return {
        "reference": f"place_ref_{secrets.token_hex(12)}",
        "ordinal": ordinal,
        "label": label,
        "category": _safe_text(card.get("category"), 24),
        "address_summary": _safe_text(card.get("address_summary"), 180),
        # Everything below remains server-only.
        "provider": provider,
        "provider_place_id": place_id if provider == "google" else "",
        "map_identity": map_url,
    }


def replace_presented_candidates(
    user_id: str,
    room_id: str,
    candidates: list[dict[str, Any]],
    *,
    presented_ordinals: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    """Atomically replace the active set with explicit presented ordinals.

    ``candidate_ref + presented_ordinal`` is the authority for follow-up
    resolution.  ``presented_ordinals`` is intentionally server-private; a
    missing slot is preserved as a gap instead of being compacted onto the
    next valid candidate.
    """
    records: list[dict[str, Any]] = []
    seen_identities: set[tuple[str, str]] = set()
    used_ordinals: set[int] = set()
    for position, card in enumerate(candidates[:MAX_PLACE_REFERENCES], start=1):
        if not isinstance(card, dict):
            continue
        reference = str(card.get("candidate_ref") or "").strip()
        try:
            ordinal = (
                int(presented_ordinals[reference])
                if presented_ordinals and reference in presented_ordinals
                else position
            )
        except (TypeError, ValueError):
            # A malformed provider binding invalidates only that candidate;
            # never let it crash replacement or alter the old set.
            continue
        if not 1 <= ordinal <= MAX_PLACE_REFERENCES or ordinal in used_ordinals:
            continue
        record = _candidate_record(card, ordinal)
        if not record:
            continue
        identity = (
            str(record.get("provider") or ""),
            str(record.get("provider_place_id") or record.get("map_identity") or ""),
        )
        if identity in seen_identities:
            continue
        seen_identities.add(identity)
        used_ordinals.add(ordinal)
        records.append(record)
    if not records:
        clear_candidate_set(user_id, room_id)
        return None

    now = time.time()
    stored = {
        "user_id": str(user_id),
        "room_id": str(room_id),
        "candidates": records,
        "selected_reference": None,
        "created_at": now,
        "expires_at": now + PLACE_REFERENCE_TTL_SECONDS,
    }
    key = (str(user_id), str(room_id))
    with _LOCK:
        _MEMORY[key] = dict(stored)
    collection = _collection()
    try:
        if collection is not None:
            mongo_record = {
                **stored,
                "created_at": datetime.now(timezone.utc),
                "expires_at": datetime.now(timezone.utc) + timedelta(
                    seconds=PLACE_REFERENCE_TTL_SECONDS,
                ),
            }
            collection.update_one(
                {"user_id": str(user_id), "room_id": str(room_id)},
                {"$set": mongo_record},
                upsert=True,
            )
    except Exception as exc:
        _warn_memory_fallback(f"Mongo write failed ({type(exc).__name__})")
    return dict(stored)


def get_candidate_set(user_id: str, room_id: str) -> dict[str, Any] | None:
    now = time.time()
    key = (str(user_id), str(room_id))
    record: dict[str, Any] | None = None
    collection = _collection()
    try:
        record = collection.find_one({"user_id": key[0], "room_id": key[1]}) if collection is not None else None
    except Exception as exc:
        _warn_memory_fallback(f"Mongo read failed ({type(exc).__name__})")
    if not record:
        with _LOCK:
            record = dict(_MEMORY.get(key) or {}) or None
    if not record:
        return None
    expires_at = record.get("expires_at")
    if isinstance(expires_at, datetime):
        expires_timestamp = (
            expires_at.replace(tzinfo=timezone.utc)
            if expires_at.tzinfo is None
            else expires_at
        ).timestamp()
    else:
        try:
            expires_timestamp = float(expires_at or 0)
        except (TypeError, ValueError):
            clear_candidate_set(*key)
            return None
    if expires_timestamp <= now:
        clear_candidate_set(*key)
        return None
    return dict(record)


def get_candidate(user_id: str, room_id: str, reference: str) -> dict[str, Any] | None:
    key = str(reference or "").strip()
    if not _REFERENCE_RE.fullmatch(key):
        return None
    record = get_candidate_set(user_id, room_id)
    if not record:
        return None
    return next(
        (
            dict(item)
            for item in (record.get("candidates") or [])
            if isinstance(item, dict) and str(item.get("reference") or "") == key
        ),
        None,
    )


def public_projection(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """Expose only opaque refs and bounded public labels to an LLM context."""
    if not record:
        return None
    candidates = [
        {
            "reference": str(item.get("reference") or "")[:40],
            "ordinal": _safe_ordinal(item.get("ordinal", 0)),
            "label": _safe_text(item.get("label"), 80),
            "category": _safe_text(item.get("category"), 24),
        }
        for item in (record.get("candidates") or [])[:MAX_PLACE_REFERENCES]
        if isinstance(item, dict)
        and _REFERENCE_RE.fullmatch(str(item.get("reference") or ""))
        and _safe_ordinal(item.get("ordinal", 0))
        and _safe_text(item.get("label"), 80)
    ]
    if not candidates:
        return None
    projection: dict[str, Any] = {"candidates": candidates}
    selected = str(record.get("selected_reference") or "")
    selected_item = next(
        (item for item in candidates if item["reference"] == selected),
        None,
    )
    if selected_item:
        projection["selected"] = selected_item
    return projection


def public_resolution(resolution: dict[str, Any] | None) -> dict[str, Any] | None:
    candidate = (resolution or {}).get("candidate")
    if not isinstance(candidate, dict) or resolution.get("status") != "resolved":
        return None
    return {
        "reference": str(candidate.get("reference") or "")[:40],
        "ordinal": _safe_ordinal(candidate.get("ordinal", 0)),
        "label": _safe_text(candidate.get("label"), 80),
        "category": _safe_text(candidate.get("category"), 24),
    }


def _save_selected(user_id: str, room_id: str, reference: str) -> None:
    key = (str(user_id), str(room_id))
    with _LOCK:
        if key in _MEMORY:
            _MEMORY[key] = {**_MEMORY[key], "selected_reference": reference}
    collection = _collection()
    try:
        if collection is not None:
            collection.update_one(
                {"user_id": key[0], "room_id": key[1]},
                {"$set": {"selected_reference": reference}},
            )
    except Exception as exc:
        _warn_memory_fallback(f"Mongo selection write failed ({type(exc).__name__})")


def resolve_message_reference(user_id: str, room_id: str, message: str) -> dict[str, Any]:
    """Resolve one closed ordinal/deictic expression against the active set."""
    text = str(message or "")
    ordinal_values = {
        _CHINESE_ORDINALS.get(match.group(1), int(match.group(1)) if match.group(1).isdigit() else 0)
        for match in _ORDINAL_RE.finditer(text)
    }
    has_deictic = bool(_DEICTIC_RE.search(text))
    has_last = bool(_LAST_RE.search(text))
    if not ordinal_values and not has_deictic and not has_last:
        return {"status": "none"}
    if len(ordinal_values) > 1 or (ordinal_values and has_last):
        active = get_candidate_set(user_id, room_id)
        return {
            "status": "ambiguous" if active else "unavailable",
            "candidate_count": len(list((active or {}).get("candidates") or [])),
        }

    record = get_candidate_set(user_id, room_id)
    candidates = list((record or {}).get("candidates") or [])
    if not candidates:
        return {"status": "unavailable", "candidate_count": 0}

    candidate: dict[str, Any] | None = None
    if ordinal_values or has_last:
        ordinal = next(iter(ordinal_values)) if ordinal_values else len(candidates)
        candidate = next(
            (
                dict(item)
                for item in candidates
                if isinstance(item, dict) and _safe_ordinal(item.get("ordinal", 0)) == ordinal
            ),
            None,
        )
        if candidate is None:
            return {"status": "invalid_ordinal", "candidate_count": len(candidates)}
    else:
        selected = str((record or {}).get("selected_reference") or "")
        if selected:
            candidate = next(
                (
                    dict(item)
                    for item in candidates
                    if isinstance(item, dict) and str(item.get("reference") or "") == selected
                ),
                None,
            )
        if candidate is None and len(candidates) == 1:
            candidate = dict(candidates[0])
        if candidate is None:
            return {"status": "ambiguous", "candidate_count": len(candidates)}

    reference = str(candidate.get("reference") or "")
    if not _REFERENCE_RE.fullmatch(reference):
        return {"status": "unavailable", "candidate_count": len(candidates)}
    _save_selected(user_id, room_id, reference)
    return {
        "status": "resolved",
        "candidate": candidate,
        "candidate_count": len(candidates),
    }


def clarification_message(resolution: dict[str, Any]) -> str:
    status = str(resolution.get("status") or "unavailable")
    count = int(resolution.get("candidate_count", 0) or 0)
    if status == "invalid_ordinal" and count:
        return f"剛才實際呈現的是第 1 到第 {count} 個候選，沒有你指定的順位。請重新選一個。"
    if status == "ambiguous" and count:
        return "我還不能唯一判斷你指的是哪一間。請直接說第一個、第二個或第三個。"
    return "剛才的地點候選已經失效或目前不可用，請再找一次地點後重新選擇。"


def clear_candidate_set(user_id: str, room_id: str) -> None:
    key = (str(user_id), str(room_id))
    with _LOCK:
        _MEMORY.pop(key, None)
    collection = _collection()
    try:
        if collection is not None:
            collection.delete_one({"user_id": key[0], "room_id": key[1]})
    except Exception as exc:
        _warn_memory_fallback(f"Mongo delete failed ({type(exc).__name__})")
