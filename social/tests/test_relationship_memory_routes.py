import unittest
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from routers import relationship_memories as routes


def _request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "headers": []})


class RelationshipMemoryRouteTests(unittest.TestCase):
    def test_list_requires_verified_owner(self):
        with patch.object(routes, "authenticated_owner_matches", return_value=False):
            with self.assertRaises(HTTPException) as caught:
                routes.get_relationship_memories(_request(), "owner")
        self.assertEqual(caught.exception.status_code, 403)

    def test_list_returns_only_service_scoped_owner_rows(self):
        row = {
            "memory_id": "m1", "other_user_id": "other", "relationship_id": "r1",
            "topic": "聊天感受", "statement": "本人覺得很自在", "version": 1,
            "updated_at": 100.0, "status": "active",
        }
        with patch.object(routes, "authenticated_owner_matches", return_value=True), \
             patch.object(routes, "list_relationship_memories", return_value=[row]) as listing, \
             patch.object(routes, "display_name", return_value="小明"):
            response = routes.get_relationship_memories(_request(), "owner", "other")
        listing.assert_called_once_with("owner", "other")
        self.assertEqual(response["memories"][0]["other_display_name"], "小明")


if __name__ == "__main__":
    unittest.main()
