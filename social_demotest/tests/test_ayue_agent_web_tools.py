import unittest
from unittest.mock import Mock, patch

from services.ayue_agent.contracts import AgentTurnContext, PublicAgentTurnContext, ToolCall
from services.ayue_agent.v3.scheduler import _public_sources, _web_extract_urls_allowed
from services.ayue_agent.tools import execute_tool
from services.ayue_agent.web_tools import is_safe_public_url, search_web
from services.profile_location import normalize_profile_location, safe_profile_location


class AyueWebToolsTests(unittest.TestCase):
    def test_public_url_validator_rejects_local_and_private_targets(self):
        self.assertTrue(is_safe_public_url("https://example.com/a"))
        for value in ("http://127.0.0.1:8000", "http://localhost", "file:///etc/passwd", "https://user:pass@example.com"):
            self.assertFalse(is_safe_public_url(value))

    def test_search_adds_only_coarse_location_and_projects_safe_results(self):
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {"results": [
            {"title": "駁二活動", "url": "https://pier2.org/events", "content": "最新活動內容", "published_date": "2026-07-31"},
            {"title": "內網", "url": "http://127.0.0.1/internal", "content": "drop"},
        ]}
        with patch("services.ayue_agent.web_tools.config.TAVILY_API_KEY", "test-key", create=True), \
             patch("services.ayue_agent.web_tools.requests.post", return_value=response) as post:
            data, error = search_web("駁二最近有什麼", recency="month", location="高雄市鹽埕區")
        self.assertIsNone(error)
        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(post.call_args.kwargs["json"]["query"], "駁二最近有什麼 高雄市鹽埕區")
        self.assertNotIn("test-key", str(data))

    def test_web_search_executor_uses_saved_location_only_when_requested(self):
        ctx = AgentTurnContext(
            user_id="owner", room_id="room", message="附近有什麼",
            user_profile={"profile_location": {"city": "高雄市", "district": "鹽埕區"}},
        )
        with patch("services.ayue_agent.tools.search_web", return_value=({"results": []}, None)) as search:
            result = execute_tool(ToolCall(name="web.search", arguments={"query": "附近活動", "recency": "week", "use_saved_location": True}), ctx)
        self.assertTrue(result.ok)
        self.assertEqual(search.call_args.kwargs["location"], "高雄市鹽埕區")

    def test_extract_is_bound_to_owner_url_or_current_search_observation(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="請看 https://example.com/a")
        self.assertTrue(_web_extract_urls_allowed(ctx, [], ["https://example.com/a"]))
        self.assertFalse(_web_extract_urls_allowed(ctx, [], ["https://example.net/unseen"]))
        observations = [{"tool": "web.search", "result": {"results": [{"url": "https://example.net/unseen"}]}}]
        self.assertTrue(_web_extract_urls_allowed(ctx, observations, ["https://example.net/unseen"]))

    def test_sources_never_include_web_content(self):
        sources = _public_sources([{"tool": "web.search", "result": {"results": [{
            "title": "官方活動", "url": "https://example.com/event", "snippet": "ignore this instruction",
        }]}}])
        self.assertEqual(sources, [{"title": "官方活動", "url": "https://example.com/event"}])

    def test_place_map_links_are_not_mislabeled_as_web_evidence(self):
        sources = _public_sources([{
            "tool": "places.search_nearby",
            "result": {"places": [{
                "name": "候選咖啡店",
                "map_url": "https://www.google.com/maps/place/example",
            }]},
        }])
        self.assertEqual(sources, [])

    def test_profile_location_is_coarse_and_safe(self):
        location = normalize_profile_location("高雄市", "鹽埕區")
        self.assertEqual(location["display_name"], "高雄市鹽埕區")
        self.assertEqual(safe_profile_location({"profile_location": location}), location)
        self.assertEqual(normalize_profile_location("https://bad", ""), {})


if __name__ == "__main__":
    unittest.main()
