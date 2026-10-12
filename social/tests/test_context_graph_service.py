import time
import unittest
from unittest.mock import Mock, patch

from services.context_graph_service import (
    build_context_projection_payload,
    enqueue_context_projection,
    reconcile_context_projections,
    retry_pending_context_projections,
    sync_current_context_projection,
)


class _Cursor(list):
    def sort(self, *_args, **_kwargs):
        return self

    def limit(self, value):
        return _Cursor(self[:value])


NOW = 1_000_000.0


def _profile(**overrides):
    profile = {
        "user_id": "owner",
        "current_context": "今天成果被教授打槍，感到煩躁",
        "current_context_revision": 11,
        "recent_context_state": {"fields": {
            "activity": {"value": "爬山"},
            "destination": {"value": "壽山"},
            "timing": {"value": "今天"},
            "event": {"value": "成果被教授打槍"},
            "feeling": {"value": "煩躁"},
            "ignored": {"value": "不投影"},
        }},
        "recent_context_expires_at": time.time() + 100,
        "recent_context_updated_at": NOW - 5,
    }
    profile.update(overrides)
    return profile


def _ok_post():
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"status": "success", "applied": True}
    return response


class ContextGraphServiceTests(unittest.TestCase):
    def test_payload_projects_the_recent_situation_fields(self):
        profile = _profile()
        payload = build_context_projection_payload(profile, now=NOW)
        self.assertEqual(payload["revision"], 11)
        self.assertEqual(payload["summary"], "今天成果被教授打槍,感到煩躁")
        self.assertEqual(
            set(payload["fields"]),
            {"activity", "destination", "timing", "event", "feeling"},
        )
        self.assertEqual(payload["expires_at"], profile["recent_context_expires_at"])

    def test_expired_profile_builds_an_explicit_clear_payload(self):
        payload = build_context_projection_payload(
            _profile(
                recent_context_expires_at=99.0,
                recent_context_state={"fields": {"event": {"value": "受挫"}}},
            ),
            now=100.0,
        )
        self.assertEqual(payload["summary"], "")
        self.assertEqual(payload["fields"], {})
        self.assertEqual(payload["expires_at"], 100.0)

    @patch("services.context_graph_service.profiles_coll")
    @patch("services.context_graph_service.CONTEXT_GRAPH_OUTBOX")
    def test_enqueue_upserts_one_pending_owner_row(self, outbox, profiles):
        profiles.find_one.return_value = _profile()
        payload = enqueue_context_projection("owner")
        self.assertEqual(payload["revision"], 11)
        args, kwargs = outbox.update_one.call_args
        self.assertEqual(args[0], {"user_id": "owner"})
        self.assertEqual(args[1]["$set"]["status"], "pending")
        self.assertEqual(args[1]["$set"]["revision"], 11)
        self.assertIn("event", args[1]["$set"]["fields"])
        self.assertTrue(kwargs["upsert"])

    @patch("services.context_graph_service.requests.post")
    @patch("services.context_graph_service.profiles_coll")
    @patch("services.context_graph_service.CONTEXT_GRAPH_OUTBOX")
    def test_successful_sync_posts_situation_and_marks_applied(self, outbox, profiles, post):
        profiles.find_one.return_value = _profile()
        post.return_value = _ok_post()
        result = sync_current_context_projection("owner")
        self.assertEqual(result["status"], "success")
        self.assertEqual(post.call_args.args[0], "http://127.0.0.1:9001/api/context/situation")
        body = post.call_args.kwargs["json"]
        self.assertNotIn("concepts", body)
        self.assertEqual(body["fields"]["feeling"], "煩躁")
        self.assertEqual(body["summary"], "今天成果被教授打槍,感到煩躁")
        self.assertEqual(outbox.update_one.call_count, 2)
        self.assertEqual(outbox.update_one.call_args.args[1]["$set"]["status"], "applied")
        marker_args = profiles.update_one.call_args
        self.assertEqual(marker_args.args[0], {"user_id": "owner", "context_projection_dirty": 11})
        self.assertEqual(marker_args.args[1], {"$unset": {"context_projection_dirty": ""}})

    @patch("services.context_graph_service.requests.post")
    @patch("services.context_graph_service.profiles_coll")
    @patch("services.context_graph_service.CONTEXT_GRAPH_OUTBOX")
    def test_clear_projection_marks_outbox_cleared(self, outbox, profiles, post):
        profiles.find_one.return_value = _profile(
            current_context="", recent_context_expires_at=1.0,
            recent_context_state={"fields": {}},
        )
        post.return_value = _ok_post()
        result = sync_current_context_projection("owner")
        self.assertEqual(result["status"], "success")
        self.assertEqual(post.call_args.kwargs["json"]["summary"], "")
        self.assertEqual(outbox.update_one.call_args.args[1]["$set"]["status"], "cleared")

    @patch("services.context_graph_service.requests.post", side_effect=OSError("offline"))
    @patch("services.context_graph_service.profiles_coll")
    @patch("services.context_graph_service.CONTEXT_GRAPH_OUTBOX")
    def test_failed_projection_remains_pending(self, outbox, profiles, _post):
        profiles.find_one.return_value = _profile()
        with patch("builtins.print"):
            result = sync_current_context_projection("owner")
        self.assertEqual(result["status"], "pending")
        self.assertEqual(outbox.update_one.call_args.args[1]["$set"]["status"], "pending")
        self.assertEqual(outbox.update_one.call_args.args[1]["$set"]["last_error"], "OSError")

    @patch("services.context_graph_service.sync_current_context_projection")
    @patch("services.context_graph_service.CONTEXT_GRAPH_OUTBOX")
    def test_retry_worker_is_bounded(self, outbox, sync):
        outbox.find.return_value = _Cursor([
            {"user_id": "u1"}, {"user_id": "u2"}, {"user_id": "u3"}, {"user_id": "u4"},
        ])
        sync.return_value = {"status": "success"}
        result = retry_pending_context_projections(limit=3)
        self.assertEqual(result, {"attempted": 3, "applied": 3})
        self.assertEqual(sync.call_count, 3)

    @patch("services.context_graph_service.sync_current_context_projection")
    @patch("services.context_graph_service.profiles_coll")
    @patch("services.context_graph_service.CONTEXT_GRAPH_OUTBOX")
    @patch("services.context_graph_service.time.time", return_value=100.0)
    def test_reconcile_only_processes_marked_and_expired_rows(
        self, _time, outbox, profiles, sync,
    ):
        outbox.find.return_value = _Cursor([{"user_id": "expired"}])
        profiles.find.return_value = _Cursor([{"user_id": "dirty"}])
        sync.return_value = {"status": "success"}
        stats = reconcile_context_projections()
        self.assertEqual(stats["expired"], 1)
        self.assertEqual(stats["dirty"], 1)
        self.assertEqual(sorted(call.args[0] for call in sync.call_args_list), ["dirty", "expired"])
        self.assertEqual(
            profiles.find.call_args.args[0],
            {"context_projection_dirty": {"$exists": True}},
        )

    @patch("services.context_graph_service.sync_current_context_projection")
    @patch("services.context_graph_service.profiles_coll")
    @patch("services.context_graph_service.CONTEXT_GRAPH_OUTBOX")
    def test_reconcile_never_sweeps_unmarked_profiles(self, outbox, profiles, sync):
        outbox.find.return_value = _Cursor([])
        profiles.find.return_value = _Cursor([])
        stats = reconcile_context_projections()
        self.assertEqual(stats, {"expired": 0, "dirty": 0, "repaired": 0})
        sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
