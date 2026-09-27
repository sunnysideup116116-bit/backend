"""Shared pure account/profile/Graph state policy; no I/O or rollout membership."""


def state_allows_use(row):
    if not isinstance(row, dict):
        return False
    for key in ('enabled', 'active', 'is_active'):
        if key in row and row[key] is not None and row[key] is not True:
            return False
    for key in ('disabled', 'is_disabled', 'blocked', 'is_blocked'):
        if key in row and row[key] is not None and row[key] is not False:
            return False
    status = row.get('status')
    if status not in (None, 'active', 'enabled'):
        return False
    return not row.get('deleted_at') and not row.get('preference_bootstrap_pending') and not row.get('preference_projection_pending')
