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
        with patch("services.ayue_agent.router.maps_enabled", return_value=True), \
             patch("services.ayue_agent.router.google_place_cards_enabled", return_value=False):
            self.assertTrue({"places.search_nearby", "places.measure_distance", "places.resolve_place"} <= tool_policy_for_turn(ctx))
        with patch("services.ayue_agent.router.maps_enabled", return_value=False), \
             patch("services.ayue_agent.router.google_place_cards_enabled", return_value=False):
            self.assertFalse({"places.search_nearby", "places.measure_distance", "places.resolve_place"} & tool_policy_for_turn(ctx))

    def test_places_tools_remain_visible_when_google_on_and_osm_off(self):
        """Google on + OSM off must not hide the place tools (visibility bug fix)."""
        ctx = AgentTurnContextV2(user_id="owner", room_id="room", message="附近有什麼餐廳")
        with patch("services.ayue_agent.router.maps_enabled", return_value=False), \
             patch("services.ayue_agent.router.google_place_cards_enabled", return_value=True):
            self.assertTrue({"places.search_nearby", "places.measure_distance", "places.resolve_place"} <= tool_policy_for_turn(ctx))

    def test_places_tools_hidden_when_both_providers_off(self):
        ctx = AgentTurnContextV2(user_id="owner", room_id="room", message="附近有什麼餐廳")
        with patch("services.ayue_agent.router.maps_enabled", return_value=False), \
             patch("services.ayue_agent.router.google_place_cards_enabled", return_value=False):
            self.assertFalse({"places.search_nearby", "places.measure_distance", "places.resolve_place"} & tool_policy_for_turn(ctx))

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
        # Force OSM path so nearby_places mock is the one called. Without this
        # patch a live .env with AYUE_GOOGLE_PLACE_CARDS_ENABLED=on would divert
        # execution to the Google branch and the mock would never fire.
        with patch("services.ayue_agent.tools.google_place_cards_enabled", return_value=False), \
             patch("services.ayue_agent.tools.nearby_places", return_value=payload) as nearby:
            result = execute_tool(ToolCall(name="places.search_nearby", arguments={
                "anchor": "", "categories": ["restaurant"], "radius_m": 1500, "limit": 8, "use_saved_location": True,
            }), ctx)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["origin_kind"], "saved_profile")
        self.assertEqual(nearby.call_args.args[0], "高雄市鹽埕區")

    def test_nearby_forwards_cuisine_to_google_search(self):
        ctx = AgentTurnContext(
            user_id="owner", room_id="room", message="我想吃火鍋",
            user_profile={"profile_location": {"city": "高雄市", "district": "鹽埕區"}},
        )
        payload = {
            "anchor_label": "高雄市鹽埕區", "distance_basis": "straight_line",
            "attribution": "Google Maps", "attribution_url": "https://www.google.com/maps",
            "places": [{"name": "示範火鍋店", "category": "restaurant", "distance_m": 350,
                        "address_summary": "鹽埕區", "map_url": "https://www.google.com/maps/place/x",
                        "provider": "google", "place_id": "ChIJabc"}],
        }
        with patch("services.ayue_agent.tools.google_place_cards_enabled", return_value=True), \
             patch("services.ayue_agent.tools.nominatim_search", return_value={
                 "label": "高雄市鹽埕區", "lat": 22.62, "lon": 120.28,
             }), \
             patch("services.ayue_agent.tools.search_nearby_places", return_value=payload["places"]) as google:
            result = execute_tool(ToolCall(name="places.search_nearby", arguments={
                "anchor": "", "categories": ["restaurant"], "cuisine": "火鍋",
                "radius_m": 1500, "limit": 8, "use_saved_location": True,
            }), ctx)
        self.assertTrue(result.ok)
        self.assertEqual(google.call_args.kwargs["cuisine"], "火鍋")

    def test_nearby_osm_path_ignores_cuisine_without_google(self):
        ctx = AgentTurnContext(
            user_id="owner", room_id="room", message="我想吃火鍋",
            user_profile={"profile_location": {"city": "高雄市", "district": "鹽埕區"}},
        )
        payload = {
            "anchor_label": "高雄市鹽埕區", "distance_basis": "straight_line",
            "attribution": "© OpenStreetMap contributors", "attribution_url": "https://www.openstreetmap.org/copyright",
            "places": [{"name": "示範餐廳", "category": "restaurant", "distance_m": 350,
                        "address_summary": "鹽埕區", "map_url": "https://www.openstreetmap.org/?mlat=22.6&mlon=120.2"}],
        }
        with patch("services.ayue_agent.tools.google_place_cards_enabled", return_value=False), \
             patch("services.ayue_agent.tools.nearby_places", return_value=payload) as nearby:
            result = execute_tool(ToolCall(name="places.search_nearby", arguments={
                "anchor": "", "categories": ["restaurant"], "cuisine": "火鍋",
                "radius_m": 1500, "limit": 8, "use_saved_location": True,
            }), ctx)
        self.assertTrue(result.ok)
        self.assertEqual(nearby.call_args.args[0], "高雄市鹽埕區")

    def test_distance_without_origin_does_not_guess_a_location(self):
        result = execute_tool(ToolCall(name="places.measure_distance", arguments={
            "origin": "", "destination": "駁二藝術特區", "use_saved_origin": False,
        }), AgentTurnContext(user_id="owner", room_id="room", message="多遠"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "location_required")

    def test_distance_uses_google_routes_when_enabled(self):
        google_data = {
            "origin_label": "高雄車站", "destination_label": "駁二藝術特區",
            "distance_m": 4200, "duration_text": "約 15 分鐘",
            "distance_basis": "driving",
            "attribution": "Google Maps", "attribution_url": "https://www.google.com/maps",
        }
        with patch("services.ayue_agent.tools.google_place_cards_enabled", return_value=True), \
             patch("services.ayue_agent.tools.measure_distance_matrix", return_value=google_data):
            result = execute_tool(ToolCall(name="places.measure_distance", arguments={
                "origin": "高雄車站", "destination": "駁二藝術特區", "use_saved_origin": False,
            }), AgentTurnContext(user_id="owner", room_id="room", message="多遠"))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["distance_m"], 4200)
        self.assertEqual(result.data["duration_text"], "約 15 分鐘")
        self.assertEqual(result.data["distance_basis"], "driving")

    def test_distance_falls_back_to_osm_haversine_when_google_returns_none(self):
        with patch("services.ayue_agent.tools.google_place_cards_enabled", return_value=True), \
             patch("services.ayue_agent.tools.measure_distance_matrix", return_value=None), \
             patch("services.ayue_agent.tools.measure_distance", return_value={
                 "origin_label": "高雄車站", "destination_label": "駁二藝術特區",
                 "distance_m": 3800, "distance_basis": "straight_line",
                 "attribution": "© OpenStreetMap contributors", "attribution_url": "https://www.openstreetmap.org/copyright",
             }) as osm_distance:
            result = execute_tool(ToolCall(name="places.measure_distance", arguments={
                "origin": "高雄車站", "destination": "駁二藝術特區", "use_saved_origin": False,
            }), AgentTurnContext(user_id="owner", room_id="room", message="多遠"))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["distance_basis"], "straight_line")
        self.assertEqual(result.data["distance_m"], 3800)
        osm_distance.assert_called_once()

    def test_distance_falls_back_to_osm_when_google_exception(self):
        with patch("services.ayue_agent.tools.google_place_cards_enabled", return_value=True), \
             patch("services.ayue_agent.tools.measure_distance_matrix", side_effect=Exception("boom")), \
             patch("services.ayue_agent.tools.measure_distance", return_value={
                 "origin_label": "A地", "destination_label": "B地",
                 "distance_m": 1000, "distance_basis": "straight_line",
                 "attribution": "© OpenStreetMap contributors", "attribution_url": "https://www.openstreetmap.org/copyright",
             }):
            result = execute_tool(ToolCall(name="places.measure_distance", arguments={
                "origin": "A地", "destination": "B地", "use_saved_origin": False,
            }), AgentTurnContext(user_id="owner", room_id="room", message="多遠"))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["distance_basis"], "straight_line")

    def test_distance_uses_osm_when_google_disabled(self):
        with patch("services.ayue_agent.tools.google_place_cards_enabled", return_value=False), \
             patch("services.ayue_agent.tools.measure_distance", return_value={
                 "origin_label": "A地", "destination_label": "B地",
                 "distance_m": 500, "distance_basis": "straight_line",
                 "attribution": "© OpenStreetMap contributors", "attribution_url": "https://www.openstreetmap.org/copyright",
             }) as osm_distance, \
             patch("services.ayue_agent.tools.measure_distance_matrix") as google_distance:
            result = execute_tool(ToolCall(name="places.measure_distance", arguments={
                "origin": "A地", "destination": "B地", "use_saved_origin": False,
            }), AgentTurnContext(user_id="owner", room_id="room", message="多遠"))
        self.assertTrue(result.ok)
        google_distance.assert_not_called()
        osm_distance.assert_called_once()

    def test_explicit_place_resolution_uses_google_when_enabled(self):
        payload = {
            "name": "示範餐廳", "category": "restaurant", "distance_m": 0,
            "address_summary": "鹽埕區", "map_url": "https://www.google.com/maps/place/example",
            "provider": "google", "place_id": "ChIJexample",
        }
        with patch("services.ayue_agent.tools.google_place_cards_enabled", return_value=True), \
             patch("services.ayue_agent.tools.resolve_google_place", return_value=payload):
            result = execute_tool(ToolCall(name="places.resolve_place", arguments={"query": "示範餐廳"}), AgentTurnContext(
                user_id="owner", room_id="room", message="示範餐廳在哪？",
            ))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["place"]["place_id"], "ChIJexample")
        self.assertNotIn("raw", str(result.data))

    def test_nearby_google_results_can_carry_photo_url_without_extra_requests(self):
        ctx = AgentTurnContext(
            user_id="owner", room_id="room", message="鹽埕區附近餐廳",
            user_profile={"profile_location": {"city": "高雄市", "district": "鹽埕區"}},
        )
        payload = {
            "anchor_label": "高雄市鹽埕區", "distance_basis": "straight_line",
            "attribution": "Google Maps", "attribution_url": "https://www.google.com/maps",
            "places": [{
                "name": "示範餐廳", "category": "restaurant", "distance_m": 350,
                "address_summary": "鹽埕區", "map_url": "https://www.google.com/maps/place/x",
                "provider": "google", "place_id": "ChIJabc",
                "photo_url": "https://places.googleapis.com/v1/places/ChIJabc/photos/ref/media?maxWidthPx=400&key=k",
            }],
        }
        with patch("services.ayue_agent.tools.google_place_cards_enabled", return_value=True), \
             patch("services.ayue_agent.tools.nominatim_search", return_value={
                 "label": "高雄市鹽埕區", "lat": 22.62, "lon": 120.28,
             }), \
             patch("services.ayue_agent.tools.search_nearby_places", return_value=payload["places"]) as google:
            result = execute_tool(ToolCall(name="places.search_nearby", arguments={
                "anchor": "", "categories": ["restaurant"],
                "radius_m": 1500, "limit": 8, "use_saved_location": True,
            }), ctx)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["places"][0]["photo_url"], payload["places"][0]["photo_url"])

    def test_explicit_place_resolution_falls_back_to_osm_without_google(self):
        payload = {
            "name": "示範餐廳", "category": "restaurant", "distance_m": 0,
            "address_summary": "高雄市鹽埕區", "map_url": "https://www.openstreetmap.org/?mlat=22.6&mlon=120.2",
            "provider": "openstreetmap", "place_id": "",
        }
        with patch("services.ayue_agent.tools.google_place_cards_enabled", return_value=False), \
             patch("services.ayue_agent.tools.resolve_osm_place", return_value=payload):
            result = execute_tool(ToolCall(name="places.resolve_place", arguments={"query": "示範餐廳"}), AgentTurnContext(
                user_id="owner", room_id="room", message="示範餐廳在哪？",
            ))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["place"]["provider"], "openstreetmap")
        self.assertEqual(result.data["attribution"], "© OpenStreetMap contributors")


if __name__ == "__main__":
    unittest.main()
