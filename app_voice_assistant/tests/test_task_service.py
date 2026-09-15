from datetime import datetime
import time

import mongomock
import pytest

from app_voice_assistant.task_service import VoiceTaskService


def service():
    db = mongomock.MongoClient().db
    return VoiceTaskService(
        db["app_voice_task_batches"], db["app_voice_tasks"], enabled=True,
    )


def operation(key, *, depends_on=None, risk="read"):
    return {
        "operation_key": key,
        "depends_on": depends_on or [],
        "arguments": {"query": key},
        "action": {
            "capability_id": "memory.query",
            "title": key,
            "execution_kind": "inline_device",
            "risk": risk,
            "cancellable": True,
        },
    }


def test_batch_dependencies_and_revisioned_projection():
    tasks = service()
    created = tasks.create_batch(
        user_id="u1", session_id="s1",
        operations=[operation("first"), operation("second", depends_on=["first"])],
        now=10,
    )
    assert [row["operation_key"] for row in tasks.ready_queued_tasks("u1", created.batch["batch_id"])] == ["first"]
    first = created.tasks[0]
    tasks.set_action(first["task_id"], action_id="a1")
    done = tasks.complete_action("u1", "a1", success=True, message="done", now=11)
    assert done["status"] == "completed"
    assert done["revision"] == 3
    assert isinstance(done["expires_at"], datetime)
    assert [row["operation_key"] for row in tasks.ready_queued_tasks("u1", created.batch["batch_id"])] == ["second"]


def test_cycle_and_cross_owner_cancel_fail_closed():
    tasks = service()
    with pytest.raises(ValueError, match="dependency_cycle"):
        tasks.create_batch(
            user_id="u1", session_id="s1",
            operations=[
                operation("one", depends_on=["two"]),
                operation("two", depends_on=["one"]),
            ],
        )
    created = tasks.create_batch(
        user_id="u1", session_id="s1", operations=[operation("one")],
    )
    with pytest.raises(ValueError, match="not_found"):
        tasks.cancel("u2", created.tasks[0]["task_ref"])
    with pytest.raises(ValueError, match="operation_count"):
        tasks.create_batch(
            user_id="u1", session_id="s1",
            operations=[operation(f"op-{index}") for index in range(9)],
        )


def test_cancel_is_idempotent_and_write_becomes_non_cancellable_after_submit():
    tasks = service()
    created = tasks.create_batch(
        user_id="u1", session_id="s1", operations=[operation("one")],
    )
    ref = created.tasks[0]["task_ref"]
    first = tasks.cancel("u1", ref)
    second = tasks.cancel("u1", ref)
    assert first[0]["status"] == second[0]["status"] == "cancelled"

    write = tasks.create_batch(
        user_id="u1", session_id="s1", operations=[operation("write", risk="write")],
    ).tasks[0]
    tasks.set_action(
        write["task_id"], action_id="write-action", cancellable=False,
    )
    unchanged = tasks.cancel("u1", write["task_ref"])
    assert unchanged[0]["status"] == "waiting_device"
    assert unchanged[0]["cancellable"] is False


def test_serialized_operations_gain_implicit_order_but_reads_stay_independent():
    tasks = service()
    created = tasks.create_batch(
        user_id="u1", session_id="s1",
        operations=[
            operation("read-one"),
            operation("open", risk="control"),
            operation("read-two"),
            operation("write-one", risk="write"),
            operation("write-two", risk="write"),
        ],
    )
    rows = {row["operation_key"]: row for row in created.tasks}
    assert rows["read-one"]["depends_on"] == []
    assert rows["read-two"]["depends_on"] == []
    assert rows["open"]["depends_on"] == []
    assert rows["write-one"]["depends_on"] == ["open"]
    assert rows["write-two"]["depends_on"] == ["write-one"]


def test_worker_lease_recovery_and_confirmation_expiry_are_revisioned():
    tasks = service()
    created = tasks.create_batch(
        user_id="u1", session_id="s1", operations=[operation("read")], now=10,
    )
    task = created.tasks[0]
    claimed = tasks.claim(task["task_id"], stage="waiting_worker", now=10)
    assert claimed["status"] == "running"
    assert claimed["attempt"] == 1
    assert tasks.active_count("u1", risk="read") == 1
    assert tasks.recover_expired_leases(now=131) == 1
    recovered = tasks.get_task("u1", task["task_id"])
    assert recovered["status"] == "queued"
    assert recovered["revision"] == 3

    waiting = tasks.create_batch(
        user_id="u1", session_id="s1",
        operations=[operation("write", risk="write")], now=20,
    ).tasks[0]
    tasks.set_action(
        waiting["task_id"], action_id="confirm-id",
        status="waiting_confirmation", stage="waiting_confirmation", now=20,
    )
    assert tasks.expire_waiting_confirmations(now=51) == 1
    expired = tasks.get_task("u1", waiting["task_id"])
    assert expired["status"] == "waiting_input"
    assert expired["error_code"] == "confirmation_expired"


def test_cancel_requested_result_is_discarded_and_finishes_cancelled():
    tasks = service()
    row = tasks.create_batch(
        user_id="u1", session_id="s1", operations=[operation("read")],
    ).tasks[0]
    tasks.claim(row["task_id"], stage="working")
    tasks.update(
        row["task_id"], action_id="action-one", expected_statuses={"running"},
    )
    cancelled = tasks.cancel("u1", row["task_ref"])
    assert cancelled[0]["status"] == "cancel_requested"
    final = tasks.complete_action(
        "u1", "action-one", success=True, message="late",
    )
    assert final["status"] == "cancelled"
    assert final["result_summary"] == "任務已取消；完成結果已捨棄。"


def test_reconnect_never_replays_an_unknown_write_and_expires_device_reads():
    tasks = service()
    write = tasks.create_batch(
        user_id="u1", session_id="old",
        operations=[operation("write", risk="write")], now=10,
    ).tasks[0]
    tasks.set_action(
        write["task_id"], action_id="write-action", cancellable=False, now=11,
    )
    batches = tasks.recover_for_session("u1")
    recovered = tasks.get_task("u1", write["task_id"])
    assert recovered["status"] == "waiting_input"
    assert recovered["error_code"] == "device_result_unknown"
    assert write["batch_id"] in batches

    read = tasks.create_batch(
        user_id="u1", session_id="old",
        operations=[operation("read")], now=20,
    ).tasks[0]
    tasks.set_action(read["task_id"], action_id="read-action", now=20)
    assert tasks.expire_waiting_device(now=621) == 1
    expired = tasks.get_task("u1", read["task_id"])
    assert expired["status"] == "expired"
    assert isinstance(expired["expires_at"], datetime)


def test_waiting_input_projects_structured_form_and_supports_validated_edit():
    tasks = service()
    row = tasks.create_batch(
        user_id="u1", session_id="s1", operations=[operation("memory")],
    ).tasks[0]
    waiting = tasks.update(
        row["task_id"], status="waiting_input", stage="needs_input",
        error_code="validation_failed", result_summary="需要修改。",
        expected_statuses={"queued"},
    )
    projected = tasks.project(waiting)
    assert projected["retryable"] is True
    assert projected["interaction"]["kind"] == "form"
    assert projected["interaction"]["fields"][0]["key"] == "query"
    model_view = tasks.list_tasks(
        "u1", "all", include_edit_values=False,
    )[0]
    assert "value" not in model_view["interaction"]["fields"][0]

    edited = tasks.provide_input(
        "u1", row["task_ref"], {"query": "咖啡"},
        expected_revision=waiting["revision"],
    )
    assert edited["status"] == "queued"
    assert edited["arguments"] == {"query": "咖啡"}
    assert edited["interaction"] == {}


def test_retry_and_dismiss_are_owner_and_revision_scoped():
    tasks = service()
    row = tasks.create_batch(
        user_id="u1", session_id="s1", operations=[operation("read")],
    ).tasks[0]
    failed = tasks.update(
        row["task_id"], status="failed", stage="failed",
        error_code="network_error", expected_statuses={"queued"},
    )
    with pytest.raises(ValueError, match="revision_stale"):
        tasks.retry("u1", row["task_ref"], expected_revision=1)
    retried = tasks.retry(
        "u1", row["task_ref"], expected_revision=failed["revision"],
    )
    assert retried["status"] == "queued"
    reopened_batch = tasks.batches.find_one({"batch_id": row["batch_id"]})
    assert reopened_batch["status"] == "active"
    assert "expires_at" not in reopened_batch
    waiting = tasks.update(
        row["task_id"], status="waiting_input", stage="needs_input",
        expected_statuses={"queued"},
    )
    dismissed = tasks.dismiss(
        "u1", row["task_ref"], expected_revision=waiting["revision"],
    )
    assert dismissed["status"] == "cancelled"


def test_completed_visual_setting_has_one_time_durable_undo():
    tasks = service()
    instant = time.time()
    operation_row = operation("setting", risk="write")
    operation_row["arguments"] = {"key": "ui.dark_mode", "enabled": True}
    operation_row["action"] = {
        "capability_id": "settings.set",
        "title": "開啟深色模式",
        "execution_kind": "inline_device",
        "risk": "write",
        "cancellable": True,
    }
    row = tasks.create_batch(
        user_id="u1", session_id="s1", operations=[operation_row], now=instant,
    ).tasks[0]
    tasks.set_action(row["task_id"], action_id="setting-action", now=instant)
    completed = tasks.complete_action(
        "u1", "setting-action", success=True, message="done", now=instant + 1,
    )
    assert tasks.project(completed)["undoable"] is True
    undo = tasks.create_undo_batch(
        "u1", row["task_ref"], session_id="s1",
        expected_revision=completed["revision"], now=instant + 2,
    )
    assert undo.tasks[0]["arguments"] == {
        "key": "ui.dark_mode", "enabled": False,
    }
    original = tasks.get_by_ref("u1", row["task_ref"])
    assert tasks.project(original)["undoable"] is False
    with pytest.raises(ValueError, match="not_undoable"):
        tasks.create_undo_batch(
            "u1", row["task_ref"], session_id="s1", now=instant + 3,
        )


def test_unknown_write_result_cannot_be_retried_but_can_be_dismissed():
    tasks = service()
    row = tasks.create_batch(
        user_id="u1", session_id="s1",
        operations=[operation("write", risk="write")],
    ).tasks[0]
    waiting = tasks.update(
        row["task_id"], status="waiting_input", stage="result_unknown",
        error_code="result_unknown", expected_statuses={"queued"},
    )
    assert tasks.project(waiting)["retryable"] is False
    with pytest.raises(ValueError, match="not_retryable"):
        tasks.retry("u1", row["task_ref"])
    assert tasks.dismiss("u1", row["task_ref"])["status"] == "cancelled"

    expired_row = tasks.create_batch(
        user_id="u1", session_id="s1",
        operations=[operation("expired-write", risk="write")], now=20,
    ).tasks[0]
    tasks.set_action(
        expired_row["task_id"], action_id="submitted-write",
        cancellable=False, now=20,
    )
    assert tasks.expire_waiting_device(now=621) == 1
    expired = tasks.get_by_ref("u1", expired_row["task_ref"])
    assert tasks.project(expired)["retryable"] is False
