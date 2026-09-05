"""Offline coverage for queued discovery and post-consent chat navigation."""

import contextlib
import io
import os
import unittest
from unittest.mock import MagicMock, patch

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pymongo.errors import AutoReconnect

from models import EventDiscoveryRequest, MatchDecisionRequest
import event_worker
from routers import match as routes
from services import event_cycle_service as cycle
from services import event_discovery_job_service as jobs
from services import event_discovery_service as discovery


MATCH_ID = "64f000000000000000000001"


class AcceptedChatNavigationTests(unittest.TestCase):
    def _document(self, status="accepted", **fields):
        return {
            "_id": MATCH_ID, "from_user": "alice", "to_user": "bob",
            "status": status, "proposal_revision": 2,
            "proposal_namespace": "event_invitation",
            **({"last_decision": {
                "from": "pending", "to": "accepted", "action": "accept", "actor": "bob",
            }} if status == "accepted" else {}),
            **fields,
        }

    def _decide(self, result, document, *, user_id="bob", read_error=None):
        collection = MagicMock()
        collection.find_one.return_value = document
        if read_error:
            collection.find_one.side_effect = read_error
        request = MatchDecisionRequest(
            user_id=user_id, match_id=MATCH_ID, action="accept",
            expected_status="pending", expected_revision=1,
            proposal_namespace="event_invitation",
        )
        with patch.object(routes, "decide_match_action", return_value=result) as decide, \
                patch.object(routes, "matches_coll", collection):
            response = routes._apply_match_decision(request, BackgroundTasks())
        decide.assert_called_once()
        collection.find_one_and_update.assert_not_called()
        return response, collection

    def test_final_acceptance_returns_server_bound_chat_target(self):
        response, collection = self._decide(
            {"status": "success", "new_status": "accepted", "proposal_revision": 2},
            self._document(),
        )
        self.assertEqual(response["other_id"], "alice")
        self.assertEqual(response["proposal_revision"], 2)
        query = collection.find_one.call_args.args[0]
        self.assertEqual(query["status"], "accepted")
        self.assertEqual(query["$or"], [{"from_user": "bob"}, {"to_user": "bob"}])

    def test_nonaccepted_responses_never_expose_navigation_identity(self):
        for status in ("pending", "declined", "expired"):
            with self.subTest(status=status):
                response, collection = self._decide(
                    {"status": "success", "new_status": status,
                     "other_id": "must-not-leak", "other_name": "must-not-leak"},
                    self._document(status),
                )
                self.assertNotIn("other_id", response)
                self.assertNotIn("other_name", response)
                collection.find_one.assert_not_called()

    def test_navigation_requires_accepted_canonical_state_and_membership(self):
        for document in (
            None,
            self._document("pending"),
            self._document(from_user="someone", to_user="else"),
            self._document(from_user="bob", to_user="bob"),
        ):
            with self.subTest(document=document):
                response, _ = self._decide(
                    {"status": "success", "new_status": "accepted"}, document,
                )
                self.assertNotIn("other_id", response)

    def test_idempotent_acceptance_keeps_same_target_and_chat_reuse_flag(self):
        response, _ = self._decide(
            {"status": "success", "new_status": "accepted", "idempotent": True,
             "chat_reused": True, "proposal_namespace": "event_invitation"},
            self._document(relationship_establishing=False),
        )
        self.assertEqual(response["other_id"], "alice")
        self.assertTrue(response["idempotent"])
        self.assertTrue(response["chat_reused"])

    def test_bare_or_malformed_accepted_imports_do_not_prove_consent(self):
        for last_decision in ({}, {"from": "draft", "to": "accepted", "action": "accept"}, "invalid"):
            with self.subTest(last_decision=last_decision):
                response, _ = self._decide(
                    {"status": "success", "new_status": "accepted"},
                    self._document(last_decision=last_decision, state_history=[]),
                )
                self.assertNotIn("other_id", response)

    def test_consent_history_can_prove_acceptance_after_last_decision_changes(self):
        response, _ = self._decide(
            {"status": "success", "new_status": "accepted"},
            self._document(last_decision={}, state_history=[{
                "from": "pending", "to": "accepted", "action": "accept", "actor": "bob",
            }]),
        )
        self.assertEqual(response["other_id"], "alice")

    def test_navigation_read_failure_does_not_turn_committed_consent_into_failure(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            response, _ = self._decide(
                {"status": "success", "new_status": "accepted"}, None,
                read_error=AutoReconnect("private-database-detail"),
            )
        self.assertEqual(response["new_status"], "accepted")
        self.assertNotIn("other_id", response)
        self.assertNotIn("private-database-detail", output.getvalue())

    def test_stale_decision_stays_a_conflict_without_reading_chat_identity(self):
        with self.assertRaises(HTTPException) as raised:
            self._decide(
                {"stale": True, "current_status": "accepted", "current_revision": 2},
                self._document(),
            )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertNotIn("other_id", raised.exception.detail)

    def test_state_read_recovers_navigation_for_either_accepted_participant(self):
        for user_id, other_id in (("alice", "bob"), ("bob", "alice")):
            with self.subTest(user_id=user_id), \
                    patch.object(routes, "matches_coll") as collection, \
                    patch.object(routes, "build_active_proposal_card", return_value=None):
                collection.find_one.return_value = self._document(relationship_establishing=False)
                response = routes.get_single_match_state(user_id, MATCH_ID)
            self.assertEqual(response["other_id"], other_id)
            self.assertTrue(response["chat_reused"])

    def test_pending_state_retains_public_event_but_not_counterparty_identity(self):
        with patch.object(routes, "matches_coll") as collection, \
                patch.object(routes, "build_active_proposal_card", return_value={
                    "viewer_reason": "一起逛市集", "event": {"title": "港邊市集"},
                }):
            collection.find_one.return_value = self._document("pending")
            response = routes.get_single_match_state("alice", MATCH_ID)
        self.assertEqual(response["event"]["title"], "港邊市集")
        self.assertNotIn("other_id", response)

    def test_nonparticipant_cannot_read_accepted_navigation(self):
        with patch.object(routes, "matches_coll") as collection:
            collection.find_one.return_value = self._document()
            with self.assertRaises(HTTPException) as raised:
                routes.get_single_match_state("outsider", MATCH_ID)
        self.assertEqual(raised.exception.status_code, 403)

    def test_state_read_does_not_reveal_target_for_unverified_accepted_import(self):
        with patch.object(routes, "matches_coll") as collection, \
                patch.object(routes, "build_active_proposal_card", return_value=None):
            collection.find_one.return_value = self._document(last_decision={})
            response = routes.get_single_match_state("alice", MATCH_ID)
        self.assertNotIn("other_id", response)


class PublicEventQueueTests(unittest.TestCase):
    def test_manual_discovery_enqueues_without_invitation_or_reset(self):
        with patch.object(routes, "enqueue_event_discovery_job", return_value={
            "status": "queued", "state": "queued", "run_number": 7,
        }) as enqueue:
            result = routes.discover_public_events(EventDiscoveryRequest(
                region="高雄", window_days=30, categories=["市集", "音樂"],
            ))
        self.assertEqual(result["status"], "queued")
        enqueue.assert_called_once_with(
            region="高雄", window_days=30, categories=["市集", "音樂"],
            source="api", job_kind="discovery",
        )

    def test_default_categories_are_delegated_to_queue_normalization(self):
        with patch.object(routes, "enqueue_event_discovery_job", return_value={}) as enqueue:
            routes.discover_public_events(EventDiscoveryRequest())
        self.assertEqual(enqueue.call_args.kwargs["categories"], [])

    def test_already_running_keeps_current_run_instead_of_starting_another(self):
        snapshot = {"status": "already_running", "state": "running", "run_number": 7}
        with patch.object(routes, "enqueue_event_discovery_job", return_value=snapshot):
            self.assertEqual(routes.discover_public_events(EventDiscoveryRequest()), snapshot)

    def test_queue_write_failure_returns_bounded_service_unavailable(self):
        with patch.object(routes, "enqueue_event_discovery_job", side_effect=AutoReconnect("private")):
            with self.assertRaises(HTTPException) as raised:
                routes.discover_public_events(EventDiscoveryRequest())
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail["code"], "event_queue_unavailable")
        self.assertNotIn("private", str(raised.exception.detail))

    def test_status_uses_public_snapshot_without_worker_tokens(self):
        with patch.object(jobs, "_jobs") as collection:
            collection.find_one.return_value = {
                "state": "running", "stage": "discovering", "run_number": 7,
                "job_token": "private-token", "lease_owner": "private-worker",
            }
            result = routes.get_public_event_discovery_status()
        self.assertEqual(result["stage"], "discovering")
        self.assertNotIn("job_token", result)
        self.assertNotIn("lease_owner", result)

    def test_unavailable_status_is_not_reported_as_a_completed_job(self):
        with patch.object(routes, "event_discovery_job_snapshot", return_value={"state": "unavailable"}):
            with self.assertRaises(HTTPException) as raised:
                routes.get_public_event_discovery_status()
        self.assertEqual(raised.exception.status_code, 503)

    def test_public_post_and_status_routes_are_registered(self):
        app = FastAPI()
        app.include_router(routes.router)
        with patch.object(routes, "enqueue_event_discovery_job", return_value={
            "status": "queued", "state": "queued", "run_number": 7,
        }), patch.object(routes, "event_discovery_job_snapshot", return_value={
            "state": "completed", "outcome": "partial", "run_number": 7,
        }), TestClient(app) as client:
            queued = client.post("/api/match/events/discover", json={})
            snapshot = client.get("/api/match/events/discover/status")
        self.assertEqual(queued.status_code, 200)
        self.assertEqual(queued.json()["status"], "queued")
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(snapshot.json()["state"], "completed")
        self.assertEqual(snapshot.json()["outcome"], "partial")


class EventScanOwnershipTests(unittest.TestCase):
    def test_manual_queue_job_disables_legacy_invitation_hook(self):
        with patch.object(event_worker, "update_event_discovery_job_stage"), \
                patch.object(event_worker, "discover_and_ingest_events", return_value={"status": "success"}) as discover:
            event_worker._execute_job({"job_kind": "discovery"}, "worker")
        self.assertFalse(discover.call_args.kwargs["request_invitation_scan"])

    def test_weekly_cycle_scans_once_after_readiness_without_legacy_hook(self):
        calls = []
        with patch.object(cycle, "reset_event_inventory", return_value={}), \
                patch.object(cycle, "discover_and_ingest_events", side_effect=lambda **_kw: {
                    "status": "success", "active_category_counts": {"市集": 1},
                }) as discover, \
                patch.object(cycle, "wait_for_event_relevance", side_effect=lambda: calls.append("ready") or {"ready": True}), \
                patch.object(cycle, "scan_event_opportunities", side_effect=lambda **_kw: calls.append("scan") or {"status": "success"}) as scan:
            cycle.run_weekly_event_cycle(region="高雄", window_days=30, categories=["市集"])
        self.assertFalse(discover.call_args.kwargs["request_invitation_scan"])
        self.assertEqual(calls, ["ready", "scan"])
        scan.assert_called_once()

    def test_ingest_can_suppress_hook_even_when_legacy_auto_scan_is_enabled(self):
        candidate = {
            "title": "活動 A", "snippet": "有明確日期與地點的來源", "source_url": "https://example.com/a",
            "discovery_category": "市集", "skill_name": "event-market-discovery", "skill_version": "1",
        }
        event = {
            "event_id": "test-event", "dedupe_key": "test-event", "title": "活動 A", "category": "市集",
            "venue": "高雄港", "starts_at": 1_800_000_000, "ends_at": 1_800_003_600,
            "source_url": "https://example.com/a", "tags": ["戶外"], "vibes": ["輕鬆"],
        }
        for enabled in (False, True):
            with self.subTest(enabled=enabled), contextlib.ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, {
                    "EVENT_ADAPTIVE_SUPPLEMENTAL_SEARCH": "off",
                    "EVENT_OPPORTUNITY_AUTO_SCAN_ENABLED": "on",
                }))
                stack.enter_context(patch.object(discovery, "_active_event_inventory", return_value={
                    "status": "unavailable", "category_counts": {"市集": 0},
                }))
                stack.enter_context(patch.object(discovery, "_reconcile_event_inventory", return_value={
                    "status": "success", "category_counts": {},
                }))
                stack.enter_context(patch.object(discovery, "_bounded_search_results", return_value=([candidate], [])))
                stack.enter_context(patch.object(discovery, "_cached_extraction", return_value=None))
                stack.enter_context(patch.object(discovery, "_store_cached_extraction"))
                stack.enter_context(patch.object(discovery, "_post_event_ingest", return_value={
                    "status": "success", "ingested_count": 1, "events": [event],
                }))
                stack.enter_context(patch.object(discovery, "project_event_relevance", return_value={"status": "success"}))
                request_scan = stack.enter_context(patch.object(discovery, "request_event_opportunity_scan"))
                arguments = {} if enabled else {"request_invitation_scan": False}
                result = discovery.discover_and_ingest_events(
                    region="高雄", window_days=30, categories=["市集"], **arguments,
                )
            self.assertEqual(result["ingested_count"], 1)
            self.assertEqual(request_scan.call_count, int(enabled))


if __name__ == "__main__":
    unittest.main()
