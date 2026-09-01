"""Live Google Maps API smoke test.

Requires real GOOGLE_PLACES_SERVER_API_KEY in .env. Temporarily overrides
AYUE_GOOGLE_PLACE_CARDS_ENABLED to "on" in-process (does NOT modify .env).
Run: python -m unittest tests.test_google_maps_live_smoke -v

If this hits the network, it consumes Google Text Search and Routes quota.
Check the provider's current pricing before opting in.
The photo test only verifies the URL is built from the Text Search response;
it never loads the media bytes, so GetPhotoMediaRequest quota is not touched.
"""

import os
import unittest
from unittest.mock import patch

import config
from services.ayue_agent.google_places_client import (
    GooglePlacesError, google_place_cards_enabled,
    measure_distance_matrix, resolve_place, search_nearby_places,
)


def _has_server_key() -> bool:
    return bool(getattr(config, "GOOGLE_PLACES_SERVER_API_KEY", ""))


@unittest.skipUnless(_has_server_key(), "GOOGLE_PLACES_SERVER_API_KEY not set in .env")
class GoogleMapsLiveSmokeTests(unittest.TestCase):
    """Real network calls against Google Maps Platform. Bounded to 3 calls."""

    @classmethod
    def setUpClass(cls):
        # Force the Google provider on for this test run only. We patch the
        # config attribute the enabled-check reads, so .env stays untouched.
        cls._original_cards = getattr(config, "AYUE_GOOGLE_PLACE_CARDS_ENABLED", False)
        cls._original_photos = getattr(config, "AYUE_GOOGLE_PLACE_PHOTOS_ENABLED", False)
        config.AYUE_GOOGLE_PLACE_CARDS_ENABLED = True
        config.AYUE_GOOGLE_PLACE_PHOTOS_ENABLED = True
        if not google_place_cards_enabled():
            raise unittest.SkipTest("google_place_cards_enabled() still False after override")

    @classmethod
    def tearDownClass(cls):
        config.AYUE_GOOGLE_PLACE_CARDS_ENABLED = cls._original_cards
        config.AYUE_GOOGLE_PLACE_PHOTOS_ENABLED = cls._original_photos

    def test_01_resolve_known_landmark(self):
        """Text Search Pro: resolve a well-known Taiwan landmark."""
        place = resolve_place("台北 101")
        self.assertIsNotNone(place, "resolve_place returned None for 台北 101")
        self.assertEqual(place["provider"], "google")
        self.assertTrue(place["place_id"], "missing place_id")
        self.assertTrue(place["name"], "missing name")
        self.assertTrue(place["map_url"].startswith("https://"), "unsafe map_url")
        print(f"\n[resolve_place] name={place['name']!r} place_id={place['place_id'][:20]}...")

    def test_02_nearby_photo_url_comes_from_text_search(self):
        """Photo URLs must be built from the Text Search response, no extra API."""
        places = search_nearby_places(
            "台北 101", 25.0340, 121.5645, ["restaurant"], limit=3,
        )
        self.assertIsInstance(places, list)
        self.assertGreaterEqual(len(places), 1, "no nearby places returned")
        photo_places = [p for p in places if p.get("photo_url")]
        self.assertGreaterEqual(len(photo_places), 1, "no place carried a photo_url")
        photo_url = photo_places[0]["photo_url"]
        self.assertTrue(photo_url.startswith("https://places.googleapis.com/v1/places/"))
        self.assertIn("/media", photo_url)
        self.assertNotIn("rating", photo_places[0])
        # Loading the media bytes bills GetPhotoMediaRequest and is never done
        # here; this test only asserts the URL was derived from the response.
        print(f"\n[nearby photo] count={len(photo_places)} sample={photo_url[:90]}...")

    def test_03_routes_real_driving_distance(self):
        """Routes API Compute Routes Essentials: real driving distance."""
        result = measure_distance_matrix("台北車站", "台北 101")
        self.assertIsNotNone(result, "measure_distance_matrix returned None")
        self.assertEqual(result["distance_basis"], "driving")
        self.assertGreater(result["distance_m"], 0)
        self.assertTrue(result["duration_text"], "missing duration_text")
        self.assertEqual(result["attribution"], "Google Maps")
        print(f"\n[measure_distance_matrix] distance={result['distance_m']}m "
              f"duration={result['duration_text']!r}")

    def test_04_nearby_search_returns_places(self):
        """Text Search Pro: nearby restaurants around a known anchor."""
        # Use a coarse anchor + coords so the locationBias is well-formed.
        places = search_nearby_places(
            "台北 101", 25.0340, 121.5645, ["restaurant"], limit=3,
        )
        self.assertIsInstance(places, list)
        self.assertGreaterEqual(len(places), 1, "no nearby places returned")
        first = places[0]
        self.assertEqual(first["provider"], "google")
        self.assertTrue(first["name"])
        self.assertTrue(first["place_id"])
        self.assertTrue(first["map_url"].startswith("https://"))
        self.assertIn(first["category"], {"restaurant", "cafe", "bar", "attraction", "park"})
        print(f"\n[search_nearby_places] count={len(places)} first={first['name']!r} "
              f"distance={first['distance_m']}m")

    def test_05_cache_hit_does_not_call_api_twice(self):
        """Second resolve within TTL must hit cache, not the network."""
        import services.ayue_agent.google_places_client as gpc
        original_post = gpc.requests.post
        call_count = {"n": 0}
        class CountingPost:
            def __init__(self, *a, **k):
                call_count["n"] += 1
                return original_post(*a, **k)
        with patch.object(gpc.requests, "post", side_effect=CountingPost):
            resolve_place("台北 101")  # likely cache hit from test_01
            resolve_place("台北 101")  # definitely cache hit
        # At least the second call must be served from cache (0 POSTs for it).
        # If cache was cold, first call = 1 POST; if warm from test_01, 0 POSTs.
        self.assertLessEqual(call_count["n"], 1,
                             f"cache miss: {call_count['n']} POSTs, expected <=1")
        print(f"\n[cache] POST calls for 2x resolve: {call_count['n']} (<=1 means cache worked)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
