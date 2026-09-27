"""Neo4j owner-node serialization shared by every runtime memory writer."""
import math
from neo4j.exceptions import ResultNotSingleError
from matchmaker_agent.semantic_rollout_policy import valid_owner_id
from matchmaker_agent.owner_eligibility_contract import state_allows_use


class PreferenceFenceError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def lock_preferences(tx, owner, *, expected_revision=None, source_created_at=None,
                     bootstrap_operation=None, require_existing=False):
    # SET with a property dependency obtains the node write lock BEFORE the
    # read. No increment on duplicates; transaction rollback undoes first-use 0.
    match = "MATCH" if require_existing else "MERGE"
    if not valid_owner_id(owner):
        raise PreferenceFenceError('preference_owner_invalid')
    try:
        row = tx.run(f"""
        {match} (u:User {{id:$owner}})
        SET u.preference_revision=coalesce(u.preference_revision,0)
        RETURN u.preference_revision AS revision,
               coalesce(u.preference_epoch,0) AS epoch,
               coalesce(u.preference_epoch_at,0) AS epoch_at,
               u.preference_projection_pending AS pending,
               u.enabled AS enabled,u.active AS active,u.is_active AS is_active,
               u.disabled AS disabled,u.is_disabled AS is_disabled,
               u.blocked AS blocked,u.is_blocked AS is_blocked,
               u.deleted_at AS deleted_at,u.status AS status
    """, owner=owner).single(strict=True)
    except ResultNotSingleError:
        raise PreferenceFenceError('preference_owner_missing_or_duplicate') from None
    if row is None:
        raise PreferenceFenceError("preference_owner_missing")
    state = dict(row)
    if bootstrap_operation is None and not state_allows_use(state):
        raise PreferenceFenceError('preference_owner_not_ready')
    revision = state.get("revision", 0)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise PreferenceFenceError("preference_revision_invalid")
    if expected_revision is not None and revision != expected_revision:
        raise PreferenceFenceError("stale_preview")
    if state.get("pending") and state["pending"] != bootstrap_operation:
        raise PreferenceFenceError("preference_projection_pending")
    if bootstrap_operation is None and state.get("epoch_at", 0):
        if (not isinstance(source_created_at, (int, float)) or isinstance(source_created_at, bool)
                or not math.isfinite(source_created_at)
                or source_created_at <= state["epoch_at"]):
            raise PreferenceFenceError("preference_source_superseded")
    return {"revision": revision, "epoch": state.get("epoch", 0),
            "epoch_at": state.get("epoch_at", 0), "pending": state.get("pending")}


def bump_preferences(tx, owner):
    tx.run("""MATCH (u:User {id:$owner})
        SET u.preference_revision=coalesce(u.preference_revision,0)+1
    """, owner=owner).consume()


def fence_error_result(exc):
    return {"status": "error", "error_code": exc.code,
            "retryable": exc.code == "preference_projection_pending"}
