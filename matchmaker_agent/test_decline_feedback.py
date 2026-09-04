"""Selected feedback -> existing normalizer -> canonical Concept writer, no network."""

import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

with patch.dict(os.environ, {"LLM_API_KEY": "stub", "LLM_BASE_URL": "http://provider.invalid/v1",
                             "LLM_MODEL_ID": "stub", "OLLAMA_HOST": "http://127.0.0.1:9"}), \
        patch("dotenv.load_dotenv", return_value=False):
    import agent_api


def feedback(reasons, action="decline"):
    return agent_api.FeedbackRequest(
        user_id="owner", target_id="other", action=action,
        target_traits={"summary": "unselected-private-trait"}, explicit_reasons=reasons,
    )


def reflection(labels, relation="DISLIKES_TRAIT"):
    return json.dumps({"relationships": [
        {"relation_type": relation, "trait": label} for label in labels
    ]}, ensure_ascii=False)


class DeclineFeedbackTests(unittest.TestCase):
    def test_memory_marker_and_edges_share_one_write_transaction(self):
        driver, session, transaction = MagicMock(), MagicMock(), MagicMock()
        driver.__enter__.return_value = driver
        driver.session.return_value.__enter__.return_value = session
        transaction.run.return_value.single.return_value = {"created": True}
        session.execute_write.side_effect = lambda callback: callback(transaction)
        request = agent_api.MemoryApplyRequest(
            user_id="owner", message_id="message-atomic", surface="profile",
            memories=[{"key": "quiet_cafe", "label": "安靜咖啡廳",
                       "stance": "like", "confidence": 0.95}],
        )
        with patch.object(agent_api.GraphDatabase, "driver", return_value=driver):
            result = asyncio.run(agent_api.apply_memory(request))
        self.assertEqual(result["status"], "success")
        session.execute_write.assert_called_once()
        self.assertEqual(transaction.run.call_count, 2)
        self.assertIn("MemoryObservation", transaction.run.call_args_list[0].args[0])
        self.assertIn("UNWIND $memories", transaction.run.call_args_list[1].args[0])

    def test_duplicate_marker_reports_prior_atomic_memory_as_applied(self):
        driver, session, transaction = MagicMock(), MagicMock(), MagicMock()
        driver.__enter__.return_value = driver
        driver.session.return_value.__enter__.return_value = session
        transaction.run.return_value.single.return_value = {"created": False}
        session.execute_write.side_effect = lambda callback: callback(transaction)
        request = agent_api.MemoryApplyRequest(
            user_id="owner", message_id="message-duplicate", surface="profile",
            memories=[{"key": "quiet_cafe", "label": "安靜咖啡廳",
                       "stance": "like", "confidence": 0.95}],
        )
        with patch.object(agent_api.GraphDatabase, "driver", return_value=driver):
            result = asyncio.run(agent_api.apply_memory(request))
        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(result["memories"][0]["key"], "quiet_cafe")
        self.assertEqual(transaction.run.call_count, 1)

    def test_bare_decline_never_calls_provider_or_graph(self):
        for reasons in ([], ["", "  "]):
            with self.subTest(reasons=reasons), \
                    patch.object(agent_api.agent, "generate_graph_reflection") as normalize, \
                    patch.object(agent_api, "apply_memory", new_callable=AsyncMock) as write:
                result = asyncio.run(agent_api.receive_feedback(feedback(reasons)))
                self.assertEqual(result["status"], "skipped")
                self.assertEqual(result["memories"], [])
                normalize.assert_not_called()
                write.assert_not_called()

    def test_selected_list_is_passed_intact_without_unselected_traits_or_old_history(self):
        reasons = ["個性：夜生活", "近期情境：看音樂祭", "價值觀：自由", "興趣：爬山"]
        labels = ["夜生活", "音樂祭", "自由", "爬山"]
        async def saved(request):
            return {"status": "success", "memories": request.memories}
        with patch.dict(agent_api.agent_memory_db, {"owner": {"history": [{"target_traits": "old-private-trait"}]}}), \
                patch.object(agent_api.agent, "generate_graph_reflection", return_value=reflection(labels)) as normalize, \
                patch.object(agent_api, "apply_memory", side_effect=saved) as write:
            result = asyncio.run(agent_api.receive_feedback(feedback(reasons)))
        self.assertEqual(normalize.call_args.kwargs["explicit_reasons"], reasons)
        self.assertEqual(json.loads(normalize.call_args.args[0]), {"action": "decline", "explicit_reasons": reasons})
        batches = [call.args[0] for call in write.call_args_list]
        self.assertEqual([len(batch.memories) for batch in batches], [3, 1])
        self.assertTrue(all(batch.user_id == "owner" and batch.surface == "match_feedback" for batch in batches))
        self.assertEqual([item["label"] for item in result["memories"]], labels)
        self.assertTrue(all(item["stance"] == "avoid" for item in result["memories"]))

    def test_actual_canonical_writer_uses_avoids_concepts_for_all_selected_reasons(self):
        labels = ["夜生活", "音樂祭", "自由", "爬山"]
        driver, session = MagicMock(), MagicMock()
        driver.__enter__.return_value = driver
        driver.session.return_value.__enter__.return_value = session
        session.execute_write.side_effect = lambda callback: callback(session)
        session.run.return_value.single.return_value = {"created": True}
        with patch.object(agent_api.agent, "generate_graph_reflection", return_value=reflection(labels)), \
                patch.object(agent_api.GraphDatabase, "driver", return_value=driver):
            result = asyncio.run(agent_api.receive_feedback(feedback(labels)))
        writes = [call for call in session.run.call_args_list if "UNWIND $memories" in call.args[0]]
        self.assertEqual(len(writes), 2)
        self.assertEqual(
            [item["label"] for call in writes for item in call.kwargs["memories"]],
            labels,
        )
        for call in writes:
            self.assertEqual(call.kwargs["user_id"], "owner")
            self.assertTrue(all(item["stance"] == "avoid" for item in call.kwargs["memories"]))
            self.assertIn("MERGE (u)-[:AVOIDS]->(c)", call.args[0])
            self.assertNotIn("HAS_PREFERENCE", call.args[0])
            self.assertNotIn(":Trait", call.args[0])
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["memories"]), 4)

    def test_graph_failure_is_not_reported_as_saved(self):
        with patch.object(agent_api.agent, "generate_graph_reflection", return_value=reflection(["夜生活"])), \
                patch.object(agent_api.GraphDatabase, "driver", side_effect=RuntimeError("stub-unavailable")), \
                patch("builtins.print"):
            response = TestClient(agent_api.app).post("/api/feedback", json=feedback(["夜生活"]).model_dump())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "feedback_graph_unavailable")
        self.assertNotIn("stub-unavailable", response.text)

    def test_invalid_normalization_never_turns_a_decline_into_positive_preferences(self):
        for raw in ("not-json", "[]", "{}", reflection(["夜生活"], "LIKES_TRAIT"), reflection(["夜生活"], "UNKNOWN")):
            with self.subTest(raw=raw), \
                    patch.object(agent_api.agent, "generate_graph_reflection", return_value=raw), \
                    patch.object(agent_api, "apply_memory", new_callable=AsyncMock) as write, \
                    patch("builtins.print"):
                response = TestClient(agent_api.app).post("/api/feedback", json=feedback(["夜生活"]).model_dump())
                self.assertEqual(response.status_code, 502)
                write.assert_not_called()

    def test_existing_empty_normalization_contract_does_not_invent_concepts(self):
        with patch.object(agent_api.agent, "generate_graph_reflection", return_value=reflection([])), \
                patch.object(agent_api, "apply_memory", new_callable=AsyncMock) as write:
            result = asyncio.run(agent_api.receive_feedback(feedback(["候選人特質"])))
        self.assertEqual(result["status"], "no_preferences")
        self.assertEqual(result["memories"], [])
        write.assert_not_called()

    def test_normalizer_prompt_keeps_all_selected_reasons_as_data(self):
        reasons = ["近期情境：看電影", "興趣：爬山", "個性：外向", "價值觀：自由"]
        client = MagicMock()
        client.chat.completions.create.return_value.choices[0].message.content = reflection(["電影"])
        with patch.object(agent_api.agent, "client", client):
            agent_api.agent.generate_graph_reflection('{"action":"decline"}', explicit_reasons=reasons)
        prompt = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        self.assertIn(json.dumps(reasons, ensure_ascii=False), prompt)
        self.assertIn("不可自行按理由類別丟棄", prompt)


if __name__ == "__main__":
    unittest.main()
