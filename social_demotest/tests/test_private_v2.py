import unittest
from dataclasses import replace
from unittest.mock import MagicMock, patch

from services.ayue_agent.private_v2 import (
    PRIVATE_TOOL_REGISTRY,
    PrivateAgentDecision,
    PrivateAgentTurnContextV2,
    _compose,
    run_private_agent_turn_v2,
)


def _context():
    return PrivateAgentTurnContextV2(
        user_id="owner", other_id="other", room_id="private", message="幫我看一下我們最近聊什麼",
        pair_revision=1, viewer_profile={"recent_context": "想去旅行"},
        counterparty_shareable={"display_name": "小晴", "recent_context": "喜歡咖啡"},
        counterparty_advisory={"private_secret": "never expose"},
        shared_history=[{"role": "本人", "content": "我喜歡電影"}, {"role": "對方", "content": "我也喜歡電影"}],
        private_history=[{"role": "本人", "content": "我該怎麼聊"}],
        shared_facts=[{"visibility": "shared_fact", "value": "你們都提到電影"}], local_time="2026-08-01 12:00",
    )


class PrivateV2Tests(unittest.TestCase):
    def test_composer_never_receives_counterparty_advisory(self):
        ctx = _context()
        with patch("services.ayue_agent.private_v2.generate_chat_completion", return_value="可以從最近看的電影延伸聊聊。") as model:
            reply = _compose(ctx, [], "warm")
        self.assertIn("電影", reply)
        self.assertNotIn("never expose", model.call_args.args[0])

    def test_read_tool_loop_reuses_safe_shared_history(self):
        ctx = _context()
        first = PrivateAgentDecision(kind="tool_call", intent="shared_history", tool_name="private.relationship.get_shared_history", confidence=.9, evidence_span="最近聊什麼")
        second = PrivateAgentDecision(kind="final", intent="advice", confidence=.9, strategy="warm")
        with patch("services.ayue_agent.private_v2.build_private_turn_context_v2", return_value=ctx), \
             patch("services.ayue_agent.private_v2._plan", side_effect=[first, second]), \
             patch("services.ayue_agent.private_v2._compose", return_value="你們可以順著電影繼續聊。") as compose, \
             patch("services.ayue_agent.private_v2._trace"):
            result = run_private_agent_turn_v2(user_id="owner", other_id="other", message=ctx.message, match_doc={"status": "accepted", "proposal_revision": 1})
        self.assertTrue(result.handled)
        self.assertEqual(result.agent_mode, "v2")
        observations = compose.call_args.args[1]
        self.assertEqual(observations[0]["tool"], "private.relationship.get_shared_history")

    def test_write_intent_only_creates_confirmation(self):
        ctx = _context()
        decision = PrivateAgentDecision(kind="confirmation", intent="date_coordination", tool_name="private.date.start_coordination", confidence=.9, evidence_span="幫我協調")
        with patch("services.ayue_agent.private_v2.build_private_turn_context_v2", return_value=ctx), \
             patch("services.ayue_agent.private_v2._plan", return_value=decision), \
             patch("services.ayue_agent.private_v2.profiles_coll.update_one") as save, \
             patch("services.ayue_agent.private_v2._trace"):
            result = run_private_agent_turn_v2(user_id="owner", other_id="other", message="幫我協調", match_doc={"status": "accepted", "proposal_revision": 1})
        self.assertIn("確認", result.reply)
        self.assertTrue(save.called)

    def test_fun_fact_is_not_a_visible_tool_and_old_confirmation_is_safely_disabled(self):
        self.assertNotIn("private.relationship.request_fun_fact", PRIVATE_TOOL_REGISTRY)
        ctx = replace(_context(), message="確認")
        old_pending = {"private_agent_confirmation": {
            "other_id": "other", "action": "private.relationship.request_fun_fact",
            "pair_revision": 1, "created_at": 9_999_999_999,
        }}
        with patch("services.ayue_agent.private_v2.build_private_turn_context_v2", return_value=ctx), \
             patch("services.ayue_agent.private_v2.profiles_coll.find_one_and_update", return_value=old_pending), \
             patch("services.ayue_agent.private_v2._trace"):
            result = run_private_agent_turn_v2(user_id="owner", other_id="other", message="確認", match_doc={"status": "accepted", "proposal_revision": 1})
        self.assertIn("先收起來", result.reply)

    def test_terminal_planner_reply_skips_private_composer(self):
        ctx = _context()
        decision = PrivateAgentDecision(kind="final", intent="advice", confidence=.9, reply="你可以先從電影聊起，再接住對方的回應。")
        with patch("services.ayue_agent.private_v2.build_private_turn_context_v2", return_value=ctx), \
             patch("services.ayue_agent.private_v2._plan", return_value=decision), \
             patch("services.ayue_agent.private_v2._compose") as compose, \
             patch("services.ayue_agent.private_v2._trace"):
            result = run_private_agent_turn_v2(user_id="owner", other_id="other", message=ctx.message, match_doc={"status": "accepted", "proposal_revision": 1})
        self.assertEqual(result.reply, "你可以先從電影聊起，再接住對方的回應。")
        compose.assert_not_called()


if __name__ == "__main__":
    unittest.main()
