"""Shared Appwrite KB contract for the 2026-08-31 safety actions."""

from __future__ import annotations

import json


ACTION_OPTIONS_ATTRIBUTE = {
    "key": "action_options",
    "type": "string",
    "size": 4000,
    "required": False,
    "default": None,
}

RESTRICTED_OPTIONS = [
    {"action": "block_user", "label": "封鎖"},
    {"action": "report_user", "label": "檢舉"},
    {"action": "leave_conversation", "label": "停止對話"},
]

BLOCKED_OPTIONS = [
    {"action": "dismiss", "label": "繼續對話"},
    {"action": "block_user", "label": "封鎖"},
    {"action": "report_user", "label": "檢舉"},
    {"action": "leave_conversation", "label": "結束對話"},
]

EXEMPT_OPTIONS = [
    {"action": "block_user", "label": "封鎖"},
    {"action": "report_user", "label": "檢舉"},
    {"action": "leave_conversation", "label": "停止對話"},
]


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


SAFETY_ACTION_UPDATES = {
    "restrict_receiver_options": {
        "action_options": _json(RESTRICTED_OPTIONS),
    },
    "block_receiver_notice": {
        "action_options": _json(BLOCKED_OPTIONS),
    },
    "receiver_state_notice": {
        "action_type": "show_safety_info_card",
        "ui_behavior": _json({
            "show_options": True,
            "show_feedback_buttons": False,
            "allow_report_text": True,
            "mascot": "heart",
            "display_throttle_seconds": 300,
            "cooldown": 0,
            "require_ack": False,
        }),
        "action_options": _json(EXEMPT_OPTIONS),
    },
}
