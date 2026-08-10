import unittest

from services.ayue_agent.v3.scheduler import _public_place_cards, _google_embed_url


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

    def test_google_place_cards_are_bounded_to_five(self):
        cards = _public_place_cards([{"tool": "places.search_nearby", "result": {"places": [
            {"provider": "google", "place_id": f"ChIJ{index}card"} for index in range(8)
        ]}}])
        self.assertEqual(len(cards), 5)

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

    def test_google_card_carries_photo_but_not_enterprise_fields(self):
        cards = _public_place_cards([{"tool": "places.resolve_place", "result": {
            "found": True,
            "place": {
                "provider": "google", "place_id": "ChIJdetails", "name": "示範餐廳",
                "category": "restaurant", "distance_m": 0, "map_url": "https://www.google.com/maps/place/x",
                "photo_url": "https://places.googleapis.com/v1/places/ChIJdetails/photos/ref/media?maxWidthPx=400&key=k",
                "rating": 4.5, "user_rating_count": 1234,
                "opening_hours_summary": "營業中",
            },
        }}])
        self.assertEqual(len(cards), 1)
        self.assertNotIn("rating", cards[0])
        self.assertNotIn("user_rating_count", cards[0])
        self.assertNotIn("opening_hours_summary", cards[0])
        self.assertEqual(
            cards[0]["photo_url"],
            "https://places.googleapis.com/v1/places/ChIJdetails/photos/ref/media?maxWidthPx=400&key=k",
        )

    def test_google_card_rejects_invalid_rating(self):
        cards = _public_place_cards([{"tool": "places.resolve_place", "result": {
            "found": True,
            "place": {
                "provider": "google", "place_id": "ChIJbad", "name": "怪店",
                "category": "restaurant", "distance_m": 0, "map_url": "https://www.google.com/maps/place/x",
                "rating": 9.9,  # enterprise field must never leak into cards
                "opening_hours_summary": "",
            },
        }}])
        self.assertEqual(len(cards), 1)
        self.assertNotIn("rating", cards[0])

    def test_google_card_drops_unsafe_photo_url(self):
        cards = _public_place_cards([{"tool": "places.resolve_place", "result": {
            "found": True,
            "place": {
                "provider": "google", "place_id": "ChIJphoto", "name": "有照片",
                "category": "restaurant", "distance_m": 0, "map_url": "https://www.google.com/maps/place/x",
                "photo_url": "https://evil.example.com/photo.jpg",
            },
        }}])
        self.assertEqual(len(cards), 1)
        self.assertNotIn("photo_url", cards[0])

    def test_google_embed_url_returns_empty_without_browser_key(self):
        # Force no browser key regardless of .env so this test is independent.
        import config
        original = getattr(config, "GOOGLE_MAPS_BROWSER_API_KEY", "")
        config.GOOGLE_MAPS_BROWSER_API_KEY = ""
        try:
            self.assertEqual(_google_embed_url("ChIJtest123"), "")
        finally:
            config.GOOGLE_MAPS_BROWSER_API_KEY = original

    def test_google_embed_url_rejects_invalid_place_id(self):
        import config
        original = getattr(config, "GOOGLE_MAPS_BROWSER_API_KEY", "")
        config.GOOGLE_MAPS_BROWSER_API_KEY = "test_key"
        try:
            self.assertEqual(_google_embed_url("not valid id with space"), "")
            self.assertEqual(_google_embed_url(""), "")
        finally:
            config.GOOGLE_MAPS_BROWSER_API_KEY = original

    def test_google_card_embed_url_when_browser_key_present(self):
        import config
        original = getattr(config, "GOOGLE_MAPS_BROWSER_API_KEY", "")
        config.GOOGLE_MAPS_BROWSER_API_KEY = "test_browser_key_123"
        try:
            cards = _public_place_cards([{"tool": "places.resolve_place", "result": {
                "found": True,
                "place": {
                    "provider": "google", "place_id": "ChIJembed", "name": "示範",
                    "category": "restaurant", "distance_m": 0, "map_url": "https://www.google.com/maps/place/x",
                },
            }}])
        finally:
            config.GOOGLE_MAPS_BROWSER_API_KEY = original
        self.assertEqual(len(cards), 1)
        self.assertIn("https://www.google.com/maps/embed/v1/place", cards[0]["embed_url"])
        self.assertIn("place_id%3AChIJembed", cards[0]["embed_url"])

    def test_osm_card_still_uses_osm_embed_when_google_disabled(self):
        cards = _public_place_cards([{"tool": "places.search_nearby", "result": {"places": [{
            "provider": "openstreetmap", "name": "OSM 店", "category": "cafe",
            "distance_m": 100, "map_url": "https://www.openstreetmap.org/?mlat=22.6&mlon=120.2",
        }]}}])
        self.assertEqual(len(cards), 1)
        self.assertTrue(cards[0]["embed_url"].startswith("https://www.openstreetmap.org/"))


if __name__ == "__main__":
    unittest.main()
