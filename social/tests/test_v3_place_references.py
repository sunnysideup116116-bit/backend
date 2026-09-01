import unittest
from unittest.mock import patch

from pydantic import ValidationError
from services.ayue_agent.v3.calendar_commands import CalendarCommand
from services.ayue_agent.v3.place_references import (
    clear_runtime_state,
    get_candidate,
    get_candidate_set,
    public_projection,
    replace_presented_candidates,
    resolve_message_reference,
)


def _cards(*names):
    return [
        {
            "name": name,
            "category": "cafe",
            "address_summary": "高雄市鹽埕區",
            "provider": "google",
            "place_id": f"ChIJplace{index}",
            "map_url": f"https://www.google.com/maps/place/{index}",
        }
        for index, name in enumerate(names, start=1)
    ]


class V3PlaceReferenceTests(unittest.TestCase):
    def setUp(self):
        clear_runtime_state()
        self.collection = patch(
            "services.ayue_agent.v3.place_references._collection",
            return_value=None,
        )
        self.collection.start()

    def tearDown(self):
        self.collection.stop()
        clear_runtime_state()

    def test_second_presented_candidate_resolves_with_server_identity(self):
        replace_presented_candidates(
            "owner", "room",
            _cards("樺達奶茶", "不二 TEA&NO.1", "鹽埕小熊奶茶"),
        )
        resolution = resolve_message_reference("owner", "room", "就剛剛第二個")
        self.assertEqual(resolution["status"], "resolved")
        self.assertEqual(resolution["candidate"]["ordinal"], 2)
        self.assertEqual(resolution["candidate"]["label"], "不二 TEA&NO.1")
        self.assertEqual(resolution["candidate"]["provider_place_id"], "ChIJplace2")

        projection = public_projection(get_candidate_set("owner", "room"))
        self.assertEqual(projection["candidates"][1]["label"], "不二 TEA&NO.1")
        self.assertTrue(projection["candidates"][1]["reference"].startswith("place_ref_"))
        self.assertNotIn("provider_place_id", str(projection))
        self.assertNotIn("map_identity", str(projection))

    def test_stale_candidate_set_expires(self):
        with patch("services.ayue_agent.v3.place_references.time.time", return_value=100.0):
            replace_presented_candidates("owner", "room", _cards("A", "B", "C"))
        with patch("services.ayue_agent.v3.place_references.time.time", return_value=701.0):
            resolution = resolve_message_reference("owner", "room", "第二個")
            self.assertEqual(resolution["status"], "unavailable")
            self.assertIsNone(get_candidate_set("owner", "room"))

    def test_new_candidate_set_replaces_old_references(self):
        old = replace_presented_candidates("owner", "room", _cards("A", "B", "C"))
        old_second_ref = old["candidates"][1]["reference"]
        replace_presented_candidates("owner", "room", _cards("D", "E"))

        self.assertIsNone(get_candidate("owner", "room", old_second_ref))
        resolution = resolve_message_reference("owner", "room", "第二家")
        self.assertEqual(resolution["status"], "resolved")
        self.assertEqual(resolution["candidate"]["label"], "E")

    def test_invalid_ordinal_and_ambiguous_deictic_fail_closed(self):
        replace_presented_candidates("owner", "room", _cards("A", "B", "C"))
        invalid = resolve_message_reference("owner", "room", "第四個")
        ambiguous = resolve_message_reference("owner", "room", "那間")
        multiple = resolve_message_reference("owner", "room", "第一個或第二個")
        self.assertEqual(invalid["status"], "invalid_ordinal")
        self.assertEqual(ambiguous["status"], "ambiguous")
        self.assertEqual(multiple["status"], "ambiguous")

    def test_malformed_stored_ordinal_fails_closed(self):
        replace_presented_candidates("owner", "room", _cards("A", "B", "C"))
        record = get_candidate_set("owner", "room")
        record["candidates"][1]["ordinal"] = 2.5
        resolution = resolve_message_reference("owner", "room", "第二個")
        self.assertEqual(resolution["status"], "invalid_ordinal")

    def test_deictic_resolves_only_after_unique_selection(self):
        replace_presented_candidates("owner", "room", _cards("A", "B", "C"))
        last = resolve_message_reference("owner", "room", "最後一個")
        self.assertEqual(last["candidate"]["label"], "C")
        selected = resolve_message_reference("owner", "room", "第二個")
        self.assertEqual(selected["status"], "resolved")
        follow_up = resolve_message_reference("owner", "room", "那間")
        self.assertEqual(follow_up["status"], "resolved")
        self.assertEqual(follow_up["candidate"]["label"], "B")

    def test_calendar_command_rejects_model_authored_place_reference(self):
        with self.assertRaises(ValidationError):
            CalendarCommand.model_validate({
                "action": "create",
                "title": "A",
                "date": "2026-08-29",
                "start_time": "05:00",
                "end_time": "06:00",
                "place_reference": "place_ref_0123456789abcdef01234567",
            })


if __name__ == "__main__":
    unittest.main()
