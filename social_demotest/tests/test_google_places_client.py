import unittest
from unittest.mock import patch

from services.ayue_agent.google_places_client import (
    resolve_place,
    search_nearby_places,
)


class _FakeResponse:
    def __init__(self, payload, ok=True, status_code=200):
        self._payload = payload
        self.ok = ok
        self.status_code = status_code

    def json(self):
        return self._payload


class GooglePlacesCuisineTests(unittest.TestCase):
    def setUp(self):
        from services.ayue_agent.google_places_client import _MEMORY_CACHE
        _MEMORY_CACHE.clear()

    def _enabled(self):
        return patch(
            "services.ayue_agent.google_places_client.google_place_cards_enabled",
            return_value=True,
        )

    def test_cuisine_is_folded_into_text_query(self):
        payload = {"places": [{
            "id": "ChIJabc", "displayName": {"text": "示範火鍋店"},
            "formattedAddress": "高雄市鹽埕區", "location": {"latitude": 22.62, "longitude": 120.28},
            "types": ["restaurant"], "googleMapsUri": "https://www.google.com/maps/place/x",
        }]}
        with self._enabled(), \
             patch("services.ayue_agent.google_places_client.requests.post",
                   return_value=_FakeResponse(payload)) as post:
            places = search_nearby_places(
                "高雄市鹽埕區", 22.62, 120.28, ["restaurant"], limit=3, cuisine="火鍋",
            )
        body = post.call_args.kwargs["json"]
        self.assertIn("火鍋", body["textQuery"])
        self.assertIn("restaurant", body["textQuery"])
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["name"], "示範火鍋店")

    def test_cuisine_omitted_when_empty(self):
        payload = {"places": []}
        with self._enabled(), \
             patch("services.ayue_agent.google_places_client.requests.post",
                   return_value=_FakeResponse(payload)) as post:
            search_nearby_places("高雄市鹽埕區", 22.62, 120.28, ["restaurant"], limit=3)
        body = post.call_args.kwargs["json"]
        self.assertNotIn("火鍋", body["textQuery"])
        self.assertIn("restaurant", body["textQuery"])

    def test_cuisine_is_bounded_and_cleaned(self):
        payload = {"places": []}
        with self._enabled(), \
             patch("services.ayue_agent.google_places_client.requests.post",
                   return_value=_FakeResponse(payload)) as post:
            search_nearby_places(
                "高雄市鹽埕區", 22.62, 120.28, ["restaurant"], limit=3,
                cuisine="火鍋\n日式\t料理" + "長" * 100,
            )
        body = post.call_args.kwargs["json"]
        self.assertNotIn("\n", body["textQuery"])
        self.assertNotIn("\t", body["textQuery"])
        self.assertLessEqual(len(body["textQuery"]), 200)


    def test_photos_are_taken_directly_from_text_search_response(self):
        payload = {"places": [{
            "id": "ChIJabc", "displayName": {"text": "示範餐廳"},
            "formattedAddress": "高雄市鹽埕區", "location": {"latitude": 22.62, "longitude": 120.28},
            "types": ["restaurant"], "googleMapsUri": "https://www.google.com/maps/place/x",
            "photos": [{"name": "places/ChIJabc/photos/ref1", "widthPx": 800}],
        }]}
        import config
        original = getattr(config, "AYUE_GOOGLE_PLACE_PHOTOS_ENABLED", False)
        config.AYUE_GOOGLE_PLACE_PHOTOS_ENABLED = True
        try:
            with self._enabled(), \
                 patch.object(config, "GOOGLE_MAPS_BROWSER_API_KEY", "browser-test-key"), \
                 patch.object(config, "GOOGLE_PLACES_SERVER_API_KEY", "server-secret-key"), \
                 patch("services.ayue_agent.google_places_client.requests.post",
                       return_value=_FakeResponse(payload)) as post:
                places = search_nearby_places("高雄市鹽埕區", 22.62, 120.28, ["restaurant"], limit=3)
        finally:
            config.AYUE_GOOGLE_PLACE_PHOTOS_ENABLED = original
        self.assertIn("places.photos", post.call_args.kwargs["headers"]["X-Goog-FieldMask"])
        self.assertEqual(len(places), 1)
        self.assertIn(
            "https://places.googleapis.com/v1/places/ChIJabc/photos/ref1/media",
            places[0]["photo_url"],
        )

    def test_resolve_place_takes_photo_from_text_search_response(self):
        payload = {"places": [{
            "id": "ChIJxyz", "displayName": {"text": "示範店"},
            "formattedAddress": "高雄市鹽埕區", "location": {"latitude": 22.62, "longitude": 120.28},
            "types": ["restaurant"], "googleMapsUri": "https://www.google.com/maps/place/x",
            "photos": [{"name": "places/ChIJxyz/photos/ref9", "widthPx": 800}],
        }]}
        import config
        original = getattr(config, "AYUE_GOOGLE_PLACE_PHOTOS_ENABLED", False)
        config.AYUE_GOOGLE_PLACE_PHOTOS_ENABLED = True
        try:
            with self._enabled(), \
                 patch.object(config, "GOOGLE_MAPS_BROWSER_API_KEY", "browser-test-key"), \
                 patch.object(config, "GOOGLE_PLACES_SERVER_API_KEY", "server-secret-key"), \
                 patch("services.ayue_agent.google_places_client.requests.post",
                       return_value=_FakeResponse(payload)):
                place = resolve_place("示範店")
        finally:
            config.AYUE_GOOGLE_PLACE_PHOTOS_ENABLED = original
        self.assertIsNotNone(place)
        self.assertIn("places/ChIJxyz/photos/ref9/media", place["photo_url"])

    def test_no_photo_field_when_response_has_none(self):
        payload = {"places": [{
            "id": "ChIJabc", "displayName": {"text": "示範餐廳"},
            "formattedAddress": "高雄市鹽埕區", "location": {"latitude": 22.62, "longitude": 120.28},
            "types": ["restaurant"], "googleMapsUri": "https://www.google.com/maps/place/x",
        }]}
        with self._enabled(), \
             patch("services.ayue_agent.google_places_client.requests.post",
                   return_value=_FakeResponse(payload)):
            places = search_nearby_places("高雄市鹽埕區", 22.62, 120.28, ["restaurant"], limit=3)
        self.assertEqual(places[0].get("photo_url"), "")

    def test_photo_url_suppressed_when_photos_flag_off(self):
        payload = {"places": [{
            "id": "ChIJabc", "displayName": {"text": "示範餐廳"},
            "formattedAddress": "高雄市鹽埕區", "location": {"latitude": 22.62, "longitude": 120.28},
            "types": ["restaurant"], "googleMapsUri": "https://www.google.com/maps/place/x",
            "photos": [{"name": "places/ChIJabc/photos/ref1", "widthPx": 800}],
        }]}
        import config
        original = getattr(config, "AYUE_GOOGLE_PLACE_PHOTOS_ENABLED", False)
        config.AYUE_GOOGLE_PLACE_PHOTOS_ENABLED = False
        try:
            with self._enabled(), \
                 patch("services.ayue_agent.google_places_client.requests.post",
                       return_value=_FakeResponse(payload)):
                places = search_nearby_places("高雄市鹽埕區", 22.62, 120.28, ["restaurant"], limit=3)
        finally:
            config.AYUE_GOOGLE_PLACE_PHOTOS_ENABLED = original
        self.assertEqual(places[0].get("photo_url"), "")

    def test_photo_url_present_when_photos_flag_on(self):
        payload = {"places": [{
            "id": "ChIJabc", "displayName": {"text": "示範餐廳"},
            "formattedAddress": "高雄市鹽埕區", "location": {"latitude": 22.62, "longitude": 120.28},
            "types": ["restaurant"], "googleMapsUri": "https://www.google.com/maps/place/x",
            "photos": [{"name": "places/ChIJabc/photos/ref1", "widthPx": 800}],
        }]}
        import config
        original = getattr(config, "AYUE_GOOGLE_PLACE_PHOTOS_ENABLED", False)
        config.AYUE_GOOGLE_PLACE_PHOTOS_ENABLED = True
        try:
            with self._enabled(), \
                 patch.object(config, "GOOGLE_MAPS_BROWSER_API_KEY", "browser-test-key"), \
                 patch.object(config, "GOOGLE_PLACES_SERVER_API_KEY", "server-secret-key"), \
                 patch("services.ayue_agent.google_places_client.requests.post",
                       return_value=_FakeResponse(payload)):
                places = search_nearby_places("高雄市鹽埕區", 22.62, 120.28, ["restaurant"], limit=3)
        finally:
            config.AYUE_GOOGLE_PLACE_PHOTOS_ENABLED = original
        self.assertIn("places/ChIJabc/photos/ref1/media", places[0]["photo_url"])
        self.assertIn("browser-test-key", places[0]["photo_url"])
        self.assertNotIn("server-secret-key", places[0]["photo_url"])


if __name__ == "__main__":
    unittest.main()
