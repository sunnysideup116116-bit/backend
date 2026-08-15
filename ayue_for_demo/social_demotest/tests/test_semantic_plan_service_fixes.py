import unittest
import os
from unittest.mock import patch, MagicMock
from services.semantic_plan_service import (
    process_relationship_semantic_plan,
    _estimate_message_tokens,
    get_relationship_semantic_context,
)
from services.ayue_agent.private_v2 import (
    PrivateAgentTurnContextV2,
    _planner_prompt,
    _compose,
)

class TestSemanticPlanFixes(unittest.TestCase):
    @patch("services.semantic_plan_service.messages_coll")
    @patch("services.semantic_plan_service.get_or_reset_semantic_plan")
    def test_high_message_count_below_token_threshold_does_not_trigger(self, mock_get_plan, mock_messages_coll):
        # 10 messages but short content
        mock_messages_coll.count_documents.return_value = 10
        mock_get_plan.return_value = {"last_processed_message_count": 0}
        
        mock_cursor = MagicMock()
        mock_cursor.sort.return_value.skip.return_value = [
            {"sender_id": "A", "content": "hi"} for _ in range(10)
        ]
        mock_messages_coll.find.return_value = mock_cursor
        
        result = process_relationship_semantic_plan({"from_user": "A", "to_user": "B", "_id": "123"}, "room1")
        self.assertEqual(result.get("status"), "skipped")
        self.assertEqual(result.get("reason"), "buffer_not_full")

    @patch("services.semantic_plan_service._write_triples_to_neo4j")
    @patch("services.semantic_plan_service.semantic_plans_coll")
    @patch("services.semantic_plan_service.generate_chat_completion")
    @patch("services.semantic_plan_service.messages_coll")
    @patch("services.semantic_plan_service.get_or_reset_semantic_plan")
    def test_token_threshold_triggers_semantic_update_and_chat_log_format(self, mock_get_plan, mock_messages_coll, mock_generate, mock_plans_coll, mock_neo4j):
        mock_messages_coll.count_documents.return_value = 2
        mock_get_plan.return_value = {"last_processed_message_count": 0}
        
        long_message = "x" * 300
        mock_cursor = MagicMock()
        mock_cursor.sort.return_value.skip.return_value = [
            {"sender_id": "A", "content": long_message},
            {"sender_id": "B", "content": long_message}
        ]
        
        mock_cursor_real = MagicMock()
        mock_cursor_real.sort.return_value.limit.return_value = [
            {"sender_id": "A", "content": "hi"},
            {"sender_id": "B", "content": "hello"}
        ]
        
        mock_messages_coll.find.side_effect = [mock_cursor, mock_cursor_real]
        
        mock_response = MagicMock()
        mock_response.content = '{"macro_summary": "ok"}'
        mock_generate.return_value = mock_response
        
        result = process_relationship_semantic_plan({"from_user": "A", "to_user": "B", "_id": "123"}, "room1")
        self.assertEqual(result.get("status"), "updated")
        
        prompt_used = mock_generate.call_args[0][0]
        self.assertIn("User A: hi", prompt_used)
        self.assertIn("User B: hello", prompt_used)
        self.assertNotIn("{message.get('sender_id')}", prompt_used)

    @patch("services.semantic_plan_service.semantic_plans_coll")
    @patch("services.semantic_plan_service._read_triples_from_neo4j")
    def test_private_v2_context_reads_get_relationship_semantic_context(self, mock_neo4j, mock_plans_coll):
        mock_plans_coll.find_one.return_value = {
            "current_role": "FRIEND",
            "context": {"macro_summary": "They are friends"},
            "strategy": {"theme": "casual", "action_plan": "be nice", "dynamic_content_bounds": ["no politics"]}
        }
        mock_neo4j.return_value = [{"subject": "A", "predicate": "LIKES", "object": "coffee"}]
        
        res = get_relationship_semantic_context({"from_user": "A", "to_user": "B"}, "room1")
        self.assertEqual(res["semantic_plan"]["current_role"], "FRIEND")
        self.assertEqual(res["semantic_plan"]["context"]["macro_summary"], "They are friends")
        self.assertEqual(len(res["knowledge_graph_triples"]), 1)

    @patch("services.ayue_agent.private_v2.generate_chat_completion")
    def test_private_prompts_do_not_include_raw_content(self, mock_generate):
        mock_response = MagicMock()
        mock_response.content = '{"kind": "final", "intent": "advice", "reply": "hi", "strategy": "warm"}'
        mock_generate.return_value = mock_response

        ctx = PrivateAgentTurnContextV2(
            user_id="A", other_id="B", room_id="room1", message="hello", pair_revision=1,
            viewer_profile={"raw": "secret"}, counterparty_shareable={"name": "B"},
            counterparty_advisory={}, shared_history=[], private_history=[], shared_facts=[], local_time="now",
            relationship_semantic_context={
                "semantic_plan": {
                    "current_role": "ADVISOR",
                    "context": {"macro_summary": "ok", "raw_db": "BAD"},
                    "strategy": {"theme": "fun", "action_plan": "go", "dynamic_content_bounds": [], "secret": "BAD"}
                },
                "knowledge_graph_triples": [{"subject": "A", "predicate": "LIKES", "object": "B", "raw_neo4j": "BAD"}]
            }
        )
        prompt = _planner_prompt(ctx, [])
        self.assertIn("ADVISOR", prompt)
        self.assertIn("macro_summary", prompt)
        self.assertNotIn("raw_db", prompt)
        self.assertNotIn("raw_neo4j", prompt)
        
        mock_compose_response = MagicMock()
        mock_compose_response.content = "composed reply"
        mock_generate.return_value = mock_compose_response
        compose_prompt = ""
        def side_effect(p, **kwargs):
            nonlocal compose_prompt
            compose_prompt = p
            return mock_compose_response
        mock_generate.side_effect = side_effect
        _compose(ctx, [], "warm")
        
        self.assertNotIn("theme", compose_prompt)
        self.assertNotIn("action_plan", compose_prompt)
        self.assertIn("macro_summary", compose_prompt)
        self.assertIn("ADVISOR", compose_prompt)
        self.assertNotIn("raw_db", compose_prompt)

    def test_private_mediator_route_does_not_call_semantic_updater(self):
        route_file = os.path.join(os.path.dirname(__file__), "..", "routers", "private_mediator.py")
        if os.path.exists(route_file):
            with open(route_file, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertNotIn("process_relationship_semantic_plan", content)

if __name__ == "__main__":
    unittest.main()
