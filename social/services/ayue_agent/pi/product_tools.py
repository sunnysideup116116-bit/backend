"""Pi deterministic product-knowledge retrieval."""
from __future__ import annotations

from typing import Any

from services.ayue_agent.shared.product_info import retrieve_product_info


TOOL_NAMES = frozenset({"product.get_info"})
PROMPT = """【產品說明】
詢問本 App 功能、使用方式、隱私或能力邊界時先讀正式產品知識。
同時比較其他 App 時，再用 Web 查公開資料；自家能力與外部產品事實要分開引用。"""

SCHEMAS = ({
    "name": "product.get_info",
    "description": "讀取 App 功能、使用方式、隱私邊界、配對、行事曆、邀約或探索的正式產品說明。",
    "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string", "maxLength": 800}},
        "required": ["query"],
        "additionalProperties": False,
    },
},)


def handle(runtime: Any, _name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    query = str(arguments.get("query") or runtime.turn.message).strip()[:800]
    return runtime.project(name="product.get_info", status="ok", result=retrieve_product_info(query))
