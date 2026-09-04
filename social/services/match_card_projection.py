"""Read-only projection of saved proposal cards from canonical, owner-bound state."""
from copy import deepcopy
import re
import unicodedata
from typing import Any, Callable

from bson import ObjectId

from database import matches_coll
from services.match_state_service import derive_match_stage

PROPOSAL_CARD_EVENTS = {"match_proposal", "incoming_match_interest"}
NicknameLookup = Callable[[str], str]


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
    if document.get("status") == "draft" and user_id != first:
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
    }, {"status": 1, "from_user": 1, "to_user": 1, "proposal_revision": 1, "last_decision.action": 1})} if ids else {}
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
        if not state:
            metadata.update(canonical_status="unavailable", stage="unavailable", actions=[])
            metadata["counterparty_nickname"] = ""
            for candidate in metadata.get("matches") or []:
                if isinstance(candidate, dict):
                    candidate.pop("counterparty_nickname", None)
            continue
        metadata.update(canonical_status=state["status"], **{k: v for k, v in state.items() if k != "status"})
        if nickname_lookup is not None:
            metadata["counterparty_nickname"] = proposal_counterparty_nickname(document, user_id, lookup_once)
        if state["status"] not in {"draft", "pending"}:
            metadata["actions"] = []
        for candidate in metadata.get("matches") or []:
            if isinstance(candidate, dict) and str(candidate.get("match_id") or "") == match_id:
                candidate.update(state)
                if nickname_lookup is not None:
                    candidate["counterparty_nickname"] = metadata["counterparty_nickname"]
    return projected
