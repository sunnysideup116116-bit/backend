import unittest
from unittest.mock import patch

from services.ayue_agent.contracts import AgentTurnContext, AgentTurnContextV2, ToolCall
from services.ayue_agent.maps_client import MapClientError, build_overpass_nearby, haversine_m
from services.ayue_agent.router import tool_policy_for_turn
from services.ayue_agent.tools import execute_tool


class AyueMapsToolsTests(unittest.TestCase):
    def test_overpass_query_is_server_built_and_category_allowlisted(self):
        query = build_overpass_nearby(["restaurant", "park"], 22.626, 120.286, 1500)
        self.assertIn('node(around:1500,22.626000,120.286000)["amenity"="restaurant"]', query)
        self.assertIn('node(around:1500,22.626000,120.286000)["leisure"="park"]', query)
        with self.assertRaises(MapClientError):
            build_overpass_nearby(["anything"], 0, 0, 1500)

    def test_haversine_is_straight_line_estimate(self):
        self.assertEqual(haversine_m(22.626, 120.286, 22.626, 120.286), 0)
        self.assertGreater(haversine_m(22.626, 120.286, 22.616, 120.296), 1_000)

    def test_places_tools_are_visible_only_when_enabled(self):
        ctx = AgentTurnContextV2(user_id="owner", room_id="room", message="附近有什麼餐廳")
        with patch("services.ayue_agent.router.maps_enabled", return_value=True):
            self.assertTrue({"places.search_nearby", "places.measure_distance"} <= tool_policy_for_turn(ctx))
        with patch("services.ayue_agent.router.maps_enabled", return_value=False):
            self.assertFalse({"places.search_nearby", "places.measure_distance"} & tool_policy_for_turn(ctx))

    def test_nearby_uses_saved_coarse_location_only_when_requested(self):
        ctx = AgentTurnContext(
            user_id="owner", room_id="room", message="附近餐廳",
            user_profile={"profile_location": {"city": "高雄市", "district": "鹽埕區"}},
        )
        payload = {
            "anchor_label": "高雄市鹽埕區", "distance_basis": "straight_line",
            "attribution": "© OpenStreetMap contributors", "attribution_url": "https://www.openstreetmap.org/copyright",
            "places": [{"name": "示範餐廳", "category": "restaurant", "distance_m": 350, "address_summary": "鹽埕區", "map_url": "https://www.openstreetmap.org/?mlat=22.6&mlon=120.2"}],
        }
        with patch("services.ayue_agent.tools.nearby_places", return_value=payload) as nearby:
            result = execute_tool(ToolCall(name="places.search_nearby", arguments={
                "anchor": "", "categories": ["restaurant"], "radius_m": 1500, "limit": 8, "use_saved_location": True,
            }), ctx)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["origin_kind"], "saved_profile")
        self.assertEqual(nearby.call_args.args[0], "高雄市鹽埕區")

    def test_distance_without_origin_does_not_guess_a_location(self):
        result = execute_tool(ToolCall(name="places.measure_distance", arguments={
            "origin": "", "destination": "駁二藝術特區", "use_saved_origin": False,
        }), AgentTurnContext(user_id="owner", room_id="room", message="多遠"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "location_required")


if __name__ == "__main__":
    unittest.main()
