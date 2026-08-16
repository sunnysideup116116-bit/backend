"""Room-first onboarding state for the Public Ayue surface."""

from __future__ import annotations

from database import messages_coll, profiles_coll
from services.chat_service import generate_room_id


PUBLIC_AYUE_ONBOARDING_VERSION = 1
PUBLIC_AYUE_ONBOARDING_MESSAGES = (
    "嗨，我是阿月。你可以把我當成一個會陪你聊生活、也幫你留意緣分的媒人朋友。",
    "不用學指令，最近在忙什麼、去了哪裡，或心裡卡著什麼，都可以直接跟我說。我會慢慢把你的近況接起來，也不會把你的原話直接丟給別人。",
    "哪天我真的想到值得認識的人，會先問你，你點頭後才牽線。認識之後，我也會在你們聊天室裡陪你；阿月悄悄話還是我，只是會專心看你跟這個人的互動。",
)


def public_ayue_onboarding_state(user_id: str) -> dict | None:
    """Return onboarding bubbles only for a genuinely empty Public room."""
    profile = profiles_coll.find_one({"user_id": user_id}, {"onboarding_completed": 1, "public_ayue_onboarding_version": 1}) or {}
    if bool(profile.get("onboarding_completed")):
        return None
    if int(profile.get("public_ayue_onboarding_version", 0) or 0) >= PUBLIC_AYUE_ONBOARDING_VERSION:
        return None
    room_id = generate_room_id(user_id, "ai_assistant")
    if messages_coll.count_documents({"room_id": room_id}) > 0:
        return None
    return {
        "version": PUBLIC_AYUE_ONBOARDING_VERSION,
        "messages": list(PUBLIC_AYUE_ONBOARDING_MESSAGES),
    }


def complete_public_ayue_onboarding(user_id: str) -> None:
    """Idempotently mark the additive onboarding version complete."""
    profiles_coll.update_one(
        {"user_id": user_id},
        {"$max": {"public_ayue_onboarding_version": PUBLIC_AYUE_ONBOARDING_VERSION}},
        upsert=True,
    )

