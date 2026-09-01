"""Gateway contracts for all eight public risk action routes."""

from unittest.mock import MagicMock

import pytest
import requests
from fastapi import HTTPException
from starlette.requests import Request

from routers import risk_actions


def _request(query: str) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/pair/risk_state",
            "raw_path": b"/api/pair/risk_state",
            "query_string": query.encode(),
            "headers": [],
            "client": ("test", 123),
            "server": ("test", 80),
        }
    )


def test_all_eight_gateway_features_forward_to_distinct_backend_routes(monkeypatch):
    post_calls = []
    get_calls = []

    def fake_post(path, payload):
        post_calls.append((path, payload))
        return {"status": "ok", "path": path}

    def fake_get(path, *, params=None, raw_query=""):
        get_calls.append((path, params, raw_query))
        if path == "blocks":
            return {"blocked_user_ids": ["bob"], "excluded_user_ids": ["bob", "carol"]}
        return {"risk_level": "safe", "remaining_cooldown": 0}

    monkeypatch.setattr(risk_actions, "_post", fake_post)
    monkeypatch.setattr(risk_actions, "_get", fake_get)

    risk_actions.submit_risk_feedback(
        risk_actions.RiskFeedbackRequest(
            triggered_by_msg_id="m1",
            role="receiver",
            feedback="comfortable",
        )
    )
    risk_actions.submit_sender_appeal(
        risk_actions.SenderAppealProxyRequest(
            triggered_by_msg_id="m1",
            sender_id="alice",
            appeal_text="context",
        )
    )
    risk_actions.submit_receiver_report(
        risk_actions.ReceiverReportProxyRequest(
            triggered_by_msg_id="m1",
            receiver_id="bob",
            report_text="detail",
        )
    )
    state = risk_actions.get_risk_state(
        _request("conversation_id=alice_bob&user_id=alice")
    )
    risk_actions.block_user(
        risk_actions.BlockUserProxyRequest(
            blocker_id="alice",
            blocked_id="bob",
            conversation_id="alice_bob",
        )
    )
    risk_actions.unblock_user(
        risk_actions.UnblockUserProxyRequest(blocker_id="alice", blocked_id="bob")
    )
    blocks = risk_actions.list_blocked_users("alice")
    risk_actions.report_user(
        risk_actions.ReportUserProxyRequest(
            reporter_id="alice",
            reported_id="bob",
            reason_category="harassment",
        )
    )

    assert [path for path, _ in post_calls] == [
        "feedback",
        "appeal",
        "report",
        "block",
        "unblock",
        "report-user",
    ]
    assert get_calls == [
        ("state", None, "conversation_id=alice_bob&user_id=alice"),
        ("blocks", {"user_id": "alice"}, ""),
    ]
    assert state["remaining_cooldown"] == 0
    assert blocks == {
        "user_id": "alice",
        "blocked_user_ids": ["bob"],
        "count": 1,
    }
    assert "excluded_user_ids" not in blocks


def test_backend_4xx_detail_is_preserved(monkeypatch):
    response = MagicMock(status_code=400)
    response.json.return_value = {"detail": "cannot block yourself"}
    monkeypatch.setattr(risk_actions.requests, "post", lambda *_, **__: response)

    with pytest.raises(HTTPException) as raised:
        risk_actions._post("block", {"blocker_id": "alice", "blocked_id": "alice"})

    assert raised.value.status_code == 400
    assert raised.value.detail == "cannot block yourself"


def test_backend_connection_failure_becomes_503(monkeypatch):
    def unavailable(*_, **__):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(risk_actions.requests, "post", unavailable)
    with pytest.raises(HTTPException) as raised:
        risk_actions._post("report-user", {"reporter_id": "alice"})

    assert raised.value.status_code == 503
    assert "暫時無法連線" in raised.value.detail
