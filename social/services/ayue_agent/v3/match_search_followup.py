"""Legacy widening detector kept for old imports.

The current search is one pass.  This module is intentionally not imported by
the public scheduler; new requests carry their topic in ``search_context`` and
normal no-result replies never create a second automatic search.
"""

from __future__ import annotations

import re
from typing import Any


def requested_search_widening(ctx: Any, search: dict[str, Any]) -> bool:
    """A short answer belongs only to the immediately preceding search offer.

    This prepares a preview; it never authorizes or executes a search itself.
    The public adapter includes the current user message in recent_history.
    """
    if search.get("reason_code") != "insufficient_common_ground":
        return False
    message = str(getattr(ctx, "message", "") or "").strip()
    if re.search(r"不要|不用|不想|不願|不放寬|先別|別放寬|不可以|不同意|\b(?:no|not|don't|do not)\b", message, re.I):
        return False
    if re.search(
        r"放寬|寬一點|廣一點|不同方向|相近就好|不一定要完全|也可以認識|\b(?:broaden|widen|relax)\b.*\b(?:search|scope|criteria)\b",
        message, re.I,
    ):
        return True
    answer = message.strip(" \t\n。！!，,.～~")
    if not re.fullmatch(
        r"好|可以|可以啊|願意|確認|好啊|沒問題|那就這樣|ok(?:ay)?|yes(?: please)?|sure",
        answer, re.I,
    ):
        return False
    history = list(getattr(ctx, "recent_history", None) or [])
    if history and isinstance(history[-1], dict):
        latest = history[-1]
        owner_message = latest.get("sender_id") == ctx.user_id or latest.get("role") == "user"
        if owner_message and str(latest.get("content") or latest.get("message") or "").strip() == message:
            history.pop()
    if not history or not isinstance(history[-1], dict):
        return False
    prior = history[-1]
    if prior.get("room_id") and prior["room_id"] != ctx.room_id:
        return False
    is_assistant = (
        prior.get("sender_id") == "ai_assistant"
        if prior.get("sender_id") else prior.get("role") == "assistant"
    )
    if not is_assistant:
        return False
    # Do not let an old offer answer a later, completed search in another room.
    completed_at, offered_at = search.get("completed_at"), prior.get("timestamp")
    if isinstance(completed_at, (int, float)) and isinstance(offered_at, (int, float)) and offered_at < completed_at:
        return False
    content = str(prior.get("content") or prior.get("message") or "")
    return bool(re.search(r"要不要[^？?。\n]*放寬[^？?。\n]*搜尋", content))
