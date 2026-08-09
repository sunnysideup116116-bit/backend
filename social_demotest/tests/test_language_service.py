import unittest

from services.language_service import normalize_public_reply


class PublicLanguageNormalizationTests(unittest.TestCase):
    def test_simplified_public_reply_becomes_traditional(self):
        self.assertEqual(normalize_public_reply("这是简体中文。"), "這是簡體中文。")

    def test_opaque_url_and_json_are_not_rewritten(self):
        url = "https://example.com/這是路徑?x=简体"
        self.assertIn(url, normalize_public_reply("请查看 " + url))
        raw_json = '{"text":"这是"}'
        self.assertEqual(normalize_public_reply(raw_json), raw_json)

    def test_markdown_layout_keeps_headings_lists_and_blank_lines(self):
        value = "### 查詢結果\n\n- **第一筆**：內容\n- 第二筆\n\n### 參考來源\n\n1. `官方頁面`"
        normalized = normalize_public_reply(value)
        self.assertIn("### 查詢結果\n\n", normalized)
        self.assertIn("- **第一筆**:內容\n- 第二筆", normalized)
        self.assertIn("\n\n### 參考來源", normalized)
        self.assertEqual(normalize_public_reply(normalized), normalized)


if __name__ == "__main__":
    unittest.main()
