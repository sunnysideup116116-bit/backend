import unittest
from pathlib import Path

from services.ayue_agent.product_identity import (
    AYUE_CORE_IDENTITY,
    AYUE_MISSION_SHORT,
    AYUE_VOICE,
    AYUE_VOICE_SHORT,
    LEGACY_AYUE_PERSONA,
    PRIVATE_AYUE_PERSONA,
    PUBLIC_AYUE_PERSONA,
    PUBLIC_CAPABILITY_REPLY,
    PUBLIC_REPLY_LENGTH,
    PUBLIC_REPLY_TONE,
    PUBLIC_VOICE_FEW_SHOTS,
)
from services.ayue_agent.private_v2 import PrivateAgentTurnContextV2, _compose
from services.ayue_agent.pi.prompts import POLICY
from services.mediator_context_service import MEDIATOR_PERSONA
from unittest.mock import patch


class AyueProductIdentityTests(unittest.TestCase):
    def test_surface_personas_share_core_but_keep_surface_role_distinct(self):
        self.assertIn(AYUE_CORE_IDENTITY, PUBLIC_AYUE_PERSONA)
        self.assertIn(AYUE_CORE_IDENTITY, PRIVATE_AYUE_PERSONA)
        self.assertIn(AYUE_VOICE, PUBLIC_AYUE_PERSONA)
        self.assertIn(AYUE_VOICE, PRIVATE_AYUE_PERSONA)
        self.assertNotEqual(PUBLIC_AYUE_PERSONA, PRIVATE_AYUE_PERSONA)
        self.assertIn("看不到你和對方的雙人聊天室紀錄", PUBLIC_AYUE_PERSONA)
        self.assertIn("可以讀取這個雙人聊天室允許的近期聊天紀錄", PRIVATE_AYUE_PERSONA)
        self.assertIn("聊天建議", PRIVATE_AYUE_PERSONA)
        self.assertEqual(MEDIATOR_PERSONA, LEGACY_AYUE_PERSONA)

    def test_public_pi_receives_canonical_identity(self):
        self.assertIn("公開阿月", POLICY)
        self.assertIn("繁體中文", POLICY)
        self.assertIn("Markdown `-` 條列", POLICY)
        self.assertIn("不要為了格式列點", POLICY)
        for question, reply in PUBLIC_VOICE_FEW_SHOTS:
            self.assertNotIn(question, POLICY)
            self.assertNotIn(reply, POLICY)

    def test_private_composer_receives_private_surface_identity(self):
        context = PrivateAgentTurnContextV2(
            user_id="owner", other_id="other", room_id="private", message="怎麼回？",
            pair_revision=1, viewer_profile={}, counterparty_shareable={},
            counterparty_advisory={}, shared_history=[], private_history=[],
            shared_facts=[], local_time="2026-08-09 12:00",
        )
        with patch(
            "services.ayue_agent.private_v2.generate_chat_completion",
            return_value="可以先接住對方剛剛說的事。",
        ) as provider:
            _compose(context, [], "warm")
        self.assertIn(PRIVATE_AYUE_PERSONA, provider.call_args.args[0])

    def test_capability_copy_is_product_facing_and_not_model_metadata(self):
        self.assertIn("阿月", PUBLIC_CAPABILITY_REPLY)
        self.assertIn("媒人朋友", PUBLIC_CAPABILITY_REPLY)
        self.assertIn("陪你聊生活", PUBLIC_CAPABILITY_REPLY)
        self.assertNotIn("agent", PUBLIC_CAPABILITY_REPLY)
        self.assertNotIn("tool", PUBLIC_CAPABILITY_REPLY)

    def test_public_reply_policy_is_not_injected_into_private_persona(self):
        self.assertIn("1–2 句", PUBLIC_REPLY_LENGTH)
        self.assertIn("不要套公式", PUBLIC_REPLY_TONE)
        self.assertNotIn(PUBLIC_REPLY_LENGTH, PRIVATE_AYUE_PERSONA)
        self.assertNotIn(PUBLIC_REPLY_TONE, PRIVATE_AYUE_PERSONA)

    def test_frontend_uses_product_language_and_does_not_show_internal_contact_id(self):
        frontend = (Path(__file__).parents[1] / "frontend.html").read_text(encoding="utf-8")
        self.assertIn("先懂你，再牽線", frontend)
        self.assertIn("私下問阿月你們兩個的事", frontend)
        self.assertNotIn('"關於 " + activeContactId + "，只有你看得到"', frontend)


if __name__ == "__main__":
    unittest.main()
