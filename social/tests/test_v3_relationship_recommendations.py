import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from services.ayue_agent.contracts import AgentTurnContext, ToolCall
from services.ai_service import ToolCallResult
from services.ayue_agent.tools import execute_tool
from services.ayue_agent.v3.contracts import (
    AgentContextSlice,
    SubTask,
    SubTaskResult,
    SubTaskStatus,
    ToolProposal,
)
from services.ayue_agent.v3 import relationship_recommendations
from services.ayue_agent.v3 import relationship_runtime
from services.ayue_agent.v3.sub_agents.base import SubAgentMetrics
from services.ayue_agent.v3.sub_agents.relationship_agent import (
    RecommendationCandidate,
    RelationshipRecommendationDecision,
    finish_recommendation,
)


def _metrics():
    return SubAgentMetrics(
        llm_call_count=1, input_tokens=1, output_tokens=1, duration_ms=1,
    )


class _FakeRelationshipServices:
    def __init__(self):
        self.run_id = "a" * 32
        self.trace = {"guard_results": [], "tool_results": []}
        self.turn_ctx = AgentTurnContext(
            user_id="owner", room_id="room", message="獵人展約誰", message_id="owner-message-1",
        )
        self.calls = []

    def execute(self, proposal, **kwargs):
        self.calls.append((proposal.tool_name, proposal.arguments))
        if proposal.tool_name == "relationship.list_accepted_contacts":
            data = {
                "contacts": [{
                    "contact_ref": "relref_one",
                    "display_name": "小宇",
                    "recent_context": "規劃潛水",
                    "initial_interest": "",
                    "personality_summary": "直率",
                    "safe_match_reason": "",
                    "verified_common_ground": [],
                    "distinctive_tags": [],
                    "evidence_available": True,
                }],
                "truncated": False,
                "total_count": 1,
            }
        else:
            data = {
                "contacts": [{
                    "contact_ref": "relref_one",
                    "display_name": "小宇",
                    "recent_context": "規劃潛水",
                    "initial_interest": "",
                    "personality_summary": "直率",
                    "safe_match_reason": "",
                    "verified_common_ground": [],
                    "distinctive_tags": [],
                    "evidence_fields": ["recent_context", "personality_summary"],
                    "truncated": False,
                }],
                "unavailable_refs": [],
            }
        return SimpleNamespace(
            attempted=True,
            result=SubTaskResult(
                task_id="r1", status=SubTaskStatus.OK,
                tool_name=proposal.tool_name, observation=data,
            ),
        )


class RelationshipRecommendationTests(unittest.TestCase):
    def test_finish_recommendation_returns_typed_evidence_classification(self):
        context = AgentContextSlice(
            agent="relationship",
            payload={"message": "獵人展約誰", "recent_context": "", "relevant_memories": []},
        )
        provider_result = ToolCallResult(content="", tool_calls=[{
            "name": "finish_relationship_recommendation",
            "arguments": {
                "status": "exploratory",
                "activity": "獵人展",
                "recommendations": [{
                    "contact_ref": "relref_one",
                    "classification": "exploratory",
                    "evidence_fields": ["personality_summary"],
                    "reason": "喜歡嘗試新事物，可能願意一起體驗",
                    "unknowns": ["不知道是否喜歡獵人"],
                }],
                "unknowns": ["不知道是否喜歡獵人"],
            },
        }])
        with patch(
            "services.ayue_agent.v3.sub_agents.relationship_agent.generate_chat_completion_with_tools",
            return_value=provider_result,
        ):
            decision, metrics = finish_recommendation(
                context,
                task_brief="評估誰適合一起去看獵人展",
                observations=[],
            )
        self.assertEqual(decision.status, "exploratory")
        self.assertEqual(decision.recommendations[0].evidence_fields, ["personality_summary"])
        self.assertEqual(metrics.llm_call_count, 1)

    def test_contact_refs_are_scoped_to_owner_message_and_revalidated(self):
        match = {
            "from_user": "owner", "to_user": "contact-1", "status": "accepted",
            "reason_items": [], "updated_at": 2,
        }
        profile = {
            "display_name": "小宇", "current_context": "規劃潛水",
            "initial_interest": "", "big_five": {"summary": "直率"},
        }
        first = AgentTurnContext(
            user_id="owner", room_id="room", message="獵人展約誰", message_id="message-1",
        )
        second = first.model_copy(update={"message_id": "message-2"})
        with patch(
            "services.ayue_agent.public_relationship_projection.matches_coll.find",
            return_value=[match],
        ), patch(
            "services.ayue_agent.public_relationship_projection.profiles_coll.find_one",
            return_value=profile,
        ):
            first_list = execute_tool(
                ToolCall(name="relationship.list_accepted_contacts", arguments={}), first,
            )
            second_list = execute_tool(
                ToolCall(name="relationship.list_accepted_contacts", arguments={}), second,
            )
            first_ref = first_list.data["contacts"][0]["contact_ref"]
            second_ref = second_list.data["contacts"][0]["contact_ref"]
            self.assertNotEqual(first_ref, second_ref)
            stale = execute_tool(ToolCall(
                name="relationship.get_contact_evidence",
                arguments={"contact_refs": [first_ref]},
            ), second)
            self.assertTrue(stale.ok)
            self.assertEqual(stale.data["contacts"], [])
            self.assertEqual(stale.data["unavailable_refs"], [first_ref])

    def test_contact_list_database_failure_is_not_reported_as_zero_contacts(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="我有幾個聯絡人")
        with patch(
            "services.ayue_agent.tools.accepted_contact_summaries",
            side_effect=RuntimeError("database unavailable"),
        ):
            result = execute_tool(
                ToolCall(name="relationship.list_accepted_contacts", arguments={}), ctx,
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "accepted_contact_list_unavailable")

    def test_recommendation_runtime_reads_once_then_finishes_once(self):
        context = AgentContextSlice(
            agent="relationship",
            payload={
                "message": "獵人展可以約誰去",
                "recent_messages": [],
                "recent_context": "",
                "relevant_memories": ["喜歡衝浪"],
                "prior_observations": [],
            },
        )
        task = SubTask(
            id="r1", agent="relationship", relationship_intent="recommend",
            task_brief="從已接受聯絡人中評估誰適合一起去看獵人展，說明直接依據與未知條件",
        )
        services = _FakeRelationshipServices()
        proposals = [
            [ToolProposal(tool_name="relationship.list_accepted_contacts", arguments={})],
            [ToolProposal(tool_name="relationship.get_contact_evidence", arguments={"contact_refs": ["relref_one"]})],
        ]
        decision = RelationshipRecommendationDecision(
            status="exploratory",
            activity="獵人展",
            recommendations=[RecommendationCandidate(
                contact_ref="relref_one",
                classification="exploratory",
                evidence_fields=["personality_summary"],
                reason="直率的行動風格可能適合一起解謎",
                unknowns=["不知道他是否喜歡獵人"],
            )],
            unknowns=["不知道他是否喜歡獵人"],
        )
        with patch(
            "services.ayue_agent.v3.relationship_runtime.relationship_agent.run",
            side_effect=[
                (proposals[0], _metrics()),
                (proposals[1], _metrics()),
            ],
        ), patch(
            "services.ayue_agent.v3.relationship_runtime.relationship_agent.finish_recommendation",
            return_value=(decision, _metrics()),
        ):
            result, metrics = relationship_runtime.run(context, task=task, services=services)
        observation = result.completed_results[0].observation
        self.assertEqual(observation["schema_version"], "relationship_recommendation.v1")
        self.assertEqual(
            [name for name, _args in services.calls],
            ["relationship.list_accepted_contacts"],
        )
        self.assertEqual(observation["candidate_pool"][0]["display_name"], "小宇")
        self.assertEqual(observation["evidence"], [])
        self.assertEqual(observation["recommendations"][0]["classification"], "exploratory")
        self.assertEqual(observation["recommended_candidate_refs"], ["relref_one"])
        self.assertIn("不知道他是否喜歡獵人", observation["unknowns"])
        self.assertEqual(metrics.llm_call_count, 1)

    def test_snapshot_is_room_scoped_and_expires(self):
        snapshot = {
            "schema_version": "relationship_recommendation.v1",
            "intent": "recommend",
            "request": "獵人展可以約誰去",
            "candidate_refs": ["relref_one"],
            "candidate_pool": [{"contact_ref": "relref_one", "display_name": "小宇"}],
            "evidence": [], "unknowns": ["不知道對方是否喜歡獵人"],
            "status": "evidence_collected", "stop_reason": "model_finished",
        }
        with patch.dict(os.environ, {"AYUE_TEST_MODE": "on"}, clear=False):
            relationship_recommendations.clear_runtime_state()
            record = relationship_recommendations.save_snapshot(
                "owner", "room-a", "run-1", "reply-1", snapshot,
            )
            self.assertIsInstance(record["expires_at"], datetime)
            self.assertGreater(record["expires_at_epoch"], record["published_at_epoch"])
            self.assertIsNotNone(relationship_recommendations.get_snapshot("owner", "room-a"))
            self.assertIsNone(relationship_recommendations.get_snapshot("owner", "room-b"))
            projection = relationship_recommendations.public_projection(
                relationship_recommendations.get_snapshot("owner", "room-a"),
            )
            self.assertEqual(projection["candidate_refs"], ["relref_one"])
            relationship_recommendations.clear_runtime_state()


if __name__ == "__main__":
    unittest.main()
