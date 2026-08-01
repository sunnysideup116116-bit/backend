import unittest

from services.ayue_agent.runtime import _public_place_cards


class AyueAgentPlaceCardTests(unittest.TestCase):
    def test_place_cards_only_use_verified_place_observations(self):
        cards = _public_place_cards([
            {"tool": "web.search", "result": {"results": [{"title": "不可信店名"}]}},
            {"tool": "places.search_nearby", "result": {"places": [
                {"provider": "google", "place_id": "ChIJ123_test"},
                {"provider": "google", "place_id": "ChIJ123_test"},
                {"provider": "openstreetmap", "place_id": "not-a-google-card"},
                {"provider": "google", "place_id": "not valid"},
            ]}},
        ])
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["provider"], "google")
        self.assertEqual(cards[0]["place_id"], "ChIJ123_test")

    def test_google_place_cards_are_bounded_to_three(self):
        cards = _public_place_cards([{"tool": "places.search_nearby", "result": {"places": [
            {"provider": "google", "place_id": f"ChIJ{index}card"} for index in range(5)
        ]}}])
        self.assertEqual(len(cards), 3)

    def test_explicit_place_resolve_observation_can_render_one_card(self):
        cards = _public_place_cards([{"tool": "places.resolve_place", "result": {
            "found": True, "place": {"provider": "google", "place_id": "ChIJresolved"},
        }}])
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["place_id"], "ChIJresolved")

    def test_osm_place_card_contains_safe_custom_card_fields_and_embed(self):
        cards = _public_place_cards([{"tool": "places.search_nearby", "result": {
            "attribution": "© OpenStreetMap contributors",
            "attribution_url": "https://www.openstreetmap.org/copyright",
            "places": [{
                "provider": "openstreetmap", "name": "六姐傳統飯糰",
                "category": "restaurant", "distance_m": 350,
                "address_summary": "高雄市鹽埕區",
                "map_url": "https://www.openstreetmap.org/?mlat=22.626000&mlon=120.286000",
            }],
        }}])
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["name"], "六姐傳統飯糰")
        self.assertEqual(cards[0]["distance_label"], "約 350 公尺")
        self.assertTrue(cards[0]["embed_url"].startswith("https://www.openstreetmap.org/export/embed.html?"))

    def test_osm_place_card_rejects_unsafe_map_url(self):
        cards = _public_place_cards([{"tool": "places.search_nearby", "result": {"places": [{
            "provider": "openstreetmap", "name": "不安全", "category": "restaurant",
            "distance_m": 0, "map_url": "javascript:alert(1)",
        }]}}])
        self.assertEqual(cards, [])


if __name__ == "__main__":
    unittest.main()
