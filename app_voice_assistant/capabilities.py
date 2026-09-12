"""Shared base action contract; domain-specific validation stays in contracts.py."""
import json
from pathlib import Path

CATALOG = json.loads(Path(__file__).with_name('capabilities.json').read_text())
ACTIONS = CATALOG['actions']
TARGET_PERMISSIONS = CATALOG['target_permissions']


def available_actions(requested, permissions, *, targets=False):
    def allowed(name):
        action = ACTIONS[name]
        required = action.get('target_permissions') if targets else None
        required = required if required is not None else [action['permission']]
        return all(permission is None or permissions.get(permission) is True for permission in required)
    return [name for name in requested if isinstance(name, str) and name in ACTIONS and allowed(name)][:40]
