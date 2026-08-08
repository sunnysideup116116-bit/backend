import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services import demo_cleanup_service as cleanup


class _Collection:
    def __init__(self, deleted=0):
        self.deleted = deleted

    def delete_many(self, query):
        self.query = query
        return SimpleNamespace(deleted_count=self.deleted)


class _Database:
    def __init__(self):
        self.collections = {
            "calendar_events": _Collection(2),
            "matches": _Collection(1),
            "v3_pending_confirmations": _Collection(3),
            "system.profile": _Collection(99),
        }

    def list_collection_names(self):
        return list(self.collections)

    def command(self, name):
        self.command_name = name
        return {"ok": 1}

    def __getitem__(self, name):
        return self.collections[name]


class DemoCleanupTests(unittest.TestCase):
    def test_clear_mongo_clears_calendar_and_future_collections(self):
        database = _Database()
        with patch.object(cleanup, "db", database):
            result = cleanup.clear_mongo_database()

        self.assertEqual(result["collections"], 3)
        self.assertEqual(result["deleted_documents"], 6)
        self.assertNotIn("system.profile", [name for name in database.collections if database.collections[name].__dict__.get("query")])
        self.assertTrue(all(collection.query == {} for name, collection in database.collections.items() if not name.startswith("system.")))

    def test_full_clear_stops_before_mongo_when_graph_fails(self):
        with patch.object(cleanup, "clear_graph", side_effect=cleanup.DemoCleanupError("graph_unavailable")), \
             patch.object(cleanup, "clear_mongo_database") as clear_mongo, \
             patch.object(cleanup, "db", _Database()):
            with self.assertRaises(cleanup.DemoCleanupError) as raised:
                cleanup.clear_all_demo_state()

        self.assertEqual(raised.exception.code, "graph_unavailable")
        clear_mongo.assert_not_called()

    def test_full_clear_reports_mongo_unavailable_before_graph(self):
        database = _Database()
        database.command = lambda _name: (_ for _ in ()).throw(RuntimeError("dns"))
        with patch.object(cleanup, "db", database), patch.object(cleanup, "clear_graph") as clear_graph:
            with self.assertRaises(cleanup.DemoCleanupError) as raised:
                cleanup.clear_all_demo_state()

        self.assertEqual(raised.exception.code, "mongo_unavailable")
        clear_graph.assert_not_called()

    def test_full_clear_returns_each_subsystem(self):
        with patch.object(cleanup, "clear_graph", return_value={"status": "cleared"}), \
             patch.object(cleanup, "clear_runtime_fallbacks", return_value={"status": "cleared"}), \
             patch.object(cleanup, "clear_mongo_database", return_value={"status": "cleared", "collections": 2, "deleted_documents": 4}), \
             patch.object(cleanup, "db", _Database()):
            result = cleanup.clear_all_demo_state()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["mongo"]["deleted_documents"], 4)


if __name__ == "__main__":
    unittest.main()
