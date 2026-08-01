import unittest
from unittest.mock import MagicMock, patch

from services import giphy_service


class GiphyServiceTests(unittest.TestCase):
    def setUp(self):
        giphy_service._reaction_cache = None

    @patch.object(giphy_service.config, "GIPHY_API_KEY", "")
    def test_missing_key_fails_soft_without_network_call(self):
        with patch.object(giphy_service.requests, "get") as get:
            self.assertFalse(giphy_service.write_match_celebration_gifs((("a", "b"), ("b", "a")), "match-1"))
        get.assert_not_called()

    @patch.object(giphy_service.config, "GIPHY_GIF_ENABLED", True)
    @patch.object(giphy_service.config, "GIPHY_API_KEY", "test-key")
    def test_valid_giphy_reaction_is_saved_as_typed_message(self):
        response = MagicMock()
        response.json.return_value = {"data": [{"images": {
            "original": {"url": "https://media.giphy.com/media/ok/giphy.gif", "width": "480", "height": "270"},
            "fixed_height_small": {"url": "https://media.giphy.com/media/ok/100.gif"},
        }}]}
        with patch.object(giphy_service.requests, "get", return_value=response), \
             patch.object(giphy_service, "queue_mediator_event", side_effect=[{"event_id": "a"}, {"event_id": "b"}]) as queue:
            self.assertTrue(giphy_service.write_match_celebration_gifs((("a", "b"), ("b", "a")), "match-1"))
        self.assertEqual(queue.call_count, 2)
        self.assertEqual([call.args[0] for call in queue.call_args_list], ["a", "b"])
        self.assertTrue(all(call.args[2] == "match_connected_gif" for call in queue.call_args_list))
        self.assertEqual(queue.call_args_list[0].kwargs["other_id"], "b")
        self.assertEqual(queue.call_args_list[1].kwargs["other_id"], "a")
        media = queue.call_args_list[0].kwargs["media"]
        self.assertEqual(media["provider"], "giphy")
        self.assertTrue(media["url"].startswith("https://media.giphy.com/"))

    @patch.object(giphy_service.config, "GIPHY_GIF_ENABLED", True)
    @patch.object(giphy_service.config, "GIPHY_API_KEY", "test-key")
    def test_non_giphy_media_url_is_rejected(self):
        response = MagicMock()
        response.json.return_value = {"data": [{"images": {
            "original": {"url": "https://example.invalid/nope.gif"},
            "fixed_height_small": {"url": "https://example.invalid/nope-small.gif"},
        }}]}
        with patch.object(giphy_service.requests, "get", return_value=response), \
             patch.object(giphy_service, "queue_mediator_event") as queue:
            self.assertFalse(giphy_service.write_match_celebration_gifs((("a", "b"), ("b", "a")), "match-1"))
        queue.assert_not_called()

    @patch.object(giphy_service.config, "GIPHY_GIF_ENABLED", True)
    @patch.object(giphy_service.config, "GIPHY_API_KEY", "test-key")
    def test_lookalike_giphy_hostname_is_rejected(self):
        response = MagicMock()
        response.json.return_value = {"data": [{"images": {
            "original": {"url": "https://notgiphy.com/nope.gif"},
            "fixed_height_small": {"url": "https://notgiphy.com/nope-small.gif"},
        }}]}
        with patch.object(giphy_service.requests, "get", return_value=response), \
             patch.object(giphy_service, "queue_mediator_event") as queue:
            self.assertFalse(giphy_service.write_match_celebration_gifs((("a", "b"), ("b", "a")), "match-1"))
        queue.assert_not_called()

    def test_scheduler_is_used_when_available(self):
        scheduled = []
        giphy_service.schedule_match_celebration_gifs((("a", "b"), ("b", "a")), "match-1", scheduled.append)
        self.assertEqual(len(scheduled), 1)
        self.assertTrue(callable(scheduled[0]))


if __name__ == "__main__":
    unittest.main()
