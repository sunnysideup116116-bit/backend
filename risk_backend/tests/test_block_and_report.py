"""Block/report persistence and intervention action contract tests."""

import asyncio
from unittest.mock import patch

import pytest

from app.core.intervention_engine import InterventionEngine


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


TEMPLATES = {
    "restricted": [
        {
            "template_id": "restrict_receiver_options",
            "primary_risk_type": "any",
            "action_type": "show_safety_info_card",
            "message_template": {"title": "保護", "body": "…"},
            "ui_behavior": {"show_options": True},
            "action_options": RESTRICTED_OPTIONS,
        },
        {
            "template_id": "restrict_sender_general",
            "primary_risk_type": "any",
            "action_type": "show_modal_warning",
            "message_template": {"title": "暫停", "body": "…"},
            "ui_behavior": {"cooldown": 60, "require_ack": True},
        },
    ],
    "blocked": [
        {
            "template_id": "block_receiver_notice",
            "primary_risk_type": "any",
            "action_type": "show_blocked_notice",
            "message_template": {"body": "…"},
            "ui_behavior": {"show_options": True},
            "action_options": __import__("json").dumps(BLOCKED_OPTIONS, ensure_ascii=False),
        },
        {
            "template_id": "block_sender_final",
            "primary_risk_type": "any",
            "action_type": "block_message",
            "message_template": {"title": "未送出", "body": "…"},
            "ui_behavior": {"cooldown": 1800, "require_ack": True},
        },
    ],
    "warning": [
        {
            "template_id": "receiver_info_warning",
            "primary_risk_type": "any",
            "action_type": "show_safety_info_card",
            "message_template": {"body": "…"},
            "ui_behavior": {"show_options": False, "show_feedback_buttons": True},
        },
    ],
}


def _directive(level):
    with patch(
        "app.core.intervention_engine.KBService.get_interventions_by_level",
        side_effect=lambda value: TEMPLATES.get(value, []),
    ):
        return asyncio.run(InterventionEngine().execute(
            risk_level=level,
            risk_state={
                "harassment": 0.7,
                "coercion": 0.0,
                "manipulation": 0.0,
                "sexual_boundary": 0.0,
                "emotional_pressure": 0.0,
            },
            diagnosis={},
            conv_id="conversation",
            sender_id="alice",
            receiver_id="bob",
            msg_id="message",
            decision_reason="normal",
        ))


def test_restricted_and_blocked_options_preserve_order():
    assert _directive("restricted")["receiver_directive"]["action_options"] == RESTRICTED_OPTIONS
    assert _directive("blocked")["receiver_directive"]["action_options"] == BLOCKED_OPTIONS


def test_warning_and_sender_have_no_action_options():
    assert "action_options" not in _directive("warning")["receiver_directive"]
    assert "action_options" not in _directive("restricted")["sender_directive"]


def test_directives_target_exact_users():
    command = _directive("restricted")
    assert command["sender_directive"]["target_user_id"] == "alice"
    assert command["receiver_directive"]["target_user_id"] == "bob"


@pytest.mark.parametrize(
    "raw",
    ["not-json", {}, [1], [{"action": "unknown", "label": "No"}]],
)
def test_malformed_or_unknown_options_are_ignored(raw):
    assert InterventionEngine._parse_action_options(raw) == []


class FakeDocuments:
    def __init__(self, documents):
        self.documents = documents


class FakeDocument:
    def __init__(self, document_id, data):
        self.id = document_id
        self.data = data


def _service(rows=None):
    from app.services.chat_log_service import ChatLogService

    service = ChatLogService.__new__(ChatLogService)
    service.db_id = "database"
    service.created = []
    service.deleted = []
    table = list(rows or [])

    class Database:
        def list_documents(self, _db, collection, queries=None):
            text = " ".join(str(query) for query in (queries or []))
            if collection == "user_blocks" and "blocker_id" in text and "blocked_id" not in text:
                selected = [row for row in table if row.get("blocker_id") == "alice"]
            elif collection == "user_blocks" and "blocked_id" in text and "blocker_id" not in text:
                selected = [row for row in table if row.get("blocked_id") == "alice"]
            else:
                selected = table
            return FakeDocuments([
                FakeDocument(f"document-{index}", row)
                for index, row in enumerate(selected)
            ])

        def create_document(self, _db, collection, _document_id, data):
            service.created.append((collection, data))
            return FakeDocument("created", data)

        def delete_document(self, _db, collection, document_id):
            service.deleted.append((collection, document_id))

    service.db = Database()
    return service


def test_block_is_idempotent_and_self_block_is_rejected():
    service = _service([{"blocker_id": "alice", "blocked_id": "bob"}])
    result = asyncio.run(service.save_user_block("alice", "bob"))
    assert result == {"ok": True, "already": True, "error": None}
    assert not service.created
    assert asyncio.run(service.save_user_block("alice", "alice"))["error"] == "self_block"


def test_bidirectional_and_outgoing_block_sets_are_separate():
    service = _service([
        {"blocker_id": "alice", "blocked_id": "bob"},
        {"blocker_id": "carol", "blocked_id": "alice"},
    ])
    result = asyncio.run(service.get_user_block_sets("alice"))
    assert result["blocked_user_ids"] == ["bob"]
    assert result["excluded_user_ids"] == ["bob", "carol"]


def test_reports_are_not_deduplicated_and_default_to_pending():
    service = _service([{"reporter_id": "alice", "reported_id": "bob"}])
    result = asyncio.run(service.save_user_report("alice", "bob", "harassment"))
    assert result["ok"] is True
    collection, data = service.created[-1]
    assert collection == "user_reports"
    assert data["status"] == "pending"


def test_self_report_is_rejected():
    service = _service()
    assert asyncio.run(service.save_user_report("alice", "alice", "other"))["error"] == "self_report"
