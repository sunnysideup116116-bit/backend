"""Canonical, role-bound match-reason projections.

This module is intentionally transport- and provider-neutral.  Match creation
may ask an LLM to phrase each direction, but every public reader enters this
module to select exactly one reason for its authenticated viewer.
"""

from __future__ import annotations

import re
import hashlib
from typing import Any

from services.language_service import normalize_zh_tw


V4_REASON_VERSION = "v4_friend_intro"
FRIEND_COPY_VERSION = "v8_source_directional"
LIVE_PROPOSAL_STATUSES = frozenset({"draft", "pending"})
COUNTERPARTY_PLACEHOLDER = "{{counterparty}}"
MATCH_PROPOSAL_FEW_SHOTS = (
    "欸，我想到一個你可能會想認識的人。他最近想找人參加〔近期活動〕，個性〔對方性格〕；你〔本人性格〕，或許能形成舒服的互動節奏。想讓我幫你們牽個線嗎？",
    "我腦中突然有個畫面：〔活動中的自然互動畫面〕。他〔對方性格〕、你〔本人性格〕，感覺不用硬找話題也能相處得不錯。你會想認識看看嗎？",
    "我這裡有一位感覺可以介紹給你。他最近〔近期情境〕，個性〔對方性格〕；照你的互動方式，可能滿容易和他進入狀況的。要不要讓我問問他？",
    "有個人的近況讓我想到你。他〔對方性格〕，最近又想〔近期活動〕；你的〔本人性格〕，至少和這個情境有一個具體話題。你想認識看看嗎？",
    "等等，我想到一位值得認識看看的人 👀 他〔對方性格〕，最近想〔近期活動〕；你們可能有具體話題可以聊，但不保證一定合拍。要不要讓阿月先問問？",
)
MATCH_PROPOSAL_STYLE_IDS = (
    "warm_intro",
    "scene_bridge",
    "direct_intro",
    "context_hook",
    "playful_intro",
)
PRIVATE_OPENING_FEW_SHOTS = (
    "好消息，{{counterparty}}也點頭了！你可以先從〔對方近期情境〕聊起，問問他最期待哪一部分，應該很好接話。",
    "你跟{{counterparty}}正式認識啦～不知道怎麼開口的話，可以先問〔具體但輕鬆的開場問題〕。你們一個〔性格 A〕、一個〔性格 B〕，輕鬆問一句就很夠了。",
    "好啦，人我幫你介紹到了，接下來換你去跟{{counterparty}}打聲招呼 😌 可以先聊〔近期活動〕，不用一開始就問得太正式。",
    "{{counterparty}}也願意認識你！你最近〔本人情境〕，他最近〔對方情境〕，剛好可以先聊聊你們各自喜歡怎麼安排這類活動。",
    "成功牽上啦 ✦ {{counterparty}}已經在那邊了，快去說聲嗨！可以先問：『〔依近期情境產生的短問題〕』，剩下的正常聊天就好。",
)
_UNSAFE_TOKENS = (
    "seed_user", "user_id", "mongo", "資料庫", "物件", "已答應", "已同意",
    "帳號", "email", "電話", "住址",
)
_UNVERIFIED_DETAIL_RE = re.compile(
    r"(?:名叫|名字是|姓名是|住在|任職|工作於|就讀|\d+\s*歲|電話|帳號|地址|"
    r"已經答應|已經同意|(?:也|還)(?:很)?(?:喜歡|熱愛|常去|想去))",
    re.IGNORECASE,
)
_INTERNAL_REFERENCE_RE = re.compile(
    r"(?:@?seed_user_[\w-]+|@?demo_user|@?user[_-]?\d+)", re.IGNORECASE,
)

# This is a copy-contract check, rather than an intent router.  The match
# pipeline already decided that a proposal is an invitation; this helper only
# verifies that provider prose still contains a natural invitation question.
# Keep the accepted forms together so generation, fallback and read-time
# projection cannot drift apart (notably 「想先認識」 and 「願不願意先認識」).
_INVITATION_ASK_RE = re.compile(
    r"(?:"
    r"想\s*(?:先\s*)?認識"
    r"|願意\s*(?:先\s*)?認識"
    r"|願不願意\s*(?:先\s*)?認識"
    r"|要不要\s*(?:先\s*)?認識"
    r"|有興趣"
    r"|想\s*(?:讓我|請我)?\s*(?:幫你(?:們)?\s*)?(?:問問?|牽線|牽個線)"
    r"|要不要\s*(?:讓我|請我)?\s*(?:幫你(?:們)?\s*)?(?:問問?|牽線|牽個線)"
    r"|要我\s*(?:先\s*)?(?:幫你(?:們)?\s*)?(?:問問?|牽線|牽個線)"
    r"|想\s*(?:跟|和)\s*對方\s*(?:聊|認識)"
    r")",
    re.IGNORECASE,
)


def short_public_text(value: Any, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", normalize_zh_tw(str(value or ""))).strip()
    text = _INTERNAL_REFERENCE_RE.sub("對方", text)
    return text[:limit].rstrip()


def public_personality_phrase(profile: dict) -> str:
    """Return one public, non-clinical personality phrase."""
    stored = short_public_text(profile.get("public_personality"), 32)
    if stored:
        return stored
    traits = profile.get("big_five") or {}
    value = traits.get("E")
    if isinstance(value, (int, float)):
        if value >= 6:
            return "比較外向、容易帶起話題"
        if value <= 4:
            return "偏安靜、重視舒服節奏"
        return "互動節奏自然"
    value = traits.get("O")
    if isinstance(value, (int, float)) and value >= 7:
        return "願意嘗試新點子"
    value = traits.get("A")
    if isinstance(value, (int, float)) and value >= 7:
        return "願意傾聽"
    return short_public_text(traits.get("summary"), 32)


def contains_invitation_ask(value: Any) -> bool:
    """Return whether copy contains one approved invitation ask shape."""
    text = short_public_text(value, 260)
    return bool(text and _INVITATION_ASK_RE.search(text))


def contains_unverified_topic_claim(value: Any) -> bool:
    """Reject topic prose that upgrades a search request into another user's fact."""
    text = short_public_text(value, 260)
    return any(token in text for token in (
        "有興趣", "感興趣", "喜歡", "熱愛", "熟悉", "擅長",
        "已答應", "已經答應", "已同意", "已經同意",
        "可以聊看看", "可以開始聊", "現在可以聊", "已經可以聊",
    ))


def match_reason_style_id(
    viewer: dict | str,
    other: dict | str,
    *,
    context_revision: str = "",
) -> str:
    """Pick one approved copy style deterministically for this direction.

    The selector is intentionally hash-based rather than random: the same
    proposal/viewer keeps its style across reads, while different pairs do
    not all fall back to the first few-shot example.
    """
    viewer_id = str((viewer.get("user_id") if isinstance(viewer, dict) else viewer) or "")
    other_id = str((other.get("user_id") if isinstance(other, dict) else other) or "")
    revision = str(context_revision or "")
    digest = hashlib.sha256(f"{viewer_id}|{other_id}|{revision}".encode("utf-8")).digest()
    return MATCH_PROPOSAL_STYLE_IDS[int.from_bytes(digest[:4], "big") % len(MATCH_PROPOSAL_STYLE_IDS)]


def friend_intro_fallback(
    viewer: dict, other: dict, tier: str, *, style_id: str | None = None,
) -> dict:
    """Build a complete deterministic invitation without truncating its ask."""
    other_context = short_public_text(other.get("current_context"), 56)
    viewer_trait = public_personality_phrase(viewer)
    other_trait = public_personality_phrase(other)

    style_id = style_id if style_id in MATCH_PROPOSAL_STYLE_IDS else match_reason_style_id(viewer, other)
    if style_id == "scene_bridge":
        opening = "我腦中突然有個畫面："
    elif style_id == "direct_intro":
        opening = "我這裡有一位感覺可以介紹給你。"
    elif style_id == "context_hook":
        opening = "有個人的近況讓我想到你。"
    elif style_id == "playful_intro":
        opening = "等等，我想到一位值得認識看看的人 👀"
    else:
        opening = "欸，我想到一個你可能會想認識的人。"

    if other_context and other_trait:
        first = f"{opening}對方{other_trait}，最近提到「{other_context}」。"
    elif other_context:
        first = f"{opening}對方最近提到「{other_context}」。"
    elif other_trait:
        first = f"{opening}對方{other_trait}，最近還沒有公開明確的活動規劃。"
    else:
        first = f"{opening}對方最近還沒有公開明確的活動規劃。"

    if viewer_trait and other_trait:
        second = (
            f"你{viewer_trait}，你們至少有一個具體話題可以自然聊起來；"
            "你會想先認識對方，聊聊這件事嗎？"
        )
    elif viewer_trait:
        second = (
            f"你{viewer_trait}，或許可以先從這件事聊聊彼此的節奏；"
            "你會想先認識對方，聊聊這件事嗎？"
        )
    elif other_trait:
        second = (
            f"對方{other_trait}，你們或許可以先從這件事自然聊起；"
            "你會想先認識對方，聊聊這件事嗎？"
        )
    elif tier == "grounded":
        second = "你們可以先從已確認的共同點自然聊起；你會想認識對方嗎？"
    else:
        second = "你們或許可以先交換最近想做的事；你會想先認識對方嗎？"

    return {
        "style_id": style_id,
        "tier": tier,
        "viewer_text": f"{first}{second}",
        "scenario_bridge": other_context,
        "personality_dynamic": "互補" if viewer_trait and other_trait else "",
        "conversation_starter": (
            f"可以先問對方最近那件「{other_context}」最吸引人的地方。"
            if other_context else "可以先從最近想做的事聊起。"
        ),
        "accepted_opening": (
            f"好消息，{COUNTERPARTY_PLACEHOLDER}也點頭了！可以先從「{other_context}」聊起，"
            "問問對方最期待哪一部分，輕鬆打聲招呼就好。"
            if other_context else
            f"好消息，{COUNTERPARTY_PLACEHOLDER}也點頭了！先自然打聲招呼，聊聊最近想做的事就好。"
        ),
        "used_evidence_keys": [
            key for key, value in (
                ("other.current_context", other_context),
                ("viewer.big_five", viewer_trait),
                ("other.big_five", other_trait),
            ) if value
        ],
    }


def topic_friend_intro_fallback(
    viewer: dict,
    other: dict,
    topic: str,
    *,
    requester_id: str = "",
    query_text: str = "",
    auto_invite: bool = False,
) -> dict:
    """Role-specific copy for a one-search topic request.

    The topic belongs to the requester.  A receiver must therefore see it as
    another person's wish, while the requester sees it as their own request;
    neither direction claims that the other person has the skill or has agreed
    to attend.
    """
    safe_topic = short_public_text(topic, 80)
    # ``invitation_topic`` is intentionally a compact label (for example,
    # ``滑雪``), while the requester's meaning is usually ``找人一起滑雪``.
    # Keep that purpose in the copy so the card does not turn an activity ask
    # into the vague phrase ``找人聊聊滑雪``.  Prefer the original query when
    # it contains the user's own together/companion wording.
    query = short_public_text(query_text, 600)
    activity_phrase = ""
    for prefix in ("一起", "陪我", "跟我", "和我"):
        candidate = f"{prefix}{safe_topic}"
        if candidate and candidate in query:
            activity_phrase = candidate
            break
    if not activity_phrase:
        activity_phrase = safe_topic
        if activity_phrase and not activity_phrase.startswith(("一起", "陪我", "跟我", "和我")):
            activity_phrase = f"一起{activity_phrase}"
    is_requester = str(viewer.get("user_id") or "") == str(requester_id or "")
    other_trait = public_personality_phrase(other)
    basis = (
        f"推薦說明：對方{other_trait}；可先了解相處節奏，活動意願仍待確認。"
        if other_trait else
        "推薦說明：這次是探索性介紹，目前沒有足夠共同活動依據，仍需先確認彼此意願。"
    )
    if is_requester:
        first = f"你這次想找人{activity_phrase}；我找到一位可以替你詢問的人選。"
        text = first + (
            "邀請已送出，正在等對方回覆；雙方接受後才能聊天。"
            if auto_invite else
            "還不能確認對方是否願意同行。你願意先認識對方嗎？"
        )
    else:
        text = (
            f"有位朋友想找人{activity_phrase}，並邀請你先認識看看。"
            "目前還沒有替你答應認識或同行；你願意先認識對方嗎？"
        )
    return {
        "style_id": "topic_request",
        "tier": "exploratory",
        "viewer_text": basis + text,
        "recommendation_basis": basis,
        "scenario_bridge": activity_phrase,
        "personality_dynamic": "",
        "conversation_starter": f"配對成功後，可以先問對方想怎麼安排{activity_phrase}。",
        "accepted_opening": f"好消息，{COUNTERPARTY_PLACEHOLDER}也願意認識你！可以先聊聊{activity_phrase}的想法，再決定要不要同行。",
        "used_evidence_keys": [
            "search_context.invitation_topic",
        ],
    }


def valid_friend_intro_text(
    value: Any, *, required_context: str = "", introduced_personality: str = "",
    viewer_personality: str = "", role_bound: bool = False,
) -> str:
    """Validate provider prose against the public evidence supplied to it."""
    text = short_public_text(value, 220)
    lowered = text.casefold()
    if not text:
        return ""
    # The five approved style examples deliberately use different openings
    # (for example, 「欸，我想到…」 and 「我腦中有個畫面…」).  Requiring one
    # literal prefix here made valid model prose fall back to the old fixed
    # template.  Keep the privacy guarantee by requiring a neutral person
    # reference, while allowing the phrasing itself to vary.
    neutral_intro = text[:72]
    if not role_bound and not any(token in neutral_intro for token in (
        "有位", "有一位", "有個", "一個人", "一位", "人選", "介紹給你", "想到一個",
    )):
        return ""
    if any(token.casefold() in lowered for token in _UNSAFE_TOKENS):
        return ""
    detail_scan = text.replace(f"「{required_context}」", "") if required_context else text
    if _UNVERIFIED_DETAIL_RE.search(detail_scan) or "@" in text:
        return ""
    if required_context and required_context not in text:
        return ""
    if introduced_personality and introduced_personality not in text:
        return ""
    if viewer_personality and viewer_personality not in text:
        return ""
    quoted = re.findall(r"「([^」]+)」", text)
    if any(not required_context or quote != required_context for quote in quoted):
        return ""
    if not role_bound and not any(token in text for token in ("可能", "或許", "可以", "感覺")):
        return ""
    if not text.endswith(("？", "?")):
        return ""
    if not contains_invitation_ask(text):
        return ""
    return text


def valid_accepted_opening_text(
    value: Any, *, required_context: str = "", introduced_personality: str = "",
    viewer_personality: str = "",
) -> str:
    """Accept only a post-acceptance opening that remains evidence-bound."""
    text = short_public_text(value, 220)
    lowered = text.casefold()
    if not text or text.count(COUNTERPARTY_PLACEHOLDER) != 1:
        return ""
    if any(token.casefold() in lowered for token in _UNSAFE_TOKENS) or "@" in text:
        return ""
    if required_context and required_context not in text:
        return ""
    detail_scan = text.replace(required_context, "") if required_context else text
    if _UNVERIFIED_DETAIL_RE.search(detail_scan):
        return ""
    if not any(token in text for token in ("可以先", "先問", "先聊", "打聲招呼", "聊起")):
        return ""
    return text


def accepted_opening_for_viewer(
    match_doc: dict, user_id: str, other_id: str, counterparty_label: str,
) -> str:
    """Read one role-bound post-acceptance opening without exposing it early."""
    projection = match_doc.get("friend_intro_v4") or {}
    role_key = "initiator_preview" if user_id == str(match_doc.get("from_user") or "") else "receiver_invitation"
    entry = projection.get(role_key) if isinstance(projection, dict) else None
    if isinstance(entry, dict) and str(entry.get("viewer_id") or "") == user_id and str(entry.get("counterparty_id") or "") == other_id:
        text = valid_accepted_opening_text(
            entry.get("accepted_opening"),
            required_context=short_public_text(entry.get("counterparty_context_snapshot"), 56),
            introduced_personality=short_public_text(entry.get("counterparty_public_personality"), 32),
            viewer_personality=short_public_text(entry.get("viewer_public_personality"), 32),
        )
        if text:
            return text.replace(COUNTERPARTY_PLACEHOLDER, counterparty_label)
    context = ""
    if isinstance(entry, dict):
        context = short_public_text(entry.get("counterparty_context_snapshot"), 56)
    if context:
        return f"好，{counterparty_label}也點頭了！你可以先從「{context}」聊起，問問對方最期待哪一部分，輕鬆打聲招呼就好。"
    return f"好，{counterparty_label}也點頭了！先自然打聲招呼，分享一件最近讓你開心的小事，會比一開始問得太正式舒服。"


def shared_match_opening(match_doc: dict, first_label: str, second_label: str) -> str:
    """Fallback welcome when personalized generation is unavailable.

    Private directional accepted_opening prose stays in each mediator room.
    This fallback makes no model call and never reads live profile memory or
    infers mutual interest. The normal composer lives in pair_opening_service.
    """
    projection = match_doc.get("friend_intro_v4")
    contexts: list[str] = []
    if isinstance(projection, dict):
        for role, viewer, other in (
            ("initiator_preview", match_doc.get("from_user"), match_doc.get("to_user")),
            ("receiver_invitation", match_doc.get("to_user"), match_doc.get("from_user")),
        ):
            entry = projection.get(role)
            if not isinstance(entry, dict):
                continue
            if entry.get("viewer_id") != viewer or entry.get("counterparty_id") != other:
                continue
            context = short_public_text(entry.get("counterparty_context_snapshot"), 48)
            if context and context not in contexts:
                contexts.append(context)
    topic_box = match_doc.get("search_context")
    topic = short_public_text(
        topic_box.get("invitation_topic") if isinstance(topic_box, dict) else "", 48,
    )
    if topic:
        basis = f"這次是從「{topic}」的邀請牽上線，可以先交流想法，再決定要不要同行。"
    elif contexts:
        topics = "、".join(f"「{item}」" for item in contexts)
        basis = f"這次介紹從提案中提到的{topics}出發，想讓你們先聊聊彼此的想法。"
    else:
        basis = "這次先讓你們探索是否聊得來，不預設一定合拍。"
    question = "這件事最吸引你的是哪個部分？" if topic or contexts else "如果有半天空閒，你最想怎麼度過？"
    greeting = f"{first_label}、{second_label}，" if first_label and second_label and "對方" not in (first_label, second_label) else ""
    return f"阿月：{greeting}你們都願意認識彼此了！{basis}\n先從一個輕鬆的問題開始：{question}"


def _snapshot_profiles(match_doc: dict) -> tuple[dict, dict]:
    snapshot = match_doc.get("match_context_snapshot") or {}
    if not isinstance(snapshot, dict):
        return {}, {}
    initiator = dict(snapshot.get("target") or {})
    receiver = dict(snapshot.get("candidate") or {})
    if (
        str(initiator.get("user_id") or "") != str(match_doc.get("from_user") or "")
        or str(receiver.get("user_id") or "") != str(match_doc.get("to_user") or "")
    ):
        return {}, {}
    return initiator, receiver


def build_v4_snapshot_fallback(match_doc: dict) -> dict:
    initiator, receiver = _snapshot_profiles(match_doc)
    if not initiator or not receiver:
        return {}
    tier = str(match_doc.get("recommendation_tier") or "exploratory")
    if tier not in {"grounded", "exploratory"}:
        tier = "exploratory"

    def entry(viewer: dict, other: dict) -> dict:
        context_revision = str(
            other.get("context_revision")
            or other.get("profile_revision")
            or other.get("updated_at")
            or ""
        )
        style_id = (
            "warm_intro"
            if match_doc.get("reason_copy_version") != FRIEND_COPY_VERSION
            else match_reason_style_id(viewer, other, context_revision=context_revision)
        )
        topic = short_public_text(
            (match_doc.get("search_context") or {}).get("invitation_topic"), 80,
        )
        query_text = short_public_text(
            (match_doc.get("search_context") or {}).get("query_text"), 600,
        )
        fallback = (
            topic_friend_intro_fallback(
                viewer, other, topic,
                requester_id=str(match_doc.get("from_user") or ""),
                query_text=query_text,
                auto_invite=str(match_doc.get("delivery_mode") or "") == "invite_on_match",
            )
            if topic else friend_intro_fallback(viewer, other, tier, style_id=style_id)
        )
        return {
            "copy_version": FRIEND_COPY_VERSION,
            "style_id": fallback["style_id"],
            "viewer_id": str(viewer.get("user_id") or ""),
            "counterparty_id": str(other.get("user_id") or ""),
            "counterparty_context_snapshot": (
                "" if topic else short_public_text(other.get("current_context"), 56)
            ),
            "counterparty_public_personality": public_personality_phrase(other),
            "viewer_public_personality": public_personality_phrase(viewer),
            "viewer_text": fallback["viewer_text"],
            "conversation_starter": fallback["conversation_starter"],
            "accepted_opening": fallback["accepted_opening"],
            "tier": tier,
        }

    return {
        "initiator_preview": entry(initiator, receiver),
        "receiver_invitation": entry(receiver, initiator),
    }


def _safe_bound_v4_entry(
    entry: Any,
    user_id: str,
    other_id: str,
    *,
    allow_topic_statement: bool = False,
) -> str:
    if not isinstance(entry, dict):
        return ""
    if str(entry.get("viewer_id") or "") != user_id:
        return ""
    if str(entry.get("counterparty_id") or "") != other_id:
        return ""
    if entry.get("style_id") == "related_interest_v1":
        # Re-render only the validated, role-bound evidence. Never trust model
        # prose or a generic "shared" interpretation of semantic similarity.
        from services.related_interest_reason_service import related_entry_text
        return related_entry_text(entry)
    if str(entry.get("style_id") or "") == "topic_request":
        # A confirmed topic search may already have sent the invitation. Its
        # requester copy is then a short introduction, not another consent
        # question. Validate it with the same evidence/privacy rules while
        # leaving every non-topic projection on the stricter legacy path.
        text = short_public_text(entry.get("viewer_text"), 220)
        if not text:
            return ""
        # A topic belongs to the requester.  Old cards may contain the other
        # person's unrelated durable context or an LLM-invented interest claim;
        # neither is authorized evidence for a topic invitation.
        stale_context = short_public_text(
            entry.get("counterparty_context_snapshot"), 56,
        )
        if contains_unverified_topic_claim(text) or (
            stale_context and stale_context in text
        ):
            return ""
        is_question = text.endswith(("？", "?"))
        if not allow_topic_statement and not is_question:
            return ""
        validation_text = text if is_question else f"{text}你有興趣嗎？"
        if valid_friend_intro_text(
            validation_text,
            role_bound=True,
        ):
            return text
        return ""
    text = valid_friend_intro_text(
        entry.get("viewer_text"),
        required_context=short_public_text(entry.get("counterparty_context_snapshot"), 56),
        introduced_personality=short_public_text(entry.get("counterparty_public_personality"), 32),
        viewer_personality=short_public_text(entry.get("viewer_public_personality"), 32),
    )
    if text:
        return text
    return valid_friend_intro_text(
        entry.get("viewer_text"),
        required_context=short_public_text(entry.get("counterparty_context_snapshot"), 56),
        introduced_personality=short_public_text(entry.get("counterparty_public_personality"), 32),
        viewer_personality=short_public_text(entry.get("viewer_public_personality"), 32),
        role_bound=True,
    )


def reason_for_viewer(match_doc: dict, user_id: str) -> str:
    """Return the sole reason authorized for ``user_id`` from this match."""
    user_id = str(user_id or "")
    from_user = str(match_doc.get("from_user") or "")
    to_user = str(match_doc.get("to_user") or "")
    if user_id == from_user:
        role_key, other_id, legacy_key = "initiator_preview", to_user, "target"
    elif user_id == to_user:
        role_key, other_id, legacy_key = "receiver_invitation", from_user, "candidate"
    else:
        return ""
    allow_topic_statement = bool(
        role_key == "initiator_preview"
        and str(match_doc.get("delivery_mode") or "") == "invite_on_match"
    )

    if match_doc.get("reason_version") == V4_REASON_VERSION:
        # Proposals created before the five-style contract may contain a
        # perfectly valid but visibly old fallback.  For unresolved proposals
        # only, project the new copy from the immutable creation snapshot.
        # This is read-only compatibility: status, revision and stored history
        # are never rewritten by a GET.
        if (
            match_doc.get("status") in LIVE_PROPOSAL_STATUSES
            and match_doc.get("reason_copy_version") != FRIEND_COPY_VERSION
        ):
            fallback = build_v4_snapshot_fallback(match_doc)
            text = _safe_bound_v4_entry(
                fallback.get(role_key), user_id, other_id,
                allow_topic_statement=allow_topic_statement,
            ) if fallback else ""
            if text:
                return text
        projection = match_doc.get("friend_intro_v4") or {}
        text = _safe_bound_v4_entry(
            projection.get(role_key) if isinstance(projection, dict) else None,
            user_id, other_id,
            allow_topic_statement=allow_topic_statement,
        )
        if text:
            return text
        fallback = build_v4_snapshot_fallback(match_doc)
        text = _safe_bound_v4_entry(
            fallback.get(role_key), user_id, other_id,
            allow_topic_statement=allow_topic_statement,
        ) if fallback else ""
        if text:
            return text
        # A live V4 proposal may have been written by an older worker before
        # the immutable snapshot was added.  Preserve the role binding and
        # return a complete, privacy-safe minimum introduction instead of the
        # title-only/empty card that older readers produced.
        viewer = {"user_id": user_id}
        other = {"user_id": other_id}
        minimum = friend_intro_fallback(
            viewer, other,
            str(match_doc.get("recommendation_tier") or "exploratory"),
            style_id="warm_intro",
        )
        if role_key == "receiver_invitation":
            minimum["viewer_text"] = "有位朋友想和你認識看看；你願意先認識對方嗎？"
        else:
            minimum["viewer_text"] = "我想到一位可以先認識看看的人；你會想先認識對方嗎？"
        return _safe_bound_v4_entry(
            {
                "viewer_id": user_id,
                "counterparty_id": other_id,
                **minimum,
            },
            user_id,
            other_id,
            allow_topic_statement=allow_topic_statement,
        ) or "我想到一位可以先認識看看的人；你願意先認識對方嗎？"

    entries = match_doc.get("directional_reason_v3") or []
    if isinstance(entries, list):
        matches = [
            item for item in entries if isinstance(item, dict)
            and str(item.get("viewer_id") or "") == user_id
            and str(item.get("counterparty_id") or "") == other_id
        ]
        if len(matches) == 1:
            text = short_public_text(matches[0].get("viewer_text"), 220)
            if text and not any(token.casefold() in text.casefold() for token in _UNSAFE_TOKENS):
                return text

    # Only old live proposals receive read-time compatibility.  Historical
    # accepted/declined records remain byte-for-byte untouched and unrecomputed.
    if match_doc.get("status") in LIVE_PROPOSAL_STATUSES:
        initiator, receiver = _snapshot_profiles(match_doc)
        if initiator and receiver:
            viewer, other = (initiator, receiver) if user_id == from_user else (receiver, initiator)
            return friend_intro_fallback(
                viewer, other, str(match_doc.get("recommendation_tier") or "exploratory"),
                style_id="warm_intro",
            )["viewer_text"]

    directional = match_doc.get("directional_reason_v2") or {}
    candidate = directional.get(legacy_key) if isinstance(directional, dict) else None
    if isinstance(candidate, dict):
        text = short_public_text(candidate.get("viewer_text"), 220)
        if text:
            return text
    raw = match_doc.get("reason") if legacy_key == "target" else match_doc.get("receiver_reason") or match_doc.get("reason")
    return short_public_text(raw, 220)
