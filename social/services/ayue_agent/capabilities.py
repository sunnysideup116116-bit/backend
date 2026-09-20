"""One public product-truth contract shared by every public-Ayue stage."""

from __future__ import annotations

import copy
import re
from typing import Any

from services.ayue_agent.product_identity import AYUE_ROLE_LABEL, AYUE_SURFACE_IDENTITY, PUBLIC_CAPABILITY_REPLY


CAPABILITY_MANIFEST_VERSION = "v6"


CAPABILITY_MANIFEST: dict[str, Any] = {
    "version": CAPABILITY_MANIFEST_VERSION,
    "identity": {
        "role": f"本交友 App 內的 AI 媒人{AYUE_ROLE_LABEL}",
        "product_relation": "阿月協助使用者認識人、整理互動與牽線；她不是另一位使用者，也不把目前 App 當成外部服務。",
        "conversation_rule": "使用者說這個 App、交友軟體或在這裡找人時，預設是在說目前產品；阿月可以仍在認識使用者，但不能遺忘自己的媒人角色。",
        "surface_model": AYUE_SURFACE_IDENTITY,
    },
    "surfaces": {
        "public": {
            "identity": "same_ayue",
            "scope": "owner_self_and_new_relationships",
            "can_read_current_pair_chat": False,
            "can": ["聊本人近況", "一般感情建議", "探索新的人選"],
            "does_not_read": ["你和對方的雙人聊天室聊天紀錄"],
        },
        "private": {
            "identity": "same_ayue",
            "scope": "current_accepted_relationship",
            "requires": "accepted_relationship",
            "can_read_current_pair_chat": True,
            "uses": ["有限的本人 profile projection", "目前這個雙人聊天室的 bounded recent shared history", "本人私下脈絡與已同意的 facts", "busy-free projection"],
            "cannot": ["開始新的配對搜尋", "讀取完整主聊天室", "未經確認把私訊發給對方"],
        },
        "context_boundary": {
            "owner_profile_projection": "bounded_one_way",
            "raw_full_chat_cross_surface": False,
            "public_pair_chat_history": "not_available",
            "private_current_pair_chat_history": "bounded_room_scoped",
            "private_messages_update_public_profile": False,
            "confirmed_effects": "follow_existing_domain_confirmation",
        },
    },
    "public_capabilities": [
        "陪你聊近況與想法",
        "記住你主動分享、可長期使用的偏好與限制",
        "在聊天室重新進行基本性格或深層價值觀探索；中途可以隨時結束，完成後還會再確認才套用新結果",
        "查看、新增、修改及取消你自己的行程；共同約會的變動會同步通知對方，寫入前會先確認",
        "可以在你明確指定一位已建立聯絡的對象後，先向你確認，再在你們聊天室建立一張空白約會邀請卡；不代填日期、時間、地點或活動，也不代表對方已接受",
        "確認正式配對進度、對方是否接受與公開共同點，並從已接受聯絡人中整理適合一起參與活動的人選",
        "依既有資料幫你尋找合適的人選；阿月牽線會保留多張人物、主題或活動卡，方便你逐張查看",
        "查詢最新公開資訊並附上來源",
        "依地名查附近餐廳、咖啡廳、景點或公園，以及估算兩地直線距離",
    ],
    "matching": {
        "methods": [
            "依你最近分享的近況找人",
            "依你直接指定的活動或主題找人",
            "阿月發現適合的活動時，問你要不要認識活動伴",
        ],
        "selection": "ranked_not_random",
        "signals": ["近期情境", "偏好與限制", "價值觀", "個性"],
        "uses_existing_profile_when_no_new_preferences": True,
        "requires_confirmation": True,
        "may_return_no_suitable_candidate": True,
    },
    "relationship": {
        "date_invitation": {
            "available_in_public_ayue": True,
            "requires_accepted_contact": True,
            "requires_confirmation": True,
            "creates_empty_card_only": True,
            "waits_for_partner_acceptance": True,
            "participants_fill_details_later": True,
        },
        # Keep card placement and the optional entry point explicit.  This is
        # a separate projection so old clients that compare the original
        # date_invitation object byte-for-byte remain compatible.
        "date_card_surface": {
            "card_surface": "accepted_pair_chat",
            "matching_page_shortcut": "needs_action_only",
            "available_in_match_hub": False,
            "can_cancel_from_public_ayue": True,
            "cancel_requires_confirmation": True,
        },
        "date_coordination_cancel": {
            "available_in_public_ayue": True,
            "requires_server_referenced_date_card": True,
            "requires_confirmation": True,
            "checks_current_status_and_revision": True,
            "calendar_event_cancellation_is_synchronized": True,
        },
    },
    "terminology": {
        "preferred": ["對象", "人選", "對方", "旅伴"],
        "forbidden": ["物件", "配對物件", "候選物件"],
    },
}


# ProductInfo uses a small, server-owned knowledge base instead of treating the
# manifest as a collection of ready-to-send FAQ answers.  Keep this index
# deliberately small and typed: adding a section is a product-contract change,
# not a model decision.  The values below are projections of facts already
# enforced by the public runtime (matching confirmation, bounded surfaces and
# assessment/calendar confirmation); unknown behaviour is intentionally absent.
PRODUCT_KNOWLEDGE_VERSION = "product_info.v1"

_PRODUCT_KNOWLEDGE_SECTIONS: dict[str, dict[str, Any]] = {
    "capabilities.overview": {
        "domain": "capabilities",
        "facts": {
            "capabilities": copy.deepcopy(CAPABILITY_MANIFEST["public_capabilities"]),
        },
    },
    "identity.overview": {
        "domain": "identity",
        "facts": {
            "role": CAPABILITY_MANIFEST["identity"]["role"],
            "product_relation": CAPABILITY_MANIFEST["identity"]["product_relation"],
            "same_ayue_across_surfaces": True,
        },
    },
    "surfaces.public": {
        "domain": "surfaces",
        "facts": copy.deepcopy(CAPABILITY_MANIFEST["surfaces"]["public"]),
    },
    "surfaces.private": {
        "domain": "surfaces",
        "facts": copy.deepcopy(CAPABILITY_MANIFEST["surfaces"]["private"]),
    },
    "surfaces.context_boundary": {
        "domain": "privacy",
        "facts": copy.deepcopy(CAPABILITY_MANIFEST["surfaces"]["context_boundary"]),
    },
    "matching.overview": {
        "domain": "matching",
        "facts": {
            "methods": copy.deepcopy(CAPABILITY_MANIFEST["matching"]["methods"]),
            "selection": CAPABILITY_MANIFEST["matching"]["selection"],
            "may_return_no_suitable_candidate": CAPABILITY_MANIFEST["matching"]["may_return_no_suitable_candidate"],
        },
    },
    "matching.methods": {
        "domain": "matching",
        "facts": {
            "methods": copy.deepcopy(CAPABILITY_MANIFEST["matching"]["methods"]),
            "requires_confirmation": CAPABILITY_MANIFEST["matching"]["requires_confirmation"],
        },
    },
    "matching.selection": {
        "domain": "matching",
        "facts": {
            "selection": CAPABILITY_MANIFEST["matching"]["selection"],
            "signals": copy.deepcopy(CAPABILITY_MANIFEST["matching"]["signals"]),
        },
    },
    "matching.profile_usage": {
        "domain": "matching",
        "facts": {
            "uses_existing_profile_when_no_new_preferences": CAPABILITY_MANIFEST["matching"]["uses_existing_profile_when_no_new_preferences"],
            "profile_is_a_matching_signal": True,
        },
    },
    "matching.confirmation": {
        "domain": "matching",
        "facts": {
            "requires_confirmation": CAPABILITY_MANIFEST["matching"]["requires_confirmation"],
            "search_does_not_start_before_confirmation": True,
        },
    },
    "matching.search_flow": {
        "domain": "matching",
        "facts": {
            "requires_confirmation": CAPABILITY_MANIFEST["matching"]["requires_confirmation"],
            "may_return_no_suitable_candidate": CAPABILITY_MANIFEST["matching"]["may_return_no_suitable_candidate"],
        },
    },
    "matching.inbox": {
        "domain": "matching",
        "facts": {
            "multi_card_inbox": True,
            "accept_decline_withdraw_on_hub_cards": True,
            "waiting_invitations_do_not_block_new_search": True,
            "replace_card_requires_explicit_card_action": True,
            "cancel_search_only_stops_queued_or_running_search": True,
        },
    },
    "matching.limitations": {
        "domain": "matching",
        "facts": {
            "may_return_no_suitable_candidate": CAPABILITY_MANIFEST["matching"]["may_return_no_suitable_candidate"],
            "not_random": CAPABILITY_MANIFEST["matching"]["selection"] == "ranked_not_random",
        },
    },
    "relationship.date_invitation": {
        "domain": "relationship",
        "facts": {
            **copy.deepcopy(CAPABILITY_MANIFEST["relationship"]["date_invitation"]),
            **copy.deepcopy(CAPABILITY_MANIFEST["relationship"]["date_card_surface"]),
        },
    },
    "relationship.date_card_surface": {
        "domain": "relationship",
        "facts": copy.deepcopy(CAPABILITY_MANIFEST["relationship"]["date_card_surface"]),
    },
    "relationship.date_coordination_cancel": {
        "domain": "relationship",
        "facts": {
            "available_in_public_ayue": True,
            "requires_server_referenced_date_card": True,
            "requires_confirmation": True,
            "checks_current_status_and_revision": True,
            "calendar_event_cancellation_is_synchronized": True,
        },
    },
    "calendar.confirmation": {
        "domain": "calendar",
        "facts": {
            "writes_require_confirmation": True,
            "confirmation_is_preview_bound": True,
        },
    },
    "assessment.overview": {
        "domain": "assessment",
        "facts": {
            "available_kinds": ["basic", "deep"],
            "starts_only_after_confirmation": True,
        },
    },
    "assessment.lifecycle": {
        "domain": "assessment",
        "facts": {
            "supports_cancel": True,
            "supports_commit": True,
            "starts_only_after_confirmation": True,
        },
    },
    "privacy.relationship": {
        "domain": "privacy",
        "facts": {
            "public_pair_chat_history": CAPABILITY_MANIFEST["surfaces"]["context_boundary"]["public_pair_chat_history"],
            "private_current_pair_chat_history": CAPABILITY_MANIFEST["surfaces"]["context_boundary"]["private_current_pair_chat_history"],
            "private_messages_update_public_profile": CAPABILITY_MANIFEST["surfaces"]["context_boundary"]["private_messages_update_public_profile"],
        },
    },
}


def product_knowledge_catalog() -> list[dict[str, str]]:
    """Return a compact, safe catalog for ProductInfo's internal selector."""
    return [
        {
            "section_id": section_id,
            "domain": str(section.get("domain") or ""),
        }
        for section_id, section in _PRODUCT_KNOWLEDGE_SECTIONS.items()
    ]


def get_product_knowledge(
    sections: list[str] | tuple[str, ...] | None = None,
    *,
    max_sections: int = 6,
) -> dict[str, Any]:
    """Retrieve only allowlisted product facts for a ProductInfo sub-agent.

    This is a pure read projection.  Invalid section IDs are reported rather
    than guessed, so the Synthesizer can explain an ``insufficient`` result.
    """
    requested = list(dict.fromkeys(str(item).strip() for item in (sections or []) if str(item).strip()))
    requested = requested[: max(0, min(int(max_sections), 8))]
    unknown = [section_id for section_id in requested if section_id not in _PRODUCT_KNOWLEDGE_SECTIONS]
    found: list[dict[str, Any]] = []
    for section_id in requested:
        section = _PRODUCT_KNOWLEDGE_SECTIONS.get(section_id)
        if section is None:
            continue
        found.append({
            "section_id": section_id,
            "domain": section["domain"],
            "facts": copy.deepcopy(section["facts"]),
        })
    if not requested:
        coverage = "insufficient"
    elif unknown or not found:
        coverage = "insufficient"
    else:
        coverage = "sufficient"
    return {
        "schema_version": PRODUCT_KNOWLEDGE_VERSION,
        "requested_sections": requested,
        "knowledge_sections": found,
        "unknown_sections": unknown,
        "coverage": coverage,
    }


def public_manifest() -> dict[str, Any]:
    """Return only static, non-sensitive product facts suitable for an LLM prompt."""
    return CAPABILITY_MANIFEST


def capability_answer() -> str:
    return PUBLIC_CAPABILITY_REPLY


PRODUCT_INFO_FAILURE_FALLBACKS: dict[str, str] = {
    "same_identity": "都是我啦。主聊天室和雙人聊天室裡的阿月悄悄話，是同一位阿月，不是兩個 AI 或兩種人格。",
    "surface_scope": "主聊天室主要陪你處理自己的近況、行程、一般感情問題和認識新的人；進到某個人的聊天室後，我會切到只專注你們這段關係的悄悄話模式。",
    "cross_surface_context": "兩邊不會把完整對話互相搬來搬去。每個入口只會使用當下產品流程允許的有限脈絡。",
    "where_to_ask": "一般、不依賴特定聊天紀錄的感情或第一次約會問題，在主聊天室直接問我就可以；如果要判斷某個人剛說的話、你們最近的節奏或下一句怎麼回，到你們聊天室裡的悄悄話問我會比較準。",
    "relationship_chat_access": "主聊天室的我看不到你和對方的聊天紀錄；進到你們的雙人聊天室後，阿月悄悄話能使用那段關係允許的近期聊天脈絡。要問某句怎麼回或最近聊得怎樣，去那裡問會比較準。",
    "private_message_visibility": "你在阿月悄悄話裡說的內容不會直接顯示給對方；只有你明確確認會影響對方的動作時，才會照那個流程通知或更新對方。",
    "matching_principles": (
        "你可以叫我依最近的近況找人，也可以直接說想找人一起做什麼，像滑雪、攝影；"
        "我偶爾也會找到適合的活動，問你要不要認識活動伴。"
        "挑選時我會綜合你的近況、偏好與限制、價值觀和個性做排序，不會隨機配對；"
        "真的開始搜尋前，我會先跟你確認。"
    ),
    "matching_methods": (
        "配對可以依最近的近況找，也可以直接指定想一起做的活動或主題；"
        "如果我發現適合的活動，也會問你要不要認識活動伴。真的開始搜尋前，我會先跟你確認。"
    ),
    "date_invitation": (
        "沒錯喔～你指定一位已建立聯絡的對象後，我會先請你確認；確認後只會在你們的雙人聊天室放一張空白邀請卡，"
        "配對首頁只會在需要你處理時提供入口，不會放進「阿月牽線」；等對方接受，再由你們一起填寫約會資料，"
        "等雙方都確認完成後，就會同步到行事曆囉～"
    ),
    "date_coordination_cancel": (
        "可以喔～約會卡會放在你和對方的雙人聊天室；配對首頁只會在需要你處理時提供入口。"
        "如果要取消，我會先確認是哪一張、重新檢查目前狀態，再請你確認。"
        "若已同步到行事曆，取消時也會同步更新雙方行事曆。"
    ),
}


def product_info_answer(topics: list[str] | None = None) -> list[str]:
    """Return bounded product-truth copy only for provider/safety fallback."""
    selected = topics or ["capabilities"]
    replies: list[str] = []
    if "capabilities" in selected:
        replies.append(capability_answer())
    topic_aliases = {
        "matching.overview": "matching_principles",
        "matching.methods": "matching_methods",
        "matching.selection": "matching_principles",
        "relationship.date_coordination_cancel": "date_coordination_cancel",
    }
    seen_fallbacks: set[str] = set()
    for topic in selected:
        fallback_key = topic_aliases.get(topic, topic)
        if fallback_key in PRODUCT_INFO_FAILURE_FALLBACKS and fallback_key not in seen_fallbacks:
            replies.append(PRODUCT_INFO_FAILURE_FALLBACKS[fallback_key])
            seen_fallbacks.add(fallback_key)
    if not replies:
        replies = [AYUE_SURFACE_IDENTITY]
    return replies[:2]


def product_info_projection(topics: list[str] | None = None) -> dict[str, Any]:
    """Project only the typed product facts needed for this turn.

    The projection is grounding for the Synthesizer, not finished user-facing
    prose.  This keeps product truth authoritative without forcing every user
    question through the same canned sentence or sending the full manifest on
    every turn.
    """
    selected = list(dict.fromkeys(topics or ["capabilities"]))[:3]
    facts: dict[str, Any] = {}
    if "capabilities" in selected:
        facts["capabilities"] = list(CAPABILITY_MANIFEST["public_capabilities"])
    if "same_identity" in selected:
        facts["identity"] = {
            "same_ayue": True,
            "public_surface": "主聊天室",
            "private_surface": "雙人聊天室中的阿月悄悄話",
            "not_separate_personalities": True,
        }
    if "surface_scope" in selected:
        facts["surface_scope"] = {
            "public": CAPABILITY_MANIFEST["surfaces"]["public"]["scope"],
            "public_can": list(CAPABILITY_MANIFEST["surfaces"]["public"]["can"]),
            "private": CAPABILITY_MANIFEST["surfaces"]["private"]["scope"],
            "private_uses": list(CAPABILITY_MANIFEST["surfaces"]["private"]["uses"]),
        }
    if "cross_surface_context" in selected:
        facts["context_boundary"] = dict(CAPABILITY_MANIFEST["surfaces"]["context_boundary"])
    if "private_message_visibility" in selected:
        facts["private_message_visibility"] = {
            "directly_visible_to_other": False,
            "confirmed_effects": CAPABILITY_MANIFEST["surfaces"]["context_boundary"]["confirmed_effects"],
        }
    if "where_to_ask" in selected:
        facts["where_to_ask"] = {
            "public": "自己的近況、行程、不依賴特定聊天紀錄的一般感情問題、認識新的人",
            "private": "目前已接受對象的近期聊天紀錄、互動理解、聊天建議與關係規劃",
        }
    if "relationship_chat_access" in selected:
        facts["relationship_chat_access"] = {
            "public_can_read_current_pair_chat": CAPABILITY_MANIFEST["surfaces"]["public"]["can_read_current_pair_chat"],
            "private_can_read_current_pair_chat": CAPABILITY_MANIFEST["surfaces"]["private"]["can_read_current_pair_chat"],
            "private_history_scope": CAPABILITY_MANIFEST["surfaces"]["context_boundary"]["private_current_pair_chat_history"],
            "use_private_for_contextual_chat_advice": True,
        }
    if "matching_principles" in selected:
        facts["matching"] = dict(CAPABILITY_MANIFEST["matching"])
    if "date_invitation" in selected:
        facts["date_invitation"] = copy.deepcopy(
            CAPABILITY_MANIFEST["relationship"]["date_invitation"]
        )
        facts["date_card_surface"] = copy.deepcopy(
            CAPABILITY_MANIFEST["relationship"]["date_card_surface"]
        )
    if "date_coordination_cancel" in selected:
        facts["date_coordination_cancel"] = copy.deepcopy(
            CAPABILITY_MANIFEST["relationship"].get("date_coordination_cancel", {
                "available_in_public_ayue": True,
                "requires_server_referenced_date_card": True,
                "requires_confirmation": True,
            })
        )
    return {
        "manifest_version": CAPABILITY_MANIFEST_VERSION,
        "topics": selected,
        "facts": facts,
    }


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
