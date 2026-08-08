"""One public product-truth contract shared by every public-Ayue stage."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from services.ayue_agent.product_identity import AYUE_ROLE_LABEL, PUBLIC_CAPABILITY_REPLY


CAPABILITY_MANIFEST_VERSION = "v1"


CAPABILITY_MANIFEST: dict[str, Any] = {
    "version": CAPABILITY_MANIFEST_VERSION,
    "identity": {
        "role": f"本交友 App 內的 AI 媒人{AYUE_ROLE_LABEL}",
        "product_relation": "阿月協助使用者認識人、整理互動與牽線；她不是另一位使用者，也不把目前 App 當成外部服務。",
        "conversation_rule": "使用者說這個 App、交友軟體或在這裡找人時，預設是在說目前產品；阿月可以仍在認識使用者，但不能遺忘自己的媒人角色。",
    },
    "public_capabilities": [
        "陪你聊近況與想法",
        "記住你主動分享、可長期使用的偏好與限制",
        "在聊天室重新進行基本性格或深層價值觀探索；中途可以隨時結束，完成後還會再確認才套用新結果",
        "查看、新增、修改及取消你自己的行程；共同約會的變動會同步通知對方，寫入前會先確認",
        "協助整理你想和對方聊的活動想法；目前不能代替雙方發出約會邀請或成立約會",
        "確認正式配對進度、對方是否接受與公開共同點，並從已接受聯絡人中整理適合一起參與活動的人選",
        "依既有資料幫你尋找合適的人選",
        "查詢最新公開資訊並附上來源",
        "依地名查附近餐廳、咖啡廳、景點或公園，以及估算兩地直線距離",
    ],
    "matching": {
        "selection": "ranked_not_random",
        "signals": ["近期情境", "偏好與限制", "價值觀", "個性"],
        "uses_existing_profile_when_no_new_preferences": True,
        "requires_confirmation": True,
        "may_return_no_suitable_candidate": True,
    },
    "terminology": {
        "preferred": ["對象", "人選", "對方", "旅伴"],
        "forbidden": ["物件", "配對物件", "候選物件"],
    },
}


_ABILITY_QUERY_RE = re.compile(
    r"(?:你(?:有什麼|能|可以).{0,6}(?:能力|功能|做什麼|幹嘛)|你會(?:什麼|做什麼)|有什麼工具)",
)

_INVISIBLE_QUERY_CHARS = frozenset({"\u200b", "\u2060", "\ufeff"})


def _normalize_capability_query(message: str) -> str:
    text = unicodedata.normalize("NFKC", str(message or ""))
    text = "".join(char for char in text if char not in _INVISIBLE_QUERY_CHARS)
    return re.sub(r"\s+", " ", text).strip()


def public_manifest() -> dict[str, Any]:
    """Return only static, non-sensitive product facts suitable for an LLM prompt."""
    return CAPABILITY_MANIFEST


def is_capability_query(message: str) -> bool:
    return bool(_ABILITY_QUERY_RE.search(_normalize_capability_query(message)))


def capability_answer() -> str:
    return PUBLIC_CAPABILITY_REPLY


def normalize_public_language(reply: str) -> str:
    """Keep person-first terminology even when a provider drifts in wording."""
    text = str(reply or "")
    for forbidden in CAPABILITY_MANIFEST["terminology"]["forbidden"]:
        text = text.replace(forbidden, "對象")
    return text


def contains_unsupported_random_match_claim(reply: str) -> bool:
    """Reject an assistant claim that contradicts ranked matching product truth."""
    text = normalize_public_language(reply)
    if "隨機" not in text:
        return False
    if re.search(r"(?:不會|不是|並非|絕不).{0,4}隨機.{0,5}(?:配|找|媒合)", text):
        return False
    return bool(re.search(r"隨機.{0,6}(?:配|找|媒合)|(?:配|找|媒合).{0,6}隨機", text))


def matching_truth_reply() -> str:
    return (
        "我不會隨機配對。就算你暫時沒有新條件，我也會先依你已經分享的近期情境、偏好、價值觀和個性，"
        "找有根據的人選；這輪沒有足夠合適的人，我會直接跟你說。"
    )
