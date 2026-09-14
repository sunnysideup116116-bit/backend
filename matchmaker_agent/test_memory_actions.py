import asyncio
import os
import unittest
from unittest.mock import MagicMock, patch


os.environ.setdefault("NEO4J_URI", "bolt://stub.invalid:7687")
os.environ.setdefault("NEO4J_USERNAME", "stub")
os.environ.setdefault("NEO4J_PASSWORD", "stub")
os.environ.setdefault("LLM_API_KEY", "stub")
os.environ.setdefault("LLM_BASE_URL", "http://127.0.0.1:9")
os.environ.setdefault("LLM_MODEL_ID", "stub")

import agent_api


def graph(result):
    driver, session, transaction = MagicMock(), MagicMock(), MagicMock()
    driver.__enter__.return_value = driver
    driver.session.return_value.__enter__.return_value = session
    transaction.run.return_value.single.return_value = result
    session.execute_write.side_effect = lambda callback: callback(transaction)
    return driver, session, transaction


class MemoryActionTests(unittest.TestCase):
    def test_disable_preserves_original_avoid_stance(self):
        driver, session, transaction = graph({"original_relation": "AVOIDS"})
        request = agent_api.MemoryActionRequest(
            user_id="owner", key="smoking", action="disable",
        )
        with patch.object(agent_api.GraphDatabase, "driver", return_value=driver):
            result = asyncio.run(agent_api.memory_action(request))
        self.assertEqual(result["status"], "success")
        query = transaction.run.call_args.args[0]
        self.assertIn("MEMORY_DISABLED", query)
        self.assertIn("original_relation", query)
        session.execute_write.assert_called_once()

    def test_restore_recreates_the_original_relation_not_always_prefers(self):
        driver, _session, transaction = graph({
            "original_relation": "AVOIDS", "original_expires_at": None,
        })
        request = agent_api.MemoryActionRequest(
            user_id="owner", key="smoking", action="restore",
        )
        with patch.object(agent_api.GraphDatabase, "driver", return_value=driver):
            result = asyncio.run(agent_api.memory_action(request))
        self.assertEqual(result["status"], "success")
        query = transaction.run.call_args.args[0]
        self.assertIn("original_relation='AVOIDS'", query)
        self.assertIn("MERGE (u)-[:AVOIDS]->(concept)", query)

    def test_correct_moves_only_the_owner_relation_to_a_new_concept(self):
        driver, _session, transaction = graph({"relation": "PREFERS"})
        request = agent_api.MemoryActionRequest(
            user_id="owner", key="coffee", action="correct", value="安靜咖啡廳",
        )
        with patch.object(agent_api.GraphDatabase, "driver", return_value=driver):
            result = asyncio.run(agent_api.memory_action(request))
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["key"].startswith("owner_correction_"))
        query = transaction.run.call_args.args[0]
        self.assertIn("(old:Concept {key:$key})", query)
        self.assertIn("DELETE existing", query)

    def test_missing_memory_is_not_reported_as_changed(self):
        driver, _session, _transaction = graph(None)
        request = agent_api.MemoryActionRequest(
            user_id="owner", key="missing", action="disable",
        )
        with patch.object(agent_api.GraphDatabase, "driver", return_value=driver):
            result = asyncio.run(agent_api.memory_action(request))
        self.assertEqual(result["status"], "not_found")


if __name__ == "__main__":
    unittest.main()
