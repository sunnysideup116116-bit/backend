"""Neo4j owner-node serialization shared by every runtime memory writer."""
import math


class PreferenceFenceError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def lock_preferences(tx, owner, *, expected_revision=None, source_created_at=None,
                     bootstrap_operation=None, require_existing=False):
    # SET with a property dependency obtains the node write lock BEFORE the
    # read. No increment on duplicates; transaction rollback undoes first-use 0.
    match = "MATCH" if require_existing else "MERGE"
    row = tx.run(f"""
        {match} (u:User {{id:$owner}})
        SET u.preference_revision=coalesce(u.preference_revision,0)
        RETURN u.preference_revision AS revision,
               coalesce(u.preference_epoch,0) AS epoch,
               coalesce(u.preference_epoch_at,0) AS epoch_at,
               u.preference_projection_pending AS pending
    """, owner=owner).single()
    if row is None:
        raise PreferenceFenceError("preference_owner_missing")
    state = dict(row)
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
