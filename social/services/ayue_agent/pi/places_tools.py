"""Pi place search and verification tools."""
from __future__ import annotations

from typing import Any

from .domain_support import dispatch_registered


TOOL_NAMES = frozenset({
    "places.measure_distance",
    "places.resolve_place",
    "places.search_nearby",
})

PROMPT = """【場所】
Places 用於餐廳、咖啡店、景點、營業資訊、距離與具體店家核對，不用來搜尋活動、演出或展覽。
「第二間／最後一家」依使用者實際看見的 assistant 文案順序理解，再把具體名稱與地區交給工具核對。
不要建立或要求 opaque place reference，也不要把搜尋結果集中在哪個城市當成使用者位置。
同名店家無法靠名稱與地區唯一定位時要澄清，不可默選。"""

_CATEGORY_VALUES = [
    "restaurant", "cafe", "bar", "attraction", "park",
    "coffee", "coffee_shop", "餐廳", "咖啡", "咖啡店", "酒吧", "景點", "公園",
    "beverage", "bubble_tea", "飲料", "飲料店", "手搖飲", "手搖飲店", "茶飲店",
]
SCHEMAS = ({
    "name": "places.search_nearby",
    "description": (
        "查詢某地區附近的餐廳、咖啡店、小酌場所、景點或公園。"
        "優先使用 categories 與 anchor；category/location 是同義相容欄位，不能互相衝突。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "categories": {"type": "array", "items": {"type": "string", "enum": _CATEGORY_VALUES},
                           "minItems": 1, "maxItems": 3},
            "category": {"type": "string", "enum": _CATEGORY_VALUES},
            "anchor": {"type": "string", "maxLength": 160},
            "location": {"type": "string", "maxLength": 160},
            "cuisine": {"type": "string", "maxLength": 30},
            "radius_m": {"type": "integer", "minimum": 300, "maximum": 5000},
            "limit": {"type": "integer", "minimum": 1, "maximum": 8},
            "ordering": {"type": "string", "enum": ["distance", "balanced"]},
            "use_saved_location": {"type": "boolean"},
            "exclude_previously_presented": {"type": "boolean"},
            "enrichments": {"type": "array", "items": {"type": "string",
                                                           "enum": ["rating", "hours", "price", "walking"]},
                            "maxItems": 4},
        },
        "anyOf": [{"required": ["categories"]}, {"required": ["category"]}],
        "additionalProperties": False,
    },
},)

_CATEGORY_ALIASES = {
    "coffee": "cafe", "coffee_shop": "cafe", "咖啡": "cafe", "咖啡店": "cafe",
    "餐廳": "restaurant", "酒吧": "bar", "景點": "attraction", "公園": "park",
    "beverage": "cafe", "bubble_tea": "cafe", "飲料": "cafe", "飲料店": "cafe",
    "手搖飲": "cafe", "手搖飲店": "cafe", "茶飲店": "cafe",
}

_BEVERAGE_HINTS = {
    "beverage": "飲料店", "bubble_tea": "手搖飲", "飲料": "飲料店", "飲料店": "飲料店",
    "手搖飲": "手搖飲", "手搖飲店": "手搖飲", "茶飲店": "茶飲店",
}


def _normalize_nearby(arguments: dict[str, Any]) -> dict[str, Any]:
    values = dict(arguments)
    categories = values.pop("categories", None)
    category = values.pop("category", None)
    if categories is None and category is not None:
        categories = [category]
    if not isinstance(categories, list) or not categories:
        raise ValueError("categories_required")
    raw_categories = [str(item) for item in categories]
    canonical = [_CATEGORY_ALIASES.get(item, item) for item in raw_categories]
    if any(item not in {"restaurant", "cafe", "bar", "attraction", "park"} for item in canonical):
        raise ValueError("category_invalid")
    if not str(values.get("cuisine") or "").strip():
        beverage_hint = next(
            (_BEVERAGE_HINTS[item] for item in raw_categories if item in _BEVERAGE_HINTS),
            "",
        )
        if beverage_hint:
            values["cuisine"] = beverage_hint
    anchor = values.pop("anchor", None)
    location = values.pop("location", None)
    if anchor and location and str(anchor).strip() != str(location).strip():
        raise ValueError("anchor_conflict")
    values["categories"] = list(dict.fromkeys(canonical))
    if anchor or location:
        values["anchor"] = str(anchor or location).strip()
    return values


def handle(runtime: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    safe_arguments = _normalize_nearby(arguments) if name == "places.search_nearby" else arguments
    return dispatch_registered(runtime, name, safe_arguments)
