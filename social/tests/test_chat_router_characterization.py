"""Safety-net tests for the incremental ``routers.chat`` refactor.

These tests intentionally exercise the public HTTP boundary and routing
decisions without moving production code.  Later router-extraction phases
must preserve these contracts unless their API change is explicitly reviewed.
"""

import asyncio
import json
import sys
import unittest
from unittest.mock import ANY, patch

from fastapi import BackgroundTasks, HTTPException

from models import DirectChatRequest, MediatorPrivateRequest
# Offline suite injects a minimal config module before discovery.
if "config" in sys.modules and not hasattr(sys.modules["config"], "OLLAMA_FAST_CHAT_MODEL"):
    setattr(sys.modules["config"], "OLLAMA_FAST_CHAT_MODEL", "test")

from routers.chat import router
from routers.risk_actions import RiskFeedbackRequest, SenderAppealProxyRequest
from services import proactive_delivery_service


async def _collect(response):
    return [chunk async for chunk in response.body_iterator]


EXPECTED_ROUTES = {
    # (method, path): (request body model, ordered query parameters)
    ("GET", "/api/contacts"): (None, ("user_id",)),
    ("GET", "/api/mediator/private/{other_id}"): (None, ("user_id",)),
    ("GET", "/api/messages/{contact_id}"): (None, ("user_id",)),
    ("GET", "/api/proactive_check"): (None, ("user_id", "conversation_active")),
    ("GET", "/api/relationship/date/state"): (None, ("user_id", "other_id")),
    ("GET", "/api/relationship/fun/{other_id}"): (None, ("user_id",)),
    ("POST", "/api/chat"): ("ChatRequest", ()),
    ("POST", "/api/chat/reset"): ("ResetRequest", ()),
    ("POST", "/api/demo/reset_db_state"): (None, ()),
    ("POST", "/api/demo/clear_graph"): (None, ()),
    ("POST", "/api/demo/clear_all"): (None, ()),
    ("POST", "/api/direct_chat"): ("DirectChatRequest", ()),
    ("POST", "/api/direct_chat/stream"): ("DirectChatRequest", ()),
    ("POST", "/api/public-ayue/onboarding/complete"): ("ClearRequest", ()),
    ("POST", "/api/mediator/private"): ("MediatorPrivateRequest", ()),
    ("POST", "/api/mediator/private/stream"): ("MediatorPrivateRequest", ()),
    ("POST", "/api/pair/risk_appeal"): ("SenderAppealProxyRequest", ()),
    ("POST", "/api/pair/risk_feedback"): ("RiskFeedbackRequest", ()),
    ("POST", "/api/presence"): ("ClearRequest", ()),
    ("POST", "/api/relationship/date/cancel"): ("CalendarActionRequest", ("other_id", "coordination_id")),
    ("POST", "/api/relationship/date/confirm"): ("DateConfirmRequest", ()),
    ("POST", "/api/relationship/date/invite/respond"): ("DateInviteResponseRequest", ()),
    ("POST", "/api/relationship/date/update"): ("DateUpdateRequest", ()),
    ("POST", "/api/relationship/quiz/answer"): ("RelationshipQuizAnswerRequest", ()),
    ("POST", "/api/relationship/quiz/cancel"): ("RelationshipGameRequest", ()),
    ("POST", "/api/relationship/quiz/start"): ("RelationshipGameRequest", ()),
}

EXPECTED_EXTRACTED_ROUTE_MODULES = {
    ("POST", "/api/chat"): "routers.chat_onboarding",
    ("POST", "/api/chat/reset"): "routers.chat_onboarding",
    ("GET", "/api/messages/{contact_id}"): "routers.chat_messages",
    ("GET", "/api/contacts"): "routers.chat_messages",
    ("GET", "/api/mediator/private/{other_id}"): "routers.private_mediator",
    ("POST", "/api/mediator/private"): "routers.private_mediator",
    ("POST", "/api/mediator/private/stream"): "routers.private_mediator",
    ("POST", "/api/relationship/date/invite/respond"): "routers.relationship_dates",
    ("GET", "/api/relationship/date/state"): "routers.relationship_dates",
    ("POST", "/api/relationship/date/update"): "routers.relationship_dates",
    ("POST", "/api/relationship/date/confirm"): "routers.relationship_dates",
    ("POST", "/api/relationship/date/cancel"): "routers.relationship_dates",
    ("GET", "/api/relationship/fun/{other_id}"): "routers.relationship_quiz",
    ("POST", "/api/relationship/quiz/start"): "routers.relationship_quiz",
    ("POST", "/api/relationship/quiz/answer"): "routers.relationship_quiz",
    ("POST", "/api/relationship/quiz/cancel"): "routers.relationship_quiz",
    ("POST", "/api/demo/reset_db_state"): "routers.demo",
    ("GET", "/api/proactive_check"): "routers.proactive",
    ("POST", "/api/direct_chat"): "routers.public_chat",
    ("POST", "/api/direct_chat/stream"): "routers.public_chat",
    ("POST", "/api/public-ayue/onboarding/complete"): "routers.chat_messages",
}


def _route(method: str, path: str):
    for item in router.routes:
        if item.path == path and method in item.methods:
            return item
    raise AssertionError(f"missing route: {method} {path}")


def _route_module(method: str, path: str):
    endpoint = _route(method, path).endpoint
    return endpoint, sys.modules[endpoint.__module__]


class ChatRouterCharacterizationTests(unittest.TestCase):
    def test_assessment_action_is_scoped_to_public_ayue(self):
        request = DirectChatRequest(
            user_id="owner", contact_id="ai_assistant", message="退出測驗",
            assessment_action="cancel",
        )
        self.assertEqual(request.assessment_action, "cancel")
        with self.assertRaises(ValueError):
            DirectChatRequest(
                user_id="owner", contact_id="other", message="退出測驗",
                assessment_action="cancel",
            )

    def test_router_keeps_the_current_chat_http_surface(self):
        actual = {
            (method, route.path): (
                route.body_field.type_.__name__ if route.body_field else None,
                tuple(item.name for item in route.dependant.query_params),
            )
            for route in router.routes
            for method in route.methods
            if method not in {"HEAD", "OPTIONS"}
        }
        self.assertEqual(actual, EXPECTED_ROUTES)

    def test_extracted_endpoints_are_owned_by_dedicated_routers(self):
        actual = {
            key: _route(*key).endpoint.__module__
            for key in EXPECTED_EXTRACTED_ROUTE_MODULES
        }
        self.assertEqual(actual, EXPECTED_EXTRACTED_ROUTE_MODULES)

    def test_extracted_endpoints_keep_one_chat_openapi_tag(self):
        actual = {
            key: _route(*key).tags
            for key in EXPECTED_EXTRACTED_ROUTE_MODULES
        }
        self.assertEqual(actual, {key: ["Chat"] for key in EXPECTED_EXTRACTED_ROUTE_MODULES})

    def test_non_public_stream_keeps_its_direct_chat_contract(self):
        req = DirectChatRequest(user_id="owner", contact_id="contact", message="嗨")
        expected = {"reply": "嗨，想聊什麼？"}
        endpoint, module = _route_module("POST", "/api/direct_chat/stream")
        with patch.object(module, "direct_chat", return_value=expected) as direct, \
             patch.object(module, "_run_public_stream_turn") as public_stream:
            response = endpoint(req, BackgroundTasks())
            chunks = asyncio.run(_collect(response))

        self.assertEqual([json.loads(chunk) for chunk in chunks], [{"type": "final", "response": expected}])
        direct.assert_called_once()
        public_stream.assert_not_called()

    def test_private_stream_requires_an_accepted_match(self):
        req = MediatorPrivateRequest(user_id="owner", other_id="other", message="想問問")
        endpoint, module = _route_module("POST", "/api/mediator/private/stream")
        with patch.object(module, "find_accepted_match", return_value=None):
            with self.assertRaises(HTTPException) as raised:
                endpoint(req, BackgroundTasks())

        self.assertEqual(raised.exception.status_code, 403)

    def test_private_stream_keeps_the_ndjson_final_contract_after_extraction(self):
        req = MediatorPrivateRequest(user_id="owner", other_id="other", message="想問問")
        expected = {
            "reply": "我幫你整理好了。", "pending_step": None,
            "agent_run_id": "private-run", "agent_mode": "v2", "agent_version": "v2",
            "conversation_intent": "advice",
        }
        endpoint, module = _route_module("POST", "/api/mediator/private/stream")
        with patch.object(module, "find_accepted_match", return_value={"_id": "match-1"}), \
             patch.object(module, "save_message", return_value={"message_id": "owner-message"}), \
             patch.object(module, "queue_profile_skills"), \
             patch.object(module, "_run_private_v2_saved_turn", return_value=expected):
            response = endpoint(req, BackgroundTasks())
            chunks = asyncio.run(_collect(response))

        self.assertEqual(
            [json.loads(chunk) for chunk in chunks],
            [
                {"type": "run_started", "agent_run_id": ANY},
                {"type": "final", "response": expected},
            ],
        )

    def test_private_json_v2_saves_owner_once_then_delegates_without_runtime_fallthrough(self):
        req = MediatorPrivateRequest(user_id="owner", other_id="other", message="想問問")
        match = {"_id": "match-1", "status": "accepted"}
        expected = {"reply": "我幫你整理好了。", "agent_version": "v2"}
        endpoint, module = _route_module("POST", "/api/mediator/private")
        tasks = BackgroundTasks()
        with patch.object(module, "find_accepted_match", return_value=match), \
             patch.object(module, "generate_mediator_private_room_id", return_value="private-room"), \
             patch.object(module, "save_message", return_value={"message_id": "owner-message"}) as save, \
             patch.object(module.profiles_coll, "update_one"), \
             patch.object(module.profiles_coll, "find_one", return_value={"user_id": "owner"}), \
             patch.object(module, "queue_profile_skills") as queue_profile, \
             patch.object(module, "consume_pending_probe_answer", return_value=None), \
             patch.object(module, "_run_private_v2_saved_turn", return_value=expected) as run_v2:
            response = endpoint(req, tasks)

        self.assertEqual(response, expected)
        save.assert_called_once_with("private-room", "owner", "想問問")
        queue_profile.assert_not_called()
        run_v2.assert_called_once_with(req, match, "private-room")

    def test_proactive_check_for_unknown_user_has_no_side_effects(self):
        with patch.object(proactive_delivery_service.profiles_coll, "find_one", return_value=None), \
             patch.object(proactive_delivery_service, "queue_due_feedback") as queue_feedback, \
             patch.object(proactive_delivery_service, "claim_next_mediator_event") as claim_event, \
             patch.object(proactive_delivery_service, "consume_proactive_delivery") as consume_delivery:
            response = proactive_delivery_service.proactive_check("missing-user")

        self.assertEqual(response, {"has_new": False})
        queue_feedback.assert_not_called()
        claim_event.assert_not_called()
        consume_delivery.assert_not_called()

    def test_active_conversation_does_not_consume_care_delivery(self):
        with patch.object(proactive_delivery_service.profiles_coll, "find_one", return_value={"user_id": "owner"}), \
             patch.object(proactive_delivery_service.profiles_coll, "find_one_and_update", return_value=None), \
             patch.object(proactive_delivery_service, "queue_due_feedback"), \
             patch.object(proactive_delivery_service, "claim_next_mediator_event", return_value=None), \
             patch.object(proactive_delivery_service, "consume_proactive_delivery") as consume_delivery:
            response = proactive_delivery_service.proactive_check("owner", conversation_active=True)

        self.assertEqual(response, {"has_new": False})
        consume_delivery.assert_not_called()


if __name__ == "__main__":
    unittest.main()
