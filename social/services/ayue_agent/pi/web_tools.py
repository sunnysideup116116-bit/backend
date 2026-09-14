"""Pi public-web tools."""
from __future__ import annotations

from typing import Any

from services.ayue_agent.web_tools import is_safe_public_url
from services.ayue_agent.shared.web_guard import web_extract_urls_allowed

from .domain_support import dispatch_registered


TOOL_NAMES = frozenset({"web.extract", "web.search"})

PROMPT = """【公開資訊】
Web 用於活動、演出、展覽、新聞、其他產品與公開人物查證；活動不可硬塞成 Places category。
使用者沒指定活動類型時可先做範圍適當的公開搜尋。人物錯字只能在前文指涉唯一時視為候選，身分仍要查證。
條件式需求要先查證條件，再只處理成立的分支；成立分支若含寫入，仍須建立真實確認卡後才能說已安排。
同名地點的搜尋結果若混入不同國家或行政區，只能採用與可見對話一致的地點；無法排除時先澄清。
只引用工具回傳，或同房間已保存來源 metadata 中的安全網址；不得補造 URL 或事實。
官方活動時間與使用者自行選定的參加時段要分開描述。"""


def handle(runtime: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return dispatch_registered(runtime, name, arguments)


def extraction_urls_allowed(turn: Any, observations: list[dict], urls: list[str]) -> bool:
    """Allow current search/user URLs plus persisted same-room source metadata."""
    if web_extract_urls_allowed(turn, observations, urls):
        return True
    allowed: set[str] = set()
    for message in (getattr(turn, "recent_messages", None) or []):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for source in (message.get("sources") or []):
            if not isinstance(source, dict):
                continue
            url = str(source.get("url") or "").strip()
            if is_safe_public_url(url):
                allowed.add(url)
    return bool(urls) and all(str(url) in allowed for url in urls)
