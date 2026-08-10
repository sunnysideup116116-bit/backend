import unittest
from unittest.mock import patch

from services.ayue_agent.google_places_client import (
    google_routes_enabled,
    measure_distance_matrix,
    measure_walking_matrix,
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

    def test_routes_capability_does_not_require_browser_card_configuration(self):
        import config

        with patch.object(config, "AYUE_GOOGLE_DISTANCE_MATRIX_ENABLED", True), \
             patch.object(config, "GOOGLE_PLACES_SERVER_API_KEY", "server-test-key"), \
             patch.object(config, "GOOGLE_MAPS_BROWSER_API_KEY", ""), \
             patch.object(config, "AYUE_GOOGLE_PLACE_CARDS_ENABLED", False):
            self.assertTrue(google_routes_enabled())

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

    def test_radius_is_a_hard_filter_not_only_a_google_bias(self):
        payload = {"places": [
            {
                "id": "ChIJnear", "displayName": {"text": "近店"},
                "formattedAddress": "高雄市鹽埕區",
                "location": {"latitude": 22.6205, "longitude": 120.28},
                "types": ["cafe"],
                "googleMapsUri": "https://www.google.com/maps/place/near",
            },
            {
                "id": "ChIJfar", "displayName": {"text": "遠店"},
                "formattedAddress": "高雄市其他區",
                "location": {"latitude": 22.64, "longitude": 120.28},
                "types": ["cafe"],
                "googleMapsUri": "https://www.google.com/maps/place/far",
            },
        ]}
        with self._enabled(), \
             patch("services.ayue_agent.google_places_client.requests.post",
                   return_value=_FakeResponse(payload)) as post:
            places = search_nearby_places(
                "高雄市鹽埕區", 22.62, 120.28, ["cafe"],
                limit=3, radius_m=500,
            )
        radius = post.call_args.kwargs["json"]["locationBias"]["circle"]["radius"]
        self.assertEqual(radius, 500.0)
        self.assertEqual([item["name"] for item in places], ["近店"])

    def test_google_place_projection_rejects_non_google_map_links(self):
        payload = {"places": [{
            "id": "ChIJabc", "displayName": {"text": "可疑店家"},
            "formattedAddress": "高雄市鹽埕區",
            "location": {"latitude": 22.62, "longitude": 120.28},
            "types": ["restaurant"],
            "googleMapsUri": "https://example.com/not-a-google-map",
        }]}
        with self._enabled(), \
             patch("services.ayue_agent.google_places_client.requests.post",
                   return_value=_FakeResponse(payload)):
            places = search_nearby_places(
                "高雄市鹽埕區", 22.62, 120.28, ["restaurant"], limit=3,
            )
        self.assertEqual(places, [])

    def test_normal_search_mask_does_not_request_expensive_enrichments(self):
        payload = {"places": [{
            "id": "ChIJbase", "displayName": {"text": "Base Place"},
            "formattedAddress": "Address", "location": {"latitude": 22.62, "longitude": 120.28},
            "types": ["restaurant"], "googleMapsUri": "https://www.google.com/maps/place/base",
            "rating": 4.8, "userRatingCount": 800,
            "currentOpeningHours": {"openNow": True},
            "priceLevel": "PRICE_LEVEL_MODERATE",
            "priceRange": {"startPrice": {"currencyCode": "TWD", "units": "300"}},
        }]}
        with self._enabled(), \
             patch("services.ayue_agent.google_places_client.requests.post",
                   return_value=_FakeResponse(payload)) as post:
            places = search_nearby_places("Anchor", 22.62, 120.28, ["restaurant"], limit=3)
        mask = post.call_args.kwargs["headers"]["X-Goog-FieldMask"]
        self.assertNotIn("places.rating", mask)
        self.assertNotIn("places.userRatingCount", mask)
        self.assertNotIn("places.currentOpeningHours", mask)
        self.assertNotIn("places.priceLevel", mask)
        self.assertNotIn("places.priceRange", mask)
        self.assertNotIn("rating", places[0])
        self.assertNotIn("opening_hours", places[0])
        self.assertNotIn("price_level", places[0])
        self.assertNotIn("price_range", places[0])

    def test_price_enrichment_requests_mask_and_projects_price_fields(self):
        payload = {"places": [{
            "id": "ChIJprice", "displayName": {"text": "Price Place"},
            "formattedAddress": "Address", "location": {"latitude": 22.62, "longitude": 120.28},
            "types": ["restaurant"], "googleMapsUri": "https://www.google.com/maps/place/price",
            "priceLevel": "PRICE_LEVEL_MODERATE",
            "priceRange": {
                "startPrice": {"currencyCode": "TWD", "units": "300", "nanos": 0},
                "endPrice": {"currencyCode": "TWD", "units": "500", "nanos": 0},
            },
        }]}
        with self._enabled(), \
             patch("services.ayue_agent.google_places_client.requests.post",
                   return_value=_FakeResponse(payload)) as post:
            places = search_nearby_places(
                "Anchor", 22.62, 120.28, ["restaurant"], limit=3,
                enrichments=["price"],
            )
        mask = post.call_args.kwargs["headers"]["X-Goog-FieldMask"]
        self.assertIn("places.priceLevel", mask)
        self.assertIn("places.priceRange", mask)
        self.assertEqual(places[0]["price_level"], "moderate")
        self.assertEqual(places[0]["price_range"], {
            "start_price": {"currency_code": "TWD", "units": 300, "nanos": 0},
            "end_price": {"currency_code": "TWD", "units": 500, "nanos": 0},
        })

    def test_missing_or_partial_price_data_is_omitted_without_inference(self):
        payload = {"places": [{
            "id": "ChIJpartial", "displayName": {"text": "Partial Price Place"},
            "formattedAddress": "Address", "location": {"latitude": 22.62, "longitude": 120.28},
            "types": ["restaurant"], "googleMapsUri": "https://www.google.com/maps/place/partial",
            "priceRange": {
                "startPrice": {"currencyCode": "TWD", "units": "300"},
                "endPrice": {"currencyCode": "TWD"},
            },
        }]}
        with self._enabled(), \
             patch("services.ayue_agent.google_places_client.requests.post",
                   return_value=_FakeResponse(payload)):
            places = search_nearby_places(
                "Anchor", 22.62, 120.28, ["restaurant"], limit=3,
                enrichments=["price"],
            )
        self.assertNotIn("price_level", places[0])
        self.assertEqual(places[0]["price_range"], {
            "start_price": {"currency_code": "TWD", "units": 300, "nanos": 0},
        })
        self.assertNotIn("end_price", places[0]["price_range"])

    def test_requested_enrichments_are_deduplicated_and_projected(self):
        payload = {"places": [{
            "id": "ChIJrich", "displayName": {"text": "Rich Place"},
            "formattedAddress": "Address", "location": {"latitude": 22.62, "longitude": 120.28},
            "types": ["restaurant"], "googleMapsUri": "https://www.google.com/maps/place/rich",
            "rating": 4.5, "userRatingCount": 123,
            "currentOpeningHours": {
                "openNow": False,
                "nextOpenTime": "2026-08-12T09:00:00Z",
                "nextCloseTime": "",
                "weekdayDescriptions": ["Mon: 09:00–18:00", "Tue: 09:00–18:00"],
                "periods": [{"unbounded": "must not escape"}],
            },
        }]}
        with self._enabled(), \
             patch("services.ayue_agent.google_places_client.requests.post",
                   return_value=_FakeResponse(payload)) as post:
            places = search_nearby_places(
                "Anchor", 22.62, 120.28, ["restaurant"], limit=3,
                enrichments=["hours", "rating", "rating", "hours"],
            )
        mask = post.call_args.kwargs["headers"]["X-Goog-FieldMask"]
        self.assertEqual(mask.count("places.rating"), 1)
        self.assertEqual(mask.count("places.userRatingCount"), 1)
        self.assertEqual(mask.count("places.currentOpeningHours"), 1)
        self.assertEqual(places[0]["rating"], 4.5)
        self.assertEqual(places[0]["user_rating_count"], 123)
        self.assertEqual(places[0]["opening_hours"]["open_now"], False)
        self.assertEqual(places[0]["opening_hours"]["weekday_descriptions"], [
            "Mon: 09:00–18:00", "Tue: 09:00–18:00",
        ])
        self.assertNotIn("periods", places[0]["opening_hours"])

    def test_walking_enrichment_uses_one_matrix_call_and_reuses_both_caches(self):
        places_payload = {"places": [{
            "id": "ChIJwalk", "displayName": {"text": "Walk Place"},
            "formattedAddress": "Address", "location": {"latitude": 22.62, "longitude": 120.28},
            "types": ["restaurant"], "googleMapsUri": "https://www.google.com/maps/place/walk",
        }]}
        matrix_payload = [{
            "originIndex": 0, "destinationIndex": 0, "status": {},
            "condition": "ROUTE_EXISTS", "distanceMeters": 700, "duration": "480s",
        }]
        with self._enabled(), \
             patch("services.ayue_agent.google_places_client.google_routes_enabled", return_value=True), \
             patch("services.ayue_agent.google_places_client.requests.post",
                   side_effect=[_FakeResponse(places_payload), _FakeResponse(matrix_payload)]) as post:
            first = search_nearby_places(
                "Anchor", 22.62, 120.28, ["restaurant"], limit=3,
                enrichments=["walking"],
            )
            second = search_nearby_places(
                "Anchor", 22.62, 120.28, ["restaurant"], limit=3,
                enrichments=["walking"],
            )
            base = search_nearby_places(
                "Anchor", 22.62, 120.28, ["restaurant"], limit=3,
            )
        self.assertEqual(post.call_count, 2)
        self.assertEqual(first[0]["walking_duration_seconds"], 480)
        self.assertEqual(second[0]["walking_distance_m"], 700)
        self.assertNotIn("walking_distance_m", base[0])

    def test_walking_matrix_maps_elements_and_ignores_partial_failures(self):
        payload = [
            {
                "originIndex": 0, "destinationIndex": 1, "status": {},
                "condition": "ROUTE_EXISTS", "distanceMeters": 900, "duration": "600s",
            },
            {
                "originIndex": 0, "destinationIndex": 0,
                "status": {"code": 3, "message": "no route"},
                "condition": "ROUTE_NOT_FOUND",
            },
        ]
        places = [
            {"place_id": "ChIJfailed"}, {"place_id": "ChIJwalk"},
        ]
        with patch("services.ayue_agent.google_places_client.google_routes_enabled", return_value=True), \
             patch("services.ayue_agent.google_places_client.requests.post",
                   return_value=_FakeResponse(payload)) as post:
            result = measure_walking_matrix(22.62, 120.28, places)
        self.assertEqual(result, {
            "ChIJwalk": {"walking_distance_m": 900, "walking_duration_seconds": 600},
        })
        self.assertIn("distanceMatrix/v2:computeRouteMatrix", post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs["json"]["travelMode"], "WALK")
        self.assertIn("originIndex", post.call_args.kwargs["headers"]["X-Goog-FieldMask"])
        self.assertIn("status", post.call_args.kwargs["headers"]["X-Goog-FieldMask"])

    def test_single_route_walk_uses_compute_routes_without_drive_preference(self):
        payload = {"routes": [{"distanceMeters": 1200, "duration": "75.5s"}]}
        with patch("services.ayue_agent.google_places_client.google_routes_enabled", return_value=True), \
             patch("services.ayue_agent.google_places_client.requests.post",
                   return_value=_FakeResponse(payload)) as post:
            result = measure_distance_matrix("Origin", "Destination", travel_mode="WALK")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["travelMode"], "WALK")
        self.assertNotIn("routingPreference", body)
        self.assertEqual(result["distance_basis"], "walking")
        self.assertEqual(result["duration_seconds"], 76)


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
