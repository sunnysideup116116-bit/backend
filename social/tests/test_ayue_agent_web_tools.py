import unittest
from unittest.mock import Mock, patch

import requests

from services.ayue_agent.contracts import AgentTurnContext, PublicAgentTurnContext, ToolCall
from services.ayue_agent.v3.scheduler import _public_sources, _web_extract_urls_allowed
from services.ayue_agent.tools import execute_tool
from services.ayue_agent.web_tools import extract_web, is_safe_public_url, search_web
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

    def test_search_uses_advanced_depth_without_answer_or_raw_content(self):
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {"results": []}
        with patch("services.ayue_agent.web_tools.config.TAVILY_API_KEY", "test-key", create=True), \
             patch("services.ayue_agent.web_tools.requests.post", return_value=response) as post:
            data, error = search_web("explicit lookup")
        self.assertIsNone(error)
        self.assertEqual(data, {"results": []})
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["search_depth"], "advanced")
        self.assertFalse(payload["include_answer"])
        self.assertFalse(payload["include_raw_content"])

    def test_search_preserves_http_failure_classification(self):
        cases = [
            (400, "web_bad_request"),
            (401, "web_auth_error"),
            (429, "web_rate_limited"),
            (503, "web_provider_error"),
        ]
        for status_code, expected_error in cases:
            with self.subTest(status_code=status_code):
                response = Mock(ok=False, status_code=status_code)
                with patch(
                    "services.ayue_agent.web_tools.config.TAVILY_API_KEY",
                    "test-key", create=True,
                ), patch(
                    "services.ayue_agent.web_tools.requests.post",
                    return_value=response,
                ):
                    data, error = search_web("explicit lookup")
                self.assertIsNone(data)
                self.assertEqual(error, expected_error)

    def test_search_and_extract_preserve_network_failure(self):
        with patch(
            "services.ayue_agent.web_tools.config.TAVILY_API_KEY",
            "test-key", create=True,
        ), patch(
            "services.ayue_agent.web_tools.requests.post",
            side_effect=requests.ConnectionError("offline"),
        ):
            search_data, search_error = search_web("explicit lookup")
            extract_data, extract_error = extract_web(["https://example.com/article"])
        self.assertIsNone(search_data)
        self.assertEqual(search_error, "web_network_error")
        self.assertIsNone(extract_data)
        self.assertEqual(extract_error, "web_network_error")

    def test_extract_projects_up_to_8000_chars_per_page(self):
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {"results": [{
            "url": "https://example.com/article",
            "raw_content": "x" * 8_001,
        }]}
        with patch("services.ayue_agent.web_tools.config.TAVILY_API_KEY", "test-key", create=True), \
             patch("services.ayue_agent.web_tools.requests.post", return_value=response):
            data, error = extract_web(["https://example.com/article"])
        self.assertIsNone(error)
        page = data["pages"][0]
        self.assertEqual(len(page["content"]), 8_000)
        self.assertTrue(page["truncated"])

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


    def test_search_rotates_tavily_key_on_429_or_401(self):
        resp_429 = Mock(ok=False, status_code=429)
        resp_200 = Mock(ok=True, status_code=200)
        resp_200.json.return_value = {"results": [{"title": "Success", "url": "https://example.com/ok", "content": "data"}]}

        with patch("services.ayue_agent.web_tools.config.TAVILY_API_KEYS", "key1,key2", create=True), \
             patch("services.ayue_agent.web_tools.config.TAVILY_API_KEY", "", create=True), \
             patch("services.ayue_agent.web_tools.requests.post", side_effect=[resp_429, resp_200]) as post:
            data, error = search_web("key rotation test")

        self.assertIsNone(error)
        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(post.call_count, 2)
        headers1 = post.call_args_list[0].kwargs["headers"]
        headers2 = post.call_args_list[1].kwargs["headers"]
        self.assertEqual(headers1["Authorization"], "Bearer key1")
        self.assertEqual(headers2["Authorization"], "Bearer key2")


if __name__ == "__main__":
    unittest.main()
