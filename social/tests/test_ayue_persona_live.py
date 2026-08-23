"""Optional provider-backed smoke checks for the shared Ayue identity contract."""

import os
import unittest

from services.ai_service import generate_chat_completion
from services.ayue_agent.product_identity import PRIVATE_AYUE_PERSONA, PUBLIC_AYUE_PERSONA


@unittest.skipUnless(
    os.getenv("AYUE_RUN_PERSONA_LIVE") == "1",
    "set AYUE_RUN_PERSONA_LIVE=1 to run provider-backed persona checks",
)
class AyuePersonaLiveTests(unittest.TestCase):
    def _ask(self, system_prompt: str, user_prompt: str) -> str:
        result = generate_chat_completion(user_prompt, temperature=0, system_prompt=system_prompt)
        return str(getattr(result, "content", result) or "").strip()

    def test_public_surface_keeps_ayue_identity(self):
        reply = self._ask(PUBLIC_AYUE_PERSONA, "請用一句話說明你是誰，以及你會怎麼陪我。")
        self.assertIn("阿月", reply)
        self.assertNotRegex(reply.lower(), r"model|agent|tool|prompt")

    def test_private_surface_keeps_same_persona_with_relationship_context(self):
        reply = self._ask(PRIVATE_AYUE_PERSONA, "請用一句話說明你在這段關係裡會怎麼幫我。")
        self.assertIn("阿月", reply)
        self.assertNotRegex(reply.lower(), r"model|agent|tool|prompt")


if __name__ == "__main__":
    unittest.main()
