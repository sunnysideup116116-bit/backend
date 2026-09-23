"""Fresh write/search must agree on the fixed script-normalization contract.

This preserves the final-review finding that motivated the approved fresh-input fix.
The writer is real; its durable write is an in-memory echo. No Graph/provider I/O.
"""
from unittest.mock import patch

from models import ProfileMemoryAddRequest
from routers import system
from services.match_search_context import safe_search_context


def test_manual_preference_and_explicit_search_share_script_normalization():
    source = "安静的咖啡厅"
    request = ProfileMemoryAddRequest(
        user_id="synthetic-script-owner", label=source, stance="like",
        request_id="synthetic-script-normalization",
    )
    with patch(
        "services.memory_service.apply_profile_memory_proposals",
        side_effect=lambda _owner, proposals, *_args: proposals,
    ) as write:
        saved = system.add_profile_memory(request)["memory"]
    write.assert_called_once()
    query = safe_search_context({
        "search_intent": "preference", "normalized_topic": source,
        "query_text": "幫我找喜歡安静的咖啡厅的人",
    })
    assert saved["semantic_text"] == "安靜的咖啡廳"
    assert query["canonical_preference_key"] == saved["key"], (
        "Fresh manual preference and explicit search used different script-normalization "
        "inputs before the same canonicalizer; preserved P0 short-preference parity requires equality."
    )
