"""One profile creation boundary; never creates indexes or repairs old duplicates.

Database invariant requires the separately approved UNIQUE(user_id) index.
Fast path stays one update. Only a confirmed same-owner unique-key collision may
recover: bounded identity-only read, then ONE update-only write to that exact _id.
No transport/unknown-outcome retry, domain predicate relaxation, or raw logging.
"""
from collections.abc import Mapping
from copy import deepcopy

from pymongo import timeout
from pymongo.errors import DuplicateKeyError

PROFILE_RECOVERY_SECONDS = 2.0
_OPERATORS = frozenset({"$set", "$setOnInsert", "$unset", "$inc", "$max", "$min",
                        "$push", "$addToSet", "$pull", "$pullAll", "$pop", "$currentDate"})


class ProfileWriteConflict(RuntimeError):
    """Static code only; no account, document, key value or payload in errors."""


def _owner(query):
    if not isinstance(query, Mapping):
        raise ValueError("profile_filter_required")
    owner = query.get("user_id")
    if not isinstance(owner, str) or not owner or owner != owner.strip():
        raise ValueError("profile_owner_required")
    return owner


def _validate_update(update, owner):
    if not isinstance(update, Mapping) or not update:
        raise ValueError("profile_operator_update_required")
    for operator, fields in update.items():
        if operator not in _OPERATORS or not isinstance(fields, Mapping):
            raise ValueError("profile_operator_update_required")
        for name, value in fields.items():
            if not isinstance(name, str):
                raise ValueError("profile_field_required")
            if name.split(".", 1)[0] in {"user_id", "_id"}:
                if not (operator == "$setOnInsert" and name == "user_id" and value == owner):
                    raise ValueError("profile_identity_immutable")


def _same_owner_duplicate(error, owner):
    details = error.details
    # Do not parse errmsg: it can contain private data and cannot prove scope.
    return (error.code == 11000 and isinstance(details, Mapping)
            and details.get("keyPattern") == {"user_id": 1}
            and details.get("keyValue") == {"user_id": owner})


def update_profile(collection, query, update, *, upsert=False, session=None):
    """Preserve domain operators/predicates while making creation race-safe.

    upsert=True is allowed only with the exact owner identity filter. For CAS /
    conditional updates pass upsert=False; a miss stays a miss and never inserts.
    On an aborted transaction, propagate to its owning transaction coordinator;
    never run recovery within that transaction or replay any preceding effects.
    """
    owner = _owner(query)
    if not isinstance(upsert, bool):
        raise ValueError("profile_upsert_boolean_required")
    if upsert and set(query) != {"user_id"}:
        raise ValueError("conditional_profile_upsert_forbidden")
    _validate_update(update, owner)
    bound_query, bound_update = deepcopy(dict(query)), deepcopy(dict(update))
    options = {"session": session} if session is not None else {}
    in_transaction = session is not None and session.in_transaction
    try:
        return collection.update_one(bound_query, bound_update, upsert=upsert, **options)
    except DuplicateKeyError as exc:
        if (not upsert or not _same_owner_duplicate(exc, owner)
                or in_transaction):
            raise

    # Nested PyMongo CSOT never extends an already-shorter enclosing deadline.
    # Two seconds covers the entire recovery, not two seconds per operation.
    with timeout(PROFILE_RECOVERY_SECONDS):
        winners = list(collection.find({"user_id": owner}, {"_id": 1, "user_id": 1}, **options).limit(2))
        if len(winners) != 1 or winners[0].get("user_id") != owner or "_id" not in winners[0]:
            raise ProfileWriteConflict("profile_race_winner_unverified")
        # Bind the document we actually read. Deletion/recreation cannot redirect
        # the loser into a new profile or make it attempt another insert.
        result = collection.update_one({**bound_query, "_id": winners[0]["_id"]},
                                       bound_update, upsert=False, **options)
        if result.matched_count != 1:
            raise ProfileWriteConflict("profile_race_winner_changed")
        return result


def ensure_profile(collection, owner, defaults=None, *, session=None):
    """Insert-only initialization: existing rich fields always win over defaults."""
    if defaults is not None and not isinstance(defaults, Mapping):
        raise ValueError("profile_defaults_required")
    fields = dict(defaults or {})
    if "user_id" in fields and fields["user_id"] != owner:
        raise ValueError("profile_identity_immutable")
    fields["user_id"] = owner
    return update_profile(collection, {"user_id": owner}, {"$setOnInsert": fields},
                          upsert=True, session=session)
