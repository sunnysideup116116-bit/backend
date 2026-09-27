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
from concept_identity import canonicalize_concept


def graph(result):
    driver, session, transaction = MagicMock(), MagicMock(), MagicMock()
    driver.__enter__.return_value = driver
    driver.session.return_value.__enter__.return_value = session
    def run(query, **_kwargs):
        if 'RETURN old.key AS key' in query:return [result] if result else []
        response = MagicMock()
        response.single.return_value = {"revision": 0, "epoch": 0, "epoch_at": 0, "pending": None} if "AS revision" in query else result
        return response
    transaction.run.side_effect = run
    session.execute_write.side_effect = lambda callback: callback(transaction)
    return driver, session, transaction


class MemoryActionTests(unittest.TestCase):
    def setUp(self):
        # Old mutation-shape unit tests; real reference validation is covered
        # separately by test_action_reference (including the original repro).
        for name, value in (
            ('valid_format', lambda _key: True),
            ('resolve', lambda _tx,_owner,key,_state:
                {'id':'action-fixture-edge','relation':'PREFERS','concept':{'key':key}}),
        ):
            patcher = patch.object(agent_api.action_reference, name, value)
            patcher.start(); self.addCleanup(patcher.stop)

    def test_delayed_action_expires_after_owner_lock_without_preference_mutation(self):
        driver, _session, transaction = graph({'original_relation':'PREFERS'})
        request=agent_api.MemoryActionRequest(user_id='owner',key='missing',action='disable',
            source_created_at=10,expires_at=40)
        with patch.object(agent_api.GraphDatabase,'driver',return_value=driver), patch.object(agent_api.time,'time',return_value=41):
            result=asyncio.run(agent_api.memory_action(request))
        self.assertEqual(result['error_code'],'preference_action_expired')
        self.assertEqual(transaction.run.call_count,1)
        self.assertNotIn('DELETE',transaction.run.call_args.args[0])

    def test_disable_preserves_original_avoid_stance(self):
        driver, session, transaction = graph({"original_relation": "AVOIDS"})
        request = agent_api.MemoryActionRequest(
            user_id="owner", key="smoking", action="disable",
        )
        with patch.object(agent_api.GraphDatabase, "driver", return_value=driver):
            result = asyncio.run(agent_api.memory_action(request))
        self.assertEqual(result["status"], "success")
        query = next(c.args[0] for c in transaction.run.call_args_list if "DELETE active" in c.args[0])
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
        query = next(c.args[0] for c in transaction.run.call_args_list if "DELETE disabled" in c.args[0])
        self.assertIn("original_relation='AVOIDS'", query)
        self.assertIn("MERGE (u)-[:AVOIDS]->(concept)", query)

    def test_correct_moves_only_the_owner_relation_to_a_new_concept(self):
        original = canonicalize_concept("Coffee")
        driver, _session, transaction = graph({**original.as_dict(), "relation": "PREFERS"})
        request = agent_api.MemoryActionRequest(
            user_id="owner", key=original.key, action="correct", value="安靜咖啡廳",
        )
        with patch.object(agent_api.GraphDatabase, "driver", return_value=driver):
            result = asyncio.run(agent_api.memory_action(request))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["key"], canonicalize_concept("安靜咖啡廳").key)
        query = next(c.args[0] for c in transaction.run.call_args_list if "DELETE existing" in c.args[0])
        self.assertIn("(old:Concept {key:$key})", query)
        self.assertIn("DELETE existing", query)

    def test_missing_memory_is_not_reported_as_changed(self):
        driver, _session, _transaction = graph(None)
        request = agent_api.MemoryActionRequest(
            user_id="owner", key="missing", action="disable",
        )
        with patch.object(agent_api.GraphDatabase, "driver", return_value=driver):
            result = asyncio.run(agent_api.memory_action(request))
        self.assertEqual(result["error_code"], "stale_source")


if __name__ == "__main__":
    unittest.main()
