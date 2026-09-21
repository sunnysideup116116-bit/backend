import unittest
from unittest.mock import MagicMock, patch

from pymongo.errors import DuplicateKeyError

from routers import match as match_router
from services import match_search_job_service as jobs
from services.match_search_context import context_embedding_source_hash
from tests.match_flow_store import Collection


class MatchSearchJobServiceTests(unittest.TestCase):
    def setUp(self):
        with jobs._owned_lease_lock:
            jobs._owned_lease_ids.clear()

    def tearDown(self):
        with jobs._owned_lease_lock:
            jobs._owned_lease_ids.clear()

    @patch.object(jobs, "_has_live_match", return_value=False)
    @patch.object(jobs, "profiles_coll")
    @patch.object(jobs, "MATCH_SEARCH_JOBS")
    def test_enqueue_only_persists_a_job_and_never_runs_pipeline(self, collection, profiles, _live):
        profiles.find_one.return_value = {"current_context_revision": 7}
        jobs.register_match_search_pipeline(MagicMock())

        result = jobs.enqueue_match_search(
            "owner", source="agent_v2", idempotency_key="run:0", force_new=True,
            origin_room_id="ai_room::owner::origin",
        )

        self.assertEqual(result, {"status": "queued"})
        document = collection.insert_one.call_args.args[0]
        self.assertEqual(document["status"], "queued")
        self.assertEqual(document["context_revision"], 7)
        self.assertEqual(document["origin_room_id"], "ai_room::owner::origin")
        self.assertEqual(jobs._pipeline.call_count, 0)
        self.assertNotIn("job_id", result)

    @patch.object(jobs, "MATCH_SEARCH_JOBS")
    def test_public_status_never_leaks_job_or_lease_identifiers(self, collection):
        collection.find_one.return_value = {
            "job_id": "secret-job", "lease_id": "secret-lease", "context_revision": 9,
            "status": "running", "step": "vector_search", "progress_percent": 40,
        }

        result = jobs.public_match_search_status("owner")

        self.assertEqual(result["status"], "running")
        self.assertEqual(result["step"], "vector_search")
        self.assertEqual(result["estimated_seconds_min"], 60)
        self.assertEqual(result["estimated_seconds_max"], 180)
        self.assertEqual(set(result), {
            "status", "step", "progress_percent", "estimated_seconds_min", "estimated_seconds_max",
            "reason_code", "cancellable",
        })
        self.assertTrue(result["cancellable"])

    @patch.object(jobs, "_finish_job")
    @patch.object(jobs, "_requeue_after_context_change", return_value=True)
    @patch.object(jobs, "_report_progress", return_value=False)
    @patch.object(jobs, "_claim_next_job")
    def test_context_change_requeues_job_before_pipeline_runs(
        self, claim, report, requeue, finish,
    ):
        claim.return_value = {
            "_id": "job", "job_id": "safe", "user_id": "owner",
            "lease_id": "lease", "attempt": 1, "context_revision": 2,
        }
        pipeline = MagicMock()
        jobs.register_match_search_pipeline(pipeline)

        self.assertTrue(jobs.run_one_match_search_job())

        pipeline.assert_not_called()
        requeue.assert_called_once_with(claim.return_value)
        finish.assert_not_called()

    @patch.object(jobs, "_job_has_lease", return_value=True)
    @patch.object(jobs, "profiles_coll")
    @patch.object(jobs, "MATCH_SEARCH_JOBS")
    def test_first_context_change_requeues_same_job_with_latest_revision(
        self, collection, profiles, lease,
    ):
        collection.update_one.return_value.modified_count = 1
        profiles.find_one.return_value = {"current_context_revision": 4}
        job = {
            "_id": "job", "job_id": "safe", "user_id": "owner",
            "lease_id": "lease", "attempt": 1, "context_revision": 3,
            "source": "agent_v3",
        }

        self.assertTrue(jobs._requeue_after_context_change(job))

        lease.assert_called_once_with(job)
        query, update = collection.update_one.call_args.args
        self.assertEqual(query["attempt"], {"$lte": 1})
        self.assertEqual(update["$set"]["status"], "queued")
        self.assertEqual(update["$set"]["step"], "loading_profile")
        self.assertEqual(update["$set"]["context_revision"], 4)
        self.assertIn("lease_id", update["$unset"])
        projection = profiles.update_one.call_args.args[1]["$set"]["match_search"]
        self.assertEqual(projection["status"], "queued")
        self.assertEqual(projection["step"], "loading_profile")

    @patch.object(jobs, "_job_has_lease")
    @patch.object(jobs, "profiles_coll")
    @patch.object(jobs, "MATCH_SEARCH_JOBS")
    def test_second_context_change_does_not_requeue_forever(
        self, collection, profiles, lease,
    ):
        job = {
            "_id": "job", "job_id": "safe", "user_id": "owner",
            "lease_id": "lease", "attempt": 2, "context_revision": 3,
        }

        self.assertFalse(jobs._requeue_after_context_change(job))

        lease.assert_not_called()
        profiles.find_one.assert_not_called()
        collection.update_one.assert_not_called()

    @patch.object(jobs, "queue_mediator_event")
    @patch.object(jobs, "_finish_job", return_value=True)
    @patch.object(jobs, "_requeue_after_context_change", return_value=False)
    def test_exhausted_stale_search_delivers_visible_failure(
        self, requeue, finish, queue,
    ):
        job = {
            "_id": "job", "job_id": "safe", "user_id": "owner",
            "lease_id": "lease", "attempt": 2,
            "origin_room_id": "ai_room::owner::origin",
        }

        jobs._settle_stale_job(job)

        requeue.assert_called_once_with(job)
        finish.assert_called_once_with(
            job, "stale", error_code="ownership_or_context_changed",
        )
        self.assertEqual(queue.call_args.args[0], "owner")
        self.assertEqual(queue.call_args.args[2], "match_search_failed")
        self.assertEqual(
            queue.call_args.kwargs["event_key"], "match-search-job:safe:stale",
        )
        self.assertEqual(
            queue.call_args.kwargs["origin_room_id"], "ai_room::owner::origin",
        )

    def test_context_race_requeues_once_and_second_attempt_finishes(self):
        collection = Collection([{
            "_id": "job", "job_id": "safe", "user_id": "owner",
            "active_user_id": "owner", "status": "queued",
            "step": "loading_profile", "progress_percent": 0,
            "context_revision": 3, "source": "agent_v3",
            "origin_room_id": "ai_room::owner::origin",
            "attempt": 0, "created_at": 1, "updated_at": 1,
        }])
        profiles = Collection([{
            "_id": "profile", "user_id": "owner",
            "current_context_revision": 3,
            "active_match_search_job_id": "safe",
            "matchmaking_in_progress": True,
        }])
        deliveries = MagicMock()
        attempts = 0

        def pipeline(_user_id, _source, *, report_progress, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                self.assertTrue(report_progress("vector_search"))
                profiles.update_one(
                    {"user_id": "owner"},
                    {"$set": {"current_context_revision": 4}},
                )
                self.assertFalse(report_progress("matchmaker_request"))
                return {"status": "stale", "matches": []}
            self.assertTrue(report_progress("matchmaker_request"))
            return {"status": "no_suitable_candidate", "matches": []}

        with patch.object(jobs, "MATCH_SEARCH_JOBS", collection), \
             patch.object(jobs, "profiles_coll", profiles), \
             patch.object(jobs, "_live_match", return_value=None), \
             patch.object(jobs, "queue_mediator_event", deliveries):
            jobs.register_match_search_pipeline(pipeline)

            self.assertTrue(jobs.run_one_match_search_job())
            first = collection.find_one({"_id": "job"})
            self.assertEqual(first["status"], "queued")
            self.assertEqual(first["attempt"], 1)
            self.assertEqual(first["context_revision"], 4)
            self.assertEqual(
                jobs.public_match_search_status("owner")["status"], "queued",
            )
            deliveries.assert_not_called()

            self.assertTrue(jobs.run_one_match_search_job())
            second = collection.find_one({"_id": "job"})
            self.assertEqual(second["status"], "no_candidates")
            self.assertEqual(second["attempt"], 2)
            self.assertEqual(attempts, 2)
            self.assertEqual(deliveries.call_args.args[2], "match_search_empty")

    @patch.object(jobs, "profiles_coll")
    @patch.object(jobs, "MATCH_SEARCH_JOBS")
    def test_job_ownership_requires_the_same_context_revision(self, collection, profiles):
        collection.find_one.return_value = {"_id": "job"}
        profiles.find_one.return_value = {"current_context_revision": 3}
        self.assertFalse(jobs._job_has_ownership({
            "_id": "job", "user_id": "owner", "lease_id": "lease", "context_revision": 2,
        }))

    @patch.object(jobs, "_job_context_matches", return_value=False)
    @patch.object(jobs, "_live_match", return_value={"search_job_id": "job-1", "status": "draft"})
    @patch.object(jobs, "_job_has_lease", return_value=True)
    def test_same_job_proposal_remains_recoverable_after_commit(self, _lease, _live, _context):
        self.assertTrue(jobs._job_is_current({"job_id": "job-1", "user_id": "owner"}))

    @patch.object(jobs, "_has_live_match", return_value=True)
    @patch.object(jobs, "profiles_coll")
    @patch.object(jobs, "MATCH_SEARCH_JOBS")
    def test_cancelled_job_wins_over_an_unrelated_live_match(self, collection, profiles, live):
        collection.find_one_and_update.return_value = {"job_id": "job-1"}

        result = jobs.cancel_match_search("owner")

        self.assertEqual(result, {"status": "cancelled"})
        live.assert_not_called()
        profiles.update_one.assert_called_once()

    @patch.object(jobs, "_has_live_match", return_value=False)
    @patch.object(jobs, "profiles_coll")
    @patch.object(jobs, "MATCH_SEARCH_JOBS")
    def test_cancel_search_cas_is_bound_to_expected_job(self, collection, profiles, _live):
        collection.find_one_and_update.return_value = None
        collection.find_one.return_value = None

        result = jobs.cancel_match_search("owner", expected_job_id="job-expected")

        query = collection.find_one_and_update.call_args.args[0]
        self.assertEqual(query["job_id"], "job-expected")
        self.assertEqual(result, {"status": "idle"})
        profiles.update_one.assert_not_called()

    @patch.object(jobs, "_has_live_match", return_value=False)
    @patch.object(jobs, "profiles_coll")
    @patch.object(jobs, "MATCH_SEARCH_JOBS")
    def test_terminal_idempotency_replay_returns_the_original_outcome(self, collection, profiles, _live):
        profiles.find_one.return_value = {"current_context_revision": 2}
        collection.insert_one.side_effect = DuplicateKeyError("duplicate")
        collection.find_one.return_value = {"status": "no_candidates"}

        result = jobs.enqueue_match_search(
            "owner", source="agent_v2", idempotency_key="same-run", force_new=True,
        )

        self.assertEqual(result, {"status": "no_candidates"})
        profiles.update_one.assert_not_called()

    @patch.object(jobs, "queue_mediator_event")
    @patch.object(jobs, "_finish_job", return_value=True)
    @patch.object(jobs, "_job_has_ownership", return_value=True)
    @patch.object(jobs, "_report_progress", return_value=True)
    @patch.object(jobs, "_claim_next_job")
    def test_empty_pipeline_result_finishes_as_no_candidates(
        self, claim, _report, _ownership, finish, queue,
    ):
        job = {
            "_id": "job", "job_id": "safe", "user_id": "owner", "lease_id": "lease",
            "origin_room_id": "ai_room::owner::origin",
        }
        claim.return_value = job
        jobs.register_match_search_pipeline(
            lambda _user, _source, **_callbacks: {
                "status": "no_suitable_candidate", "matches": [],
            }
        )

        self.assertTrue(jobs.run_one_match_search_job())

        finish.assert_called_once_with(job, "no_candidates")
        self.assertEqual(queue.call_args.args[2], "match_search_empty")
        self.assertEqual(queue.call_args.kwargs["origin_room_id"], "ai_room::owner::origin")

    @patch.object(jobs, "queue_mediator_event")
    @patch.object(jobs, "_finish_job", return_value=True)
    @patch.object(jobs, "_claim_next_job")
    def test_unavailable_pipeline_is_a_stable_failure_code(self, claim, finish, queue):
        job = {"_id": "job", "job_id": "safe", "user_id": "owner", "lease_id": "lease"}
        claim.return_value = job
        jobs.register_match_search_pipeline(None)

        self.assertTrue(jobs.run_one_match_search_job())

        finish.assert_called_once_with(
            job, "failed", error_code="pipeline_unavailable", failure_stage="loading_profile",
        )
        self.assertEqual(queue.call_args.args[0], "owner")
        self.assertEqual(queue.call_args.args[2], "match_search_failed")

    @patch.object(jobs, "queue_mediator_event")
    @patch.object(jobs, "_finish_job", return_value=True)
    @patch.object(jobs, "_report_progress", return_value=True)
    @patch.object(jobs, "_claim_next_job")
    def test_pipeline_error_preserves_allowlisted_code_and_stage(
        self, claim, report, finish, queue,
    ):
        job = {"_id": "job", "job_id": "safe", "user_id": "owner", "lease_id": "lease"}
        claim.return_value = job
        jobs.register_match_search_pipeline(
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                jobs.MatchSearchPipelineError("matchmaker_unavailable", "matchmaker_request")
            )
        )

        self.assertTrue(jobs.run_one_match_search_job())

        finish.assert_called_once_with(
            job, "failed", error_code="matchmaker_unavailable", failure_stage="matchmaker_request",
        )
        self.assertEqual(queue.call_args.args[0], "owner")
        self.assertEqual(queue.call_args.args[2], "match_search_failed")

    @patch.object(match_router, "matches_coll")
    @patch.object(match_router, "profiles_coll")
    def test_zero_vector_candidates_is_a_normal_empty_result(self, profiles, matches):
        profiles.find_one.return_value = {
            "user_id": "owner", "current_context": "想找人去走走",
            "current_context_revision": 4, "context_embedding": [0.1],
            "context_embedding_source_hash": context_embedding_source_hash("想找人去走走"),
        }
        profiles.aggregate.return_value = []
        matches.find.return_value = []

        result = match_router.generate_matches_for_user(
            "owner", report_progress=lambda _step: True, can_commit=lambda: True,
        )

        self.assertEqual(result["status"], "no_suitable_candidate")
        self.assertEqual(result["matches"], [])

    @patch.object(match_router, "get_embedding", return_value=[0.2])
    @patch.object(match_router, "matches_coll")
    @patch.object(match_router, "profiles_coll")
    def test_embedding_refresh_cannot_overwrite_a_newer_context(self, profiles, matches, _embedding):
        profiles.find_one.return_value = {
            "user_id": "owner", "current_context": "最近想去散步",
            "current_context_revision": 5, "context_embedding": [],
        }
        profiles.aggregate.return_value = []
        matches.find.return_value = []

        match_router.generate_matches_for_user(
            "owner", report_progress=lambda _step: True, can_commit=lambda: True,
        )

        query, update = profiles.update_one.call_args.args
        self.assertEqual(query, {"user_id": "owner", "current_context": "最近想去散步"})
        self.assertEqual(update, {"$set": {
            "context_embedding": [0.2],
            "context_embedding_source_hash": context_embedding_source_hash("最近想去散步"),
        }})
        self.assertNotIn("current_context", update["$set"])

    @patch.object(match_router, "get_embedding", return_value=[0.9])
    @patch.object(match_router, "matches_coll")
    @patch.object(match_router, "profiles_coll")
    def test_topic_query_embedding_is_ephemeral_and_does_not_pollute_profile(
        self, profiles, matches, embedding,
    ):
        profiles.find_one.return_value = {
            "user_id": "owner", "current_context": "最近想看展",
            "current_context_revision": 7, "context_embedding": [0.1],
            "context_embedding_source_hash": context_embedding_source_hash("最近想看展"),
        }
        profiles.aggregate.return_value = []
        matches.find.return_value = []

        with patch.object(match_router.risk_block_service, "excluded_user_ids", return_value=[]):
            result = match_router.generate_matches_for_user(
                "owner", report_progress=lambda _step: True, can_commit=lambda: True,
                search_context={
                    "invitation_topic": "衝浪",
                    "query_text": "想找人一起衝浪",
                    "source_message_id": "owner-message",
                },
            )

        self.assertEqual(result["status"], "no_suitable_candidate")
        embedding.assert_called_once_with("想找人一起衝浪")
        profiles.update_one.assert_not_called()
        pipeline = profiles.aggregate.call_args.args[0]
        self.assertEqual(pipeline[0]["$vectorSearch"]["queryVector"], [0.9])

    @patch.object(match_router, "matches_coll")
    @patch.object(match_router, "profiles_coll")
    def test_match_test_cohort_only_recalls_the_same_cohort(self, profiles, matches):
        profiles.find_one.return_value = {
            "user_id": "seed_user_01", "current_context": "最近想喝咖啡",
            "current_context_revision": 7, "context_embedding": [0.1],
            "context_embedding_source_hash": context_embedding_source_hash("最近想喝咖啡"),
            "test_match_cohort": "match_v1",
        }
        profiles.aggregate.return_value = []
        matches.find.return_value = []

        with patch.object(match_router.risk_block_service, "excluded_user_ids", return_value=[]):
            match_router.generate_matches_for_user(
                "seed_user_01", report_progress=lambda _step: True,
                can_commit=lambda: True,
            )

        match_stage = profiles.aggregate.call_args.args[0][1]["$match"]
        self.assertEqual(match_stage["test_match_cohort"], "match_v1")

    @patch.object(match_router, "matches_coll")
    @patch.object(match_router, "profiles_coll")
    def test_real_user_candidate_pool_excludes_all_test_ids(self, profiles, matches):
        profiles.find_one.return_value = {
            "user_id": "real-owner", "current_context": "最近想喝咖啡",
            "current_context_revision": 7, "context_embedding": [0.1],
            "context_embedding_source_hash": context_embedding_source_hash("最近想喝咖啡"),
        }
        profiles.aggregate.return_value = []
        matches.find.return_value = []

        with patch.object(match_router.risk_block_service, "excluded_user_ids", return_value=[]):
            match_router.generate_matches_for_user(
                "real-owner", report_progress=lambda _step: True,
                can_commit=lambda: True,
            )

        match_stage = profiles.aggregate.call_args.args[0][1]["$match"]
        self.assertEqual(match_stage["test_match_cohort"], {"$exists": False})
        self.assertIn(
            {"user_id": {"$not": {"$regex": match_router.MATCH_TEST_ID_PATTERN}}},
            match_stage["$and"],
        )

    @patch.object(match_router, "get_embedding", side_effect=RuntimeError("embedding unavailable"))
    @patch.object(match_router, "profiles_coll")
    def test_topic_embedding_failure_is_a_search_service_error(self, profiles, _embedding):
        profiles.find_one.return_value = {
            "user_id": "owner", "current_context": "最近想看展",
            "current_context_revision": 7, "context_embedding": [0.1],
        }

        with self.assertRaises(match_router.MatchSearchPipelineError) as raised:
            match_router.generate_matches_for_user(
                "owner", report_progress=lambda _step: True, can_commit=lambda: True,
                search_context={"invitation_topic": "衝浪", "query_text": "想找人一起衝浪"},
            )

        self.assertEqual(raised.exception.code, "vector_search_unavailable")

    @patch.object(match_router, "get_embedding", return_value=[0.9])
    @patch.object(match_router, "matches_coll")
    @patch.object(match_router, "profiles_coll")
    def test_next_ordinary_search_uses_stored_context_after_topic_search(
        self, profiles, matches, embedding,
    ):
        profiles.find_one.return_value = {
            "user_id": "owner", "current_context": "最近想看展",
            "current_context_revision": 7, "context_embedding": [0.1],
            "context_embedding_source_hash": context_embedding_source_hash("最近想看展"),
        }
        profiles.aggregate.return_value = []
        matches.find.return_value = []

        with patch.object(match_router.risk_block_service, "excluded_user_ids", return_value=[]):
            match_router.generate_matches_for_user(
                "owner", report_progress=lambda _step: True, can_commit=lambda: True,
                search_context={"invitation_topic": "衝浪", "query_text": "想找人一起衝浪"},
            )
            match_router.generate_matches_for_user(
                "owner", report_progress=lambda _step: True, can_commit=lambda: True,
            )

        self.assertEqual(embedding.call_args_list[0].args, ("想找人一起衝浪",))
        self.assertEqual(embedding.call_count, 1)
        pipelines = [call.args[0] for call in profiles.aggregate.call_args_list]
        self.assertEqual(pipelines[0][0]["$vectorSearch"]["queryVector"], [0.9])
        self.assertEqual(pipelines[1][0]["$vectorSearch"]["queryVector"], [0.1])
        profiles.update_one.assert_not_called()

    @patch.object(jobs, "profiles_coll")
    @patch.object(jobs, "MATCH_SEARCH_JOBS")
    def test_graceful_shutdown_requeues_only_this_process_lease(
        self, collection, profiles,
    ):
        collection.find_one_and_update.return_value = {
            "job_id": "job-1", "user_id": "owner", "source": "agent_v3",
        }
        with jobs._owned_lease_lock:
            jobs._owned_lease_ids.update({"owned-lease"})

        jobs.stop_match_search_worker()

        query, update = collection.find_one_and_update.call_args.args[:2]
        self.assertEqual(query, {"status": "running", "lease_id": "owned-lease"})
        self.assertEqual(update["$set"]["status"], "queued")
        self.assertIn("lease_id", update["$unset"])
        profile_projection = profiles.update_one.call_args.args[1]["$set"]["match_search"]
        self.assertEqual(profile_projection["status"], "queued")
        self.assertEqual(profile_projection["step"], "loading_profile")
        self.assertEqual(jobs._owned_lease_ids, set())

    @patch.object(match_router, "matches_coll")
    @patch.object(match_router, "profiles_coll")
    def test_reclaimed_worker_recovers_the_same_proposal_without_reinserting(self, profiles, matches):
        profiles.find_one.side_effect = [
            {"user_id": "owner", "current_context": "想去散步", "current_context_revision": 2},
            {"current_context": "想去看展", "big_five": {"summary": "安靜"}},
        ]
        matches.find_one.return_value = {
            "_id": "proposal-1", "from_user": "owner", "to_user": "other",
            "status": "draft", "search_job_id": "job-1", "reason": "可以一起走走",
        }

        result = match_router.generate_matches_for_user(
            "owner", report_progress=lambda _step: True,
            can_commit=lambda: True, search_job_id="job-1",
        )

        self.assertEqual(result["matches"][0]["match_id"], "proposal-1")
        self.assertEqual(result["matches"][0]["matched_user_id"], "other")
        matches.insert_one.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
def test_retrieval_diagnostics_are_bounded_and_drop_private_fields():
    result = jobs._bounded_diagnostics({
        "search_intent": "preference",
        "normalized_topic": "K-pop",
        "canonical_preference_key": "k_pop",
        "retrieval_source": "graph_exact",
        "candidate_count_before_filter": 12,
        "candidate_count_after_filter": 3,
        "shared_preferences": ["k_pop"],
        "hard_conflicts": ["smoking"],
        "qualification_reason_codes": {"requested_preference": 3},
        "candidate_ids": ["private-user-id"],
        "raw_memory": "private memory",
    })
    assert result["search_intent"] == "preference"
    assert result["canonical_preference_key"] == "k_pop"
    assert result["candidate_count_after_filter"] == 3
    assert "candidate_ids" not in result
    assert "raw_memory" not in result
