import unittest
from unittest.mock import patch

from models import DirectChatRequest
from services.ayue_agent.context import _request_location_label
from services.ayue_agent.contracts import PublicAgentRequestContext, ToolCall
from services.ayue_agent.tools import execute_tool


DEVICE_LOCATION = {
    "latitude": 22.626,
    "longitude": 120.286,
    "accuracy_m": 15,
    "captured_at": "2026-09-05T06:00:00Z",
    "city": "高雄市",
    "district": "鹽埕區",
    "road": "大勇路",
    "display_name": "高雄市鹽埕區大勇路",
}


class DeviceLocationRequestTests(unittest.TestCase):
    def test_request_location_is_excluded_from_serialized_request(self):
        request = DirectChatRequest(
            user_id="owner",
            contact_id="ai_assistant",
            message="附近咖啡",
            device_location=DEVICE_LOCATION,
        )

        self.assertIsNotNone(request.device_location)
        self.assertNotIn("device_location", request.model_dump())
        self.assertNotIn("22.626", repr(request))

    def test_prompt_context_receives_only_the_bounded_display_label(self):
        context = PublicAgentRequestContext(
            user_id="owner",
            room_id="room",
            message="附近咖啡",
            user_profile={"profile_location": {"city": "台北市", "district": "中山區"}},
            device_location=DEVICE_LOCATION,
        )

        self.assertEqual(_request_location_label(context), "高雄市鹽埕區大勇路")
        self.assertNotIn("22.626", _request_location_label(context))

    def test_nearby_tool_prefers_request_coordinates_over_saved_profile(self):
        context = PublicAgentRequestContext(
            user_id="owner",
            room_id="room",
            message="附近咖啡",
            user_profile={"profile_location": {"city": "台北市", "district": "中山區"}},
            device_location=DEVICE_LOCATION,
        )
        payload = {
            "anchor_label": "高雄市鹽埕區大勇路",
            "distance_basis": "straight_line",
            "attribution": "© OpenStreetMap contributors",
            "attribution_url": "https://www.openstreetmap.org/copyright",
            "places": [],
        }
        with patch("services.ayue_agent.tools.google_place_cards_enabled", return_value=False), \
             patch("services.ayue_agent.tools.nearby_places", return_value=payload) as nearby:
            result = execute_tool(
                ToolCall(name="places.search_nearby", arguments={
                    "anchor": "",
                    "categories": ["cafe"],
                    "use_saved_location": True,
                }),
                context,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.data["origin_kind"], "device")
        self.assertEqual(nearby.call_args.kwargs["latitude"], 22.626)
        self.assertEqual(nearby.call_args.kwargs["longitude"], 120.286)

    def test_explicit_anchor_wins_over_device_location(self):
        context = PublicAgentRequestContext(
            user_id="owner",
            room_id="room",
            message="台北車站附近咖啡",
            device_location=DEVICE_LOCATION,
        )
        payload = {
            "anchor_label": "台北車站",
            "distance_basis": "straight_line",
            "attribution": "© OpenStreetMap contributors",
            "attribution_url": "https://www.openstreetmap.org/copyright",
            "places": [],
        }
        with patch("services.ayue_agent.tools.google_place_cards_enabled", return_value=False), \
             patch("services.ayue_agent.tools.nearby_places", return_value=payload) as nearby:
            result = execute_tool(
                ToolCall(name="places.search_nearby", arguments={
                    "anchor": "台北車站", "categories": ["cafe"],
                }),
                context,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.data["origin_kind"], "explicit")
        self.assertIsNone(nearby.call_args.kwargs["latitude"])
        self.assertIsNone(nearby.call_args.kwargs["longitude"])


if __name__ == "__main__":
    unittest.main()
