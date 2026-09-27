"""Bounded owner-profile proof for preference writes; no profile creation."""
from matchmaker_agent.preference_bootstrap_contract import BootstrapError
from matchmaker_agent.semantic_rollout_policy import valid_owner_id
from services.semantic_user_eligibility import PROFILE_FIELDS, state_allows_use
from pymongo import timeout


def unique_profile(collection, owner, *, session=None, allow_pending=False, check_state=True):
    if not valid_owner_id(owner):
        raise BootstrapError('invalid_owner', 422)
    try:
        with timeout(3):
            rows = list(collection.find({'user_id': owner}, PROFILE_FIELDS, session=session).limit(2).max_time_ms(2000))
    except Exception:
        raise BootstrapError('profile_eligibility_unavailable', 503) from None
    if len(rows) != 1 or rows[0].get('user_id') != owner or not rows[0].get('_id'):
        raise BootstrapError('profile_missing_or_ambiguous')
    row = dict(rows[0])
    if allow_pending:
        row.pop('preference_bootstrap_pending', None)
    if check_state and not state_allows_use(row):
        raise BootstrapError('profile_not_ready')
    return rows[0]
