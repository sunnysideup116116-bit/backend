import unittest

from services.ayue_agent.v3.public_reply import build_presentation, validate_public_reply


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

    def test_ordinary_reply_uses_shared_3600_character_envelope(self):
        reply = "第一句先接住使用者的具體處境，給一點自然反應。第二句補上一個有根據的看法或下一步，讓對話可以往前走。第三句再留一個輕鬆選項。"
        result = validate_public_reply(
            reply,
            reject_internal_identifiers=True,
            reject_structured_output=True,
        )
        self.assertIsNotNone(result.reply)
        self.assertIn("第三句再留一個輕鬆選項", result.reply)
        self.assertEqual(result.reply, reply)

    def test_ordinary_reply_does_not_impose_a_sentence_count(self):
        reply = "第一句。第二句。第三句。第四句也要保留。"
        result = validate_public_reply(
            reply,
            reject_internal_identifiers=True,
            reject_structured_output=True,
        )
        self.assertEqual(result.reply, reply)

    def test_grounded_reply_keeps_longer_verified_detail_envelope(self):
        reply = "這是第一段已驗證的行程說明，提供日期、開始時間與活動內容，讓你先知道安排。這是第二段補充，交代使用者需要知道的細節，避免把重要資訊藏起來。這是第三段補充，說明目前資料的範圍與限制，方便你判斷下一步。這是第四段補充，只用於完整呈現 grounded result 的必要內容。這是第五段補充，沒有額外加入客套或未驗證的推測。"
        result = validate_public_reply(reply, preserve_details=True)
        self.assertIsNotNone(result.reply)
        self.assertIn("grounded result", result.reply)
        self.assertGreater(len(result.reply), 160)

    def test_grounded_recommendation_has_separate_bounded_envelope(self):
        presentation = build_presentation(
            ["先選 A；A 有目前可確認的營業資訊。", "B 可作替代，但公開資料尚未確認同一條件。"],
            "grounded_recommendation",
        )
        self.assertIsNotNone(presentation)
        self.assertEqual(presentation.presentation_class, "grounded_recommendation")
        self.assertLessEqual(sum(len(item) for item in presentation.messages), 3_600)

    def test_grounded_recommendation_keeps_markdown_beyond_short_chat_limit(self):
        message = "### 查詢結果\n\n" + "\n".join(
            f"- **候選 {index}** — 這是有來源支持的整理內容，包含使用者需要比較的細節。"
            for index in range(10)
        )
        presentation = build_presentation([message], "grounded_recommendation")
        self.assertIsNotNone(presentation)
        self.assertGreater(len(presentation.messages[0]), 240)
        self.assertIn("### 查詢結果", presentation.messages[0])
        self.assertIn("**候選 9**", presentation.messages[0])

    def test_grounded_recommendation_shortens_overlong_total_once(self):
        message = "候選資訊 " * 500
        presentation = build_presentation([message, message], "grounded_recommendation")
        self.assertIsNotNone(presentation)
        self.assertLessEqual(sum(len(item) for item in presentation.messages), 3_600)
        self.assertTrue(presentation.messages[-1].endswith("……"))

    def test_more_than_three_bubbles_are_merged_in_order(self):
        presentation = build_presentation(
            ["第一", "第二", "第三", "第四", "第五"],
            "conversation",
        )
        self.assertEqual(
            presentation.messages,
            ["第一", "第二", "第三\n\n第四\n\n第五"],
        )

    def test_every_presentation_class_accepts_one_to_three_bubbles(self):
        classes = (
            "conversation", "social_opportunity", "product_info", "transaction",
            "capability", "fallback", "onboarding", "grounded_recommendation",
        )
        for presentation_class in classes:
            with self.subTest(presentation_class=presentation_class):
                for count in (1, 2, 3):
                    presentation = build_presentation(
                        [f"自然回覆 {index}" for index in range(count)],
                        presentation_class,
                    )
                    self.assertIsNotNone(presentation)


if __name__ == "__main__":
    unittest.main()
