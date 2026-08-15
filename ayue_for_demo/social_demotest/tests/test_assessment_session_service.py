import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.assessment_session_service import (
    ASSESSMENT_SESSION_TTL_SECONDS,
    active_assessment_session,
    advance_assessment_session,
    assessment_cancel_choice,
    assessment_commit_choice,
    assessment_ui_projection,
    awaiting_assessment_commit,
    cancel_assessment_session,
    commit_assessment_session,
    handle_assessment_ui_message,
    start_assessment_session,
)


def _active_session(kind="big_five", revision=0, **extra):
    return {
        "session_id": "session-1", "user_id": "owner", "kind": kind,
        "status": "active", "revision": revision, "turn_count": 0,
        "draft": {}, "expires_at": 9_999_999_999,
        **extra,
    }


class AssessmentSessionServiceTests(unittest.TestCase):
    def test_start_creates_24_hour_bounded_draft_without_touching_completed_profile(self):
        profile = {"big_five": {"summary": "原本的性格資料"}}
        with patch("services.assessment_session_service.time.time", return_value=100), \
             patch("services.assessment_session_service.profiles_coll.find_one", return_value=profile), \
             patch("services.assessment_session_service.profiles_coll.update_one", return_value=SimpleNamespace(modified_count=1)) as update:
            outcome = start_assessment_session("owner", "big_five", idempotency_key="confirm-1")

        self.assertEqual(outcome["status"], "started")
        session = update.call_args.args[1]["$set"]["agentic_assessment_session"]
        self.assertEqual(session["user_id"], "owner")
        self.assertEqual(session["revision"], 0)
        self.assertEqual(session["draft"], {})
        self.assertEqual(session["expires_at"], 100 + ASSESSMENT_SESSION_TTL_SECONDS)
        self.assertNotIn("big_five", update.call_args.args[1]["$set"])

    def test_awaiting_commit_blocks_another_start(self):
        profile = {"agentic_assessment_session": {
            **_active_session(), "status": "awaiting_commit", "draft": {"O": 7},
        }}
        with patch("services.assessment_session_service.profiles_coll.find_one", return_value=profile), \
             patch("services.assessment_session_service.profiles_coll.update_one") as update:
            outcome = start_assessment_session("owner", "deep_profile", idempotency_key="new-confirm")
        self.assertEqual(outcome["status"], "already_active")
        update.assert_not_called()

    def test_completion_only_moves_to_awaiting_commit_and_keeps_old_profile(self):
        profile = {
            "agentic_assessment_session": _active_session(revision=4, turn_count=4),
            "big_five": {"summary": "舊資料"}, "deep_profile": {"summary": "保留"},
        }
        model_result = {
            "reply": "你很願意探索，也能自然和人互動。",
            "big_five": {"O": 8, "C": 6, "E": 7, "A": 8, "N": 4, "summary": "好奇且外向"},
            "is_complete": True,
        }
        with patch("services.assessment_session_service.profiles_coll.find_one", return_value=profile), \
             patch("services.assessment_session_service.analyze_big_five", return_value=model_result), \
             patch("services.assessment_session_service.profiles_coll.update_one", return_value=SimpleNamespace(modified_count=1)) as update:
            outcome = advance_assessment_session("owner", "session-1", "我喜歡臨時出發", message_id="message-1")

        self.assertEqual(outcome["status"], "awaiting_commit")
        mutation = update.call_args.args[1]
        self.assertNotIn("big_five", mutation["$set"])
        self.assertEqual(mutation["$set"]["agentic_assessment_session.status"], "awaiting_commit")
        self.assertEqual(mutation["$set"]["agentic_assessment_session.draft"]["O"], 8.0)
        self.assertNotIn("agentic_assessment_session", mutation.get("$unset", {}))

    def test_confirmed_commit_atomically_replaces_only_its_target_profile(self):
        draft = {"O": 8, "C": 6, "E": 7, "A": 8, "N": 4, "summary": "好奇且外向"}
        profile = {
            "agentic_assessment_session": {**_active_session(revision=5), "status": "awaiting_commit", "draft": draft},
            "big_five": {"summary": "舊資料"}, "deep_profile": {"summary": "保留"},
        }
        with patch("services.assessment_session_service.profiles_coll.find_one", return_value=profile), \
             patch("services.assessment_session_service.profiles_coll.update_one", return_value=SimpleNamespace(modified_count=1)) as update:
            outcome = commit_assessment_session("owner", "session-1", expected_revision=5, idempotency_key="commit-1")

        self.assertEqual(outcome["status"], "committed")
        mutation = update.call_args.args[1]
        self.assertEqual(mutation["$set"]["big_five"], draft)
        self.assertNotIn("deep_profile", mutation["$set"])
        self.assertEqual(mutation["$set"]["agentic_assessment_session.status"], "completed")
        self.assertIn("agentic_assessment_session.draft", mutation["$unset"])

    def test_cancel_clears_draft_but_keeps_completed_profile(self):
        profile = {"agentic_assessment_session": _active_session(revision=2), "big_five": {"summary": "保留"}}
        with patch("services.assessment_session_service.profiles_coll.find_one", return_value=profile), \
             patch("services.assessment_session_service.profiles_coll.update_one", return_value=SimpleNamespace(modified_count=1)) as update:
            outcome = cancel_assessment_session("owner", "session-1", "big_five")
        self.assertEqual(outcome["status"], "cancelled")
        mutation = update.call_args.args[1]
        self.assertNotIn("big_five", mutation["$set"])
        self.assertEqual(mutation["$set"]["agentic_assessment_session.status"], "cancelled")
        self.assertIn("agentic_assessment_session.draft", mutation["$unset"])

    def test_expired_session_can_be_replaced_but_unexpired_malformed_session_is_not(self):
        expired = {"agentic_assessment_session": {**_active_session(), "expires_at": 1}, "big_five": {"summary": "保留"}}
        with patch("services.assessment_session_service.time.time", return_value=100), \
             patch("services.assessment_session_service.profiles_coll.find_one", return_value=expired), \
             patch("services.assessment_session_service.profiles_coll.update_one", return_value=SimpleNamespace(modified_count=1)) as update:
            outcome = start_assessment_session("owner", "deep_profile", idempotency_key="confirm-2")
        self.assertEqual(outcome["status"], "started")
        self.assertNotIn("big_five", update.call_args.args[1]["$set"])

        malformed = {"agentic_assessment_session": {"session_id": "old", "kind": "big_five", "status": "active"}}
        with patch("services.assessment_session_service.profiles_coll.find_one", return_value=malformed), \
             patch("services.assessment_session_service.profiles_coll.update_one") as update:
            outcome = start_assessment_session("owner", "deep_profile", idempotency_key="confirm-3")
        self.assertEqual(outcome["status"], "already_active")
        update.assert_not_called()

    def test_duplicate_message_never_reanalyzes(self):
        profile = {"agentic_assessment_session": _active_session(last_message_id="message-1")}
        with patch("services.assessment_session_service.profiles_coll.find_one", return_value=profile), \
             patch("services.assessment_session_service.analyze_big_five") as analyze:
            outcome = advance_assessment_session("owner", "session-1", "同一則回答", message_id="message-1")
        self.assertEqual(outcome["status"], "duplicate")
        analyze.assert_not_called()

    def test_provider_error_keeps_active_draft_unchanged(self):
        profile = {"agentic_assessment_session": _active_session()}
        with patch("services.assessment_session_service.profiles_coll.find_one", return_value=profile), \
             patch("services.assessment_session_service.analyze_big_five", side_effect=RuntimeError("secret")), \
             patch("services.assessment_session_service.profiles_coll.update_one") as update:
            outcome = advance_assessment_session("owner", "session-1", "回答")
        self.assertEqual(outcome["status"], "provider_error")
        self.assertEqual(outcome["session_state"], "active")
        self.assertEqual(outcome["revision"], 0)
        self.assertNotIn("secret", outcome["reply"])
        update.assert_not_called()

    def test_ui_projection_prefers_the_current_typed_draft_over_the_old_completed_profile(self):
        profile = {
            "big_five": {"O": 3, "summary": "舊資料"},
            "agentic_assessment_session": {
                **_active_session(revision=2),
                "draft": {"O": 8, "summary": "本輪草稿"},
            },
        }
        projection = assessment_ui_projection(profile, "big_five")
        self.assertEqual(projection["assessment_state"], "active")
        self.assertEqual(projection["assessment_revision"], 2)
        self.assertEqual(projection["value"]["summary"], "本輪草稿")

    def test_ui_initialization_starts_the_session_without_treating_control_text_as_an_answer(self):
        session = _active_session()
        with patch("services.assessment_session_service.profiles_coll.find_one", return_value={}), \
             patch("services.assessment_session_service.start_assessment_session", return_value={
                 "status": "started", "reply": "第一題。", "session": session,
             }), \
             patch("services.assessment_session_service.advance_assessment_session") as advance:
            outcome = handle_assessment_ui_message(
                "owner", "big_five", "我想開始測驗", initialize=True,
            )
        self.assertEqual(outcome["status"], "started")
        self.assertEqual(outcome["reply"], "第一題。")
        advance.assert_not_called()

    def test_active_assessment_never_passes_completed_profile_or_context_to_the_analyzer(self):
        basic_profile = {
            "agentic_assessment_session": _active_session(),
            "initial_interest": "看電影", "current_context": "下週去旅行",
            "big_five": {"summary": "既有資料"},
        }
        deep_profile = {
            "agentic_assessment_session": _active_session(kind="deep_profile"),
            "current_context": "下週去旅行", "big_five": {"summary": "既有資料"},
        }
        with patch("services.assessment_session_service.profiles_coll.find_one", return_value=basic_profile), \
             patch("services.assessment_session_service.analyze_big_five", return_value={
                 "reply": "下一題。", "big_five": {"O": 7}, "is_complete": False,
             }) as basic_analyze, \
             patch("services.assessment_session_service.profiles_coll.update_one", return_value=SimpleNamespace(modified_count=1)):
            self.assertEqual(
                advance_assessment_session("owner", "session-1", "我的回答", message_id="basic-message")["status"],
                "continued",
            )
        self.assertIsNone(basic_analyze.call_args.args[3])

        with patch("services.assessment_session_service.profiles_coll.find_one", return_value=deep_profile), \
             patch("services.assessment_session_service.analyze_deep_profile", return_value={
                 "reply": "下一題。", "deep_profile": {"values": ["誠實"]}, "is_complete": False,
             }) as deep_analyze, \
             patch("services.assessment_session_service.profiles_coll.update_one", return_value=SimpleNamespace(modified_count=1)):
            self.assertEqual(
                advance_assessment_session("owner", "session-1", "我的回答", message_id="deep-message")["status"],
                "continued",
            )
        self.assertIsNone(deep_analyze.call_args.args[3])

    def test_concurrent_answer_loser_cannot_overwrite_newer_revision(self):
        profile = {"agentic_assessment_session": _active_session(revision=2)}
        newer = {"agentic_assessment_session": _active_session(revision=3, last_message_id="other-message")}
        with patch("services.assessment_session_service.profiles_coll.find_one", side_effect=[profile, newer]), \
             patch("services.assessment_session_service.analyze_big_five", return_value={
                 "reply": "下一題。", "big_five": {"O": 7}, "is_complete": False,
             }), \
             patch("services.assessment_session_service.profiles_coll.update_one", return_value=SimpleNamespace(modified_count=0)):
            outcome = advance_assessment_session("owner", "session-1", "第一個分頁的回答", message_id="message-1")
        self.assertEqual(outcome["status"], "stale")

    def test_duplicate_commit_returns_the_existing_completed_state(self):
        completed = {"agentic_assessment_session": {**_active_session(revision=6), "status": "completed"}}
        with patch("services.assessment_session_service.profiles_coll.find_one", side_effect=[None, completed]):
            outcome = commit_assessment_session("owner", "session-1", expected_revision=5, idempotency_key="commit-1")
        self.assertEqual(outcome["status"], "already_committed")

    def test_cancel_losing_to_a_concurrent_commit_reports_completed_instead_of_cancelled(self):
        waiting = {
            "agentic_assessment_session": {
                **_active_session(revision=5), "status": "awaiting_commit",
                "draft": {"O": 8, "C": 6, "E": 7, "A": 8, "N": 4, "summary": "新草稿"},
            }
        }
        completed = {
            "agentic_assessment_session": {
                **_active_session(revision=6), "status": "completed",
            }
        }
        with patch("services.assessment_session_service.profiles_coll.find_one", side_effect=[waiting, completed]), \
             patch("services.assessment_session_service.profiles_coll.update_one", return_value=SimpleNamespace(modified_count=0)):
            outcome = cancel_assessment_session("owner", "session-1", "big_five")
        self.assertEqual(outcome["status"], "stale")
        self.assertEqual(outcome["session_state"], "completed")
        self.assertEqual(outcome["revision"], 6)

    def test_commit_losing_to_a_concurrent_cancel_reports_cancelled_instead_of_waiting(self):
        cancelled = {
            "agentic_assessment_session": {
                **_active_session(revision=6), "status": "cancelled",
            }
        }
        with patch("services.assessment_session_service.profiles_coll.find_one", side_effect=[None, cancelled]):
            outcome = commit_assessment_session(
                "owner", "session-1", expected_revision=5, idempotency_key="commit-race",
            )
        self.assertEqual(outcome["status"], "stale")
        self.assertEqual(outcome["session_state"], "cancelled")
        self.assertEqual(outcome["revision"], 6)

    def test_active_and_commit_protocols_are_closed(self):
        self.assertTrue(active_assessment_session({"agentic_assessment_session": _active_session()}))
        self.assertTrue(awaiting_assessment_commit({"agentic_assessment_session": {**_active_session(), "status": "awaiting_commit"}}))
        self.assertTrue(assessment_cancel_choice("結束測驗"))
        self.assertEqual(assessment_commit_choice("確認"), "confirm")
        self.assertEqual(assessment_commit_choice("取消"), "cancel")
        self.assertEqual(assessment_commit_choice("我不太確定"), "none")


if __name__ == "__main__":
    unittest.main()
