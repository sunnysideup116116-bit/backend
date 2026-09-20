"""Model-only time annotations must not become user-visible Pi text."""
import pytest

from services.ayue_agent.pi.reply import confirmed_reply, pending_preview, pending_reply, validate_pi_reply
from services.ayue_agent.shared.confirmation_layout import confirmation_layout


STAMP = "[訊息時間：2026-09-14T15:53:05+08:00；時區：Asia/Taipei]"


@pytest.mark.parametrize("separator", ["", " ", "\n", "\\n", "\n\n"])
def test_pi_reply_removes_model_time_annotation_and_preserves_answer(separator):
    assert validate_pi_reply(STAMP + separator + "要安排在下午五點嗎？", []) == "要安排在下午五點嗎？"


def test_repeated_time_annotations_do_not_remove_normal_dates():
    text = f"{STAMP}\n9/24 下午五點到六點。\n{STAMP}\n要調整時間嗎？"
    assert validate_pi_reply(text, []) == "9/24 下午五點到六點。\n\n要調整時間嗎？"


@pytest.mark.parametrize("reply", [STAMP, "[訊息時間：2026-09-14T15:53", "【消息时间：2026-09-14T15:53:05+08:00；时区：Asia/Taipei】"])
def test_annotation_without_an_answer_cannot_be_published(reply):
    assert validate_pi_reply(reply, []) is None


def test_real_event_dates_and_timezone_explanations_are_preserved():
    reply = "[預約時間：2026-09-24 17:00] 時區是 Asia/Taipei，要確認嗎？"
    assert validate_pi_reply(reply, []) == reply


def test_confirmation_copy_keeps_layout_when_time_annotation_is_echoed():
    layout = confirmation_layout(
        f"{STAMP}\n我整理好了。[[confirmation]]{STAMP}\\n時間不對可以告訴我。",
        fallback_preview="要新增 9/24 17:00–18:00 駁二行程嗎？",
    )
    assert layout.used_fallback is False
    assert layout.messages == ["我整理好了。", "時間不對可以告訴我。"]
    assert [block["type"] for block in layout.interaction_blocks_v1] == ["text", "confirmation", "text"]


def test_pending_and_confirmed_receipts_hide_internal_annotations():
    observations = [{"result": {"pending_confirmation": True, "preview": STAMP + "\n要新增駁二行程嗎？"}}]
    assert pending_preview(observations) == "要新增駁二行程嗎？"
    assert pending_reply(observations) == "要新增駁二行程嗎？"
    assert confirmed_reply([{"data": {"reply": STAMP + "\n駁二行程已新增。"}}]) == "駁二行程已新增。"


def test_timestamp_removal_does_not_bypass_unverified_write_check():
    assert validate_pi_reply(STAMP + "\n我已新增行程。", []) is None


def test_informal_unverified_schedule_claim_is_rejected():
    assert validate_pi_reply("綠島空氣很好，所以幫你排了明天下午一點。", []) is None


def test_future_schedule_offer_remains_publishable():
    reply = "我可以幫你安排；建立確認卡並由你確認後才會新增。"
    assert validate_pi_reply(reply, []) == reply


def test_pi_reply_removes_display_emoji():
    assert validate_pi_reply("我懂你🙂，我們慢慢來💛。", []) == "我懂你，我們慢慢來。"
    assert validate_pi_reply("可以呀❤️", []) == "可以呀"
