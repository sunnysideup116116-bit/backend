import json
import unittest
from unittest.mock import MagicMock, patch

import mongomock

from services import relationship_memory_service as service


class RelationshipMemoryServiceTests(unittest.TestCase):
    def test_two_non_conflicting_facets_are_kept_and_rendered_with_separator(self):
        candidates = [
            {"statement": "我覺得對方聊天時有點冷淡", "evidence_span": "聊天有點冷"},
            {"statement": "我覺得對方外表很帥", "evidence_span": "他很帥"},
        ]
        operations = [
            {"action": "add", "candidate_slot": 1, "existing_slot": None,
             "statement": candidates[0]["statement"]},
            {"action": "add", "candidate_slot": 2, "existing_slot": None,
             "statement": candidates[1]["statement"]},
        ]
        facets, changed, counts = service._apply_facet_operations(
            [], candidates, operations,
            owner="owner", other="other", category="impression",
            source_id="source-1", source_at=100, current=110,
        )
        self.assertTrue(changed)
        self.assertEqual(counts["add"], 2)
        self.assertEqual(
            service._render_facets(facets),
            "我覺得對方聊天時有點冷淡、我覺得對方外表很帥",
        )

    def test_replace_changes_only_the_targeted_facet(self):
        existing = [
            {"facet_id": "cold", "text": "我覺得對方聊天時有點冷淡",
             "source_at": 100, "updated_at": 100},
            {"facet_id": "handsome", "text": "我覺得對方外表很帥",
             "source_at": 101, "updated_at": 101},
        ]
        candidates = [{
            "statement": "我發現對方見面時其實很熱情",
            "evidence_span": "見面後發現他其實很熱情，之前是我誤會",
        }]
        facets, changed, _counts = service._apply_facet_operations(
            existing,
            [{**candidates[0], "category": "impression"}],
            [{"action": "replace", "candidate_slot": 1, "existing_slot": 1,
              "statement": candidates[0]["statement"]}],
            owner="owner", other="other", category="impression",
            source_id="source-2", source_at=200, current=210,
        )
        self.assertTrue(changed)
        self.assertEqual(facets[0]["text"], "我發現對方見面時其實很熱情")
        self.assertEqual(facets[1]["text"], "我覺得對方外表很帥")

    def test_late_source_cannot_replace_a_newer_facet(self):
        existing = [{
            "facet_id": "newer", "text": "我覺得對方現在很熱情",
            "source_at": 300, "updated_at": 300,
        }]
        facets, changed, _counts = service._apply_facet_operations(
            existing,
            [{"category": "impression", "statement": "我覺得對方很冷淡",
              "evidence_span": "他很冷淡"}],
            [{"action": "replace", "candidate_slot": 1, "existing_slot": 1,
              "statement": "我覺得對方很冷淡"}],
            owner="owner", other="other", category="impression",
            source_id="old", source_at=200, current=400,
        )
        self.assertFalse(changed)
        self.assertEqual(facets, existing)

    def test_legacy_statement_projects_as_one_facet_without_writing(self):
        row = {
            "_id": "memory-1", "owner_user_id": "owner", "other_user_id": "other",
            "category": "impression", "topic": "相處感受",
            "statement": "本人覺得「滿率的其實」", "source_message_id": "source-1",
            "evidence_span": "滿率的其實", "version": 1, "updated_at": 100,
        }
        projected = service._project_memory(row)
        self.assertEqual(projected["schema_version"], 1)
        self.assertEqual(projected["facets"][0]["text"], row["statement"])
        self.assertEqual(projected["statement"], row["statement"])

    def test_single_facet_delete_keeps_other_view_and_uses_cas(self):
        current = {
            "_id": "memory-1", "owner_user_id": "owner", "other_user_id": "other",
            "category": "impression", "topic": "相處感受", "version": 2,
            "status": "active", "statement": "冷淡、帥氣",
            "facets": [
                {"facet_id": "cold", "text": "我覺得聊天偏冷淡",
                 "source_message_id": "source-cold", "source_at": 10, "updated_at": 10},
                {"facet_id": "handsome", "text": "我覺得對方外表很帥",
                 "source_message_id": "source-handsome", "source_at": 20, "updated_at": 20},
            ],
        }
        updated = {
            **current,
            "version": 3,
            "statement": "我覺得對方外表很帥",
            "facets": [current["facets"][1]],
        }
        with patch.object(
            service.RELATIONSHIP_MEMORIES, "find_one", return_value=current,
        ), patch.object(
            service.RELATIONSHIP_MEMORIES, "find_one_and_update", return_value=updated,
        ) as save, patch.object(
            service.RELATIONSHIP_MEMORY_SUPPRESSIONS, "update_one",
        ) as suppress:
            result = service.update_relationship_memory(
                "owner", "memory-1", expected_version=2,
                facet_id="cold", delete=True,
            )

        self.assertEqual(result["statement"], "我覺得對方外表很帥")
        self.assertEqual(save.call_args.args[0]["version"], 2)
        self.assertEqual(
            save.call_args.args[1]["$set"]["facets"][0]["facet_id"],
            "handsome",
        )
        suppress.assert_called_once()

    def test_committed_batch_adds_two_facets_then_replaces_only_one(self):
        store = mongomock.MongoClient().test
        first_job = {"_id": "job-1", "source_message_id": "source-1"}
        first_candidates = [
            {"category": "impression", "statement": "我覺得對方聊天有點冷",
             "evidence_span": "聊天有點冷"},
            {"category": "impression", "statement": "我覺得對方外表很帥",
             "evidence_span": "他很帥"},
        ]
        with patch.object(service, "RELATIONSHIP_MEMORIES", store.memories), \
             patch.object(service, "RELATIONSHIP_MEMORY_OUTBOX", store.outbox):
            first = service._commit_memory_batch(
                job=first_job,
                token="lease-1",
                owner="owner",
                other="other",
                match={"_id": "match-1"},
                source={
                    "content": "他很帥但聊天有點冷",
                    "timestamp": 100,
                },
                candidates=first_candidates,
                current=110,
            )
            memory_id = first["memory_ids"][0]
            initial = store.memories.find_one({"_id": memory_id})
            self.assertEqual(
                initial["statement"],
                "我覺得對方聊天有點冷、我覺得對方外表很帥",
            )

            with patch.object(
                service,
                "_plan_facet_operations",
                return_value=[{
                    "action": "replace",
                    "candidate_slot": 1,
                    "existing_slot": 1,
                    "statement": "我發現對方見面時其實很熱情",
                }],
            ):
                service._commit_memory_batch(
                    job={"_id": "job-2", "source_message_id": "source-2"},
                    token="lease-2",
                    owner="owner",
                    other="other",
                    match={"_id": "match-1"},
                    source={
                        "content": "見面後發現他其實很熱情，之前是我誤會",
                        "timestamp": 200,
                    },
                    candidates=[{
                        "category": "impression",
                        "statement": "我發現對方見面時其實很熱情",
                        "evidence_span": "見面後發現他其實很熱情，之前是我誤會",
                    }],
                    current=210,
                )
            updated = store.memories.find_one({"_id": memory_id})

        self.assertEqual(
            updated["statement"],
            "我發現對方見面時其實很熱情、我覺得對方外表很帥",
        )
        self.assertEqual(updated["version"], 2)

    def test_extractor_requires_exact_evidence_and_subjective_statement(self):
        response = MagicMock(content=json.dumps({
            "action": "create", "existing_slot": None, "topic": "聊天投入",
            "statement": "對方聊天時不太專心", "evidence_span": "一直看手機",
            "confidence": .95,
        }))
        with patch.object(service, "list_relationship_memories", return_value=[]), \
             patch.object(service, "generate_chat_completion", return_value=response):
            result = service._extract(
                {"owner_user_id": "owner", "other_user_id": "other"},
                {"content": "我覺得他一直看手機，但其他部分還不錯"},
            )
        self.assertEqual(result["action"], "create")
        self.assertEqual(result["statement"], "我覺得對方聊天時不太專心")

    def test_extractor_rejects_unrelated_or_unanchored_output(self):
        response = MagicMock(content=json.dumps({
            "action": "create", "topic": "天氣", "statement": "本人覺得會下雨",
            "evidence_span": "不存在", "confidence": .99,
        }))
        with patch.object(service, "list_relationship_memories", return_value=[]), \
             patch.object(service, "generate_chat_completion", return_value=response):
            result = service._extract(
                {"owner_user_id": "owner", "other_user_id": "other"},
                {"content": "幫我查明天天氣"},
            )
        self.assertEqual(result["action"], "none")

    def test_context_fails_closed_when_relationship_is_not_active(self):
        with patch.object(service, "_active_match", return_value=None), \
             patch.object(service, "list_relationship_memories") as memories:
            self.assertEqual(service.relationship_memory_context("owner", "other"), [])
        memories.assert_not_called()

    def test_candidate_evidence_allows_attached_request_and_question(self):
        self.assertTrue(service._memory_evidence_allowed(
            "我喜歡他，幫我記住", "我喜歡他",
        ))
        self.assertTrue(service._memory_evidence_allowed(
            "我覺得跟他相處很自在，你覺得呢？",
            "我覺得跟他相處很自在",
        ))
        self.assertTrue(service._memory_evidence_allowed(
            "跟他相處很自在", "跟他相處很自在",
        ))
        self.assertTrue(service._memory_evidence_allowed(
            "他說「我喜歡你」，我也覺得很開心", "我也覺得很開心",
        ))

    def test_candidate_evidence_only_requires_exact_source_span(self):
        self.assertTrue(service._memory_evidence_allowed(
            "他說「我喜歡你」", "我喜歡你",
        ))
        self.assertTrue(service._memory_evidence_allowed(
            "我想約他出去，可以幫我安排嗎？", "我想約他出去",
        ))
        self.assertTrue(service._memory_evidence_allowed(
            "如果我喜歡他，就會答應", "如果我喜歡他",
        ))
        self.assertFalse(service._memory_evidence_allowed(
            "如果我喜歡他，就會答應", "訊息中沒有這句",
        ))
        self.assertFalse(service._memory_evidence_allowed("", "我喜歡他"))

    def test_agent_candidate_is_structured_without_a_second_model_decision(self):
        with patch.object(
            service,
            "list_relationship_memories",
            return_value=[],
        ), patch.object(service, "generate_chat_completion") as model:
            result = service._extract(
                {
                    "owner_user_id": "owner",
                    "other_user_id": "other",
                    "candidate": {
                        "category": "impression",
                        "evidence_span": "我很在乎他",
                        "statement": "我很重視和對方的關係",
                    },
                },
                {"content": "前面聊了很多，我很在乎他"},
            )
        self.assertEqual(result, {
            "action": "merge_batch",
            "candidates": [{
                "category": "impression",
                "topic": "相處感受",
                "statement": "我很重視和對方的關係",
                "evidence_span": "我很在乎他",
            }],
        })
        model.assert_not_called()

    def test_short_accurate_candidate_is_kept_for_merge_planning(self):
        with patch.object(
            service,
            "list_relationship_memories",
            return_value=[],
        ), patch.object(service, "generate_chat_completion") as model:
            result = service._extract(
                {
                    "owner_user_id": "owner",
                    "other_user_id": "other",
                    "candidate": {
                        "category": "impression",
                        "evidence_span": "它好冷漠",
                        "statement": "它好冷漠",
                    },
                },
                {"content": "它好冷漠"},
            )

        self.assertEqual(
            result["candidates"][0]["statement"],
            "我覺得它好冷漠",
        )
        model.assert_not_called()

    def test_enqueue_result_is_structured_and_idempotent(self):
        source = {
            "_id": "source-1",
            "sender_id": "owner",
            "room_id": "mediator_private::owner::other",
            "content": "我喜歡他，幫我記住",
        }
        inserted = MagicMock(upserted_id="job-1")
        with (
            patch.object(service, "relationship_memory_mode", return_value="on"),
            patch.object(service.messages_coll, "find_one", return_value=source),
            patch.object(service, "_active_match", return_value={"_id": "match-1"}),
            patch.object(service.RELATIONSHIP_MEMORY_OUTBOX, "find_one", return_value=None),
            patch.object(service.RELATIONSHIP_MEMORY_OUTBOX, "update_one", return_value=inserted) as update,
        ):
            result = service.enqueue_relationship_memory_extraction(
                "owner", "other", "match-1", "source-1",
                category="impression", evidence_span="我喜歡他",
                statement="我對對方有好感",
            )
        self.assertEqual(result["status"], "queued")
        self.assertTrue(result)
        document = update.call_args.args[1]["$setOnInsert"]
        self.assertEqual(document["candidate"], {
            "category": "impression", "evidence_span": "我喜歡他",
            "statement": "我對對方有好感",
        })

        with (
            patch.object(service, "relationship_memory_mode", return_value="on"),
            patch.object(service.messages_coll, "find_one", return_value=source),
            patch.object(service, "_active_match", return_value={"_id": "match-1"}),
            patch.object(
                service.RELATIONSHIP_MEMORY_OUTBOX,
                "find_one",
                return_value={"status": "pending"},
            ),
        ):
            duplicate = service.enqueue_relationship_memory_extraction(
                "owner", "other", "match-1", "source-1",
            )
        self.assertEqual(duplicate["status"], "already_queued")
        self.assertTrue(duplicate)

    def test_explicit_owner_opt_out_is_enforced_before_enqueue(self):
        source = {
            "sender_id": "owner",
            "room_id": "mediator_private::owner::other",
            "content": "阿月，這段不要記，我只是想聊聊",
        }
        with (
            patch.object(service, "relationship_memory_mode", return_value="on"),
            patch.object(service.messages_coll, "find_one", return_value=source),
            patch.object(service.RELATIONSHIP_MEMORY_SUPPRESSIONS, "update_one") as suppress,
            patch.object(service.RELATIONSHIP_MEMORY_OUTBOX, "update_one") as enqueue,
        ):
            result = service.enqueue_relationship_memory_extraction(
                "owner", "other", "match-1", "source-1",
                category="impression", evidence_span="我只是想聊聊",
                statement="我想先聊聊這段關係",
            )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "suppressed")
        suppress.assert_called_once()
        enqueue.assert_not_called()

    def test_existing_job_reports_saved_failed_and_skipped_outcomes(self):
        source = {
            "sender_id": "owner",
            "room_id": "mediator_private::owner::other",
            "content": "我很在乎他",
        }
        cases = (
            ({"status": "completed", "outcome": "saved"}, "already_saved"),
            ({"status": "failed", "outcome": "failed"}, "failed"),
            ({"status": "completed", "outcome": "not_explicit"}, "skipped"),
        )
        for existing, expected in cases:
            with (
                patch.object(service, "relationship_memory_mode", return_value="on"),
                patch.object(service.messages_coll, "find_one", return_value=source),
                patch.object(service, "_active_match", return_value={"_id": "match-1"}),
                patch.object(
                    service.RELATIONSHIP_MEMORY_OUTBOX,
                    "find_one",
                    return_value=existing,
                ),
            ):
                result = service.enqueue_relationship_memory_extraction(
                    "owner", "other", "match-1", "source-1",
                    category="impression", evidence_span="我很在乎他",
                    statement="我很重視和對方的關係",
                )
            self.assertEqual(result["status"], expected)

    def test_delete_removes_content_and_adds_source_suppression(self):
        current = {
            "_id": "memory-1", "owner_user_id": "owner", "other_user_id": "other",
            "source_message_id": "source-1", "version": 2,
        }
        updated = {**current, "status": "deleted", "version": 3}
        with patch.object(service.RELATIONSHIP_MEMORIES, "find_one", return_value=current), \
             patch.object(service.RELATIONSHIP_MEMORIES, "find_one_and_update", return_value=updated) as update, \
             patch.object(service.RELATIONSHIP_MEMORY_SUPPRESSIONS, "update_one") as suppress:
            result = service.update_relationship_memory("owner", "memory-1", expected_version=2, delete=True)
        self.assertEqual(result["status"], "deleted")
        self.assertIn("statement", update.call_args.args[1]["$unset"])
        suppress.assert_called_once()

    def test_update_rejects_zero_and_negative_existing_slots(self):
        for slot in (0, -1, 1.5, "abc"):
            response = MagicMock(content=json.dumps({
                "action": "update", "existing_slot": slot,
                "topic": "錯誤", "statement": "本人覺得自在",
                "evidence_span": "我覺得很自在", "confidence": .99,
            }))
            with patch.object(
                service, "list_relationship_memories",
                return_value=[{"topic": "相處", "statement": "本人覺得還好"}],
            ), patch.object(service, "generate_chat_completion", return_value=response):
                result = service._extract(
                    {"owner_user_id": "owner", "other_user_id": "other"},
                    {"content": "我覺得很自在"},
                )
            self.assertEqual(result["action"], "none")
            self.assertEqual(result["reason"], "invalid_output")

    def test_outbox_records_saved_outcome_without_injecting_chat_copy(self):
        job = {
            "_id": "job-1",
            "owner_user_id": "owner",
            "other_user_id": "other",
            "relationship_id": "match-1",
            "source_message_id": "source-1",
            "status": "pending",
            "attempts": 0,
            "notification_status": "pending",
        }
        source = {
            "_id": "source-1",
            "sender_id": "owner",
            "room_id": "mediator_private::owner::other",
            "content": "我喜歡他，幫我記住",
            "timestamp": 123,
        }
        decision = {
            "action": "create",
            "topic": "相處感受",
            "statement": "本人覺得相處很自在",
            "evidence_span": "我喜歡他",
        }
        with (
            patch.object(service, "relationship_memory_mode", return_value="on"),
            patch.object(service.RELATIONSHIP_MEMORY_OUTBOX, "find_one_and_update", return_value=job),
            patch.object(service.RELATIONSHIP_MEMORY_SUPPRESSIONS, "find_one", return_value=None),
            patch.object(service, "_active_match", return_value={"_id": "match-1"}),
            patch.object(service.messages_coll, "find_one", return_value=source),
            patch.object(service.RELATIONSHIP_MEMORIES, "find_one", return_value=None),
            patch.object(service, "_extract", return_value=decision),
            patch.object(
                service,
                "_commit_memory_batch",
                return_value={
                    "memory_ids": ["relationship-memory:1"],
                    "memory_versions": {"relationship-memory:1": 1},
                    "operation_counts": {"add": 1},
                },
            ) as commit,
            patch.object(service.RELATIONSHIP_MEMORY_OUTBOX, "update_one") as outbox_update,
        ):
            stats = service.process_relationship_memory_outbox_once(now=5000)

        self.assertEqual(stats["saved"], 1)
        self.assertEqual(stats["retried"], 0)
        commit.assert_called_once()
        notification_updates = [
            call.args[1]["$set"]
            for call in outbox_update.call_args_list
            if call.args[1].get("$set", {}).get("notification_status")
        ]
        self.assertEqual(len(notification_updates), 1)
        self.assertEqual(
            notification_updates[0]["notification_status"], "not_required",
        )
        self.assertIsInstance(
            notification_updates[0]["notification_completed_at"], float,
        )
        finish = outbox_update.call_args_list[-1].args[1]["$set"]
        self.assertEqual(finish["outcome"], "saved")
        self.assertEqual(finish["write_status"], "saved")

    def test_existing_saved_job_finishes_without_rewriting_memory(self):
        retry_job = {
            "_id": "job-1",
            "owner_user_id": "owner",
            "other_user_id": "other",
            "relationship_id": "match-1",
            "source_message_id": "source-1",
            "status": "pending",
            "attempts": 1,
            "notification_status": "pending",
            "write_status": "saved",
            "memory_id": "relationship-memory:1",
        }
        source = {
            "_id": "source-1",
            "sender_id": "owner",
            "room_id": "mediator_private::owner::other",
            "content": "我喜歡他，幫我記住",
        }
        with (
            patch.object(service, "relationship_memory_mode", return_value="on"),
            patch.object(
                service.RELATIONSHIP_MEMORY_OUTBOX,
                "find_one_and_update",
                return_value=retry_job,
            ),
            patch.object(service.RELATIONSHIP_MEMORY_SUPPRESSIONS, "find_one", return_value=None),
            patch.object(service, "_active_match", return_value={"_id": "match-1"}),
            patch.object(service.messages_coll, "find_one", return_value=source),
            patch.object(
                service.RELATIONSHIP_MEMORIES,
                "find_one",
                side_effect=[None, {}, {"_id": "relationship-memory:1"}],
            ),
            patch.object(service.RELATIONSHIP_MEMORIES, "update_one") as memory_update,
            patch.object(service, "_extract") as extract,
            patch.object(service.RELATIONSHIP_MEMORY_OUTBOX, "update_one") as outbox_update,
        ):
            stats = service.process_relationship_memory_outbox_once(now=5060)

        self.assertEqual(stats["saved"], 1)
        memory_update.assert_not_called()
        extract.assert_not_called()
        self.assertTrue(any(
            call.args[1].get("$set", {}).get("notification_status") == "not_required"
            for call in outbox_update.call_args_list
        ))


if __name__ == "__main__":
    unittest.main()
