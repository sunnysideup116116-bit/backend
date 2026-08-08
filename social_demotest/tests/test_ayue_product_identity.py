import unittest
from pathlib import Path

from services.ayue_agent.product_identity import (
    AYUE_CORE_IDENTITY,
    AYUE_VOICE,
    LEGACY_AYUE_PERSONA,
    PRIVATE_AYUE_PERSONA,
    PUBLIC_AYUE_PERSONA,
    PUBLIC_CAPABILITY_REPLY,
    PUBLIC_REPLY_LENGTH,
    PUBLIC_REPLY_TONE,
)
from services.ayue_agent.private_v2 import PrivateAgentTurnContextV2, _compose
from services.ayue_agent.v3.planner import _PLANNER_SYSTEM
from services.ayue_agent.v3.synthesizer import _synthesizer_system_prompt
from services.mediator_context_service import MEDIATOR_PERSONA
from unittest.mock import patch


class AyueProductIdentityTests(unittest.TestCase):
    def test_surface_personas_share_core_but_keep_surface_role_distinct(self):
        self.assertIn(AYUE_CORE_IDENTITY, PUBLIC_AYUE_PERSONA)
        self.assertIn(AYUE_CORE_IDENTITY, PRIVATE_AYUE_PERSONA)
        self.assertIn(AYUE_VOICE, PUBLIC_AYUE_PERSONA)
        self.assertIn(AYUE_VOICE, PRIVATE_AYUE_PERSONA)
        self.assertNotEqual(PUBLIC_AYUE_PERSONA, PRIVATE_AYUE_PERSONA)
        self.assertEqual(MEDIATOR_PERSONA, LEGACY_AYUE_PERSONA)

    def test_public_planner_and_synthesizer_receive_canonical_identity(self):
        self.assertIn(PUBLIC_AYUE_PERSONA, _PLANNER_SYSTEM)
        self.assertIn(PUBLIC_AYUE_PERSONA, _synthesizer_system_prompt("general_conversation", False))
        self.assertIn(PUBLIC_AYUE_PERSONA, _synthesizer_system_prompt("grounded_result", False))

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
        self.assertIn("先懂你", PUBLIC_CAPABILITY_REPLY)
        self.assertIn("認識之後也會繼續陪你", PUBLIC_CAPABILITY_REPLY)
        self.assertNotIn("agent", PUBLIC_CAPABILITY_REPLY)
        self.assertNotIn("tool", PUBLIC_CAPABILITY_REPLY)

    def test_public_reply_policy_is_not_injected_into_private_persona(self):
        self.assertIn("1–3 句", PUBLIC_REPLY_LENGTH)
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
