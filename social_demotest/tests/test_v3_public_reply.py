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

    def test_ordinary_reply_allows_three_sentences_with_160_char_envelope(self):
        reply = "第一句先接住使用者的具體處境，給一點自然反應。第二句補上一個有根據的看法或下一步，讓對話可以往前走。第三句再留一個輕鬆選項。"
        result = validate_public_reply(
            reply,
            reject_internal_identifiers=True,
            reject_structured_output=True,
        )
        self.assertIsNotNone(result.reply)
        self.assertIn("第三句再留一個輕鬆選項", result.reply)
        self.assertLessEqual(len(result.reply), 160)

    def test_ordinary_reply_bounds_fourth_sentence(self):
        reply = "第一句。第二句。第三句。第四句不應該出現在 ordinary reply。"
        result = validate_public_reply(
            reply,
            reject_internal_identifiers=True,
            reject_structured_output=True,
        )
        self.assertEqual(result.reply, "第一句。第二句。第三句。")

    def test_grounded_reply_keeps_longer_verified_detail_envelope(self):
        reply = "這是第一段已驗證的行程說明，提供日期、開始時間與活動內容，讓你先知道安排。這是第二段補充，交代使用者需要知道的細節，避免把重要資訊藏起來。這是第三段補充，說明目前資料的範圍與限制，方便你判斷下一步。這是第四段補充，只用於完整呈現 grounded result 的必要內容。這是第五段補充，沒有額外加入客套或未驗證的推測。"
        result = validate_public_reply(reply, preserve_details=True)
        self.assertIsNotNone(result.reply)
        self.assertIn("grounded result", result.reply)
        self.assertGreater(len(result.reply), 160)


if __name__ == "__main__":
    unittest.main()
