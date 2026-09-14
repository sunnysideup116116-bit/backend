import unittest
from unittest.mock import Mock, patch

from services.context_graph_service import (
    _projection_concepts,
    retry_pending_context_projections,
    sync_current_context_projection,
)


class _Cursor(list):
    def sort(self, *_args, **_kwargs):
        return self

    def limit(self, value):
        return _Cursor(self[:value])


class ContextGraphServiceTests(unittest.TestCase):
    def test_projection_uses_only_activity_and_destination(self):
        state = {"fields": {
            "activity": {"value": "爬山"},
            "destination": {"value": "壽山"},
            "timing": {"value": "下週六"},
            "companion_intent": {"value": "找人同行"},
        }}
        concepts = _projection_concepts(state)
        self.assertEqual([item["label"] for item in concepts], ["爬山", "壽山"])
        self.assertTrue(all(item["key"].startswith("concept_") for item in concepts))

    @patch("services.context_graph_service.CONTEXT_GRAPH_OUTBOX")
    @patch("services.context_graph_service.requests.post")
    @patch("services.context_graph_service.profiles_coll")
    def test_successful_projection_marks_outbox_applied(self, profiles, post, outbox):
        profiles.find_one.return_value = {
            "recent_context_state": {"fields": {"activity": {"value": "爬山"}}},
            "recent_context_expires_at": 2_000_000_000,
            "current_context_revision": 4,
        }
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"status": "success", "concept_count": 1}
        post.return_value = response
        result = sync_current_context_projection("owner")
        self.assertEqual(result["status"], "success")
        self.assertEqual(outbox.update_one.call_count, 2)
        self.assertEqual(outbox.update_one.call_args.args[0], {"user_id": "owner", "revision": 4})
        self.assertEqual(post.call_args.kwargs["json"]["concepts"][0]["label"], "爬山")
        self.assertEqual(post.call_args.kwargs["json"]["revision"], 4)

    @patch("services.context_graph_service.CONTEXT_GRAPH_OUTBOX")
    @patch("services.context_graph_service.requests.post", side_effect=OSError("offline"))
    @patch("services.context_graph_service.profiles_coll")
    def test_failed_projection_remains_pending(self, profiles, _post, outbox):
        profiles.find_one.return_value = {
            "recent_context_state": {"fields": {}},
            "current_context_revision": 5,
        }
        with patch("builtins.print"):
            result = sync_current_context_projection("owner")
        self.assertEqual(result["status"], "pending")
        self.assertEqual(outbox.update_one.call_args.args[1]["$set"]["status"], "pending")

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


if __name__ == "__main__":
    unittest.main()
