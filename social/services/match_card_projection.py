"""Read-only projection of saved proposal cards from canonical, owner-bound state."""
from copy import deepcopy
import re
import unicodedata
from typing import Any, Callable

from bson import ObjectId

from database import matches_coll
from services.match_state_service import derive_match_stage, has_verified_acceptance

PROPOSAL_CARD_EVENTS = {"match_proposal", "incoming_match_interest"}
NicknameLookup = Callable[[str], str]


def _safe_match_basis(document: dict) -> dict:
    """Project the shared evidence contract without participant identifiers."""
    raw = document.get("match_basis")
    if not isinstance(raw, dict):
        return {}
    level = str(raw.get("level") or "insufficient")
    if level not in {"direct", "adjacent", "insufficient"}:
        level = "insufficient"

    def bounded(value: Any, limit: int = 4) -> list[str]:
        values = value if isinstance(value, list) else [value]
        result: list[str] = []
        for item in values:
            text = re.sub(r"\s+", " ", str(item or "")).strip()[:120]
            if text and text not in result:
                result.append(text)
            if len(result) >= limit:
                break
        return result

    return {
        "level": level,
        "need_evidence": bounded(raw.get("need_evidence")),
        "counterparty_evidence": bounded(raw.get("counterparty_evidence")),
        "concrete_overlap": bounded(raw.get("concrete_overlap")),
        "cannot_infer": bounded(raw.get("cannot_infer")),
    }


def safe_proposal_nickname(value: Any, other_user_id: str) -> str:
    """Allow a bounded public nickname, never an account ID or contact address."""
    if not isinstance(value, str):
        return ""
    name = unicodedata.normalize("NFKC", value).strip()
    if (
        not name or name == "對方" or name.casefold() == other_user_id.casefold()
        or (len(other_user_id) >= 8 and other_user_id.casefold() in name.casefold())
        or any(unicodedata.category(char) in {"Cc", "Cf"} for char in name)
        or re.search(r"seed_user_|demo_user|user_id", name, re.IGNORECASE)
        or re.search(r"@|https?://|www\.", name, re.IGNORECASE)
        or re.search(r"\d[\d\s()+-]{5,}\d", name)
        or re.fullmatch(r"[0-9a-fA-F-]{20,}", name)
    ):
        return ""
    return re.sub(r"\s+", " ", name)[:30]


def proposal_counterparty_nickname(
    document: dict, user_id: str, lookup: NicknameLookup,
) -> str:
    """Resolve the other participant from canonical state, not card metadata."""
    first, second = document.get("from_user"), document.get("to_user")
    if user_id not in {first, second}:
        return ""
    if document.get("status") != "accepted" or not has_verified_acceptance(document):
        return ""
    other = second if user_id == first else first
    if not isinstance(other, str) or not other or other == user_id:
        return ""
    try:
        return safe_proposal_nickname(lookup(other), other)
    except Exception:
        # A failed public-name read must not change the proposal's state or
        # invite a client to fall back to a private account identifier.
        return ""


def proposal_card_state(document: dict, user_id: str) -> dict:
    if user_id not in {document.get("from_user"), document.get("to_user")}:
        return {}
    action = (document.get("last_decision") or {}).get("action")
    return {
        "status": document.get("status"),
        "stage": derive_match_stage(document, user_id),
        "proposal_revision": int(document.get("proposal_revision", 0) or 0),
        "decision_action": action if action in {"accept", "decline", "cancel"} else "",
    }


def project_match_card_history(
    messages: list[dict], user_id: str, *, collection: Any = None,
    nickname_lookup: NicknameLookup | None = None,
) -> list[dict]:
    """Never rewrite saved messages or revive proposals while reading history."""
    collection = matches_coll if collection is None else collection
    entries = []
    for index, message in enumerate(messages):
        metadata = message.get("metadata") or {}
        if not isinstance(metadata, dict) or metadata.get("event_type") not in PROPOSAL_CARD_EVENTS:
            continue
        candidates = metadata.get("matches") or []
        first = candidates[0] if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict) else {}
        match_id = str(metadata.get("match_id") or first.get("match_id") or "")
        entries.append((index, match_id))
    if not entries:
        return messages
    ids = list({ObjectId(mid) for _, mid in entries if ObjectId.is_valid(mid)})
    documents = {str(doc["_id"]): doc for doc in collection.find({
        "_id": {"$in": ids}, "$or": [{"from_user": user_id}, {"to_user": user_id}],
    }, {"status": 1, "from_user": 1, "to_user": 1, "proposal_revision": 1,
        "last_decision": 1, "state_history": 1, "match_basis": 1, "source_room_id": 1,
        "source_room_title": 1, "source_summary": 1, "match_source_kind": 1,
        "search_context.invitation_topic": 1, "proposal_namespace": 1})} if ids else {}
    projected = deepcopy(messages)
    nickname_cache: dict[str, str] = {}

    def lookup_once(other_user_id: str) -> str:
        if other_user_id not in nickname_cache:
            try:
                nickname_cache[other_user_id] = nickname_lookup(other_user_id) if nickname_lookup else ""
            except Exception:
                nickname_cache[other_user_id] = ""
        return nickname_cache[other_user_id]

    for index, match_id in entries:
        metadata = projected[index]["metadata"]
        document = documents.get(match_id, {})
        state = proposal_card_state(document, user_id)
        if document.get("status") != "accepted" or not has_verified_acceptance(document):
            projected[index] = anonymous_proposal_message(projected[index])
            metadata = projected[index]["metadata"]
        if not state:
            metadata.update(canonical_status="unavailable", stage="unavailable", actions=[])
            metadata["counterparty_nickname"] = ""
            for candidate in metadata.get("matches") or []:
                if isinstance(candidate, dict):
                    candidate.pop("counterparty_nickname", None)
            continue
        metadata.update(canonical_status=state["status"], **{k: v for k, v in state.items() if k != "status"})
        if document:
            basis = _safe_match_basis(document)
            if basis:
                metadata["match_basis"] = basis
            source_room_id = str(document.get("source_room_id") or "")
            if source_room_id:
                metadata["source_room_id"] = source_room_id
            if document.get("source_room_title"):
                metadata["source_room_title"] = str(document["source_room_title"])[:60]
            if document.get("source_summary"):
                metadata["source_summary"] = str(document["source_summary"])[:180]
            source_kind = str(document.get("match_source_kind") or "").strip()
            if source_kind:
                metadata["match_source_kind"] = source_kind[:32]
            topic = str((document.get("search_context") or {}).get("invitation_topic") or "").strip()
            if topic:
                metadata["invitation_topic"] = topic[:80]
            metadata["focus_match_id"] = match_id
        if nickname_lookup is not None:
            metadata["counterparty_nickname"] = proposal_counterparty_nickname(document, user_id, lookup_once)
        if state["status"] not in {"draft", "pending"}:
            metadata["actions"] = []
        for candidate in metadata.get("matches") or []:
            if isinstance(candidate, dict) and str(candidate.get("match_id") or "") == match_id:
                candidate.update(state)
                if nickname_lookup is not None:
                    candidate["counterparty_nickname"] = metadata["counterparty_nickname"]
        if document.get("status") != "accepted" or not has_verified_acceptance(document):
            # Canonical source/basis hydration above can reintroduce a saved
            # name. Redact it using the original aliases before returning.
            other_id = document.get("to_user") if document.get("from_user") == user_id else document.get("from_user")
            current_name = lookup_once(other_id) if other_id and nickname_lookup else ""
            projected[index] = anonymous_proposal_message(projected[index], aliases=[
                messages[index].get("metadata"), {"counterparty_nickname": current_name},
            ])
    return projected


def anonymous_proposal_message(message: dict, *, aliases: Any = None) -> dict:
    """Redact saved UI-only aliases before history can enter an AI context."""
    identity_fields = {
        "counterparty_nickname", "counterparty_name", "matched_user_name",
        "other_name", "nickname", "display_name",
    }
    names: set[str] = set()

    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in identity_fields and isinstance(item, str) and item.strip():
                    names.add(item.strip())
                elif isinstance(item, (dict, list)):
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    collect(message.get("metadata") or {})
    collect(aliases)

    def clean(value, key=""):
        if key in identity_fields:
            return ""
        if isinstance(value, dict):
            return {field: clean(item, field) for field, item in value.items()}
        if isinstance(value, list):
            return [clean(item, key) for item in value]
        if isinstance(value, str) and not (
            key.endswith("_id") or key.endswith("_ids") or key in {"id", "ref"}
        ):
            for name in sorted(names, key=len, reverse=True):
                pattern = re.escape(name)
                if re.fullmatch(r"[A-Za-z0-9]+", name):
                    pattern = rf"(?<![A-Za-z0-9]){pattern}(?![A-Za-z0-9])"
                value = re.sub(pattern, "對方", value)
        return value

    return clean(message)
