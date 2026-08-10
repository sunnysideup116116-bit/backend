import unittest
from unittest.mock import patch

from services.ayue_agent.contracts import AgentTurnContext, ToolCall
from services.ayue_agent.tool_registry import (
    READ_ONLY_TOOLS,
    TOOL_REGISTRY,
    ToolRisk,
    executor_arguments_for_turn,
    tool_call_key,
)
from services.ayue_agent.tools import execute_tool


class AyueAgentRegistryTests(unittest.TestCase):
    def test_each_registered_read_tool_has_a_runtime_executor_key(self):
        executor_keys = {
            "calendar_events", "calendar_event_find", "calendar_next_event", "current_time", "match_status",
            "counterparty_summary", "recent_context", "relationship_evidence", "mentioned_contact_summary", "accepted_contact_list", "memory_profile",
            "self_profile", "calendar_mutation_verification",
        }
        for tool_name in READ_ONLY_TOOLS:
            spec = TOOL_REGISTRY[tool_name]
            self.assertEqual(spec.risk, ToolRisk.READ)
            self.assertIn(spec.executor_key, executor_keys)
            self.assertIsNotNone(spec.executor_arguments_model)
            self.assertIsNotNone(spec.output_model)

    def test_write_tool_cannot_bypass_runtime_executor(self):
        result = execute_tool(
            ToolCall(name="match.start_search"),
            AgentTurnContext(user_id="owner", room_id="room", message="幫我找人"),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "tool_not_allowed")

    def test_duplicate_key_uses_executor_owned_arguments_not_planner_noise(self):
        spec = TOOL_REGISTRY["system.get_current_time"]
        arguments = executor_arguments_for_turn(spec, [])
        self.assertEqual(arguments, {})
        self.assertEqual(tool_call_key(spec, arguments), tool_call_key(spec, arguments))

    def test_proposal_decision_keeps_only_the_typed_planner_enum(self):
        spec = TOOL_REGISTRY["match.decide_active_proposal"]
        arguments = executor_arguments_for_turn(spec, [], {"decision": "interested"})
        self.assertEqual(arguments, {"decision": "interested"})
        with self.assertRaises(Exception):
            executor_arguments_for_turn(spec, [], {"decision": "interested", "proposal_id": "nope"})

    def test_calendar_find_accepts_an_omitted_or_null_date_hint(self):
        spec = TOOL_REGISTRY["calendar.find_my_event"]
        self.assertEqual(
            executor_arguments_for_turn(spec, [], {"event_hint": "看電影"}),
            {"event_hint": "看電影", "date_hint": None, "companion_hint": None, "limit": None},
        )
        self.assertEqual(
            executor_arguments_for_turn(spec, [], {"event_hint": "看電影", "date_hint": None}),
            {"event_hint": "看電影", "date_hint": None, "companion_hint": None, "limit": None},
        )

    def test_calendar_find_accepts_limit_and_candidates_cap_at_ten(self):
        spec = TOOL_REGISTRY["calendar.find_my_event"]
        self.assertEqual(
            executor_arguments_for_turn(spec, [], {"event_hint": "看電影", "limit": 20}),
            {"event_hint": "看電影", "date_hint": None, "companion_hint": None, "limit": 20},
        )
        output_schema = spec.output_model.model_json_schema()
        candidates = output_schema["properties"]["candidates"]
        self.assertEqual(candidates["maxItems"], 10)
        ref = candidates["items"]["$ref"].split("/")[-1]
        candidate_props = output_schema["$defs"][ref]["properties"]
        self.assertIn("location", candidate_props)
        self.assertIn("notes", candidate_props)
        self.assertIn("event_kind", candidate_props)

    def test_calendar_find_output_accepts_found_shape_with_new_fields(self):
        # found 分支的 data 含 location/notes/event_kind（_calendar_event_fields 展開），
        # 必須通過 output_model 驗證，否則 execute_tool 回 invalid_tool_output。
        spec = TOOL_REGISTRY["calendar.find_my_event"]
        data = {
            "status": "found", "reason_code": "",
            "activity": "看醫生", "date": "2026-08-09",
            "start_time": "08:30", "end_time": "12:05",
            "location": "台大醫院", "notes": "回診", "event_kind": "personal",
            "companion_known": False, "companion_display_name": "對方",
            "companion_safe_summary": "", "candidates": [],
        }
        spec.output_model.model_validate(data)

    def test_calendar_find_output_accepts_not_found_shape_with_query(self):
        # not_found 分支的 data 含 query（原始 hint），必須通過驗證。
        spec = TOOL_REGISTRY["calendar.find_my_event"]
        data = {
            "status": "not_found", "reason_code": "event_not_found",
            "activity": "", "date": "", "start_time": "", "end_time": "",
            "location": "", "notes": "", "event_kind": "",
            "companion_known": False, "companion_display_name": "對方",
            "companion_safe_summary": "", "query": "不能吃東西", "candidates": [],
        }
        spec.output_model.model_validate(data)

    def test_calendar_find_output_accepts_ambiguous_shape_with_candidates(self):
        spec = TOOL_REGISTRY["calendar.find_my_event"]
        data = {
            "status": "ambiguous", "reason_code": "event_ambiguous",
            "activity": "", "date": "", "start_time": "", "end_time": "",
            "location": "", "notes": "", "event_kind": "",
            "companion_known": False, "companion_display_name": "對方",
            "companion_safe_summary": "", "query": "",
            "candidates": [
                {"activity": "吃牛排", "date": "2026-08-12", "start_time": "18:00",
                 "end_time": "20:00", "location": "", "notes": "", "event_kind": "personal"},
            ],
        }
        spec.output_model.model_validate(data)

    def test_calendar_find_execute_tool_never_returns_invalid_tool_output(self):
        # 端到端：find_my_event 的 found / not_found / ambiguous 三種回傳
        # 都不能被 output schema 擋成 invalid_tool_output。
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="查行程")
        with patch("services.ayue_agent.tools.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.tools.find_owned_events", return_value=[
                 {
                     "event_id": "e1", "source_type": "personal", "title": "看醫生",
                     "activity": "看醫生", "location": "台大醫院", "notes": "回診",
                     "start_at": __import__("datetime").datetime(2026, 8, 9, 0, 30,
                         tzinfo=__import__("datetime").timezone.utc),
                     "end_at": __import__("datetime").datetime(2026, 8, 9, 4, 5,
                         tzinfo=__import__("datetime").timezone.utc),
                     "timezone": "Asia/Taipei",
                 },
             ]):
            result = execute_tool(
                ToolCall(name="calendar.find_my_event", arguments={"event_hint": "看醫生"}),
                ctx,
            )
        self.assertTrue(result.ok)
        self.assertNotEqual(result.error_code, "invalid_tool_output")
        self.assertEqual(result.data["status"], "found")
        self.assertEqual(result.data["location"], "台大醫院")

        with patch("services.ayue_agent.tools.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.tools.find_owned_events", return_value=[]):
            result = execute_tool(
                ToolCall(name="calendar.find_my_event", arguments={"event_hint": "不能吃東西"}),
                ctx,
            )
        self.assertTrue(result.ok)
        self.assertNotEqual(result.error_code, "invalid_tool_output")
        self.assertEqual(result.data["status"], "not_found")
        self.assertEqual(result.data["query"], "不能吃東西")


    def test_calendar_next_event_projects_serialized_domain_event(self):
        from datetime import datetime, timezone

        ctx = AgentTurnContext(user_id="owner", room_id="room", message="最近有啥行程")
        event = {
            "event_id": "e-next", "source_type": "personal", "participants": ["owner"],
            "title": "睡覺", "activity": "睡覺", "status": "confirmed", "revision": 2,
            "start_at": "2026-08-08T09:00:00+00:00",
            "end_at": "2026-08-08T11:00:00+00:00",
            "timezone": "Asia/Taipei", "location": "", "notes": "",
        }
        with patch("services.ayue_agent.tools.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.tools.get_next_event", return_value=event):
            result = execute_tool(ToolCall(name="calendar.get_next_my_event"), ctx)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["status"], "found")
        self.assertEqual(result.data["event"]["activity"], "睡覺")
        self.assertEqual(result.private_data["calendar_event_reference"]["event"]["event_id"], "e-next")

    def test_places_enrichments_are_bounded_and_deduplicated(self):
        spec = TOOL_REGISTRY["places.search_nearby"]
        arguments = executor_arguments_for_turn(spec, [], {
            "categories": ["restaurant"],
            "enrichments": ["hours", "rating", "hours"],
        })
        self.assertEqual(arguments["enrichments"], ["hours", "rating"])
        with self.assertRaises(Exception):
            executor_arguments_for_turn(spec, [], {
                "categories": ["restaurant"], "enrichments": ["reviews"],
            })

    def test_places_output_accepts_optional_rating_hours_and_walking_fields(self):
        spec = TOOL_REGISTRY["places.search_nearby"]
        spec.output_model.model_validate({
            "anchor_label": "Anchor", "origin_kind": "explicit",
            "distance_basis": "straight_line", "attribution": "Google Maps",
            "attribution_url": "https://www.google.com/maps",
            "places": [{
                "name": "Place", "category": "restaurant", "distance_m": 100,
                "map_url": "https://www.google.com/maps/place/x",
                "provider": "google", "place_id": "ChIJabc",
                "rating": 4.2, "user_rating_count": 42,
                "opening_hours": {
                    "open_now": True, "next_open_time": None,
                    "next_close_time": "2026-08-11T21:00:00Z",
                    "weekday_descriptions": ["Mon: 09:00–21:00"],
                },
                "walking_distance_m": 500,
                "walking_duration_seconds": 360,
            }],
        })

    def test_distance_tool_defaults_to_drive_and_accepts_walk(self):
        spec = TOOL_REGISTRY["places.measure_distance"]
        self.assertEqual(
            executor_arguments_for_turn(spec, [], {"destination": "Destination"})["travel_mode"],
            "DRIVE",
        )
        self.assertEqual(
            executor_arguments_for_turn(spec, [], {"destination": "Destination", "travel_mode": "WALK"})["travel_mode"],
            "WALK",
        )


if __name__ == "__main__":
    unittest.main()
