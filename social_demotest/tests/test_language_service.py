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


if __name__ == "__main__":
    unittest.main()
