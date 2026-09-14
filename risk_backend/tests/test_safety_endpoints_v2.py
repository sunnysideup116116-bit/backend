"""HTTP-level block, unblock, list, and person-report behavior."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import risk_detection as api
from app.models.schemas import BlockUserRequest, ReportUserRequest, UnblockUserRequest


def test_block_first_and_repeat_are_idempotent(monkeypatch):
    save = AsyncMock(
        side_effect=[
            {"ok": True, "already": False, "error": None},
            {"ok": True, "already": True, "error": None},
        ]
    )
    monkeypatch.setattr(api.chat_log_service, "save_user_block", save)
    request = BlockUserRequest(blocker_id="alice", blocked_id="bob")

    first = asyncio.run(api.block_user(request))
    repeated = asyncio.run(api.block_user(request))

    assert first["already"] is False
    assert repeated["already"] is True
    assert save.await_count == 2


@pytest.mark.parametrize(
    ("error", "status"),
    [("self_block", 400), ("collection_missing", 503)],
)
def test_block_errors_map_to_contract_status(monkeypatch, error, status):
    monkeypatch.setattr(
        api.chat_log_service,
        "save_user_block",
        AsyncMock(return_value={"ok": False, "already": False, "error": error}),
    )
    request = BlockUserRequest(blocker_id="alice", blocked_id="alice")

    with pytest.raises(HTTPException) as raised:
        asyncio.run(api.block_user(request))

    assert raised.value.status_code == status


def test_unblock_success_and_missing_record(monkeypatch):
    remove = AsyncMock(
        side_effect=[
            {"ok": True, "error": None},
            {"ok": False, "error": "not_found"},
        ]
    )
    monkeypatch.setattr(api.chat_log_service, "remove_user_block", remove)
    request = UnblockUserRequest(blocker_id="alice", blocked_id="bob")

    assert asyncio.run(api.unblock_user(request))["status"] == "ok"
    with pytest.raises(HTTPException) as raised:
        asyncio.run(api.unblock_user(request))
    assert raised.value.status_code == 404


def test_block_list_store_failure_is_503(monkeypatch):
    monkeypatch.setattr(
        api.chat_log_service,
        "get_user_block_sets",
        AsyncMock(return_value={"ok": False, "error": "collection_missing"}),
    )
    with pytest.raises(HTTPException) as raised:
        asyncio.run(api.list_blocked_users("alice"))
    assert raised.value.status_code == 503


@pytest.mark.parametrize("category", sorted(api.REPORT_REASON_CATEGORIES))
def test_each_formal_report_category_creates_pending_record(monkeypatch, category):
    save = AsyncMock(
        return_value={"ok": True, "error": None, "report_id": f"report-{category}"}
    )
    monkeypatch.setattr(api.chat_log_service, "save_user_report", save)
    request = ReportUserRequest(
        reporter_id="alice",
        reported_id="bob",
        reason_category=category,
        detail_text="detail",
    )

    response = asyncio.run(api.report_user(request))

    assert response["review_status"] == "pending"
    assert response["report_id"] == f"report-{category}"
    save.assert_awaited_once()


def test_invalid_report_category_is_400(monkeypatch):
    save = AsyncMock()
    monkeypatch.setattr(api.chat_log_service, "save_user_report", save)
    request = ReportUserRequest(
        reporter_id="alice",
        reported_id="bob",
        reason_category="spam",
    )
    with pytest.raises(HTTPException) as raised:
        asyncio.run(api.report_user(request))
    assert raised.value.status_code == 400
    save.assert_not_awaited()


def test_report_detail_is_limited_to_2000_characters():
    with pytest.raises(ValidationError):
        ReportUserRequest(
            reporter_id="alice",
            reported_id="bob",
            reason_category="other",
            detail_text="x" * 2001,
        )
