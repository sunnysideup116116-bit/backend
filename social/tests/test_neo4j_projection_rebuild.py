import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.rebuild_neo4j_projection import rebuild_projection_transaction


class _Result:
    def __init__(self, row=None):
        self.row = row

    def consume(self):
        return None

    def single(self):
        return self.row


class _Transaction:
    def __init__(self, users):
        self.users = users
        self.calls = []

    def run(self, query, **parameters):
        self.calls.append((" ".join(query.split()), parameters))
        if "RETURN count(DISTINCT user) AS users" in query:
            return _Result({"users": self.users, "relations": 2})
        return _Result()


class Neo4jProjectionRebuildTests(unittest.TestCase):
    def test_rebuild_is_batched_and_preserves_event_relevance_for_current_users(self):
        tx = _Transaction(users=2)
        result = rebuild_projection_transaction(
            tx,
            [{"user_id": "owner"}, {"user_id": "other"}],
            [{"user_id": "owner", "key": "music", "label": "音樂", "relation": "PREFERS"}],
            [{
                "user_id": "other", "expires_at": 100,
                "concepts": [{"key": "market", "label": "市集"}],
            }],
        )

        queries = "\n".join(query for query, _params in tx.calls)
        self.assertEqual(result["users"], 2)
        self.assertIn("NOT user.id IN $user_ids DETACH DELETE user", queries)
        self.assertIn("PREFERS|AVOIDS|CURRENTLY_WANTS", queries)
        self.assertNotIn("EVENT_RELEVANCE", queries)
        self.assertNotIn("MATCH (user:User) DETACH DELETE user", queries)

    def test_verification_failure_aborts_transaction(self):
        tx = _Transaction(users=0)
        with self.assertRaisesRegex(RuntimeError, "transaction rolled back"):
            rebuild_projection_transaction(tx, [{"user_id": "owner"}], [], [])


if __name__ == "__main__":
    unittest.main()
