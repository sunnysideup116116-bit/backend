"""Room-title recovery without network calls, model credentials, or MongoDB."""

from copy import deepcopy
import re
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from services import ai_room_service as titles


ROOM = "ai_room::owner::title-job"


def _matches(document, query):
    for key, expected in query.items():
        if key == "$and":
            if not all(_matches(document, item) for item in expected):
                return False
        elif key == "$or":
            if not any(_matches(document, item) for item in expected):
                return False
        elif isinstance(expected, dict):
            for operator, value in expected.items():
                if operator == "$exists" and (key in document) != value:
                    return False
                if operator == "$lte" and (key not in document or document[key] > value):
                    return False
                if operator == "$regex" and (
                    not isinstance(document.get(key), str) or re.search(value, document[key]) is None
                ):
                    return False
        elif document.get(key) != expected:
            return False
    return True


class RoomStore:
    """Small atomic Mongo double: match filters, then apply set/unset."""

    def __init__(self, **overrides):
        self.document = {
            "room_id": ROOM, "user_id": "owner", "needs_title": True, "title": None,
            **overrides,
        }
        self.lock = threading.Lock()
        self.finished = threading.Event()

    def find_one(self, query, projection=None):
        with self.lock:
            return deepcopy(self.document) if _matches(self.document, query) else None

    def _apply(self, update):
        self.document.update(update.get("$set", {}))
        for key in update.get("$unset", {}):
            self.document.pop(key, None)
        if "title" in update.get("$set", {}) or "title_retry_after" in update.get("$set", {}):
            self.finished.set()

    def find_one_and_update(self, query, update, **kwargs):
        with self.lock:
            if not _matches(self.document, query):
                return None
            self._apply(update)
            return deepcopy(self.document)

    def update_one(self, query, update, **kwargs):
        with self.lock:
            if not _matches(self.document, query):
                return SimpleNamespace(modified_count=0, matched_count=0)
            self._apply(update)
            return SimpleNamespace(modified_count=1, matched_count=1)


def _install_store(monkeypatch, **overrides):
    rooms = RoomStore(**overrides)
    messages = MagicMock()
    messages.find_one.return_value = {"content": "週末喜歡在河邊散步"}
    monkeypatch.setattr(titles, "ai_rooms_coll", rooms)
    monkeypatch.setattr(titles, "messages_coll", messages)
    return rooms, messages


def test_success_after_the_old_five_second_window_is_persisted(monkeypatch):
    from services import ai_service

    rooms, _ = _install_store(monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(titles.time, "monotonic", lambda: clock[0])
    calls = []

    def slow_success(prompt, **kwargs):
        calls.append(kwargs)
        assert kwargs["tools"] == []
        assert kwargs["deadline_monotonic"] == 160.0
        # The real provider is still inside its deadline after six seconds.
        clock[0] += 6.0
        return SimpleNamespace(content="河邊散步")

    monkeypatch.setattr(ai_service, "generate_chat_completion_with_tools", slow_success)
    titles.ensure_room_title(ROOM, "owner", "週末喜歡在河邊散步")
    assert len(calls) == 1
    assert rooms.document["title"] == "河邊散步"
    assert rooms.document["needs_title"] is False
    assert "title_job_token" not in rooms.document
    assert "title_lease_until" not in rooms.document


def test_repeated_history_reads_return_without_waiting_and_share_one_job(monkeypatch):
    rooms, _ = _install_store(monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def blocked_provider(_prompt):
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return "河邊散步"

    monkeypatch.setattr(titles, "_generate_title_with_retry", blocked_provider)
    try:
        titles.maybe_backfill_title(ROOM, "owner")
        # This point is reached while the worker is still waiting; GET history
        # does not have to wait for generation before it can return messages.
        assert entered.wait(1)
        assert not release.is_set()
        titles.maybe_backfill_title(ROOM, "owner")
        assert titles.queue_room_title(ROOM, "owner", "另一個開場") is False
        assert len(calls) == 1
    finally:
        release.set()
        assert rooms.finished.wait(2)
    assert rooms.document["title"] == "河邊散步"


def test_failure_releases_lease_and_cools_down_before_a_later_retry(monkeypatch):
    rooms, _ = _install_store(monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(titles.time, "time", lambda: clock[0])
    generator = MagicMock(side_effect=RuntimeError("offline"))
    monkeypatch.setattr(titles, "_generate_title_with_retry", generator)
    titles.ensure_room_title(ROOM, "owner", "散步")
    assert rooms.document["needs_title"] is True
    assert rooms.document["title_retry_after"] == 220.0
    assert "title_job_token" not in rooms.document
    assert "title_lease_until" not in rooms.document
    titles.ensure_room_title(ROOM, "owner", "散步")
    assert generator.call_count == 1
    clock[0] = 221.0
    generator.side_effect = None
    generator.return_value = "散步日常"
    titles.ensure_room_title(ROOM, "owner", "散步")
    assert generator.call_count == 2
    assert rooms.document["needs_title"] is False
    assert "title_retry_after" not in rooms.document


def test_nonpending_rooms_never_queue_or_lookup_old_user_messages(monkeypatch):
    rooms, messages = _install_store(monkeypatch, needs_title=False, title="已完成的標題")
    generator = MagicMock()
    monkeypatch.setattr(titles, "_generate_title_with_retry", generator)
    assert titles.queue_room_title(ROOM, "owner", "開場") is False
    titles.ensure_room_title(ROOM, "owner", "開場")
    titles.maybe_backfill_title(ROOM, "owner")
    generator.assert_not_called()
    messages.find_one.assert_not_called()
    assert rooms.document["title"] == "已完成的標題"


def test_old_blank_room_with_user_history_repairs_missing_pending_flag(monkeypatch):
    rooms, _ = _install_store(monkeypatch, needs_title=False)
    monkeypatch.setattr(titles, "_generate_title_with_retry", lambda _: "舊對話標題")
    titles.maybe_backfill_title(ROOM, "owner")
    assert rooms.finished.wait(2)
    assert rooms.document["title"] == "舊對話標題"
    assert rooms.document["needs_title"] is False


def test_new_empty_room_stays_idle_without_a_fake_generation_state(monkeypatch):
    rooms, messages = _install_store(monkeypatch, needs_title=False)
    messages.find_one.return_value = None
    generator = MagicMock()
    monkeypatch.setattr(titles, "_generate_title_with_retry", generator)
    titles.maybe_backfill_title(ROOM, "owner")
    generator.assert_not_called()
    assert rooms.document["needs_title"] is False
    assert "title_lease_until" not in rooms.document


def test_expired_process_lease_is_recoverable_but_active_lease_is_not(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(titles.time, "time", lambda: clock[0])
    rooms, _ = _install_store(monkeypatch, title_job_token="old-process", title_lease_until=105.0)
    generator = MagicMock(return_value="重新接續")
    monkeypatch.setattr(titles, "_generate_title_with_retry", generator)
    titles.ensure_room_title(ROOM, "owner", "開場")
    generator.assert_not_called()
    clock[0] = 106.0
    titles.ensure_room_title(ROOM, "owner", "開場")
    generator.assert_called_once()
    assert rooms.document["title"] == "重新接續"


def test_late_worker_cannot_overwrite_a_manual_rename(monkeypatch):
    rooms, _ = _install_store(monkeypatch)

    def rename_while_waiting(_prompt):
        rooms.update_one({"room_id": ROOM}, {"$set": {"title": "使用者取的名字", "needs_title": False}})
        return "模型取的名字"

    monkeypatch.setattr(titles, "_generate_title_with_retry", rename_while_waiting)
    titles.ensure_room_title(ROOM, "owner", "開場")
    assert rooms.document["title"] == "使用者取的名字"
    assert rooms.document["needs_title"] is False


def test_first_message_preserves_an_existing_manual_title(monkeypatch):
    rooms, messages = _install_store(monkeypatch, title="先取好的名字", needs_title=False)
    messages.count_documents.return_value = 1
    assert titles.mark_first_message_for_title(ROOM, "owner") is False
    assert rooms.document["title"] == "先取好的名字"
    assert rooms.document["needs_title"] is False


def test_first_message_can_still_generate_a_pending_placeholder_title(monkeypatch):
    rooms, messages = _install_store(monkeypatch, title="新對話", needs_title=True)
    messages.count_documents.return_value = 1
    monkeypatch.setattr(titles, "_generate_title_with_retry", lambda _: "散步日常")
    assert titles.mark_first_message_for_title(ROOM, "owner") is True
    titles.ensure_room_title(ROOM, "owner", "週末喜歡在河邊散步")
    assert rooms.document["title"] == "散步日常"
    assert rooms.document["needs_title"] is False


def test_rename_between_message_count_and_first_message_flag_wins(monkeypatch):
    rooms, messages = _install_store(monkeypatch, needs_title=False)

    def count_with_concurrent_rename(_query):
        rooms.update_one({"room_id": ROOM}, {"$set": {"title": "剛取好的名字", "needs_title": False}})
        return 1

    messages.count_documents.side_effect = count_with_concurrent_rename
    assert titles.mark_first_message_for_title(ROOM, "owner") is False
    assert rooms.document["title"] == "剛取好的名字"
    assert rooms.document["needs_title"] is False


def test_failed_thread_start_releases_the_claim(monkeypatch):
    rooms, _ = _install_store(monkeypatch)
    thread = MagicMock()
    thread.start.side_effect = RuntimeError("unable to start")
    monkeypatch.setattr(titles.threading, "Thread", MagicMock(return_value=thread))
    assert titles.queue_room_title(ROOM, "owner", "開場") is False
    assert rooms.document["needs_title"] is True
    assert "title_job_token" not in rooms.document
    assert "title_retry_after" in rooms.document


def test_empty_results_retry_serially_with_one_shared_deadline(monkeypatch):
    from services import ai_service

    generator = MagicMock(return_value=SimpleNamespace(content=""))
    monkeypatch.setattr(ai_service, "generate_chat_completion_with_tools", generator)
    assert titles._generate_title_with_retry("開場") == ""
    assert generator.call_count == titles.TITLE_MAX_ATTEMPTS
    deadlines = {call.kwargs["deadline_monotonic"] for call in generator.call_args_list}
    assert len(deadlines) == 1


def test_exhausted_provider_deadline_does_not_start_another_request(monkeypatch):
    from services import ai_service

    clock = [100.0]
    monkeypatch.setattr(titles.time, "monotonic", lambda: clock[0])

    def deadline_exhausted(_prompt, **kwargs):
        clock[0] = kwargs["deadline_monotonic"]
        raise TimeoutError("provider deadline")

    generator = MagicMock(side_effect=deadline_exhausted)
    monkeypatch.setattr(ai_service, "generate_chat_completion_with_tools", generator)
    assert titles._generate_title_with_retry("開場") == ""
    generator.assert_called_once()
