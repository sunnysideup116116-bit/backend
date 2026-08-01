import json
import os
import inspect
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from bson.objectid import ObjectId

from services.language_service import normalize_zh_tw
from services.memory_service import memory_summary
from services.profile_skills import (
    _active_recent_episode,
    _compose_recent_context_summary,
    apply_recent_context,
    analyze_profile_message,
    analyze_recent_context,
    memory_candidate_allowed,
    process_profile_message,
    profile_skills_mode_for_user,
    safe_recent_context,
)
from services.skill_loader import load_profile_skill
import services.profile_projection as profile_projection
import services.profile_skills as profile_skills


def router_payload(activity=None, memories=None, *, recent=True, confidence=0.95, evidence_span="我想去日本", destination=None, timing="近期", companion="solo", temporal_status="planned"):
    fields = {}
    for name, value in (("activity", activity), ("destination", destination), ("timing", timing), ("companion_intent", companion), ("temporal_status", temporal_status)) if recent else ():
        if value:
            fields[name] = {"operation": "set", "value": value, "evidence_span": evidence_span,
                            "confidence": confidence, "subject": "owner"}
    memories = [{"subject": "owner", "evidence_span": item.get("evidence_span", evidence_span), **item} for item in (memories or [])]
    return json.dumps({"recent_context": {"action": "update" if recent else "none", "message_kind": "real_world_update" if recent else "other", "confidence": confidence,
                       "fields": fields, "reason_code": "test"}, "memories": memories})


def patch_payload(fields, *, recent=True, confidence=0.95, kind="real_world_update", episode_relation="new"):
    for value in fields.values():
        if isinstance(value, dict):
            value.setdefault("confidence", confidence)
            value.setdefault("subject", "owner")
    return json.dumps({"recent_context": {"message_kind": kind, "action": "update" if recent else "none",
                       "confidence": confidence, "fields": fields, "episode_relation": episode_relation,
                       "reason_code": "test"}, "memories": []})


class ProfileSkillsTests(unittest.TestCase):
    def test_short_activity_phrase_is_written_only_when_the_extractor_proposes_it(self):
        payload = patch_payload({
            "activity": {"operation": "set", "value": "看煙火", "evidence_span": "看煙火"},
        })
        with patch("services.profile_skills.generate_chat_completion", return_value=payload):
            decision = analyze_profile_message("看煙火", "", plan_id="message:1")
        self.assertTrue(decision["recent_context"]["should_update"])
        self.assertEqual(decision["recent_context"]["fields"]["activity"]["value"], "看煙火")
        self.assertEqual(decision["recent_context"]["plan_id"], "message:1")

    def test_valid_recent_field_survives_an_invalid_sibling_field(self):
        payload = patch_payload({
            "activity": {"operation": "set", "value": "衝浪", "evidence_span": "衝浪"},
            "destination": {"operation": "set", "value": "杜撰地點", "evidence_span": "不存在的證據"},
        })
        with patch("services.profile_skills.generate_chat_completion", return_value=payload):
            decision = analyze_profile_message("我最近很想去衝浪", "")
        recent = decision["recent_context"]
        self.assertTrue(recent["should_update"])
        self.assertEqual(recent["fields"]["activity"]["value"], "衝浪")
        self.assertNotIn("destination", recent["fields"])
        self.assertEqual(recent["reason_code"], "partial_fields_rejected")

    def test_empty_real_world_update_gets_one_narrow_retry(self):
        first = patch_payload({}, recent=True)
        retry = patch_payload({
            "activity": {"operation": "set", "value": "衝浪", "evidence_span": "衝浪"},
        })
        with patch("services.profile_skills.generate_chat_completion", side_effect=[first, retry]) as model:
            decision = analyze_profile_message("我最近很想去衝浪", "")
        self.assertEqual(model.call_count, 2)
        self.assertTrue(decision["recent_context"]["should_update"])
        self.assertEqual(decision["recent_context"]["reason_code"], "retry_accepted")
    def setUp(self):
        self.old_mode = os.environ.get("AYUE_PROFILE_SKILLS_MODE")
        self.old_allowlist = os.environ.get("AYUE_PROFILE_SKILLS_USER_ALLOWLIST")

    def tearDown(self):
        for key, value in (("AYUE_PROFILE_SKILLS_MODE", self.old_mode), ("AYUE_PROFILE_SKILLS_USER_ALLOWLIST", self.old_allowlist)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_recent_context_uses_only_safe_owner_summary(self):
        with patch("services.profile_skills.generate_chat_completion", return_value=router_payload("去日本旅行")) as model:
            proposal = analyze_recent_context("我想去日本", "最近看電影")
        self.assertTrue(proposal["should_update"])
        self.assertEqual(proposal["summary_zh_tw"], "近期想去日本旅行，想自己去")
        prompt = model.call_args.args[0]
        self.assertIn("我想去日本", prompt)
        self.assertNotIn("最近看電影", prompt)
        self.assertNotIn("seed_user", prompt)

    def test_system_state_and_user_ids_are_rejected(self):
        with patch("services.profile_skills.generate_chat_completion", return_value=router_payload("等待seed_user_10回覆")):
            decision = analyze_profile_message("我想去日本", "")
        self.assertFalse(decision["recent_context"]["should_update"])
        self.assertEqual(decision["recent_context"]["summary_zh_tw"], "")
        self.assertEqual(analyze_profile_message("我跟seed_user_10有約", "")["memory_codes"], ["blocked_input"])

    def test_profile_evidence_must_be_an_owner_substring(self):
        payload = router_payload("去日本旅行", [], evidence_span="模型杜撰的證據")
        with patch("services.profile_skills.generate_chat_completion", return_value=payload):
            decision = analyze_profile_message("我想去日本", "")
        self.assertFalse(decision["recent_context"]["should_update"])
        self.assertEqual(decision["recent_context"]["reason_code"], "invalid_evidence_span")

    def test_match_outcome_reaction_is_classified_by_extractor_not_keyword_router(self):
        with patch("services.profile_skills.generate_chat_completion", return_value=router_payload(recent=False)) as model:
            decision = analyze_profile_message("為什麼!!!", "近期規劃去日本旅行")
        model.assert_called_once()
        self.assertFalse(decision["recent_context"]["should_update"])
        self.assertEqual(decision["memories"], [])
    def test_match_operations_are_classified_by_extractor_not_keyword_router(self):
        for message in ("幫我找人", "幫我找個人一起去吧", "等待對方回覆", "近期規劃瞭解配對物件"):
            with patch("services.profile_skills.generate_chat_completion", return_value=router_payload(recent=False)) as model:
                decision = analyze_profile_message(message, "")
            model.assert_called_once()
            self.assertFalse(decision["recent_context"]["should_update"])
    def test_explicit_smoking_preference_becomes_memory_only(self):
        payload = router_payload(None, [{"key": "smoking_partner", "label_zh_tw": "會抽菸的對象", "stance": "avoid", "category": "lifestyle", "confidence": 0.95}], recent=False, confidence=0.0, evidence_span="我不喜歡抽菸的人")
        with patch("services.profile_skills.generate_chat_completion", return_value=payload):
            decision = analyze_profile_message("我不喜歡抽菸的人", "")
        self.assertFalse(decision["recent_context"]["should_update"])
        self.assertEqual(decision["memories"][0]["key"], "smoking_partner")
        self.assertEqual(decision["memory_codes"], ["accepted"])

    def test_mixed_message_splits_context_and_memory(self):
        payload = router_payload("去日本旅行", [{"key": "smoking_partner", "label_zh_tw": "會抽菸的對象", "stance": "avoid", "category": "lifestyle", "confidence": 0.95}], evidence_span="我最近想去日本，而且不喜歡抽菸的人")
        with patch("services.profile_skills.generate_chat_completion", return_value=payload):
            decision = analyze_profile_message("我最近想去日本，而且不喜歡抽菸的人", "")
        self.assertTrue(decision["recent_context"]["should_update"])
        self.assertNotIn("抽菸", decision["recent_context"]["summary_zh_tw"])
        self.assertIn("抽菸", decision["memories"][0]["label"])

    def test_travel_placeholder_becomes_traditional_chinese_context(self):
        payload = router_payload("去美國旅行", [], recent=True, evidence_span="我最近想去美國玩", destination="美國")
        with patch("services.profile_skills.generate_chat_completion", return_value=payload):
            decision = analyze_profile_message("我最近想去美國玩", "")
        self.assertTrue(decision["recent_context"]["should_update"])
        self.assertEqual(decision["recent_context"]["summary_zh_tw"], "近期想去美國旅行，想自己去")

    def test_invalid_model_evidence_fails_closed_without_regex_recovery(self):
        payload = router_payload("travel", [], recent=True, confidence=0.95, evidence_span="模型杜撰")
        with patch("services.profile_skills.generate_chat_completion", return_value=payload):
            decision = analyze_profile_message("我最近想去非洲旅行", "")
        recent = decision["recent_context"]
        self.assertFalse(recent["should_update"])

    def test_recent_context_patch_keeps_destination_separate_from_timing(self):
        first = patch_payload({
            "activity": {"operation": "set", "value": "旅行", "evidence_span": "去日本合掌村玩"},
            "destination": {"operation": "set", "value": "日本合掌村", "evidence_span": "日本合掌村"},
            "temporal_status": {"operation": "set", "value": "planned", "evidence_span": "想"},
        })
        with patch("services.profile_skills.generate_chat_completion", return_value=first):
            decision = analyze_recent_context("我想去日本合掌村玩")
        self.assertEqual(decision["fields"]["destination"]["value"], "日本合掌村")
        self.assertEqual(decision["summary_zh_tw"], "近期規劃前往日本合掌村旅行")
        second = patch_payload({
            "timing": {"operation": "set", "value": "近期", "evidence_span": "最近"},
        })
        with patch("services.profile_skills.generate_chat_completion", return_value=second):
            timing = analyze_recent_context("最近就會出發吧")
        self.assertEqual(set(timing["fields"]), {"timing"})
        self.assertEqual(timing["fields"]["timing"]["evidence_span"], "最近")

    def test_recent_context_llm_composer_avoids_duplicate_destination_activity(self):
        fields = {
            "activity": {"value": "去當地市集"},
            "destination": {"value": "當地市集"},
            "timing": {"value": "近期"},
            "temporal_status": {"value": "planned"},
        }
        with patch(
            "services.profile_skills.generate_chat_completion",
            return_value="近期想去當地市集逛逛",
        ):
            summary = _compose_recent_context_summary(fields)
        self.assertEqual(summary, "近期想去當地市集")
        self.assertNotIn("市集去當地市集", summary)

    def test_legacy_duplicate_recent_context_is_repaired_on_read(self):
        self.assertEqual(
            safe_recent_context("近期規劃前往當地市集去當地市集"),
            "近期想去當地市集",
        )

    def test_legacy_activity_without_tense_is_neutral_on_read(self):
        self.assertEqual(
            safe_recent_context("近期規劃游泳 吃東西"),
            "近期活動：游泳、吃東西",
        )

    def test_completed_activity_is_not_rendered_as_a_future_plan(self):
        payload = patch_payload({
            "activity": {"operation": "set", "value": "游泳 吃東西", "evidence_span": "游泳"},
            "timing": {"operation": "set", "value": "昨天", "evidence_span": "昨天"},
            "temporal_status": {"operation": "set", "value": "past", "evidence_span": "昨天"},
        })
        with patch("services.profile_skills.generate_chat_completion", return_value=payload):
            decision = analyze_recent_context("我昨天有去游泳，也吃了東西")
        self.assertTrue(decision["should_update"])
        self.assertEqual(decision["summary_zh_tw"], "昨天有在游泳、吃東西")
        self.assertNotIn("規劃", decision["summary_zh_tw"])

    def test_mixed_travel_and_match_command_keeps_only_real_activity_patch(self):
        payload = patch_payload({
            "activity": {"operation": "set", "value": "旅行", "evidence_span": "去合掌村玩"},
            "destination": {"operation": "set", "value": "合掌村", "evidence_span": "合掌村"},
            "temporal_status": {"operation": "set", "value": "planned", "evidence_span": "想"},
        })
        with patch("services.profile_skills.generate_chat_completion", return_value=payload):
            decision = analyze_recent_context("我想去合掌村玩，也幫我找人一起去")
        self.assertTrue(decision["should_update"])
        self.assertNotIn("companion_intent", decision["fields"])
        self.assertEqual(decision["summary_zh_tw"], "近期規劃前往合掌村旅行")

    def test_protected_attribute_is_never_stored_or_contextualized(self):
        decision = analyze_profile_message("我最近想去日本，但我不喜歡黑人", "")
        self.assertFalse(decision["recent_context"]["should_update"])
        self.assertEqual(decision["memories"], [])
        self.assertEqual(decision["memory_codes"], ["protected_attribute"])

    def test_memory_preflight_is_only_a_safety_check(self):
        self.assertTrue(memory_candidate_allowed("我很重視準時，也喜歡安靜的咖啡廳"))
        self.assertTrue(memory_candidate_allowed("他 8 月 1 日不行"))
        self.assertFalse(memory_candidate_allowed("我今天和 seed_user_10 有約"))
        self.assertTrue(memory_candidate_allowed("我想去日本"))

    def test_time_words_and_mixed_search_are_semantic_extraction_inputs(self):
        fields = {
            "activity": {"operation": "set", "value": "爬山", "evidence_span": "想去爬山"},
            "timing": {"operation": "set", "value": "下週", "evidence_span": "下週"},
        }
        with patch("services.profile_skills.generate_chat_completion", return_value=patch_payload(fields)):
            decision = analyze_profile_message("我下週想去爬山，也幫我找人一起去", "")
        self.assertTrue(decision["recent_context"]["should_update"])
        self.assertEqual(decision["recent_context"]["fields"]["timing"]["value"], "下週")
        self.assertNotIn("companion_intent", decision["recent_context"]["fields"])

    def test_third_party_reference_can_yield_only_owner_preference(self):
        payload = router_payload([], [{"key": "outgoing_personality", "label_zh_tw": "偏好外向類型", "stance": "like", "category": "personality", "confidence": 0.92, "evidence_span": "他很外向，我喜歡這種類型"}], recent=False)
        with patch("services.profile_skills.generate_chat_completion", return_value=payload):
            decision = analyze_profile_message("他很外向，我喜歡這種類型", "")
        self.assertFalse(decision["recent_context"]["should_update"])
        self.assertIn("外向", decision["memories"][0]["label"])

    def test_explicit_no_store_blocks_before_the_model(self):
        with patch("services.profile_skills.generate_chat_completion") as model:
            decision = analyze_profile_message("我下週想去爬山，不要記", "")
        model.assert_not_called()
        self.assertEqual(decision["recent_context"]["reason_code"], "blocked_input")

    def test_contract_does_not_infer_missing_owner_attribution(self):
        payload = json.loads(router_payload("爬山", evidence_span="我想去爬山"))
        del payload["recent_context"]["fields"]["activity"]["subject"]
        with patch("services.profile_skills.generate_chat_completion", return_value=json.dumps(payload)):
            decision = analyze_profile_message("我想去爬山", "")
        self.assertFalse(decision["recent_context"]["should_update"])
        self.assertEqual(decision["contract"], {})

    def test_low_confidence_memory_fails_closed(self):
        payload = router_payload(None, [{
            "key": "quiet_cafe", "label_zh_tw": "安靜咖啡廳", "stance": "like",
            "category": "lifestyle", "confidence": 0.89, "evidence_span": "我喜歡安靜咖啡廳",
        }], recent=False, confidence=0.0)
        with patch("services.profile_skills.generate_chat_completion", return_value=payload):
            decision = analyze_profile_message("我喜歡安靜咖啡廳", "")
        self.assertEqual(decision["memories"], [])
        self.assertEqual(decision["memory_codes"], ["low_confidence"])

    def test_public_display_name_mention_is_not_treated_as_internal_id(self):
        payload = patch_payload({
            "activity": {"operation": "set", "value": "去日本旅行", "evidence_span": "去日本"},
            "destination": {"operation": "set", "value": "日本", "evidence_span": "日本"},
        })
        with patch("services.profile_skills.generate_chat_completion", return_value=payload) as model:
            decision = analyze_profile_message("我想跟@小安去日本", "")
        model.assert_called_once()
        self.assertTrue(decision["recent_context"]["should_update"])

    def test_summary_composer_rejects_unverified_additions(self):
        fields = {"activity": {"value": "去市集"}, "timing": {"value": "近期"}}
        with patch("services.profile_skills.generate_chat_completion", return_value="近期想去市集和帥哥約會"):
            summary = _compose_recent_context_summary(fields)
        self.assertEqual(summary, "近期活動：去市集")

    def test_new_activity_does_not_inherit_old_plan_fields(self):
        profile = {
            "current_context": "下週想去日本旅行，想找人同行",
            "current_context_revision": 4,
            "recent_context_state": {"version": 2, "revision": 4, "fields": {
                "destination": {"value": "日本", "plan_id": "message:old", "source_timestamp": 1},
                "timing": {"value": "下週", "plan_id": "message:old", "source_timestamp": 1},
                "companion_intent": {"value": "找人同行", "plan_id": "message:old", "source_timestamp": 1},
            }},
        }
        proposal = {
            "should_update": True, "message_kind": "real_world_update", "context_action": "update",
            "plan_id": "message:new", "fields": {
                "activity": {"operation": "set", "value": "逛市集", "evidence_span": "逛市集"},
            },
        }
        with patch("services.profile_skills.profiles_coll.find_one", return_value=profile), \
             patch("services.profile_skills.profiles_coll.update_one", return_value=SimpleNamespace(modified_count=1)) as update, \
             patch("services.profile_skills.generate_chat_completion", side_effect=RuntimeError), \
             patch("services.profile_skills.get_embedding", return_value=[]):
            self.assertTrue(apply_recent_context("owner", proposal, message_id="new", source_timestamp=2))
        fields = update.call_args.args[1]["$set"]["recent_context_state"]["fields"]
        self.assertEqual(set(fields), {"activity"})

    def test_short_followup_merges_into_the_same_typed_episode(self):
        now = time.time()
        profile = {
            "current_context": "近期想去旅行",
            "current_context_revision": 1,
            "recent_context_updated_at": now - 5,
            "recent_context_state": {"version": 4, "revision": 1, "episode_id": "episode:first", "updated_at": now - 5, "fields": {
                "activity": {"value": "旅行", "plan_id": "episode:first", "source_timestamp": 1},
                "temporal_status": {"value": "planned", "plan_id": "episode:first", "source_timestamp": 1},
            }},
        }
        proposal = {
            "should_update": True, "message_kind": "real_world_update", "context_action": "update",
            "episode_relation": "continue", "active_episode_id": "episode:first",
            "fields": {"destination": {"operation": "set", "value": "合掌村", "evidence_span": "合掌村"}},
        }
        with patch("services.profile_skills.profiles_coll.find_one", return_value=profile), \
             patch("services.profile_skills.profiles_coll.update_one", return_value=SimpleNamespace(modified_count=1)) as update, \
             patch("services.profile_skills.generate_chat_completion", side_effect=RuntimeError), \
             patch("services.profile_skills.get_embedding", return_value=[]):
            self.assertTrue(apply_recent_context("owner", proposal, message_id="second", source_timestamp=2))
        state = update.call_args.args[1]["$set"]["recent_context_state"]
        self.assertEqual(state["episode_id"], "episode:first")
        self.assertEqual(state["fields"]["activity"]["value"], "旅行")
        self.assertEqual(state["fields"]["destination"]["value"], "合掌村")
        self.assertEqual(state["fields"]["destination"]["evidence_message_id"], "second")

    def test_extractor_uses_only_bounded_typed_episode_for_continuity(self):
        payload = patch_payload({
            "destination": {"operation": "set", "value": "合掌村", "evidence_span": "合掌村"},
        }, episode_relation="continue")
        active = {"episode_id": "episode:first", "fields": {"activity": "旅行", "temporal_status": "planned"}}
        with patch("services.profile_skills.generate_chat_completion", return_value=payload) as model:
            decision = analyze_profile_message("合掌村", active_episode=active)
        prompt = model.call_args.args[0]
        self.assertIn('"activity": "旅行"', prompt)
        self.assertNotIn("evidence_message_id", prompt)
        self.assertNotIn("episode:first", prompt)
        self.assertEqual(decision["recent_context"]["episode_relation"], "continue")
        self.assertEqual(decision["recent_context"]["active_episode_id"], "episode:first")

    def test_recent_context_followup_draft_can_start_a_typed_episode(self):
        episode = _active_recent_episode({
            "recent_context_draft": {
                "goal": "activity_or_destination", "created_at": 100.0,
            },
        }, now=110.0)
        self.assertEqual(episode["episode_id"], "draft:100000")
        self.assertEqual(episode["fields"], {})
        self.assertEqual(episode["goal"], "activity_or_destination")

    def test_continuation_fails_closed_after_episode_expires(self):
        profile = {
            "current_context": "近期想去旅行", "current_context_revision": 1,
            "recent_context_updated_at": 1,
            "recent_context_state": {"version": 4, "revision": 1, "episode_id": "episode:first", "updated_at": 1, "fields": {
                "activity": {"value": "旅行", "plan_id": "episode:first", "source_timestamp": 1},
            }},
        }
        proposal = {
            "should_update": True, "message_kind": "real_world_update", "context_action": "update",
            "episode_relation": "continue", "active_episode_id": "episode:first",
            "fields": {"timing": {"operation": "set", "value": "下週", "evidence_span": "下週"}},
        }
        with patch("services.profile_skills.profiles_coll.find_one", return_value=profile), \
             patch("services.profile_skills.profiles_coll.update_one") as update:
            self.assertFalse(apply_recent_context("owner", proposal, message_id="late", source_timestamp=2))
        update.assert_not_called()

    def test_clear_action_clears_the_whole_recent_context(self):
        profile = {
            "current_context": "近期規劃爬山", "current_context_revision": 2,
            "recent_context_state": {"version": 2, "revision": 2, "fields": {
                "activity": {"value": "爬山", "plan_id": "message:old", "source_timestamp": 1},
                "timing": {"value": "近期", "plan_id": "message:old", "source_timestamp": 1},
            }},
        }
        proposal = {
            "should_update": True, "message_kind": "real_world_update", "context_action": "clear",
            "plan_id": "message:clear", "fields": {
                "activity": {"operation": "clear", "evidence_span": "清掉近期情境"},
            },
        }
        with patch("services.profile_skills.profiles_coll.find_one", return_value=profile), \
             patch("services.profile_skills.profiles_coll.update_one", return_value=SimpleNamespace(modified_count=1)) as update:
            self.assertTrue(apply_recent_context("owner", proposal, message_id="clear", source_timestamp=2))
        written = update.call_args.args[1]["$set"]
        self.assertEqual(written["recent_context_state"]["fields"], {})
        self.assertEqual(written["current_context"], "")

    def test_v2_profile_semantics_do_not_use_legacy_keyword_gates(self):
        source = inspect.getsource(profile_skills)
        for name in ("SYSTEM_CONTEXT_RE", "ONE_TIME_RE", "THIRD_PARTY_RE", "PREFERENCE_RE", "VAGUE_RECENT_RE"):
            self.assertNotIn(name, source)
        self.assertFalse(hasattr(profile_projection, "db"))
        self.assertFalse(hasattr(profile_projection, "generate_chat_completion"))

    def test_language_normalization_and_skill_packs(self):
        self.assertEqual(normalize_zh_tw("用户想去日本，等待回复约会"), "使用者想去日本,等待回覆約會")
        self.assertEqual(memory_summary([{"stance": "like", "label": "户外活动"}]), "喜歡戶外活動")
        self.assertEqual(load_profile_skill("memory")["version"], "2")
        self.assertEqual(load_profile_skill("recent-context")["name"], "recent-context")

    def test_profile_skill_rollout_and_registry(self):
        os.environ.pop("AYUE_PROFILE_SKILLS_MODE", None)
        os.environ.pop("AYUE_PROFILE_SKILLS_USER_ALLOWLIST", None)
        self.assertEqual(profile_skills_mode_for_user("demo_user"), "off")
        os.environ["AYUE_PROFILE_SKILLS_MODE"] = "on"
        os.environ["AYUE_PROFILE_SKILLS_USER_ALLOWLIST"] = "demo_user"
        self.assertEqual(profile_skills_mode_for_user("demo_user"), "on")
        self.assertEqual(profile_skills_mode_for_user("seed_user_04"), "off")

    def test_profile_source_is_loaded_by_mongo_object_id(self):
        os.environ["AYUE_PROFILE_SKILLS_MODE"] = "shadow"
        message_id = "64b64c8f0000000000000001"
        decision = {
            "recent_context": {"should_update": False, "reason_code": "test"},
            "memories": [], "memory_codes": ["no_memory_candidate"], "policy_versions": {},
        }
        with patch("services.profile_skills.messages_coll.find_one", return_value={"content": "我最近想去非洲"}) as find_message, \
             patch("services.profile_skills.profiles_coll.find_one", return_value={}), \
             patch("services.profile_skills._claim_profile_message", return_value=True), \
             patch("services.profile_skills.analyze_profile_message", return_value=decision), \
             patch("services.profile_skills._trace"):
            result = process_profile_message("owner", "我最近想去非洲", message_id, "global")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(find_message.call_args.args[0], {
            "_id": ObjectId(message_id), "sender_id": "owner",
        })
        self.assertEqual(find_message.call_args.args[1], {
            "content": 1, "metadata.owner_raw_content": 1, "timestamp": 1,
        })

    def test_profile_source_accepts_server_preserved_raw_owner_content(self):
        os.environ["AYUE_PROFILE_SKILLS_MODE"] = "shadow"
        message_id = "64b64c8f0000000000000003"
        decision = {
            "recent_context": {"should_update": False, "reason_code": "test"},
            "memories": [], "memory_codes": ["no_memory_candidate"], "policy_versions": {},
        }
        source = {"content": "@seed_user_01 我想去爬山", "metadata": {"owner_raw_content": "我想去爬山"}}
        with patch("services.profile_skills.messages_coll.find_one", return_value=source), \
             patch("services.profile_skills.profiles_coll.find_one", return_value={}), \
             patch("services.profile_skills._claim_profile_message", return_value=True), \
             patch("services.profile_skills.analyze_profile_message", return_value=decision) as analyze, \
             patch("services.profile_skills._trace"):
            result = process_profile_message("owner", "我想去爬山", message_id, "global")
        analyze.assert_called_once()
        self.assertEqual(result["status"], "skipped")

    def test_duplicate_message_is_rejected_before_extraction(self):
        os.environ["AYUE_PROFILE_SKILLS_MODE"] = "on"
        message_id = "64b64c8f0000000000000002"
        with patch("services.profile_skills.messages_coll.find_one", return_value={"content": "我想去爬山", "timestamp": 1}), \
             patch("services.profile_skills._claim_profile_message", return_value=False), \
             patch("services.profile_skills.analyze_profile_message") as analyze:
            result = process_profile_message("owner", "我想去爬山", message_id, "global")
        analyze.assert_not_called()
        self.assertEqual(result, {"status": "skipped", "reason": "already_processed"})


if __name__ == "__main__":
    unittest.main()

