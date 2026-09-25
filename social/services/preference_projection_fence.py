"""Mongo-local owner fence. This is not a Graph+Mongo transaction."""
from contextlib import contextmanager
import math

from database import db, profiles_coll
from matchmaker_agent.preference_bootstrap_contract import BootstrapError


@contextmanager
def projection_write(owner, operation_id=None, *, source_created_at=None):
    with db.client.start_session() as session:
        with session.start_transaction(max_commit_time_ms=5000):
            allowed = [{"preference_bootstrap_pending": {"$exists": False}}]
            if operation_id:
                allowed.append({"preference_bootstrap_pending": operation_id})
            row = profiles_coll.find_one_and_update(
                {"user_id": owner, "$or": allowed},
                {"$inc": {"preference_projection_serial": 1}}, session=session,
            )
            if not row:
                raise BootstrapError("preference_projection_pending_or_profile_missing")
            cutoff = row.get("preference_projection_epoch_at", 0)
            if not operation_id and cutoff and (
                not isinstance(source_created_at, (int, float)) or isinstance(source_created_at, bool)
                or not math.isfinite(source_created_at) or source_created_at <= cutoff
            ):
                raise BootstrapError("preference_source_superseded")
            yield session
