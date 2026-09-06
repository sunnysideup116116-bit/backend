"""Durable server-owned references for presented Places candidates.

The public conversation may render a plain-text list, but a later message must
resolve against the exact list that was presented.  A presentation is therefore
an immutable snapshot keyed by the originating V3 run.  The old
``replace_presented_candidates`` name is retained as a compatibility seam for
the scheduler and tests; it now creates a snapshot instead of replacing one
room-wide TTL record.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import threading
import time
import unicodedata
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit


# Kept as a compatibility constant for callers and old diagnostics. Durable
# presentation identity intentionally does not use this TTL.
PLACE_REFERENCE_TTL_SECONDS = 10 * 60
MAX_PLACE_REFERENCES = 8
MAX_PRESENTATION_SCAN = 500
_REFERENCE_RE = re.compile(r"place_ref_[a-f0-9]{24}")
_ORDINAL_CHARS = "一二三四五六七八九十"
_ORDINAL_RE = re.compile(
    rf"(?<![0-9{_ORDINAL_CHARS}])(?:第\s*)"
    rf"([0-9]+|[{_ORDINAL_CHARS}]+)\s*"
    r"(?:個(?!\s*(?:星期|禮拜|週|周|月|年|天))|家|間)"
)
_PLACE_ORDINAL_RE = re.compile(
    rf"(?<![0-9{_ORDINAL_CHARS}])(?:第\s*)"
    rf"(?:[0-9]+|[{_ORDINAL_CHARS}]+)\s*(?:家|間)"
)
_LAST_RE = re.compile(r"最後(?:一)?(?:個|家|間)")
_DEICTIC_RE = re.compile(r"(?:那一間|那間|那一家|那家)")
_PRONOUN_RE = re.compile(r"(?:它|這間|這家)")
_PERSON_PRONOUN_RE = re.compile(r"(?:他|他的)")
_PLACE_ATTRIBUTE_CONTEXT_RE = re.compile(
    r"營業|開門|關門|公休|地址|電話|菜單|選單|價格|價位|評價|店家資訊",
)
_GOOGLE_PLACE_ID_RE = re.compile(r"[A-Za-z0-9_-]{3,180}")
_CHINESE_ORDINALS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_CHINESE_DIGITS = {key: value for key, value in _CHINESE_ORDINALS.items() if key != "十"}
_PLACE_NOUN_CONTEXT_RE = re.compile(
    r"地點|候選|推薦地點|店|餐廳|飲料|奶茶|咖啡|酒吧|公園|哪一間|哪一家",
)
_PLACE_ACTION_CONTEXT_RE = re.compile(r"加到|加入|安排|排一下|選擇|挑選")
_STRONG_NON_PLACE_CONTEXT_RE = re.compile(
    r"提醒|通知|訊息|問題|步驟|原因|公司|學校|部門|工作|郵件|信件|"
    r"聯絡人|朋友|對象|人|誰|會議|任務|活動|展覽|市集",
)
_CALENDAR_CONTEXT_RE = re.compile(r"行事曆|日曆")
_CATEGORY_HINTS: tuple[tuple[re.Pattern[str], frozenset[str]], ...] = (
    (re.compile(r"飲料店|手搖飲|手搖|茶飲|奶茶|咖啡廳|咖啡店|咖啡|喫茶|吃茶"), frozenset({"cafe"})),
    (re.compile(r"雞排店|雞排|炸雞店|炸雞|餐廳|餐館|小吃店|牛排|火鍋"), frozenset({"restaurant"})),
    (re.compile(r"酒吧|酒館"), frozenset({"bar"})),
    (re.compile(r"公園"), frozenset({"park"})),
    (re.compile(r"景點|展館|博物館"), frozenset({"attraction"})),
)
_LATEST_SOURCE_RE = re.compile(r"剛才|剛剛|最新|這次")
_PREVIOUS_SOURCE_RE = re.compile(r"上一組|上一次|前一組|前一次")
_OLDER_SOURCE_RE = re.compile(r"之前|先前|原本")
_LIST_SOURCE_RE = re.compile(r"推薦|清單|候選")
_DRAFT_SOURCE_RE = re.compile(r"草稿")
_NAME_CONTEXT_NOISE_RE = re.compile(
    r"幫我|麻煩|請|可以|把|將|替我|加到|加入|安排|排一下|選擇|挑選|"
    r"行程|行事曆|日曆|推薦|清單|候選|之前|先前|原本|剛才|剛剛|最新|這次|"
    r"那一間|那間|那一家|那家|今天|明天|後天|大後天|早上|上午|中午|下午|晚上|凌晨|"
    r"[0-9０-９一二三四五六七八九十]+(?:點半|點|時|分)|"
    r"[0-9０-９]{1,2}:[0-9０-９]{2}",
)
_GENERIC_NAME_TOKENS = frozenset({
    "店", "餐廳", "飲料", "咖啡", "酒吧", "公園", "地方", "地點",
    "公司", "學校", "行程", "提醒", "通知",
})

_COLLECTION = None
_LOCK = threading.RLock()
# Test/demo memory is deliberately separate from production persistence. It is
# enabled only by the existing AYUE_TEST_MODE switch and is never used as a
# silent production fallback.
_MEMORY: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
_LOGGER = logging.getLogger(__name__)
_FALLBACK_WARNING_EMITTED = False


class PlaceReferencePersistenceError(RuntimeError):
    """Raised when a presentation cannot be durably saved or loaded."""


def _test_mode_enabled() -> bool:
    return os.getenv("AYUE_TEST_MODE", "off").strip().lower() in {
        "1", "true", "on",
    }


def _warn_memory_fallback(reason: str) -> None:
    global _FALLBACK_WARNING_EMITTED
    if _FALLBACK_WARNING_EMITTED:
        return
    _FALLBACK_WARNING_EMITTED = True
    _LOGGER.warning(
        "V3 place presentation persistence is using process-local memory only "
        "for AYUE_TEST_MODE: %s",
        reason,
    )


def clear_runtime_state() -> None:
    """Clear the process-local test store without touching durable snapshots."""
    with _LOCK:
        _MEMORY.clear()


def _collection() -> Any:
    global _COLLECTION
    if _test_mode_enabled():
        return None
    if os.getenv("AYUE_PLACE_REFERENCE_MONGO", "on").strip().lower() not in {
        "1", "true", "on",
    }:
        return None
    if _COLLECTION is None:
        try:
            from database import db
            _COLLECTION = db["v3_place_presentations"]
        except Exception as exc:
            _warn_memory_fallback(f"Mongo collection unavailable ({type(exc).__name__})")
            return None
    return _COLLECTION


def ensure_indexes() -> None:
    """Create indexes for immutable snapshots; no expiry index is intentional."""
    collection = _collection()
    if collection is None:
        return
    try:
        collection.create_index(
            [("user_id", 1), ("room_id", 1), ("origin_run_id", 1)],
            unique=True,
            name="v3_place_presentations_origin",
        )
        collection.create_index(
            [("user_id", 1), ("room_id", 1), ("published", 1), ("created_at", -1)],
            name="v3_place_presentations_recent",
        )
        collection.create_index(
            [("user_id", 1), ("room_id", 1), ("candidates.reference", 1)],
            name="v3_place_presentations_reference",
        )
    except Exception as exc:
        _LOGGER.warning("Unable to create V3 place presentation indexes: %s", type(exc).__name__)


def _safe_text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _requested_categories(text: str) -> frozenset[str]:
    categories: set[str] = set()
    for pattern, values in _CATEGORY_HINTS:
        if pattern.search(text):
            categories.update(values)
    return frozenset(categories)


def _records_matching_categories(
    records: list[dict[str, Any]], categories: frozenset[str],
) -> list[dict[str, Any]]:
    if not categories:
        return records
    matching = [
        record for record in records
        if any(
            isinstance(item, dict)
            and str(item.get("category") or "") in categories
            for item in (record.get("candidates") or [])
        )
    ]
    # Older snapshots may not have category metadata. In that case retain the
    # ordinary latest/source behavior instead of making the reference vanish.
    return matching or records


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


def _parse_ordinal_token(value: Any) -> int:
    """Parse one complete Arabic or traditional Chinese ordinal token.

    The regular expression deliberately captures the whole token first.  This
    parser then accepts the closed Chinese forms from one to ninety-nine and
    leaves larger or malformed values as invalid ordinals for the caller to
    reject against the presented list.
    """
    token = unicodedata.normalize("NFKC", str(value or "")).replace(" ", "")
    if not token:
        return 0
    if token.isdigit():
        try:
            return int(token)
        except ValueError:
            return 0
    if any(char not in _CHINESE_ORDINALS for char in token):
        return 0
    if token in _CHINESE_ORDINALS:
        return _CHINESE_ORDINALS[token]
    if token.count("十") != 1:
        return 0
    tens_text, ones_text = token.split("十")
    if len(tens_text) > 1 or len(ones_text) > 1:
        return 0
    tens = _CHINESE_DIGITS.get(tens_text, 1 if not tens_text else 0)
    ones = _CHINESE_DIGITS.get(ones_text, 0 if not ones_text else 0)
    if not tens or (ones_text and not ones):
        return 0
    return tens * 10 + ones


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
        # Provider identity remains server-only and is never returned by the
        # public projection or AgentResult.
        "provider": provider,
        "provider_place_id": place_id if provider == "google" else "",
        "map_identity": map_url,
    }


def _origin_key(value: Any) -> str:
    origin = _safe_text(value, 160)
    return origin or f"legacy_{secrets.token_hex(12)}"


def _storage_record(record: dict[str, Any], *, backend: str) -> dict[str, Any]:
    result = dict(record)
    result["storage_backend"] = backend
    return result


def _memory_snapshots(
    user_id: str,
    room_id: str,
    *,
    include_unpublished: bool = False,
) -> list[dict[str, Any]]:
    key = (str(user_id), str(room_id))
    with _LOCK:
        raw_records = [dict(item) for item in (_MEMORY.get(key) or {}).values()]
    records = [
        item for _index, item in sorted(
            enumerate(raw_records),
            key=lambda pair: (float(pair[1].get("created_at", 0) or 0), pair[0]),
            reverse=True,
        )
    ]
    if not include_unpublished:
        records = [item for item in records if item.get("published") is True]
    return sorted(
        records,
        key=lambda item: float(item.get("created_at", 0) or 0),
        reverse=True,
    )


def _mongo_snapshots(
    user_id: str,
    room_id: str,
    *,
    include_unpublished: bool = False,
    origin_run_id: str | None = None,
) -> list[dict[str, Any]]:
    collection = _collection()
    if collection is None:
        if _test_mode_enabled():
            return _memory_snapshots(user_id, room_id, include_unpublished=include_unpublished)
        raise PlaceReferencePersistenceError("place_presentation_store_unavailable")
    query: dict[str, Any] = {"user_id": str(user_id), "room_id": str(room_id)}
    if not include_unpublished:
        query["published"] = True
    if origin_run_id:
        query["origin_run_id"] = str(origin_run_id)
    try:
        rows = [dict(item) for item in list(collection.find(query))[:MAX_PRESENTATION_SCAN]]
    except Exception as exc:
        raise PlaceReferencePersistenceError("place_presentation_store_unavailable") from exc
    def _sort_key(item: dict[str, Any]) -> float:
        value = item.get("created_at", 0)
        if isinstance(value, datetime):
            return value.timestamp()
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0
    return sorted(rows, key=_sort_key, reverse=True)


def _all_snapshots(
    user_id: str,
    room_id: str,
    *,
    include_unpublished: bool = False,
    origin_run_id: str | None = None,
) -> list[dict[str, Any]]:
    if _test_mode_enabled():
        records = _memory_snapshots(
            user_id, room_id, include_unpublished=include_unpublished,
        )
        if origin_run_id:
            records = [
                item for item in records
                if str(item.get("origin_run_id") or "") == str(origin_run_id)
            ]
    else:
        records = _mongo_snapshots(
            user_id, room_id,
            include_unpublished=include_unpublished,
            origin_run_id=origin_run_id,
        )
    if include_unpublished:
        return records
    return [record for record in records if _published_source_is_accessible(record)]


def _published_source_is_accessible(record: dict[str, Any]) -> bool:
    """Ensure a published snapshot still points at its assistant message."""
    source_message_id = _safe_text(record.get("source_message_id"), 160)
    if _test_mode_enabled():
        return True
    if not source_message_id:
        return False
    try:
        from bson.objectid import ObjectId
        from database import messages_coll
        object_id = ObjectId(source_message_id)
    except Exception:
        return False
    try:
        message = messages_coll.find_one(
            {
                "_id": object_id,
                "room_id": str(record.get("room_id") or ""),
                "sender_id": "ai_assistant",
                "is_blocked": {"$ne": True},
            },
            {"_id": 1},
        )
    except Exception as exc:
        raise PlaceReferencePersistenceError("place_source_store_unavailable") from exc
    return isinstance(message, dict)


def replace_presented_candidates(
    user_id: str,
    room_id: str,
    candidates: list[dict[str, Any]],
    *,
    presented_ordinals: dict[str, int] | None = None,
    origin_run_id: str | None = None,
    source_message_id: str | None = None,
    published: bool = True,
) -> dict[str, Any] | None:
    """Create one immutable, ordered presentation snapshot.

    ``presented_ordinals`` is accepted for compatibility, but the scheduler
    supplies the server-owned candidate order. Gaps, duplicate ordinals and
    duplicate provider identities are rejected so ordinal selection can never
    silently shift to another candidate.
    """
    records: list[dict[str, Any]] = []
    seen_identities: set[tuple[str, str]] = set()
    used_ordinals: set[int] = set()
    for position, card in enumerate(list(candidates or [])[:MAX_PLACE_REFERENCES], start=1):
        if not isinstance(card, dict):
            continue
        candidate_ref = str(card.get("candidate_ref") or "").strip()
        try:
            ordinal = (
                int(presented_ordinals[candidate_ref])
                if presented_ordinals and candidate_ref in presented_ordinals
                else position
            )
        except (TypeError, ValueError):
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
    if not records or sorted(used_ordinals) != list(range(1, len(records) + 1)):
        return None
    records.sort(key=lambda item: int(item["ordinal"]))

    now = time.time()
    origin = _origin_key(origin_run_id)
    stored = {
        "snapshot_version": "v3-place-presentation-v1",
        "user_id": str(user_id),
        "room_id": str(room_id),
        "origin_run_id": origin,
        "source_message_id": _safe_text(source_message_id, 160) or None,
        "published": bool(published),
        "candidates": records,
        "selected_reference": None,
        "selected_at": None,
        "created_at": now,
    }

    if _test_mode_enabled():
        with _LOCK:
            bucket = _MEMORY.setdefault((str(user_id), str(room_id)), {})
            existing = bucket.get(origin)
            if existing:
                return _storage_record(dict(existing), backend="memory_fallback")
            bucket[origin] = dict(stored)
        _warn_memory_fallback("AYUE_TEST_MODE")
        return _storage_record(dict(stored), backend="memory_fallback")

    collection = _collection()
    if collection is None:
        raise PlaceReferencePersistenceError("place_presentation_store_unavailable")
    query = {"user_id": str(user_id), "room_id": str(room_id), "origin_run_id": origin}
    try:
        existing = collection.find_one(query)
        if isinstance(existing, dict):
            return _storage_record(dict(existing), backend="mongo")
        collection.insert_one(dict(stored))
    except Exception as exc:
        # A retry racing the unique origin key is safe: recover the original
        # snapshot. Any other write failure is surfaced to the Scheduler.
        try:
            existing = collection.find_one(query)
        except Exception as read_exc:
            raise PlaceReferencePersistenceError("place_presentation_store_unavailable") from read_exc
        if not isinstance(existing, dict):
            raise PlaceReferencePersistenceError("place_presentation_store_write_failed") from exc
        return _storage_record(dict(existing), backend="mongo")
    return _storage_record(dict(stored), backend="mongo")


def publish_place_presentation(
    user_id: str,
    room_id: str,
    origin_run_id: str,
    source_message_id: str,
) -> bool:
    """Publish a snapshot only after its assistant message was saved."""
    origin = str(origin_run_id or "").strip()
    message_id = _safe_text(source_message_id, 160)
    if not origin or not message_id:
        return False
    key = (str(user_id), str(room_id))
    if _test_mode_enabled():
        with _LOCK:
            record = (_MEMORY.get(key) or {}).get(origin)
            if not record:
                return False
            record.update({"published": True, "source_message_id": message_id})
        return True
    collection = _collection()
    if collection is None:
        raise PlaceReferencePersistenceError("place_presentation_store_unavailable")
    query = {
        "user_id": key[0],
        "room_id": key[1],
        "origin_run_id": origin,
        "published": {"$ne": True},
    }
    try:
        collection.update_one(
            query,
            {"$set": {"published": True, "source_message_id": message_id}},
        )
        published = collection.find_one({
            "user_id": key[0], "room_id": key[1],
            "origin_run_id": origin, "published": True,
        })
    except Exception as exc:
        raise PlaceReferencePersistenceError("place_presentation_store_write_failed") from exc
    return isinstance(published, dict)


def get_candidate_set(
    user_id: str,
    room_id: str,
    *,
    origin_run_id: str | None = None,
    include_unpublished: bool = False,
) -> dict[str, Any] | None:
    """Return the latest published snapshot, or one exact origin snapshot."""
    try:
        records = _all_snapshots(
            user_id, room_id,
            include_unpublished=include_unpublished,
            origin_run_id=origin_run_id,
        )
    except PlaceReferencePersistenceError as exc:
        _LOGGER.error(
            "Unable to load V3 place presentation user=%s room=%s reason=%s",
            str(user_id)[:80], str(room_id)[:80], str(exc),
        )
        return None
    return dict(records[0]) if records else None


def _candidate_identity(item: dict[str, Any]) -> tuple[str, str]:
    return (
        str(item.get("provider") or ""),
        str(item.get("provider_place_id") or item.get("map_identity") or ""),
    )


def get_candidate(user_id: str, room_id: str, reference: str) -> dict[str, Any] | None:
    """Find a server-owned candidate across all published room snapshots."""
    key = str(reference or "").strip()
    if not _REFERENCE_RE.fullmatch(key):
        return None
    try:
        records = _all_snapshots(user_id, room_id)
    except PlaceReferencePersistenceError:
        return None
    for record in records:
        for item in (record.get("candidates") or []):
            if isinstance(item, dict) and str(item.get("reference") or "") == key:
                return dict(item)
    return None


def get_candidate_origin(user_id: str, room_id: str, reference: str) -> str | None:
    """Return only the private snapshot origin for one opaque candidate ref."""
    key = str(reference or "").strip()
    if not _REFERENCE_RE.fullmatch(key):
        return None
    try:
        records = _all_snapshots(user_id, room_id)
    except PlaceReferencePersistenceError:
        return None
    for record in records:
        if any(
            isinstance(item, dict) and str(item.get("reference") or "") == key
            for item in (record.get("candidates") or [])
        ):
            origin = _safe_text(record.get("origin_run_id"), 160)
            return origin or None
    return None


def public_projection(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """Expose only opaque refs and bounded public labels to prompt context."""
    if not record:
        return None
    candidates = [
        {
            "reference": str(item.get("reference") or "")[:40],
            "ordinal": _safe_ordinal(item.get("ordinal", 0)),
            "label": _safe_text(item.get("label"), 80),
            "category": _safe_text(item.get("category"), 24),
            "address_summary": _safe_text(item.get("address_summary"), 180),
        }
        for item in (record.get("candidates") or [])[:MAX_PLACE_REFERENCES]
        if isinstance(item, dict)
        and _REFERENCE_RE.fullmatch(str(item.get("reference") or ""))
        and _safe_ordinal(item.get("ordinal", 0))
        and _safe_text(item.get("label"), 80)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: item["ordinal"])
    projection: dict[str, Any] = {
        "snapshot_version": str(record.get("snapshot_version") or "v3-place-presentation-v1"),
        "candidates": candidates,
    }
    selected = str(record.get("selected_reference") or "")
    selected_item = next(
        (item for item in candidates if item["reference"] == selected),
        None,
    )
    if selected_item:
        projection["selected"] = selected_item
    return projection


def _selected_candidate_from_record(
    record: dict[str, Any],
) -> dict[str, Any] | None:
    reference = str(record.get("selected_reference") or "")
    if not _REFERENCE_RE.fullmatch(reference):
        return None
    for item in record.get("candidates") or []:
        if isinstance(item, dict) and str(item.get("reference") or "") == reference:
            return dict(item)
    return None


def get_recent_selected_candidate(
    user_id: str,
    room_id: str,
) -> tuple[dict[str, Any], str] | None:
    """Return the newest server-selected candidate and its private origin."""
    try:
        records = _all_snapshots(user_id, room_id)
    except PlaceReferencePersistenceError:
        return None
    selected: list[tuple[float, float, dict[str, Any], str]] = []
    for record in records:
        candidate = _selected_candidate_from_record(record)
        if candidate is None:
            continue
        try:
            selected_at = float(record.get("selected_at") or 0)
        except (TypeError, ValueError):
            selected_at = 0.0
        try:
            created_at = float(record.get("created_at") or 0)
        except (TypeError, ValueError):
            created_at = 0.0
        selected.append((
            selected_at,
            created_at,
            candidate,
            _safe_text(record.get("origin_run_id"), 160),
        ))
    if not selected:
        return None
    _selected_at, _created_at, candidate, origin = max(
        selected, key=lambda item: (item[0], item[1]),
    )
    # A successful newer recommendation starts a new selection context. An
    # older selected candidate may still be resolved through an explicit
    # historical-list phrase, but it must not answer a bare "它／那間".
    latest_record = records[0] if records else {}
    try:
        latest_created_at = float(latest_record.get("created_at") or 0)
    except (TypeError, ValueError):
        latest_created_at = 0.0
    latest_origin = _safe_text(latest_record.get("origin_run_id"), 160)
    if origin != latest_origin and _selected_at <= latest_created_at:
        return None
    return candidate, origin


def recent_selected_projection(
    user_id: str,
    room_id: str,
) -> dict[str, Any] | None:
    """Expose the latest selected place without provider identity or origin."""
    selected = get_recent_selected_candidate(user_id, room_id)
    if selected is None:
        return None
    candidate, _origin = selected
    reference = str(candidate.get("reference") or "")
    if not _REFERENCE_RE.fullmatch(reference):
        return None
    return {
        "reference": reference[:40],
        "ordinal": _safe_ordinal(candidate.get("ordinal", 0)),
        "label": _safe_text(candidate.get("label"), 80),
        "category": _safe_text(candidate.get("category"), 24),
        "address_summary": _safe_text(candidate.get("address_summary"), 180),
    }


def public_resolution(resolution: dict[str, Any] | None) -> dict[str, Any] | None:
    candidate = (resolution or {}).get("candidate")
    if not isinstance(candidate, dict) or resolution.get("status") != "resolved":
        return None
    return {
        "status": "resolved",
        "reference": str(candidate.get("reference") or "")[:40],
        "ordinal": _safe_ordinal(candidate.get("ordinal", 0)),
        "label": _safe_text(candidate.get("label"), 80),
        "category": _safe_text(candidate.get("category"), 24),
        "address_summary": _safe_text(candidate.get("address_summary"), 180),
    }


def commit_resolved_selection(
    user_id: str,
    room_id: str,
    resolution: dict[str, Any] | None,
) -> dict[str, Any]:
    """Persist one already-resolved candidate after revalidating room ownership."""
    if not isinstance(resolution, dict) or resolution.get("status") != "resolved":
        return {"status": "invalid_resolution"}
    candidate_value = resolution.get("candidate")
    projected = candidate_value if isinstance(candidate_value, dict) else resolution
    reference = str(projected.get("reference") or "").strip()
    if not _REFERENCE_RE.fullmatch(reference):
        return {"status": "invalid_resolution"}
    candidate = get_candidate(user_id, room_id, reference)
    if not isinstance(candidate, dict):
        return {"status": "source_unavailable"}
    projected_label = _safe_text(projected.get("label"), 80)
    if projected_label and projected_label != _safe_text(candidate.get("label"), 80):
        return {"status": "invalid_resolution"}
    origin = get_candidate_origin(user_id, room_id, reference)
    expected_origin = _safe_text(resolution.get("origin_run_id"), 160)
    if not origin or (expected_origin and expected_origin != origin):
        return {"status": "source_unavailable"}
    if not _save_selected(
        user_id,
        room_id,
        reference,
        origin_run_id=origin,
    ):
        return {"status": "storage_unavailable"}
    return {
        "status": "committed",
        "selection": {
            "reference": reference,
            "ordinal": _safe_ordinal(candidate.get("ordinal", 0)),
            "label": _safe_text(candidate.get("label"), 80),
            "category": _safe_text(candidate.get("category"), 24),
            "address_summary": _safe_text(candidate.get("address_summary"), 180),
        },
    }


def _save_selected(
    user_id: str,
    room_id: str,
    reference: str,
    *,
    origin_run_id: str | None = None,
) -> bool:
    key = (str(user_id), str(room_id))
    selected_at = time.time()
    if _test_mode_enabled():
        saved = False
        with _LOCK:
            bucket = _MEMORY.get(key) or {}
            for origin, record in bucket.items():
                if origin_run_id and origin != origin_run_id:
                    continue
                if any(
                    isinstance(item, dict) and str(item.get("reference") or "") == reference
                    for item in (record.get("candidates") or [])
                ):
                    record["selected_reference"] = reference
                    record["selected_at"] = selected_at
                    saved = True
                    if origin_run_id:
                        break
        return saved
    try:
        collection = _collection()
    except PlaceReferencePersistenceError:
        return False
    if collection is None:
        return False
    query: dict[str, Any] = {
        "user_id": key[0],
        "room_id": key[1],
        "published": True,
        "candidates.reference": reference,
    }
    if origin_run_id:
        query["origin_run_id"] = origin_run_id
    try:
        result = collection.update_one(
            query,
            {"$set": {"selected_reference": reference, "selected_at": selected_at}},
        )
    except Exception as exc:
        _LOGGER.warning("Unable to persist selected place reference: %s", type(exc).__name__)
        return False
    return bool(getattr(result, "matched_count", 0))


def _name_key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(
        char for char in text
        if char.isalnum() or "\u4e00" <= char <= "\u9fff"
    )


def _name_tokens(label: str) -> list[str]:
    return [
        token for token in re.findall(
            r"[0-9A-Za-z\u4e00-\u9fff]+",
            unicodedata.normalize("NFKC", label),
        )
        if len(token) >= 2 and token not in _GENERIC_NAME_TOKENS
    ]


def _message_contains_candidate_name(message: str, label: str) -> bool:
    message_key = _name_key(message)
    label_key = _name_key(label)
    if not message_key or not label_key:
        return False
    if label_key in message_key:
        return True
    if any(_name_key(token) in message_key for token in _name_tokens(label)):
        return True

    # A provider label may omit spaces, for example ``50嵐自立六合店``.  In
    # that form the label tokenizer cannot expose the brand as a separate
    # token.  Strip the closed command/context vocabulary from the user's
    # message and accept the remaining meaningful fragment only when it is a
    # substring of the label.  _name_matches applies exact full-label matches
    # first, so this fallback cannot make a full branch name ambiguous with
    # another branch of the same brand.
    fragment_key = _name_key(_NAME_CONTEXT_NOISE_RE.sub("", message))
    if len(fragment_key) >= 2 and fragment_key in label_key:
        return True
    return False


def _looks_like_place_followup(
    text: str,
    *,
    has_candidates: bool,
    has_expression: bool,
    has_selected_reference: bool = False,
    has_place_ordinal: bool = False,
    has_place_pronoun: bool = False,
) -> bool:
    has_place_noun = bool(_PLACE_NOUN_CONTEXT_RE.search(text))
    has_place_action = bool(_PLACE_ACTION_CONTEXT_RE.search(text))
    if (
        _STRONG_NON_PLACE_CONTEXT_RE.search(text)
        and not has_place_noun
        and not has_place_ordinal
        and not has_place_pronoun
    ):
        return False
    if _CALENDAR_CONTEXT_RE.search(text) and not has_place_noun:
        return bool(has_place_action and (has_expression or has_candidates))
    if has_place_noun:
        return True
    if has_place_action:
        return bool(has_expression or has_candidates)
    if _PRONOUN_RE.search(text) or has_place_pronoun:
        # A bare pronoun is not evidence of a place on its own. It becomes a
        # place continuation only when the server has one recent selected
        # candidate to bind it to; explicit names/ordinals remain handled by
        # the normal branches below.
        return bool(has_selected_reference)
    return bool(has_candidates and has_expression)


def _candidate_options(candidates: list[dict[str, Any]]) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    for item in candidates[:3]:
        if not isinstance(item, dict):
            continue
        label = _safe_text(item.get("label"), 80)
        if not label:
            continue
        address = _safe_text(item.get("address_summary"), 100)
        options.append({"label": label, "address_summary": address})
    return options


def _active_followup_origin(user_id: str, room_id: str) -> str:
    """Read the private origin of the room's trusted place draft, if any."""
    try:
        from .place_followups import get_followup

        record = get_followup(user_id, room_id)
    except Exception:
        return ""
    resolved = record.get("resolved_place") if isinstance(record, dict) else None
    if not isinstance(resolved, dict):
        return ""
    reference = str(resolved.get("reference") or "").strip()
    origin = _safe_text(resolved.get("origin_run_id"), 160)
    if not _REFERENCE_RE.fullmatch(reference):
        return ""
    if origin:
        return origin
    # Older drafts may contain only the opaque reference. Recovering the
    # origin from the durable snapshot keeps those drafts pinned to their
    # original list after a later search.
    return get_candidate_origin(user_id, room_id, reference) or ""


def _source_hint(text: str) -> str:
    """Classify explicit source wording without exposing internal IDs."""
    has_latest = bool(_LATEST_SOURCE_RE.search(text))
    has_previous = bool(_PREVIOUS_SOURCE_RE.search(text))
    has_older = bool(_OLDER_SOURCE_RE.search(text))
    has_draft = bool(_DRAFT_SOURCE_RE.search(text))
    has_list = bool(_LIST_SOURCE_RE.search(text))

    # A phrase such as "原本那筆草稿" names the active draft's source.  The
    # draft is more precise than the generic older-list wording around it.
    if has_draft and has_older:
        return "draft"
    if has_latest:
        return "latest"
    if has_previous:
        return "previous"
    if has_older:
        return "older"
    if has_draft:
        return "draft"
    if has_list:
        # "清單／候選／推薦" without a temporal qualifier refers to the
        # currently presented list, and therefore outranks an active draft.
        return "latest"
    return ""


def _source_options(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Build concrete, public list descriptions for source clarification."""
    options: list[dict[str, str]] = []
    for index, record in enumerate(records[:MAX_PLACE_REFERENCES], start=1):
        labels = [
            _safe_text(item.get("label"), 80)
            for item in (record.get("candidates") or [])[:MAX_PLACE_REFERENCES]
            if isinstance(item, dict) and _safe_text(item.get("label"), 80)
        ]
        if not labels:
            continue
        options.append({
            "label": f"第{index}組：" + "、".join(labels[:4]),
            "address_summary": "",
        })
    return options


def _select_source(
    records: list[dict[str, Any]],
    *,
    hint: str,
    active_origin: str,
) -> tuple[dict[str, Any] | None, str | None, list[dict[str, str]]]:
    """Select an immutable snapshot in the documented priority order."""
    if hint == "latest":
        return (records[0] if records else None), None, []
    if hint == "previous":
        if len(records) >= 2:
            return records[1], None, []
        return None, "missing_snapshot", _source_options(records)
    if hint == "older":
        if len(records) > 2:
            return None, "ambiguous_source", _source_options(records)
        if len(records) == 2:
            return records[1], None, []
        return None, "missing_snapshot", _source_options(records)
    if hint == "draft" or active_origin:
        if active_origin:
            for record in records:
                if str(record.get("origin_run_id") or "") == active_origin:
                    return record, None, []
            return None, "source_unavailable", _source_options(records)
        if hint == "draft":
            return None, "missing_snapshot", _source_options(records)
    return (records[0] if records else None), None, []


def _name_matches(
    text: str,
    records: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], str]]:
    def collect(*, exact_only: bool) -> list[tuple[dict[str, Any], str]]:
        by_identity: dict[tuple[str, str], tuple[dict[str, Any], str]] = {}
        message_key = _name_key(text)
        for record in records:
            origin = str(record.get("origin_run_id") or "")
            for item in (record.get("candidates") or []):
                if not isinstance(item, dict):
                    continue
                label = _safe_text(item.get("label"), 80)
                label_key = _name_key(label)
                if not label_key:
                    continue
                if exact_only:
                    matched = label_key in message_key
                else:
                    matched = _message_contains_candidate_name(text, label)
                if not matched:
                    continue
                identity = _candidate_identity(item)
                if not identity[0] or not identity[1]:
                    identity = ("reference", str(item.get("reference") or ""))
                # records are newest-first, so the first occurrence is the
                # most recent presentation of the same provider identity.
                by_identity.setdefault(identity, (dict(item), origin))
        return list(by_identity.values())

    exact_matches = collect(exact_only=True)
    return exact_matches or collect(exact_only=False)


def resolve_message_reference(
    user_id: str,
    room_id: str,
    message: str,
    *,
    commit_selection: bool = True,
) -> dict[str, Any]:
    """Resolve an ordinal, unique public name, or previously selected deictic.

    The function deliberately returns ``none`` for non-place ordinal/deictic
    language. That lets Calendar, Match, reminders, and ordinary chat keep
    their own semantics instead of receiving a place-expired canned response.
    """
    text = unicodedata.normalize("NFKC", str(message or ""))
    last_matches = list(_LAST_RE.finditer(text))
    # ``最後一個`` contains the substring ``一個``.  Do not count that
    # substring as a second ordinal, otherwise an explicit last-item request
    # is incorrectly classified as an ambiguous "first + last" request.
    ordinal_matches = [
        match for match in _ORDINAL_RE.finditer(text)
        if not any(
            last.start() <= match.start() < last.end()
            for last in last_matches
        )
    ]
    ordinal_values = [_parse_ordinal_token(match.group(1)) for match in ordinal_matches]
    has_deictic = bool(_DEICTIC_RE.search(text))
    has_place_pronoun = bool(
        _PERSON_PRONOUN_RE.search(text)
        and _PLACE_ATTRIBUTE_CONTEXT_RE.search(text)
    )
    has_pronoun = bool(_PRONOUN_RE.search(text) or has_place_pronoun)
    has_place_ordinal = bool(_PLACE_ORDINAL_RE.search(text))
    has_last = bool(last_matches)
    has_expression = bool(ordinal_matches or has_deictic or has_pronoun or has_last)

    try:
        records = _all_snapshots(user_id, room_id)
    except PlaceReferencePersistenceError:
        # A Places-store outage must not turn an ordinary Calendar or chat
        # request into a place clarification. Only a message that looks like
        # a place continuation receives the explicit storage diagnosis.
        if not _looks_like_place_followup(
            text, has_candidates=False, has_expression=has_expression,
            has_place_ordinal=has_place_ordinal,
            has_place_pronoun=has_place_pronoun,
        ):
            return {"status": "none"}
        return {"status": "storage_unavailable", "candidate_count": 0}

    has_candidates = bool(records)
    requested_categories = _requested_categories(text)
    category_records = _records_matching_categories(records, requested_categories)
    category_scoped = bool(requested_categories and category_records != records)
    records = category_records
    latest = records[0] if records else None
    source_unavailable = False
    if not has_candidates:
        try:
            source_unavailable = any(
                record.get("published") is True
                for record in _all_snapshots(
                    user_id, room_id, include_unpublished=True,
                )
            )
        except PlaceReferencePersistenceError:
            source_unavailable = False

    source_hint = _source_hint(text)
    active_origin = _active_followup_origin(user_id, room_id)
    # An explicit category such as "第四間飲料店" identifies the historical
    # list for that category. Do not let an unrelated active selection from a
    # newer category override it; explicit source words still win as usual.
    source_active_origin = "" if category_scoped and not source_hint else active_origin
    source_record, source_error, source_options = _select_source(
        records,
        hint=source_hint,
        active_origin=source_active_origin,
    )
    # A bare ordinal uses the latest list, while a bare deictic still needs to
    # search all snapshots for the room's previously selected candidate.  An
    # explicit source phrase or an active draft constrains both forms.
    source_records = (
        [source_record]
        if source_record and (source_hint or source_active_origin)
        else records
    )

    recent_selected = get_recent_selected_candidate(user_id, room_id)
    has_recent_selected = recent_selected is not None
    if category_scoped and recent_selected is not None:
        selected_category = str(recent_selected[0].get("category") or "")
        if selected_category not in requested_categories:
            recent_selected = None
            has_recent_selected = False

    # Keep name evidence even when an ordinal/deictic is present.  The server
    # can then detect a message such as "第二間 50嵐" naming two different
    # candidates instead of silently preferring the ordinal.  When an explicit
    # source or active draft selected one snapshot, names are constrained to
    # that snapshot as well.
    name_matches = _name_matches(text, source_records)
    place_like = _looks_like_place_followup(
        text,
        has_candidates=has_candidates,
        has_expression=has_expression,
        has_selected_reference=has_recent_selected,
        has_place_ordinal=has_place_ordinal,
        has_place_pronoun=has_place_pronoun,
    )
    if not place_like:
        if has_expression:
            return {"status": "none"}
        if not name_matches:
            return {"status": "none"}

    # A concrete full name can disambiguate an older source even when several
    # historical lists exist.  Ordinal/deictic requests still require the
    # user to identify which list they meant.
    source_error_is_disambiguated = (
        source_error == "ambiguous_source"
        and not has_expression
        and len(name_matches) == 1
    )
    if source_error and not source_error_is_disambiguated:
        result: dict[str, Any] = {
            "status": source_error,
            "candidate_count": len((source_record or latest or {}).get("candidates") or [])
            if source_error != "ambiguous_source" else len(records),
        }
        if source_options:
            # Keep this duplicated safe projection for older callers that only
            # forward candidate_options to their clarification UI. No origin
            # or provider identity is included.
            result["source_options"] = source_options
            result["candidate_options"] = source_options
        return result

    # A generic calendar phrase such as "新增一筆行事曆" may contain a place
    # context word, but it does not select a place. Let Calendar own it.
    if not has_expression and not name_matches:
        if source_unavailable and _looks_like_place_followup(
            text, has_candidates=False, has_expression=has_expression,
            has_selected_reference=has_recent_selected,
        ):
            return {"status": "source_unavailable", "candidate_count": 0}
        return {"status": "none"}

    if (
        len(ordinal_values) > 1
        or (ordinal_values and has_last)
        or (ordinal_values and has_deictic)
        or (ordinal_values and has_pronoun)
        or (has_last and has_deictic)
        or (has_last and has_pronoun)
        or (has_deictic and has_pronoun)
    ):
        return {
            "status": (
                "ambiguous"
                if latest
                else "source_unavailable" if source_unavailable else "missing_snapshot"
            ),
            "candidate_count": len(list((source_record or latest or {}).get("candidates") or [])),
        }

    if not has_expression and name_matches:
        if len(name_matches) > 1:
            return {
                "status": "ambiguous",
                "candidate_count": len(name_matches),
                "candidate_options": _candidate_options(
                    [item for item, _origin in name_matches],
                ),
            }
        candidate, origin = name_matches[0]
        reference = str(candidate.get("reference") or "")
        if not _REFERENCE_RE.fullmatch(reference):
            return {"status": "unavailable", "candidate_count": 0}
        if commit_selection:
            commit = commit_resolved_selection(
                user_id,
                room_id,
                {
                    "status": "resolved",
                    "candidate": candidate,
                    "origin_run_id": origin,
                },
            )
            if commit["status"] != "committed":
                return {"status": commit["status"], "candidate_count": len(name_matches)}
        return {
            "status": "resolved",
            "candidate": candidate,
            "candidate_count": len(name_matches),
            "origin_run_id": origin,
            "resolution_method": "unique_name",
        }

    selected_source = source_record or latest
    candidates = list((selected_source or {}).get("candidates") or [])
    if not candidates:
        return {
            "status": "source_unavailable" if source_unavailable else "missing_snapshot",
            "candidate_count": 0,
        }

    if ordinal_values or has_last:
        ordinal = ordinal_values[0] if ordinal_values else len(candidates)
        candidate = next(
            (
                dict(item) for item in candidates
                if isinstance(item, dict)
                and _safe_ordinal(item.get("ordinal", 0)) == ordinal
            ),
            None,
        )
        if candidate is None:
            return {"status": "invalid_ordinal", "candidate_count": len(candidates)}
        origin = str((selected_source or {}).get("origin_run_id") or "")
    else:
        if has_pronoun and recent_selected is not None and not source_hint and not source_active_origin:
            candidate, origin = recent_selected
            resolution_method = "selected_pronoun"
        else:
            candidate = None
            origin = ""
            selected = ""
            selected_origin = ""
            selected_candidate: dict[str, Any] | None = None
            selection_records = source_records
            if (
                (has_deictic or has_pronoun)
                and not source_hint
                and not source_active_origin
                and recent_selected is None
            ):
                selection_records = [selected_source] if selected_source else []
            for record in selection_records:
                selected_reference = str(record.get("selected_reference") or "")
                if selected_reference:
                    selected = selected_reference
                    selected_origin = str(record.get("origin_run_id") or "")
                    selected_candidate = next(
                        (
                            dict(item) for item in (record.get("candidates") or [])
                            if isinstance(item, dict)
                            and str(item.get("reference") or "") == selected_reference
                        ),
                        None,
                    )
                    break
            candidate = selected_candidate if selected else None
            origin = selected_origin
            if candidate is None and len(candidates) == 1:
                candidate = dict(candidates[0])
                origin = str((selected_source or {}).get("origin_run_id") or "")
            if candidate is None:
                return {
                    "status": "ambiguous",
                    "candidate_count": len(candidates),
                    "candidate_options": _candidate_options(candidates),
                }

    if not (has_pronoun and recent_selected is not None and not source_hint and not source_active_origin):
        resolution_method = "ordinal" if ordinal_values or has_last else "selected_deictic"
    if name_matches:
        # A name and an ordinal/deictic may reinforce one another when they
        # identify the same provider entity.  If they disagree, ask instead
        # of changing the user's selected place behind their back.
        named_candidates = [item for item, _origin in name_matches]
        if len(named_candidates) != 1:
            return {
                "status": "ambiguous",
                "candidate_count": len(named_candidates),
                "candidate_options": _candidate_options(named_candidates),
            }
        named_candidate = named_candidates[0]
        if _candidate_identity(candidate) != _candidate_identity(named_candidate):
            conflict_options: list[dict[str, Any]] = [candidate, named_candidate]
            seen_conflicts: set[tuple[str, str]] = set()
            unique_options: list[dict[str, Any]] = []
            for item in conflict_options:
                identity = _candidate_identity(item)
                if identity in seen_conflicts:
                    continue
                seen_conflicts.add(identity)
                unique_options.append(item)
            return {
                "status": "ambiguous",
                "candidate_count": len(unique_options),
                "candidate_options": _candidate_options(unique_options),
            }
        resolution_method = (
            "ordinal_and_name" if ordinal_values or has_last
            else "selected_deictic_and_name"
        )

    reference = str(candidate.get("reference") or "")
    if not _REFERENCE_RE.fullmatch(reference):
        return {"status": "unavailable", "candidate_count": len(candidates)}
    if commit_selection:
        commit = commit_resolved_selection(
            user_id,
            room_id,
            {
                "status": "resolved",
                "candidate": candidate,
                "origin_run_id": origin or None,
            },
        )
        if commit["status"] != "committed":
            return {"status": commit["status"], "candidate_count": len(candidates)}
    return {
        "status": "resolved",
        "candidate": candidate,
        "candidate_count": len(candidates),
        "origin_run_id": origin,
        "resolution_method": resolution_method,
    }


def clarification_message(resolution: dict[str, Any]) -> str:
    status = str(resolution.get("status") or "unavailable")
    count = int(resolution.get("candidate_count", 0) or 0)
    if status == "storage_unavailable":
        return "這次地點推薦暫時無法讀取，請稍後再試；我還沒有替你建立行程。"
    if status == "source_unavailable":
        return "原本的地點推薦訊息目前無法存取，請重新找一次地點後再選；我還沒有替你建立行程。"
    if status == "missing_snapshot":
        return "這個聊天室目前沒有可供選取的地點推薦清單，請先重新找一次地點。"
    if status == "invalid_ordinal" and count:
        return f"剛才實際呈現的是第 1 到第 {count} 個候選，沒有你指定的順位。請重新選一個。"
    if status == "ambiguous_source":
        options = resolution.get("source_options") or resolution.get("candidate_options") or []
        lines = []
        for index, item in enumerate(options[:3], start=1):
            if not isinstance(item, dict):
                continue
            label = _safe_text(item.get("label"), 120)
            if label:
                lines.append(f"{index}. {label}")
        if lines:
            return "聊天室裡有多組地點清單，請說上一組、最新一組，或直接說店名：\n" + "\n".join(lines)
        return "聊天室裡有多組地點清單，請說上一組、最新一組，或直接說店名。"
    if status == "ambiguous":
        options = resolution.get("candidate_options") or []
        if options:
            lines = []
            for index, item in enumerate(options[:3], start=1):
                if not isinstance(item, dict):
                    continue
                label = _safe_text(item.get("label"), 80)
                address = _safe_text(item.get("address_summary"), 100)
                lines.append(f"{index}. {label}" + (f"（{address}）" if address else ""))
            if lines:
                return "我找到不只一間，請說分店或地址：\n" + "\n".join(lines)
        if count:
            return "我還不能唯一判斷你指的是哪一間。請直接說第一個、第二個或第三個。"
    return "剛才的地點候選目前無法唯一確認，請說店名或重新找一次地點。"


def clear_candidate_set(user_id: str, room_id: str) -> None:
    """Explicitly remove all snapshots for a room (used by cleanup/tests)."""
    key = (str(user_id), str(room_id))
    if _test_mode_enabled():
        with _LOCK:
            _MEMORY.pop(key, None)
        return
    collection = _collection()
    if collection is None:
        return
    try:
        collection.delete_many({"user_id": key[0], "room_id": key[1]})
    except Exception as exc:
        _LOGGER.warning("Unable to clear V3 place presentation snapshots: %s", type(exc).__name__)
