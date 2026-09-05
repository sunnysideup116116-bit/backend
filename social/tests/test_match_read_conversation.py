from unittest.mock import Mock
import unicodedata

import pytest

from services.ai_service import ToolCallResult
from services.ayue_agent.v3.contracts import AgentContextSlice
from services.ayue_agent.v3 import synthesizer as synth


def context(message, *, code="verified_status", state="no_candidates"):
    return AgentContextSlice(agent="synthesizer", payload={
        "message": message, "recent_messages": ["assistant: 上一次搜尋沒有合適人選；這次尚未開始新的搜尋。"],
        "observations": [{"tool": "match.get_status", "status": "ok", "result": {
            "state": state, "scope": "search", "is_terminal": True,
            "verified_match_read": code == "verified_status",
            "match_runtime": {"code": code, "reply": "上一次搜尋沒有合適人選；這次尚未開始新的搜尋。"},
        }}],
    })


@pytest.mark.parametrize("question,answer", [
    ("我目前配對狀態是？", "目前沒有正在搜尋；上一次搜尋沒有合適的人選。"),
    ("是嗎", "對，我剛重新確認過，目前沒有正在搜尋；剛才說的是上一次結果。"),
    ("真的嘛", "是的，現在沒有新的搜尋在跑；不是說你之後一定配不到。"),
])
def test_verified_reads_are_composed_for_the_current_question(monkeypatch, question, answer):
    provider = Mock(return_value=ToolCallResult(content=answer, tool_calls=[]))
    monkeypatch.setattr(synth, "generate_chat_completion_with_tools", provider)
    reply, _, metrics = synth.synthesize(context(question), on_token=Mock())
    assert reply == unicodedata.normalize("NFKC", answer)
    assert metrics.used_llm
    assert provider.call_args.kwargs["on_token"] is None
    assert question in provider.call_args.args[0]
    assert '"match_runtime"' not in provider.call_args.args[0]


@pytest.mark.parametrize("bad_reply", ["我已幫你開始搜尋。", "目前正在搜尋。", "我已替你撤回提案。", "你們配對成功，聊天室已開好了。", "提案已撤回。", "已送出確認按鈕。"])
def test_read_composition_cannot_claim_unexecuted_mutations(monkeypatch, bad_reply):
    monkeypatch.setattr(synth, "generate_chat_completion_with_tools", Mock(return_value=ToolCallResult(content=bad_reply, tool_calls=[])))
    reply, _, metrics = synth.synthesize(context("真的嘛"))
    assert reply != bad_reply
    assert "沒有合適" in reply
    assert metrics.fallback_reason == "unsupported_claim"


def test_provider_failure_keeps_verified_status_and_answers_followup(monkeypatch):
    monkeypatch.setattr(synth, "generate_chat_completion_with_tools", Mock(side_effect=RuntimeError("offline")))
    reply, _, metrics = synth.synthesize(context("是嗎"))
    assert "重新確認" in reply and "上一次" in reply
    assert "沒接好" not in reply
    assert metrics.fallback_reason == "provider_error"


def test_write_clarifications_still_bypass_model_composition(monkeypatch):
    provider = Mock()
    monkeypatch.setattr(synth, "generate_chat_completion_with_tools", provider)
    reply, _, _ = synth.synthesize(context("是嗎", code="proposal_not_actionable"))
    provider.assert_not_called()
    assert "上一次搜尋" in reply
