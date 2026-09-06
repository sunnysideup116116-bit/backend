import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError
from services.ayue_agent.v3.calendar_commands import CalendarCommand
from services.ayue_agent.v3.place_followups import (
    clear_followup,
    save_followup,
)
from services.ayue_agent.v3.place_references import (
    commit_resolved_selection,
    clear_runtime_state,
    get_candidate,
    clear_runtime_state,
    get_candidate,
    get_candidate_set,
    public_projection,
    recent_selected_projection,
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
        clear_followup("owner", "room")
        self.collection = patch(
            "services.ayue_agent.v3.place_references._collection",
            return_value=None,
        )
        self.collection.start()

    def tearDown(self):
        self.collection.stop()
        clear_runtime_state()
        clear_followup("owner", "room")

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

    def test_presentation_survives_the_previous_ttl_window(self):
        with patch("services.ayue_agent.v3.place_references.time.time", return_value=100.0):
            replace_presented_candidates(
                "owner", "room", _cards("A", "B", "C"), origin_run_id="run-old",
            )
        with patch("services.ayue_agent.v3.place_references.time.time", return_value=701.0):
            resolution = resolve_message_reference("owner", "room", "第二個")
            self.assertEqual(resolution["status"], "resolved")
            self.assertEqual(resolution["candidate"]["label"], "B")
            self.assertIsNotNone(get_candidate_set("owner", "room"))

    def test_new_presentation_keeps_old_references_and_latest_ordinal(self):
        old = replace_presented_candidates(
            "owner", "room", _cards("A", "B", "C"), origin_run_id="run-old",
        )
        old_second_ref = old["candidates"][1]["reference"]
        replace_presented_candidates(
            "owner", "room", _cards("D", "E"), origin_run_id="run-new",
        )

        self.assertEqual(get_candidate("owner", "room", old_second_ref)["label"], "B")
        self.assertEqual(
            get_candidate_set("owner", "room", origin_run_id="run-old")["candidates"][1]["label"],
            "B",
        )
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

    def test_ordinal_parser_does_not_suffix_match_large_numbers(self):
        replace_presented_candidates("owner", "room", _cards("A", "B", "C"))
        for message in ("第12間", "第十一間", "第22個"):
            with self.subTest(message=message):
                result = resolve_message_reference("owner", "room", message)
                self.assertEqual(result["status"], "invalid_ordinal")
                self.assertEqual(result["candidate_count"], 3)

    def test_source_phrases_choose_the_requested_snapshot(self):
        with patch("services.ayue_agent.v3.place_references.time.time", return_value=100.0):
            replace_presented_candidates(
                "owner", "room", _cards("舊店 A", "舊店 B"), origin_run_id="run-old",
            )
        with patch("services.ayue_agent.v3.place_references.time.time", return_value=200.0):
            replace_presented_candidates(
                "owner", "room", _cards("新店 A", "新店 B"), origin_run_id="run-new",
            )

        for phrase in ("上一組", "上一次", "前一組", "前一次", "之前推薦", "先前清單", "原本候選"):
            with self.subTest(phrase=phrase):
                result = resolve_message_reference("owner", "room", f"{phrase}的第二間")
                self.assertEqual(result["status"], "resolved")
                self.assertEqual(result["candidate"]["label"], "舊店 B")

        for phrase in ("剛才", "剛剛", "最新", "這次"):
            with self.subTest(phrase=phrase):
                result = resolve_message_reference("owner", "room", f"{phrase}的第二間")
                self.assertEqual(result["status"], "resolved")
                self.assertEqual(result["candidate"]["label"], "新店 B")

    def test_explicit_category_selects_matching_older_snapshot(self):
        drinks = _cards("飲料 A", "飲料 B", "飲料 C", "雪波喫茶", "飲料 E")
        chicken = _cards("炸雞 A", "炸雞 B", "炸雞 C")
        for item in chicken:
            item["category"] = "restaurant"
        with patch("services.ayue_agent.v3.place_references.time.time", return_value=100.0):
            replace_presented_candidates(
                "owner", "room", drinks, origin_run_id="drinks-run",
            )
        with patch("services.ayue_agent.v3.place_references.time.time", return_value=200.0):
            replace_presented_candidates(
                "owner", "room", chicken, origin_run_id="chicken-run",
            )

        result = resolve_message_reference(
            "owner", "room", "你幫我查剛剛第四間飲料店的資訊",
        )

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["candidate"]["label"], "雪波喫茶")
        self.assertEqual(result["origin_run_id"], "drinks-run")

    def test_older_source_with_many_snapshots_returns_concrete_source_options(self):
        for index, names in enumerate((("第一組 A", "第一組 B"), ("第二組 A", "第二組 B"), ("第三組 A", "第三組 B"))):
            with patch(
                "services.ayue_agent.v3.place_references.time.time",
                return_value=float(index + 1),
            ):
                replace_presented_candidates(
                    "owner", "room", _cards(*names), origin_run_id=f"run-{index}",
                )

        result = resolve_message_reference("owner", "room", "之前推薦的第二間")
        self.assertEqual(result["status"], "ambiguous_source")
        self.assertEqual(
            [item["label"] for item in result["source_options"]],
            ["第1組：第三組 A、第三組 B", "第2組：第二組 A、第二組 B", "第3組：第一組 A、第一組 B"],
        )
        named = resolve_message_reference("owner", "room", "之前推薦的第一組 B")
        self.assertEqual(named["status"], "resolved")
        self.assertEqual(named["candidate"]["label"], "第一組 B")

    def test_active_draft_origin_wins_after_a_new_search(self):
        old = replace_presented_candidates(
            "owner", "room", _cards("舊店 A", "舊店 B"), origin_run_id="run-old",
        )
        old_second = old["candidates"][1]
        save_followup(
            "owner", "room", CalendarCommand(action="create"),
            resolution={
                "status": "resolved",
                "reference": old_second["reference"],
                "ordinal": old_second["ordinal"],
                "label": old_second["label"],
            },
        )
        replace_presented_candidates(
            "owner", "room", _cards("新店 A", "新店 B"), origin_run_id="run-new",
        )

        result = resolve_message_reference("owner", "room", "第二間")
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["candidate"]["label"], "舊店 B")
        self.assertEqual(result["origin_run_id"], "run-old")

        latest = resolve_message_reference("owner", "room", "最新清單的第二間")
        self.assertEqual(latest["status"], "resolved")
        self.assertEqual(latest["candidate"]["label"], "新店 B")

    def test_calendar_context_allows_place_ordinal_but_rejects_reminder_ordinal(self):
        replace_presented_candidates("owner", "room", _cards("A", "B", "C"))
        place = resolve_message_reference("owner", "room", "幫我把第二間加到明天行事曆")
        reminder = resolve_message_reference("owner", "room", "第二個提醒加到行事曆")
        self.assertEqual(place["status"], "resolved")
        self.assertEqual(place["candidate"]["label"], "B")
        self.assertEqual(reminder["status"], "none")

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

    def test_unique_name_resolves_and_non_place_ordinals_pass_through(self):
        replace_presented_candidates("owner", "room", _cards("50嵐 自立六合店", "B 店"))
        by_name = resolve_message_reference("owner", "room", "幫我把 50嵐自立六合店 加到行程")
        self.assertEqual(by_name["status"], "resolved")
        self.assertEqual(by_name["candidate"]["label"], "50嵐 自立六合店")

        non_place = resolve_message_reference("owner", "room", "把第二個提醒取消")
        self.assertEqual(non_place["status"], "none")

    def test_exact_branch_name_wins_over_brand_fragment(self):
        replace_presented_candidates(
            "owner", "room", _cards("50嵐自立六合店", "50嵐七賢店"),
        )
        result = resolve_message_reference("owner", "room", "把50嵐自立六合店加到行程")
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["candidate"]["label"], "50嵐自立六合店")

    def test_unique_bare_brand_resolves_and_multiple_branches_are_ambiguous(self):
        replace_presented_candidates("owner", "room", _cards("50嵐自立六合店"))
        unique = resolve_message_reference("owner", "room", "把50嵐加到明天行事曆")
        self.assertEqual(unique["status"], "resolved")
        self.assertEqual(unique["candidate"]["label"], "50嵐自立六合店")

        clear_runtime_state()
        replace_presented_candidates(
            "owner", "room", _cards("50嵐自立六合店", "50嵐七賢店"),
        )
        ambiguous = resolve_message_reference("owner", "room", "把50嵐加到行程")
        self.assertEqual(ambiguous["status"], "ambiguous")
        self.assertEqual(
            {item["label"] for item in ambiguous["candidate_options"]},
            {"50嵐自立六合店", "50嵐七賢店"},
        )

    def test_name_and_ordinal_conflict_is_explicitly_ambiguous(self):
        replace_presented_candidates("owner", "room", _cards("A 店", "B 店"))
        result = resolve_message_reference("owner", "room", "把第二間 A 店加到行程")
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(
            {item["label"] for item in result["candidate_options"]},
            {"A 店", "B 店"},
        )

    def test_ordinal_and_deictic_combination_does_not_choose_silently(self):
        replace_presented_candidates("owner", "room", _cards("A 店", "B 店"))
        result = resolve_message_reference("owner", "room", "第二間那間")
        self.assertEqual(result["status"], "ambiguous")

    def test_newer_search_invalidates_bare_deictic_for_old_selection(self):
        replace_presented_candidates(
            "owner", "room", _cards("舊店 A", "舊店 B"), origin_run_id="run-old",
        )
        selected = resolve_message_reference("owner", "room", "第二間")
        self.assertEqual(selected["candidate"]["label"], "舊店 B")
        replace_presented_candidates(
            "owner", "room", _cards("新店 A", "新店 B"), origin_run_id="run-new",
        )

        follow_up = resolve_message_reference("owner", "room", "那間")
        self.assertEqual(follow_up["status"], "ambiguous")
        self.assertEqual(follow_up["candidate_count"], 2)

    def test_recent_selected_place_resolves_it_without_calendar_draft(self):
        replace_presented_candidates(
            "owner", "room", _cards("店 A", "店 B", "店 C"), origin_run_id="run-one",
        )
        selected = resolve_message_reference("owner", "room", "第二間怎樣？")
        self.assertEqual(selected["status"], "resolved")
        self.assertEqual(selected["candidate"]["label"], "店 B")
        self.assertEqual(recent_selected_projection("owner", "room")["label"], "店 B")

        follow_up = resolve_message_reference("owner", "room", "它好不好吃啊？")
        self.assertEqual(follow_up["status"], "resolved")
        self.assertEqual(follow_up["candidate"]["label"], "店 B")
        self.assertEqual(follow_up["resolution_method"], "selected_pronoun")

    def test_unpublished_snapshot_is_invisible_until_assistant_message_exists(self):
        replace_presented_candidates(
            "owner", "room", _cards("A", "B"),
            origin_run_id="run-unpublished", source_message_id="owner-message", published=False,
        )
        unavailable = resolve_message_reference("owner", "room", "把第二間加到行程")
        self.assertEqual(unavailable["status"], "missing_snapshot")

        from services.ayue_agent.v3.place_references import publish_place_presentation
        self.assertTrue(
            publish_place_presentation("owner", "room", "run-unpublished", "assistant-message")
        )
        resolved = resolve_message_reference("owner", "room", "把第二間加到行程")
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(resolved["candidate"]["label"], "B")

    def test_published_snapshot_with_missing_source_message_is_not_selectable(self):
        class FakeCollection:
            def __init__(self):
                self.record = None

            def find_one(self, _query):
                return self.record

            def find(self, _query):
                return [self.record] if self.record else []

            def insert_one(self, record):
                self.record = dict(record)

        fake_collection = FakeCollection()
        with patch.dict("os.environ", {"AYUE_TEST_MODE": "off"}, clear=False), patch(
            "services.ayue_agent.v3.place_references._collection",
            return_value=fake_collection,
        ):
            replace_presented_candidates(
                "owner", "room", _cards("A", "B"),
                origin_run_id="run-source-missing",
                source_message_id="not-an-object-id",
                published=True,
            )
            result = resolve_message_reference("owner", "room", "把第二間加到行程")
        self.assertEqual(result["status"], "source_unavailable")

    def test_snapshot_survives_process_memory_reset_with_durable_store(self):
        class FakeCollection:
            def __init__(self):
                self.records = []

            def _matches(self, record, query):
                for key, value in query.items():
                    if key == "candidates.reference":
                        if not any(item.get("reference") == value for item in record["candidates"]):
                            return False
                    elif isinstance(value, dict) and "$ne" in value:
                        if record.get(key) == value["$ne"]:
                            return False
                    elif record.get(key) != value:
                        return False
                return True

            def find(self, query):
                return [record for record in self.records if self._matches(record, query)]

            def find_one(self, query):
                matches = self.find(query)
                return matches[0] if matches else None

            def insert_one(self, record):
                self.records.append(dict(record))

            def update_one(self, query, update, upsert=False):
                record = self.find_one(query)
                if record is None:
                    if not upsert:
                        return SimpleNamespace(matched_count=0)
                    record = {key: value for key, value in query.items() if not isinstance(value, dict)}
                    self.records.append(record)
                record.update(update.get("$set", {}))
                return SimpleNamespace(matched_count=1)

        fake_collection = FakeCollection()
        with patch.dict("os.environ", {"AYUE_TEST_MODE": "off"}, clear=False), patch(
            "services.ayue_agent.v3.place_references._collection",
            return_value=fake_collection,
        ), patch(
            "services.ayue_agent.v3.place_references._published_source_is_accessible",
            return_value=True,
        ):
            replace_presented_candidates(
                "owner", "room", _cards("A", "B"), origin_run_id="durable-run",
            )
            clear_runtime_state()
            resolution = resolve_message_reference("owner", "room", "把第二間加到行程")
        self.assertEqual(resolution["status"], "resolved")
        self.assertEqual(resolution["candidate"]["label"], "B")

    def test_requested_result_count_does_not_change_selected_place(self):
        replace_presented_candidates(
            "owner", "room",
            _cards("第一間", "第二間", "第三間", "第四間", "第五間"),
            origin_run_id="run-five",
        )
        selected = resolve_message_reference("owner", "room", "第三間的詳細資料")
        self.assertEqual(selected["candidate"]["label"], "第三間")

        for message in ("推薦我五間飲料店", "鹽埕活動推薦我五個"):
            with self.subTest(message=message):
                result = resolve_message_reference("owner", "room", message)
                self.assertEqual(result["status"], "none")
                self.assertEqual(
                    recent_selected_projection("owner", "room")["label"],
                    "第三間",
                )

    def test_mixed_place_and_relationship_request_keeps_explicit_place_selection(self):
        replace_presented_candidates(
            "owner", "room",
            _cards("第一間", "第二間", "第三間", "第四間", "一等一咖啡茶飲"),
            origin_run_id="run-five",
        )
        result = resolve_message_reference(
            "owner", "room",
            "剛剛第五間的詳細資訊也給我，我考慮一下要不要喝，你覺得有人適合跟我一起去嗎？",
        )
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["candidate"]["label"], "一等一咖啡茶飲")

        follow_up = resolve_message_reference("owner", "room", "去幫我查他的營業資訊")
        self.assertEqual(follow_up["status"], "resolved")
        self.assertEqual(follow_up["candidate"]["label"], "一等一咖啡茶飲")

    def test_person_pronoun_without_place_attribute_does_not_bind_selected_place(self):
        replace_presented_candidates("owner", "room", _cards("一等一咖啡茶飲"))
        resolve_message_reference("owner", "room", "第一間的詳細資料")
        result = resolve_message_reference("owner", "room", "你覺得他適合跟我一起去嗎？")
        self.assertEqual(result["status"], "none")

    def test_activity_ordinal_does_not_select_a_place_candidate(self):
        replace_presented_candidates(
            "owner", "room", _cards("炸雞第一間", "豪食G炸雞"),
        )
        resolve_message_reference("owner", "room", "第二間的詳細資訊")
        before = recent_selected_projection("owner", "room")
        result = resolve_message_reference("owner", "room", "第一個活動推薦嘛")
        after = recent_selected_projection("owner", "room")
        self.assertEqual(result["status"], "none")
        self.assertEqual(after, before)

    def test_preplanner_resolution_does_not_commit_selection(self):
        replace_presented_candidates("owner", "room", _cards("第一間", "第二間"))
        result = resolve_message_reference(
            "owner", "room", "第二間的詳細資料", commit_selection=False,
        )
        self.assertEqual(result["status"], "resolved")
        self.assertIsNone(recent_selected_projection("owner", "room"))

    def test_commit_revalidates_resolution_and_persists_same_room_selection(self):
        replace_presented_candidates(
            "owner", "room", _cards("第一間", "豪食G炸雞"), origin_run_id="fried-run",
        )
        resolution = resolve_message_reference(
            "owner", "room", "第二間的詳細資訊", commit_selection=False,
        )
        committed = commit_resolved_selection("owner", "room", resolution)
        self.assertEqual(committed["status"], "committed")
        self.assertEqual(committed["selection"]["label"], "豪食G炸雞")
        self.assertEqual(recent_selected_projection("owner", "room")["label"], "豪食G炸雞")

        cross_room = commit_resolved_selection("owner", "other-room", resolution)
        self.assertEqual(cross_room["status"], "source_unavailable")
        mismatch = commit_resolved_selection(
            "owner", "room", {**resolution, "candidate": {
                **resolution["candidate"], "label": "另一家店",
            }},
        )
        self.assertEqual(mismatch["status"], "invalid_resolution")

    def test_commit_reports_storage_failure_without_changing_selection(self):
        replace_presented_candidates(
            "owner", "room", _cards("第一間", "豪食G炸雞"), origin_run_id="fried-run",
        )
        resolution = resolve_message_reference(
            "owner", "room", "第二間的詳細資訊", commit_selection=False,
        )
        with patch(
            "services.ayue_agent.v3.place_references._save_selected",
            return_value=False,
        ):
            result = commit_resolved_selection("owner", "room", resolution)
        self.assertEqual(result["status"], "storage_unavailable")
        self.assertIsNone(recent_selected_projection("owner", "room"))

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
