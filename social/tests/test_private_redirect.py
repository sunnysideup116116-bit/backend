import inspect
import json
import unittest
from unittest.mock import MagicMock, patch

from models import MediatorPrivateRequest
from services.ayue_agent.private_contracts import (
    PrivateAgentDecision,
    PrivateAgentResult,
    PrivateSurfaceHandoff,
)
from services.ayue_agent.private_v2 import (
    PrivateAgentTurnContextV2,
    _plan,
    _planner_prompt,
    _viewer_availability,
    run_private_agent_turn_v2,
)
from routers import private_mediator


def _context(message: str = "幫我看看明天台積電會不會漲"):
    return PrivateAgentTurnContextV2(
        user_id="owner",
        other_id="other",
        room_id="private",
        message=message,
        pair_revision=1,
        viewer_profile={},
        counterparty_shareable={"display_name": "對方"},
        counterparty_advisory={},
        shared_history=[],
        private_history=[],
        shared_facts=[],
        local_time="2026-08-09 12:00",
    )


class PrivateRedirectTests(unittest.TestCase):
    def test_decision_contract_requires_typed_redirect_without_tool_payload(self):
        decision = PrivateAgentDecision(
            kind="redirect",
            intent="out_of_scope",
            redirect_target="public_ayue",
            confidence=.9,
            evidence_span="台積電",
        )
        self.assertEqual(decision.redirect_target, "public_ayue")
        with self.assertRaises(ValueError):
            PrivateAgentDecision(
                kind="redirect",
                intent="out_of_scope",
                redirect_target="public_ayue",
                tool_name="private.relationship.get_shared_history",
            )

    def test_redirect_is_semantic_decision_and_executes_no_tool_or_confirmation(self):
        ctx = _context()
        decision = PrivateAgentDecision(
            kind="redirect",
            intent="out_of_scope",
            redirect_target="public_ayue",
            confidence=.9,
            evidence_span="台積電",
        )
        with patch("services.ayue_agent.private_v2.build_private_turn_context_v2", return_value=ctx), \
             patch("services.ayue_agent.private_v2._plan", return_value=decision), \
             patch("services.ayue_agent.private_v2._execute_read") as execute_read, \
             patch("services.ayue_agent.private_v2._save_confirmation") as save_confirmation, \
             patch("services.ayue_agent.private_v2._execute_write") as execute_write, \
             patch("services.ayue_agent.private_v2._trace"):
            result = run_private_agent_turn_v2(
                user_id="owner",
                other_id="other",
                message=ctx.message,
                match_doc={"status": "accepted", "proposal_revision": 1},
            )
        self.assertIsInstance(result, PrivateAgentResult)
        self.assertEqual(result.conversation_intent, "private_redirect")
        self.assertEqual(result.handoff.original_message, ctx.message)
        execute_read.assert_not_called()
        save_confirmation.assert_not_called()
        execute_write.assert_not_called()

    def test_planner_provider_redirect_is_parsed_as_typed_decision(self):
        ctx = _context("幫我查一下附近咖啡廳")
        payload = {
            "kind": "redirect",
            "intent": "out_of_scope",
            "tool_name": None,
            "arguments": {},
            "confidence": 0.91,
            "evidence_span": "附近咖啡廳",
            "strategy": "warm",
            "reply": "",
            "redirect_target": "public_ayue",
        }
        with patch(
            "services.ayue_agent.private_v2.generate_chat_completion",
            return_value=MagicMock(content=json.dumps(payload, ensure_ascii=False)),
        ):
            decision = _plan(ctx, [])
        self.assertIsNotNone(decision)
        self.assertEqual(decision.kind, "redirect")
        self.assertEqual(decision.intent, "out_of_scope")
        self.assertEqual(decision.redirect_target, "public_ayue")

    def test_planner_prompt_states_goal_first_scope_policy(self):
        prompt = _planner_prompt(_context("幫我查一下附近咖啡廳"), [])
        self.assertIn("主要 goal 是否直接服務目前兩人的 relationship", prompt)
        self.assertIn("不要因為任何單一 domain noun 就決定 scope", prompt)
        self.assertIn("intent=out_of_scope", prompt)
        self.assertIn("redirect_target=public_ayue", prompt)
        self.assertIn("不得轉成程式 keyword/regex router", prompt)

    def test_handoff_original_message_is_server_owned(self):
        result = PrivateAgentResult(
            handled=True,
            reply="redirect",
            conversation_intent="private_redirect",
            handoff=PrivateSurfaceHandoff(original_message="original user text"),
        )
        self.assertEqual(result.handoff.original_message, "original user text")
        self.assertFalse(result.handoff.auto_send)

    def test_json_adapter_propagates_typed_action_and_persists_safe_metadata(self):
        req = MediatorPrivateRequest(user_id="owner", other_id="other", message="幫我看看明天台積電會不會漲")
        result = PrivateAgentResult(
            handled=True,
            reply="請到主聊天室處理。",
            conversation_intent="private_redirect",
            agent_run_id="run-1",
            agent_mode="v2",
            handoff=PrivateSurfaceHandoff(original_message=req.message),
        )
        with patch.object(private_mediator, "run_private_agent_turn_v2", return_value=result), \
             patch.object(private_mediator, "save_message") as save:
            response = private_mediator._run_private_v2_saved_turn(
                req, {"status": "accepted", "proposal_revision": 1}, "private-room",
            )
        self.assertEqual(response["handoff"]["original_message"], req.message)
        self.assertEqual(response["actions"][0]["kind"], "navigate_public_prefill")
        self.assertEqual(response["actions"][0]["value"], req.message)
        metadata = save.call_args.kwargs["metadata"]
        self.assertEqual(metadata["handoff"]["original_message"], req.message)
        self.assertNotIn("private_history", metadata)
        self.assertNotIn("observations", metadata)

    def test_viewer_availability_returns_busy_only(self):
        ctx = _context("我星期六有沒有空可以約她？")
        calendar_context = {
            "viewer_events": [{
                "event_id": "secret-id",
                "title": "private title",
                "location": "private location",
                "start_at": "2026-08-08T01:00:00+00:00",
                "end_at": "2026-08-08T02:00:00+00:00",
            }],
            "partner_busy": [],
        }
        with patch("services.calendar_service.calendar_access_enabled", return_value=True), \
             patch("services.calendar_service.get_calendar_context", return_value=calendar_context):
            result = _viewer_availability(ctx, "2026/8/8")
        self.assertEqual(set(result), {"access", "busy", "truncated"})
        self.assertEqual(result["busy"], [{
            "start_at": "2026-08-08T01:00:00+00:00",
            "end_at": "2026-08-08T02:00:00+00:00",
            "busy": "true",
        }])
        self.assertNotIn("secret-id", str(result))
        self.assertNotIn("private title", str(result))

    def test_scope_is_not_implemented_as_a_python_keyword_router(self):
        import services.ayue_agent.private_v2 as private_v2

        source = inspect.getsource(private_v2)
        for forbidden in (
            "GENERAL_TASK_KEYWORDS",
            "RELATIONSHIP_KEYWORDS",
            "REDIRECT_PATTERNS",
            "PRIVATE_SCOPE_PATTERNS",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
