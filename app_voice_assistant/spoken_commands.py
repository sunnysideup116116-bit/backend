"""Conservative focus for corrections containing both cancellation and retention."""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def cancellation_focus(text: str) -> dict[str, str]:
    if '取消' not in text or not any(word in text for word in ('保留', '不是', '不要取消')):
        return {}
    clauses = [part.strip() for part in re.split(r'[，,。；;]|但是|而是', text) if part.strip()]
    positive = [part for part in clauses if '取消' in part and not any(word in part for word in ('不是', '不要取消', '別取消', '保留'))]
    if len(positive) != 1:
        return {'question': '要取消哪一天的哪個行程？我會保留其他行程。'}
    clause = positive[0]
    match = re.search(r'(下|這|本)?(?:週|周|星期)([一二三四五六日天])', clause)
    if not match:
        match_date = re.search(r'\d{4}-\d{2}-\d{2}',clause)
        return {'date':match_date[0]} if match_date else {'question':'請再指定要取消的日期與行程名稱。'}
    today = datetime.now(ZoneInfo('Asia/Taipei')).date()
    weekday = '一二三四五六日'.index(match[2].replace('天','日'))
    offset = weekday - today.weekday()
    if match[1] == '下' or (match[1] is None and offset < 0):
        offset += 7
    return {'date':(today+timedelta(days=offset)).isoformat()}
