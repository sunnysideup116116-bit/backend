"""One public product-truth contract shared by every public-Ayue stage."""

from __future__ import annotations

import re
from typing import Any


CAPABILITY_MANIFEST_VERSION = "v1"


CAPABILITY_MANIFEST: dict[str, Any] = {
    "version": CAPABILITY_MANIFEST_VERSION,
    "public_capabilities": [
        "陪你聊近況與想法",
        "記住你主動分享、可長期使用的偏好與限制",
        "查看、新增、修改及取消你自己的行程；共同約會的變動會同步通知對方，寫入前會先確認",
        "協助整理你想和對方聊的活動想法；目前不能代替雙方發出約會邀請或成立約會",
        "確認正式配對進度、對方是否接受與公開共同點",
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


def public_manifest() -> dict[str, Any]:
    """Return only static, non-sensitive product facts suitable for an LLM prompt."""
    return CAPABILITY_MANIFEST


def is_capability_query(message: str) -> bool:
    return bool(_ABILITY_QUERY_RE.search(message or ""))


def capability_answer() -> str:
    return (
        "我可以陪你聊近況、記住偏好、查看或整理自己的行程，也能確認牽線進度。"
        "需要時我也能查最新的公開資訊，或依地名找附近的餐廳、景點與直線距離。"
        "約會部分我目前能幫你整理想法，但不能替你直接向對方發邀請或答應。"
        "想找人時，我會依你的情境和個性挑選，不會隨機配；開始前一定先問你。"
    )


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
