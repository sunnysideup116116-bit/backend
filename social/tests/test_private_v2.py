import unittest
from dataclasses import replace
from unittest.mock import MagicMock, patch

from services.ayue_agent.private_v2 import (
    PRIVATE_TOOL_REGISTRY,
    PrivateAgentDecision,
    PrivateAgentTurnContextV2,
    _bounded_history,
    _compose,
    _search_shared_history,
    run_private_agent_turn_v2,
)


def _context():
    return PrivateAgentTurnContextV2(
        user_id="owner", other_id="other", room_id="private",
        message="幫我看一下我們最近聊什麼",
        pair_revision=1, viewer_profile={"recent_context": "想去旅行"},
        counterparty_shareable={"display_name": "小晴", "recent_context": "喜歡咖啡"},
        counterparty_advisory={"private_secret": "never expose"},
        shared_history=[{"role": "本人", "content": "我喜歡電影"}, {"role": "對方", "content": "我也喜歡電影"}],
        private_history=[{"role": "本人", "content": "我該怎麼聊"}],
        shared_facts=[{"visibility": "shared_fact", "value": "你們都提到電影"}], local_time="2026-08-01 12:00",
    )


class PrivateV2Tests(unittest.TestCase):
    def test_recent_shared_history_uses_the_expanded_bounded_window(self):
        rows = [
            {"_id": f"m-{index}", "sender_id": "owner", "content": "訊息" * 20, "timestamp": index}
            for index in range(40)
        ]
        cursor = MagicMock()
        cursor.sort.return_value.limit.return_value = rows
        collection = MagicMock()
        collection.find.return_value = cursor
        with patch("services.ayue_agent.private_v2.messages_coll", collection):
            result = _bounded_history(
                "pair-room", owner_id="owner", other_id="other",
                limit=40, char_budget=16000,
            )
        cursor.sort.return_value.limit.assert_called_once_with(40)
        self.assertEqual(len(result), 40)
        self.assertIn("message_id", result[0])
        self.assertIn("timestamp", result[0])

    def test_history_budget_keeps_the_newest_messages(self):
        cursor = MagicMock()
        cursor.sort.return_value.limit.return_value = [
            {"_id": "new", "sender_id": "owner", "content": "最新" * 80, "timestamp": 3},
            {"_id": "old", "sender_id": "other", "content": "較舊" * 80, "timestamp": 2},
        ]
        collection = MagicMock()
        collection.find.return_value = cursor
        with patch("services.ayue_agent.private_v2.messages_coll", collection):
            result = _bounded_history(
                "pair-room", owner_id="owner", other_id="other", limit=40, char_budget=100,
            )
        self.assertEqual([item["message_id"] for item in result], ["new"])
        self.assertEqual(result[0]["truncated"], "true")

    def test_full_shared_history_search_is_room_bound_and_bounded(self):
        cursor = MagicMock()
        cursor.sort.return_value.limit.return_value = [
            {"sender_id": "owner", "content": "我們聊過籃球", "timestamp": 1},
        ]
        collection = MagicMock()
        collection.find.return_value = cursor
        with patch("services.ayue_agent.private_v2.messages_coll", collection):
            result = _search_shared_history(_context(), "籃球")
        query = collection.find.call_args.args[0]
        self.assertEqual(query["room_id"], "other_owner")
        self.assertEqual(query["content"]["$regex"], "籃球")
        self.assertEqual(result["messages"][0]["role"], "本人")

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
             patch("services.ayue_agent.private_v2.ConfirmationManager.create_confirmation", return_value="choice-1") as save, \
             patch("services.ayue_agent.private_v2._trace"):
            result = run_private_agent_turn_v2(user_id="owner", other_id="other", message="幫我協調", match_doc={"status": "accepted", "proposal_revision": 1})
        self.assertIn("安排約會", result.reply)
        self.assertTrue(save.called)

    def test_fun_fact_is_not_a_visible_tool_and_old_confirmation_is_safely_disabled(self):
        self.assertNotIn("private.relationship.request_fun_fact", PRIVATE_TOOL_REGISTRY)
        ctx = replace(_context(), message="確認")
        decision = PrivateAgentDecision(
            kind="final", intent="advice", confidence=.9,
            reply="你想確認的是哪一段對話？",
        )
        with patch("services.ayue_agent.private_v2.build_private_turn_context_v2", return_value=ctx), \
             patch("services.ayue_agent.private_v2._plan", return_value=decision), \
             patch("services.ayue_agent.private_v2._execute_write") as execute, \
             patch("services.ayue_agent.private_v2._trace"):
            result = run_private_agent_turn_v2(user_id="owner", other_id="other", message="確認", match_doc={"status": "accepted", "proposal_revision": 1})
        self.assertEqual(result.reply, "你想確認的是哪一段對話？")
        execute.assert_not_called()

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
