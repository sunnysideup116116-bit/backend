import unittest
from unittest.mock import MagicMock, patch
from bson.objectid import ObjectId

from services import memory_outbox_service as outbox
from services.memory_service import MemoryWriteError
from services.message_use_service import metadata_for_use
from matchmaker_agent.concept_identity import canonicalize_concept


def record(*, attempts=1):
    return {
        "_id": "outbox-1", "lease_token": "lease-1", "attempt_count": attempts,
        "user_id": "owner", "memories": [{**canonicalize_concept("咖啡").as_dict(), "stance": "like"}],
        "surface": "profile", "message_id": str(ObjectId("64b64c8f0000000000000101")), "match_id": None,
    }


class MemoryOutboxServiceTests(unittest.TestCase):
    def test_claim_uses_a_lease_and_increments_attempt_count(self):
        with patch.object(outbox.uuid, "uuid4", return_value=MagicMock(hex="lease")), \
             patch.object(outbox.MEMORY_OUTBOX, "find_one_and_update", return_value=None) as claim:
            outbox._claim_one(now=100.0)
        args = claim.call_args
        pending_clause = args.args[0]["$or"][0]["$and"][1]["$or"]
        self.assertIn({"next_attempt_at": None}, pending_clause)
        self.assertEqual(args.args[1]["$set"]["status"], "processing")
        self.assertEqual(args.args[1]["$set"]["lease_until"], 190.0)
        self.assertEqual(args.args[1]["$inc"], {"attempt_count": 1})

    def test_success_marks_the_exact_lease_applied(self):
        claimed = record()
        with patch.object(outbox, "_claim_one", side_effect=[claimed, None]), \
             patch.object(outbox.messages_coll, "find_one", return_value={
                 "metadata": {"message_use": {
                     "version": "message-use-v1", "use": "ordinary",
                 }},
             }), \
             patch("services.memory_service.apply_profile_memory_proposals",
                   return_value=[{"key": "coffee"}]) as apply, \
             patch.object(outbox.MEMORY_OUTBOX, "update_one",
                          return_value=MagicMock(matched_count=1)) as update_one:
            result = outbox.process_memory_outbox_once(limit=2)
        self.assertEqual(result, {"processed": 1, "applied": 1, "failed": 0})
        apply.assert_called_once()
        self.assertEqual(update_one.call_args.args[0], {
            "_id": "outbox-1", "lease_token": "lease-1",
        })

    def test_transient_failure_returns_to_queue_with_backoff(self):
        with patch.object(outbox, "_claim_one", side_effect=[record(attempts=2), None]), \
             patch.object(outbox.messages_coll, "find_one", return_value={
                 "metadata": {"message_use": {
                     "version": "message-use-v1", "use": "ordinary",
                 }},
             }), \
             patch("services.memory_service.apply_profile_memory_proposals",
                   side_effect=MemoryWriteError("memory_agent_unavailable")), \
             patch.object(outbox.time, "time", return_value=100.0), \
             patch.object(outbox.MEMORY_OUTBOX, "update_one") as update_one:
            result = outbox.process_memory_outbox_once(limit=2)
        update = update_one.call_args.args[1]
        self.assertEqual(result, {"processed": 1, "applied": 0, "failed": 1})
        self.assertEqual(update["$set"]["status"], "pending")
        self.assertEqual(update["$set"]["next_attempt_at"], 160.0)

    def test_eighth_failure_is_terminal(self):
        with patch.object(outbox, "_claim_one", side_effect=[record(attempts=8), None]), \
             patch.object(outbox.messages_coll, "find_one", return_value={
                 "metadata": {"message_use": {
                     "version": "message-use-v1", "use": "ordinary",
                 }},
             }), \
             patch("services.memory_service.apply_profile_memory_proposals",
                   side_effect=MemoryWriteError("graph_write_failed")), \
             patch.object(outbox.time, "time", return_value=100.0), \
             patch.object(outbox.MEMORY_OUTBOX, "update_one") as update_one:
            outbox.process_memory_outbox_once(limit=2)
        update = update_one.call_args.args[1]
        self.assertEqual(update["$set"]["status"], "failed")
        self.assertIn("next_attempt_at", update["$unset"])

    def test_excluded_source_is_retired_without_graph_replay(self):
        claimed = record()
        with patch.object(outbox, "_claim_one", side_effect=[claimed, None]), \
             patch.object(outbox.messages_coll, "find_one", return_value={
                 "metadata": {"message_use": metadata_for_use("calendar_operation")},
             }), \
             patch("services.memory_service.apply_profile_memory_proposals") as apply, \
             patch.object(outbox.MEMORY_OUTBOX, "update_one") as update_one:
            result = outbox.process_memory_outbox_once(limit=2)
        self.assertEqual(result, {"processed": 1, "applied": 0, "failed": 0})
        apply.assert_not_called()
        self.assertEqual(update_one.call_args.args[1]["$set"]["status"], "excluded")


if __name__ == "__main__":
    unittest.main()
