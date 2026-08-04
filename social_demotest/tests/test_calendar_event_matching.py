"""回歸測試：`_event_matches_hint` 對模型回覆的各種 hint 樣式都能比對到行程。

鎖定三類已踩過的 bug：
1. hint 帶時間（HH:MM-HH:MM）但 haystack 原本沒索引時間 → 找不到
2. hint 與 event 欄位順序相反 → 整段子字串比對失敗
3. 模型用 en dash「–」而非普通連字號「-」→ 字元不一致失敗

未來只要改 matcher，CI 會擋住回歸；模型若回新怪字元，加一條 case 即可。
"""
import unittest
from datetime import datetime, timezone, timedelta

from services.calendar_service import _event_matches_hint


def _make_event(title="50嵐 喝飲料", day=3, start="09:00", end="10:00", location=""):
    zone = timezone(timedelta(hours=8), name="Asia/Taipei")
    s = datetime(2026, 8, day, int(start[:2]), int(start[3:]), tzinfo=zone)
    e = datetime(2026, 8, day, int(end[:2]), int(end[3:]), tzinfo=zone)
    return {
        "title": title, "activity": title, "location": location,
        "start_at": s.astimezone(timezone.utc), "end_at": e.astimezone(timezone.utc),
        "timezone": "Asia/Taipei",
    }


class EventMatchesHintTests(unittest.TestCase):
    def setUp(self):
        self.event = _make_event()

    # --- 命中樣本 ---
    def test_plain_name_only(self):
        self.assertTrue(_event_matches_hint(self.event, "50嵐"))

    def test_name_with_time(self):
        self.assertTrue(_event_matches_hint(self.event, "8/3 09:00-10:00 50嵐 喝飲料"))

    def test_name_with_en_dash_time(self):
        # 模型常回 09:00–10:00（U+2013）而非 09:00-10:00
        self.assertTrue(_event_matches_hint(self.event, "8/3 09:00\u201310:00 50嵐 喝飲料"))

    def test_name_with_em_dash_time(self):
        self.assertTrue(_event_matches_hint(self.event, "8/3 09:00\u201410:00 50嵐 喝飲料"))

    def test_temporal_word_today(self):
        self.assertTrue(_event_matches_hint(self.event, "今天8/3 09:00-10:00 50嵐喝飲料"))

    def test_temporal_word_with_particles(self):
        self.assertTrue(_event_matches_hint(self.event, "今天 8/3 09:00–10:00 在 50嵐 喝飲料"))

    def test_reversed_order(self):
        # hint 欄位順序與 event 不同，仍應命中
        self.assertTrue(_event_matches_hint(self.event, "50嵐 喝飲料 8/3 09:00-10:00"))

    def test_date_only_no_time(self):
        self.assertTrue(_event_matches_hint(self.event, "8/3 50嵐"))

    def test_full_width_colon_and_digits(self):
        # 模型偶爾回全形：０９：００–１０：００
        self.assertTrue(_event_matches_hint(self.event, "8/3 ０９：００\u2013１０：００ 50嵐"))

    def test_full_width_space(self):
        self.assertTrue(_event_matches_hint(self.event, "8/3\u300009:00-10:00 50嵐"))

    def test_full_width_comma(self):
        self.assertTrue(_event_matches_hint(self.event, "8/3\uff0c09:00-10:00\uff0c50嵐"))

    def test_full_width_tilde(self):
        # 波浪號範圍表示
        self.assertTrue(_event_matches_hint(self.event, "8/3 09:00\uff5e10:00 50嵐"))

    def test_with_location(self):
        ev = _make_event(title="看電影", day=8, start="15:00", end="18:00", location="信義威秀")
        self.assertTrue(_event_matches_hint(ev, "8/8 15:00-18:00 信義威秀 看電影"))

    # --- 不命中樣本 ---
    def test_wrong_date(self):
        self.assertFalse(_event_matches_hint(self.event, "8/11 牛肉麵"))

    def test_nonexistent_name(self):
        self.assertFalse(_event_matches_hint(self.event, "8/3 原始火鍋用餐"))

    def test_empty_hint(self):
        self.assertFalse(_event_matches_hint(self.event, ""))

    def test_only_temporal_words(self):
        # 全是被剔除的詞，segments 變空 → 視為不命中（避免「今天」誤觸發所有今天行程）
        self.assertFalse(_event_matches_hint(self.event, "幫我取消的行程"))

    def test_time_mismatch(self):
        # 日期對、名稱對、但時間完全不對 → 不應命中
        self.assertFalse(_event_matches_hint(self.event, "8/3 20:00-22:00 50嵐"))


if __name__ == "__main__":
    unittest.main()