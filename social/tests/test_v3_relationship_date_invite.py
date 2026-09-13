import types
import unittest
from unittest.mock import MagicMock, patch

from services.ayue_agent.contracts import AgentTurnContext, PublicAgentTurnContext, ToolResult, TurnClockV1
from services.ayue_agent.public_relationship_projection import (
    ContactNameResolution,
    resolve_accepted_contact_name,
)
from services.ayue_agent.tool_registry import TOOL_REGISTRY, ToolRisk, planner_arguments_allowed
from services.ayue_agent.v3 import relationship_references
from services.ayue_agent.v3.contracts import Plan, SubTask, ToolProposal
from services.ayue_agent.v3.confirmation import ConfirmationManager
from services.ayue_agent.v3.context_slicer import slice_for_agent
from services.ayue_agent.v3.planner import PlannerMetrics
from services.ayue_agent.v3.scheduler import run_public_agent_turn_v3
from services.ayue_agent.v3.sub_agents.base import SubAgentMetrics
from services.ayue_agent.v3.synthesizer import SynthesizerMetrics
from services.ayue_agent.v3.test_store import MemoryCollection
from services.ayue_agent.v3.write_executors import execute_write, prepare_write_confirmation
from services.ai_service import ToolCallResult


class _Cursor(list):
    def limit(self, _size):
        return self


def _accepted_match(other_id="contact-1", revision=3, coordination=None):
    return {
        "_id": "match-1",
        "from_user": "owner",
        "to_user": other_id,
        "status": "accepted",
        "proposal_revision": revision,
        "date_coordination": coordination or {},
    }


def _turn(message="我想約小按出去", *, recent=None):
    return PublicAgentTurnContext(
        user_id="owner",
        room_id="room",
        message=message,
        recent_contact_reference=recent,
        clock=TurnClockV1(
            timezone="Asia/Taipei",
            utc_iso="2026-08-11T12:00:00+00:00",
            local_iso="2026-08-11T20:00:00+08:00",
            local_date="2026-08-11",
            local_time="20:00",
            weekday_zh_tw="星期二",
        ),
    )


class RelationshipResolverTests(unittest.TestCase):
    def test_unique_typo_resolves_only_inside_accepted_contacts(self):
        matches = _Cursor([{"from_user": "owner", "to_user": "contact-1"}])
        profiles = [{"user_id": "contact-1", "display_name": "小安"}]
        with patch("services.ayue_agent.public_relationship_projection.matches_coll.find", return_value=matches), \
             patch("services.ayue_agent.public_relationship_projection.profiles_coll.find", return_value=profiles):
            result = resolve_accepted_contact_name("owner", "小按")
        self.assertEqual(result.status, "resolved_phonetic")
        self.assertEqual(result.other_id, "contact-1")
        self.assertEqual(result.display_name, "小安")
        self.assertEqual(result.kind, "phonetic")

    def test_duplicate_public_names_are_ambiguous(self):
        matches = _Cursor([
            {"from_user": "owner", "to_user": "contact-1"},
            {"from_user": "contact-2", "to_user": "owner"},
        ])
        profiles = [
            {"user_id": "contact-1", "display_name": "小安"},
            {"user_id": "contact-2", "display_name": "小安"},
        ]
        with patch("services.ayue_agent.public_relationship_projection.matches_coll.find", return_value=matches), \
             patch("services.ayue_agent.public_relationship_projection.profiles_coll.find", return_value=profiles):
            result = resolve_accepted_contact_name("owner", "小安")
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(result.candidates, ("小安", "小安"))

    def test_unique_homophone_resolves_inside_accepted_contacts_only(self):
        matches = _Cursor([
            {"from_user": "owner", "to_user": "contact-1"},
            {"from_user": "owner", "to_user": "contact-2"},
            {"from_user": "owner", "to_user": "contact-3"},
        ])
        profiles = [
            {"user_id": "contact-1", "display_name": "小涵"},
            {"user_id": "contact-2", "display_name": "小葵"},
            {"user_id": "contact-3", "display_name": "小哲"},
        ]
        with patch("services.ayue_agent.public_relationship_projection.matches_coll.find", return_value=matches), \
             patch("services.ayue_agent.public_relationship_projection.profiles_coll.find", return_value=profiles):
            result = resolve_accepted_contact_name("owner", "小寒")
        self.assertEqual(result.status, "resolved_phonetic")
        self.assertEqual(result.other_id, "contact-1")
        self.assertEqual(result.display_name, "小涵")
        self.assertEqual(result.kind, "phonetic")

    def test_duplicate_homophones_fail_closed(self):
        matches = _Cursor([
            {"from_user": "owner", "to_user": "contact-1"},
            {"from_user": "owner", "to_user": "contact-2"},
        ])
        profiles = [
            {"user_id": "contact-1", "display_name": "小涵"},
            {"user_id": "contact-2", "display_name": "小函"},
        ]
        with patch("services.ayue_agent.public_relationship_projection.matches_coll.find", return_value=matches), \
             patch("services.ayue_agent.public_relationship_projection.profiles_coll.find", return_value=profiles):
            result = resolve_accepted_contact_name("owner", "小寒")
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(result.candidates, ("小涵", "小函"))


class RelationshipDateInviteTests(unittest.TestCase):
    def setUp(self):
        relationship_references.clear_runtime_state()

    def tearDown(self):
        relationship_references.clear_runtime_state()

    def test_registry_is_confirmed_write_and_rejects_authority_or_form_fields(self):
        spec = TOOL_REGISTRY["relationship.start_date_coordination"]
        self.assertEqual(spec.risk, ToolRisk.WRITE)
        self.assertTrue(spec.requires_confirmation)
        self.assertTrue(planner_arguments_allowed(spec, {
            "target_source": "name",
            "target_evidence_span": "小安",
        }))
        self.assertFalse(planner_arguments_allowed(spec, {
            "target_source": "name",
            "target_evidence_span": "小安",
            "other_id": "contact-1",
        }))
        self.assertFalse(planner_arguments_allowed(spec, {
            "target_source": "mention",
            "target_evidence_span": "小安",
        }))

    def test_fuzzy_name_creates_one_combined_confirmation_without_form_or_evidence(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="我想約小按週六去駁二")
        turn = types.SimpleNamespace(_mentioned_ids=[], mentioned_contact_overflow=False)
        resolved = ContactNameResolution(
            "resolved_fuzzy", other_id="contact-1", display_name="小安", kind="fuzzy",
        )
        with patch("services.ayue_agent.v3.write_executors.resolve_accepted_contact_name", return_value=resolved), \
             patch("services.date_coordination_service.find_accepted_match", return_value=_accepted_match()):
            payload, preview = prepare_write_confirmation(
                "relationship.start_date_coordination",
                {"target_source": "name", "target_evidence_span": "小按"},
                ctx,
                turn,
            )
        self.assertEqual(payload["arguments"], {})
        self.assertNotIn("target_evidence_span", payload["data"])
        self.assertNotIn("form", payload["data"])
        self.assertIn("我把「小按」理解成「小安」", preview)
        self.assertIn("要幫你和「小安」建立約會邀請卡嗎？", preview)
        self.assertIn("確認後才會送出", preview)
        self.assertNotIn("她", preview)

    def test_recent_pronoun_uses_planner_resolved_name_from_public_chat(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="我想約她")
        turn = types.SimpleNamespace(
            _mentioned_ids=[], mentioned_contact_overflow=False,
            recent_messages=[{"role": "assistant", "content": "可以考慮約小安一起去。"}],
        )
        resolved = ContactNameResolution(
            "resolved_exact", other_id="contact-1", display_name="小安", kind="exact",
        )
        with patch("services.ayue_agent.v3.write_executors.resolve_accepted_contact_name", return_value=resolved), \
             patch("services.date_coordination_service.find_accepted_match", return_value=_accepted_match()):
            payload, preview = prepare_write_confirmation(
                "relationship.start_date_coordination",
                {"target_source": "name", "target_evidence_span": "小安"},
                ctx,
                turn,
            )
        self.assertEqual(payload["data"]["other_id"], "contact-1")
        self.assertIn("要幫你和「小安」建立約會邀請卡嗎？", preview)
        self.assertIn("確認後才會送出", preview)

        missing, reply = prepare_write_confirmation(
            "relationship.start_date_coordination",
            {"target_source": "name", "target_evidence_span": "另一人"},
            ctx,
            turn,
        )
        self.assertIsNone(missing)
        self.assertIn("找不到", reply)

    def test_confirmation_executes_existing_domain_service_with_empty_card_input(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="確認")
        match = _accepted_match()
        with patch("services.date_coordination_service.find_accepted_match", return_value=match), \
             patch("services.date_coordination_service.create_invite", return_value={
                 "coordination_id": "coord-1", "status": "pending_partner", "form": {},
             }) as create_invite, \
             patch("services.ayue_agent.v3.write_executors._claim_once", return_value=True), \
             patch("services.ayue_agent.v3.write_executors._finish"):
            ok, reply, code = execute_write(
                "relationship.start_date_coordination",
                {},
                ctx,
                MagicMock(),
                "run-1",
                0,
                confirmation_id="confirmation-1",
                payload={
                    "other_id": "contact-1",
                    "match_id": "match-1",
                    "expected_match_revision": 3,
                    "safe_label": "小安",
                    "resolution_kind": "fuzzy",
                },
            )
        self.assertTrue(ok)
        self.assertIsNone(code)
        self.assertIn("空白約會邀請卡已建立", reply)
        self.assertIn("等待對方接受", reply)
        create_invite.assert_called_once_with(
            match,
            "owner",
            "contact-1",
            expected_match_revision=3,
        )

    def test_confirmation_is_stale_when_relation_revision_changes(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="確認")
        with patch("services.date_coordination_service.find_accepted_match", return_value=_accepted_match(revision=4)), \
             patch("services.date_coordination_service.create_invite") as create_invite:
            ok, _reply, code = execute_write(
                "relationship.start_date_coordination", {}, ctx, MagicMock(), "run-1", 0,
                confirmation_id="confirmation-1",
                payload={
                    "other_id": "contact-1", "match_id": "match-1",
                    "expected_match_revision": 3, "safe_label": "小安",
                },
            )
        self.assertFalse(ok)
        self.assertEqual(code, "stale_relationship")
        create_invite.assert_not_called()

    def test_existing_live_coordination_is_idempotent_without_new_card(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="確認")
        match = _accepted_match(coordination={"status": "pending_partner"})
        with patch("services.date_coordination_service.find_accepted_match", return_value=match), \
             patch("services.date_coordination_service.create_invite") as create_invite:
            ok, _reply, code = execute_write(
                "relationship.start_date_coordination", {}, ctx, MagicMock(), "run-1", 0,
                confirmation_id="confirmation-1",
                payload={
                    "other_id": "contact-1", "match_id": "match-1",
                    "expected_match_revision": 3, "safe_label": "小安",
                },
            )
        self.assertTrue(ok)
        self.assertEqual(code, "date_coordination_already_live")
        create_invite.assert_not_called()

    def test_short_lived_reference_is_owner_scoped_and_has_safe_projection(self):
        relationship_references.remember_contact("owner", "contact-1", "小安")
        with patch("services.ayue_agent.v3.relationship_references.matches_coll.find_one", return_value={"_id": "m1"}):
            record = relationship_references.get_reference("owner")
        self.assertEqual(record["other_id"], "contact-1")
        projection = relationship_references.public_projection(record)
        self.assertEqual(projection["display_name"], "小安")
        self.assertNotIn("other_id", projection)
        self.assertIsNone(relationship_references.get_reference("other-owner"))

    def test_legacy_recent_reference_is_hidden_from_agent_slices(self):
        turn = _turn(recent={"display_name": "小安", "expires_in_seconds": 600})
        relationship_slice = slice_for_agent("relationship", turn, prior_observations=[])
        calendar_slice = slice_for_agent("calendar", turn, prior_observations=[])
        self.assertNotIn("recent_contact_reference", relationship_slice.payload)
        self.assertNotIn("recent_contact_reference", calendar_slice.payload)


class RelationshipSchedulerTrajectoryTests(unittest.TestCase):
    def test_date_invitation_can_share_a_turn_with_places_read(self):
        ctx = AgentTurnContext(
            user_id="owner", room_id="room",
            message="幫我送個邀約給小安，再找附近冰店",
        )
        plan = Plan(write_intent="relationship.date_invitation.v1", tasks=[
            SubTask(id="r1", agent="relationship", depends_on=[], task_brief="建立空白邀請卡"),
            SubTask(
                id="p1", agent="places", place_mode="discover", depends_on=[],
                task_brief="找附近冰店",
            ),
            SubTask(
                id="s1", agent="synthesizer", depends_on=["r1", "p1"],
                task_brief="整合推薦與確認",
            ),
        ])
        turn = _turn(ctx.message)
        preview = "要建立空白約會邀請卡嗎？請選擇是否繼續。"
        combined = f"1. 冰店 A：走路約五分鐘。\n\n{preview}"
        seen_observations = []

        def fake_synthesize(slice_payload, candidate_cards=None, on_token=None):
            del candidate_cards, on_token
            seen_observations.extend(slice_payload.payload.get("observations") or [])
            return (
                combined, None, SynthesizerMetrics(
                    presentation_class="grounded_recommendation",
                    presentation_messages=[combined],
                ),
            )

        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, PlannerMetrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context", return_value=turn), \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "relationship": MagicMock(return_value=([
                     ToolProposal(
                         tool_name="relationship.start_date_coordination",
                         arguments={"target_source": "name", "target_evidence_span": "小安"},
                     ),
                 ], SubAgentMetrics())),
                 "places": MagicMock(return_value=([
                     ToolProposal(
                         tool_name="places.search_nearby",
                         arguments={"anchor": "高雄市鹽埕區", "categories": ["cafe"]},
                     ),
                 ], SubAgentMetrics())),
             }), \
             patch("services.ayue_agent.v3.scheduler.prepare_write_confirmation", return_value=(
                 {"action": "relationship.start_date_coordination", "arguments": {}, "data": {
                     "other_id": "contact-1", "safe_label": "小安",
                 }},
                 preview,
             )), \
             patch("services.ayue_agent.v3.scheduler.execute_tool", return_value=ToolResult(ok=True, data={
                 "anchor_label": "高雄市鹽埕區", "origin_kind": "explicit",
                 "distance_basis": "straight_line", "attribution": "Google Maps",
                 "attribution_url": "https://www.google.com/maps", "requested_categories": ["cafe"],
                 "requested_cuisine": "", "radius_m": 1500, "requested_limit": 3,
                 "ordering": "distance", "places": [{
                     "name": "冰店 A", "category": "cafe", "distance_m": 300,
                     "address_summary": "高雄市", "map_url": "https://www.google.com/maps/place/a",
                     "provider": "google", "place_id": "place-a",
                 }],
             })), \
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS.update_many"), \
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS.insert_one") as insert, \
             patch("services.ayue_agent.v3.synthesizer.synthesize", side_effect=fake_synthesize):
            result = run_public_agent_turn_v3(ctx)

        self.assertIn("冰店 A", result.reply)
        self.assertIn("約會邀請卡", result.reply)
        insert.assert_called_once()
        transaction_states = [
            observation["result"]["transaction_state"]
            for observation in seen_observations
            if isinstance(observation.get("result"), dict)
            and isinstance(observation["result"].get("transaction_state"), dict)
        ]
        self.assertEqual(transaction_states, [{
            "schema_version": "relationship_transaction_state.v1",
            "action": "create_date_invitation",
            "status": "pending_confirmation",
            "counterparty": "小安",
        }])

    def test_live_relationship_runtime_routes_typed_intent_to_single_write_tool(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="我想約小安出去")
        plan = Plan(write_intent="relationship.date_invitation.v1", tasks=[
            SubTask(id="r1", agent="relationship", depends_on=[], task_brief="建立空白邀請卡"),
            SubTask(id="s1", agent="synthesizer", depends_on=["r1"], task_brief="回覆結果"),
        ])
        turn = _turn(ctx.message)
        preview = "要建立空白約會邀請卡嗎？請回覆確認或取消。"
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, PlannerMetrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context", return_value=turn), \
             patch(
                 "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
                 return_value=ToolCallResult(content="", tool_calls=[{
                     "name": "relationship.start_date_coordination",
                     "arguments": {"target_source": "name", "target_evidence_span": "小安"},
                 }]),
             ) as provider, \
             patch("services.ayue_agent.v3.scheduler.prepare_write_confirmation", return_value=(
                 {"action": "relationship.start_date_coordination", "arguments": {}, "data": {"other_id": "contact-1"}},
                 preview,
             )), \
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS.update_many"), \
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS.insert_one") as insert, \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=(
                 "模型不應改寫 server preview。", None, SynthesizerMetrics(
                     presentation_class="conversation", fallback_reason="",
                 ),
             )):
            result = run_public_agent_turn_v3(ctx)
        self.assertTrue(result.handled)
        self.assertFalse(result.reply == "planner_invalid")
        self.assertEqual(provider.call_count, 1)
        visible = provider.call_args.args[1]
        self.assertEqual(
            [item["function"]["name"] for item in visible],
            ["relationship.start_date_coordination"],
        )
        insert.assert_called_once()

    def test_missing_invitee_accepts_one_natural_clarification_without_retry(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="幫我建約會卡")
        plan = Plan(write_intent="relationship.date_invitation.v1", tasks=[
            SubTask(id="r1", agent="relationship", depends_on=[], task_brief="建立約會卡，但對象尚未指定"),
            SubTask(id="s1", agent="synthesizer", depends_on=["r1"], task_brief="自然追問對象"),
        ])
        turn = _turn(ctx.message)
        clarification = "你想邀請哪一位聯絡人？"
        seen = {}

        def fake_synthesize(context_slice, **_kwargs):
            seen.update(context_slice.payload["observations"][0])
            return clarification, None, SynthesizerMetrics(
                presentation_class="conversation", presentation_messages=[clarification],
            )

        with patch(
            "services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, PlannerMetrics()),
        ), patch(
            "services.ayue_agent.v3.scheduler.build_public_agent_turn_context", return_value=turn,
        ), patch(
            "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
            return_value=ToolCallResult(content=clarification, tool_calls=[]),
        ) as provider, patch(
            "services.ayue_agent.v3.synthesizer.synthesize", side_effect=fake_synthesize,
        ), patch(
            "services.ayue_agent.v3.scheduler._CONFIRMATIONS.insert_one",
        ) as insert:
            result = run_public_agent_turn_v3(ctx)

        self.assertEqual(provider.call_count, 1)
        self.assertEqual(result.reply, clarification)
        self.assertEqual(seen["status"], "skipped")
        self.assertEqual(seen["skip_reason"], "needs_clarification")
        insert.assert_not_called()

    def test_two_write_protocol_failures_are_naturalized_without_confirmation(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="幫我約小安")
        plan = Plan(write_intent="relationship.date_invitation.v1", tasks=[
            SubTask(id="r1", agent="relationship", depends_on=[], task_brief="建立空白邀請卡"),
            SubTask(id="s1", agent="synthesizer", depends_on=["r1"], task_brief="回覆結果"),
        ])
        turn = _turn(ctx.message)
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, PlannerMetrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context", return_value=turn), \
             patch(
                 "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
                 side_effect=[
                     ToolCallResult(content="", tool_calls=[]),
                     ToolCallResult(content="仍然沒有正確呼叫", tool_calls=[]),
                 ],
             ) as provider, \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=(
                 "這次邀請還沒有準備好，你可以直接告訴我想邀請誰。", None, SynthesizerMetrics(
                     presentation_class="conversation", fallback_reason="",
                 ),
             )) as synth, \
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS.insert_one") as insert:
            result = run_public_agent_turn_v3(ctx)
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(result.reply, "這次邀請還沒有準備好，你可以直接告訴我想邀請誰。")
        observation = synth.call_args.args[0].payload["observations"][0]
        self.assertEqual(observation["status"], "failed")
        self.assertFalse(result.reply == "planner_invalid")
        insert.assert_not_called()
        synth.assert_called_once()

    def test_relationship_write_stays_on_normal_scheduler_confirmation_path(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="我想約小安出去")
        plan = Plan(write_intent="relationship.date_invitation.v1", tasks=[
            SubTask(id="r1", agent="relationship", depends_on=[], task_brief="建立空白邀請卡"),
            SubTask(id="s1", agent="synthesizer", depends_on=["r1"], task_brief="回覆結果"),
        ])
        turn = _turn(ctx.message)
        preview = "要建立空白約會邀請卡嗎？請回覆確認或取消。"
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, PlannerMetrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context", return_value=turn), \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "relationship": MagicMock(return_value=(
                     [ToolProposal(
                         tool_name="relationship.start_date_coordination",
                         arguments={"target_source": "name", "target_evidence_span": "小安"},
                     )],
                     SubAgentMetrics(),
                 )),
             }), \
             patch("services.ayue_agent.v3.scheduler.prepare_write_confirmation", return_value=(
                 {"action": "relationship.start_date_coordination", "arguments": {}, "data": {"other_id": "contact-1"}},
                 preview,
             )), \
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS.update_many"), \
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS.insert_one") as insert, \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=(
                "好，我把邀請確認放在下面。", None, SynthesizerMetrics(
                    presentation_class="transaction", fallback_reason="",
                    presentation_messages=["好，我把邀請確認放在下面。"],
                    interaction_blocks_v1=[
                        {"type": "text", "message_index": 0},
                        {"type": "confirmation", "slot": "primary_write_confirmation"},
                    ],
                ),
             )) as synth:
            result = run_public_agent_turn_v3(ctx)
        self.assertTrue(result.handled)
        self.assertEqual(result.reply, "好，我把邀請確認放在下面。")
        self.assertEqual(
            [block.get("type") for block in result.interaction_blocks_v1 or []],
            ["text", "confirmation"],
        )
        self.assertFalse(result.reply == "planner_invalid")
        insert.assert_called_once()
        synth.assert_called_once()

    def test_confirmed_date_write_uses_natural_model_copy_with_server_facts(self):
        collection = MemoryCollection()
        manager = ConfirmationManager(collection)
        choice_id = manager.create_confirmation(
            user_id="confirm-owner",
            agent_name="relationship",
            tool_name="relationship.start_date_coordination",
            arguments={},
            payload={
                "other_id": "contact-1",
                "match_id": "match-1",
                "expected_match_revision": 3,
                "safe_label": "小安",
            },
            origin_run_id="origin-run",
            preview="要建立空白約會邀請卡嗎？",
            room_id="room",
        )
        manager.bind_final_preview(
            user_id="confirm-owner", origin_run_id="origin-run",
            final_content="要建立空白約會邀請卡嗎？",
        )
        manager.mark_presented(
            user_id="confirm-owner", origin_run_id="origin-run",
            message_id="message-1",
            persisted_content="要建立空白約會邀請卡嗎？",
        )
        ctx = AgentTurnContext(
            user_id="confirm-owner",
            room_id="room",
            message="",
            user_profile={"user_id": "confirm-owner"},
            choice_id=choice_id,
            choice_action="confirm",
        )
        turn = _turn(ctx.message).model_copy(update={"user_id": "confirm-owner"})
        with patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS", collection), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context", return_value=turn), \
             patch("services.ayue_agent.v3.scheduler.active_guidance_offer", return_value={"fingerprint": "match-offer"}), \
             patch("services.ayue_agent.v3.scheduler.accept_guidance_offer") as accept_offer, \
             patch("services.ayue_agent.v3.scheduler.execute_write", return_value=(
                 True, "已在你和「小安」的聊天室建立空白約會邀請卡；等待對方接受。", None,
             )), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=(
                 "約會邀請卡已經建好，現在等小安回覆。", None, SynthesizerMetrics(
                     presentation_class="transaction", fallback_reason="",
                 ),
             )):
            result = run_public_agent_turn_v3(ctx)
        expected_reply = "約會邀請卡已經建好，現在等小安回覆。"
        self.assertEqual(result.reply, expected_reply)
        accept_offer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
