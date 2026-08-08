import unittest

from services.ayue_agent.v3.public_reply import validate_public_reply


class V3PublicReplyTests(unittest.TestCase):
    def test_direct_reply_is_normalized_to_traditional_chinese(self):
        result = validate_public_reply(
            "这是简体中文。",
            reject_internal_identifiers=True,
            reject_structured_output=True,
        )
        self.assertEqual(result.reply, "這是簡體中文。")
        self.assertIsNone(result.reason)

    def test_direct_reply_rejects_internal_metadata_and_identifiers(self):
        for reply in ("我會呼叫 tool_call 幫你處理。", "你的 event_id 是 abc。"):
            result = validate_public_reply(
                reply,
                reject_internal_identifiers=True,
                reject_structured_output=True,
            )
            self.assertIsNone(result.reply)
            self.assertIsNotNone(result.reason)

    def test_direct_reply_rejects_structured_output(self):
        result = validate_public_reply(
            '{"event_id":"secret"}',
            reject_internal_identifiers=True,
            reject_structured_output=True,
        )
        self.assertEqual(result.reason, "structured_reply")

    def test_direct_reply_rejects_unsupported_random_match_claim(self):
        result = validate_public_reply(
            "我會隨機幫你配對。",
            reject_internal_identifiers=True,
            reject_structured_output=True,
        )
        self.assertEqual(result.reason, "unsupported_claim")


if __name__ == "__main__":
    unittest.main()
