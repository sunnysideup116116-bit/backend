import asyncio
import os
import time
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


os.environ.setdefault("NEO4J_URI", "bolt://stub.invalid:7687")
os.environ.setdefault("NEO4J_USERNAME", "stub")
os.environ.setdefault("NEO4J_PASSWORD", "stub")
os.environ.setdefault("NEO4J_DATABASE", "neo4j")
os.environ.setdefault("OLLAMA_HOST", "http://127.0.0.1:9")
os.environ.setdefault("OLLAMA_API_KEY", "stub")

import agent_api


class ContextProjectionEndpointTests(unittest.TestCase):
    def test_health_endpoint(self):
        response = TestClient(agent_api.app).get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["service"], "matchmaker")

    def test_active_event_inventory_reports_user_count_without_mutation(self):
        driver, session = self._graph()

        def run(query, **_kwargs):
            if "MATCH (event:Event)" in query:
                return [{
                    "event_id": "event-1", "title": "港邊市集",
                    "summary": "", "category": "市集",
                    "starts_at": 1, "ends_at": 2,
                    "session_starts": [1], "tags": [], "vibes": [],
                }]
            result = MagicMock()
            result.single.return_value = {"count": 11}
            return result

        session.run.side_effect = run
        with patch("agent_api.GraphDatabase.driver", return_value=driver):
            result = agent_api.list_active_events_for_relevance(limit=20)

        self.assertEqual(result["event_count"], 1)
        self.assertEqual(result["user_count"], 11)
        queries = [call.args[0] for call in session.run.call_args_list]
        self.assertFalse(any("DELETE" in query for query in queries))

    def _graph(self):
        driver = MagicMock()
        session = MagicMock()
        driver.__enter__.return_value = driver
        driver.session.return_value.__enter__.return_value = session

        def run(query, **_kwargs):
            result = MagicMock()
            result.single.return_value = None
            return result

        session.run.side_effect = run
        return driver, session

    def test_projection_replaces_old_intent_with_minimal_ttl_edge(self):
        driver, session = self._graph()
        request = agent_api.ContextProjectionRequest(
            user_id="owner",
            concepts=[{"key": "concept_hiking", "label": "爬山"}],
            expires_at=time.time() + 3600,
            revision=4,
        )
        with patch("agent_api.GraphDatabase.driver", return_value=driver):
            result = asyncio.run(agent_api.project_current_context(request))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["concept_count"], 1)
        queries = [call.args[0] for call in session.run.call_args_list]
        self.assertTrue(any("DELETE expired" in query for query in queries))
        self.assertTrue(any("DELETE old" in query for query in queries))
        edge_call = next(call for call in session.run.call_args_list if "CURRENTLY_WANTS" in call.args[0] and "MERGE (u)-[r" in call.args[0])
        self.assertEqual(set(edge_call.kwargs), {"user_id", "key", "label", "expires_at"})

    def test_empty_projection_only_clears_previous_intent(self):
        driver, session = self._graph()
        request = agent_api.ContextProjectionRequest(
            user_id="owner", concepts=[], expires_at=time.time() + 3600, revision=5,
        )
        with patch("agent_api.GraphDatabase.driver", return_value=driver):
            result = asyncio.run(agent_api.project_current_context(request))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["concept_count"], 0)
        self.assertFalse(any(
            "MERGE (u)-[r:CURRENTLY_WANTS]" in call.args[0]
            for call in session.run.call_args_list
        ))

    def test_destructive_graph_reset_is_disabled_by_default(self):
        driver, session = self._graph()
        driver.verify_connectivity.return_value = None
        with patch.dict(os.environ, {"DEMO_DESTRUCTIVE_TOOLS_ENABLED": "off"}), \
                patch("agent_api.GraphDatabase.driver", return_value=driver), \
                self.assertRaises(Exception) as raised:
            asyncio.run(agent_api.clear_graph_endpoint())
        self.assertEqual(getattr(raised.exception, "status_code", None), 403)
        session.run.assert_not_called()

    def test_event_relevance_projection_replaces_only_derived_event_links(self):
        driver, session = self._graph()
        request = agent_api.EventRelevanceProjectionRequest(
            event_ids=["event_1"],
            model="embedding-test",
            generated_at=time.time(),
            links=[{
                "user_id": "owner", "event_id": "event_1", "relation": "relevance",
                "evidence": [{
                    "user_concept": "爬山", "event_signal": "戶外",
                    "similarity": 0.88, "signal_type": "tag", "source_kind": "recent",
                }],
            }],
        )
        with patch("agent_api.GraphDatabase.driver", return_value=driver):
            result = agent_api.project_event_relevance(request)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["relevance_count"], 1)
        queries = [call.args[0] for call in session.run.call_args_list]
        self.assertTrue(any("DELETE old" in query and "EVENT_RELEVANCE" in query for query in queries))
        self.assertTrue(any("MERGE (user)-[link:EVENT_RELEVANCE]" in query for query in queries))
        self.assertFalse(any("PREFERS" in query or "AVOIDS" in query for query in queries))

    def test_event_avoidance_only_uses_explicit_activity_dislikes(self):
        _, session = self._graph()
        agent_api._refresh_semantic_event_links(session)
        avoidance_query = next(
            call.args[0]
            for call in session.run.call_args_list
            if "MATCH (user:User)-[:AVOIDS]" in call.args[0]
        )
        self.assertIn("user_concept.kind = 'activity'", avoidance_query)
        self.assertIn("type(signal_relation) = 'HAS_TAG'", avoidance_query)
        self.assertNotIn("OR user_concept.kind = 'interest'", avoidance_query)

    def test_concept_embedding_projection_stores_versioned_vector(self):
        driver, session = self._graph()
        request = agent_api.ConceptEmbeddingProjectionRequest(
            concepts=[{
                "key": "hiking", "label": "爬山", "kind": "activity",
                "embedding": [1.0] + [0.0] * 767,
            }],
        )
        with patch("agent_api.GraphDatabase.driver", return_value=driver):
            result = agent_api.project_concept_embeddings(request)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["embedded_count"], 1)
        queries = [call.args[0] for call in session.run.call_args_list]
        self.assertTrue(any("CREATE VECTOR INDEX concept_embedding_index" in query for query in queries))
        prune = next(call for call in session.run.call_args_list if "ranked_links[$limit..]" in call.args[0])
        self.assertEqual(prune.kwargs["limit"], 3)
        write = next(call for call in session.run.call_args_list if "concept.embedding=item.embedding" in call.args[0])
        self.assertEqual(len(write.kwargs["concepts"][0]["embedding"]), 768)
        self.assertEqual(write.kwargs["concepts"][0]["kind"], "activity")

    def test_missing_concepts_endpoint_returns_only_bounded_projection(self):
        driver, session = self._graph()

        def run(query, **_kwargs):
            if "RETURN concept.key AS key" in query:
                return [{"key": "hiking", "label": "爬山", "suggested_kind": "activity"}]
            result = MagicMock()
            result.single.return_value = None
            return result

        session.run.side_effect = run
        with patch("agent_api.GraphDatabase.driver", return_value=driver):
            result = agent_api.list_missing_concept_embeddings(limit=999)
        self.assertEqual(result["count"], 1)
        self.assertEqual(
            set(result["concepts"][0]), {"key", "label", "suggested_kind"},
        )
        self.assertEqual(session.run.call_args.kwargs["limit"], 50)


if __name__ == "__main__":
    unittest.main()
