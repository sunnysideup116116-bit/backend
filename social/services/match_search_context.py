"""Bounded, per-search context for the match pipeline.

The profile's recent context is durable user state.  A search context is a
short lived request hint and must stay separate from it.  This module keeps
the transport and worker boundaries consistent while intentionally dropping
the retired ``allow_adjacent``/``widened`` controls.
"""

from __future__ import annotations

import re
import hashlib
from typing import Any

from services.language_service import normalize_zh_tw
from matchmaker_agent.concept_identity import (
    PreferenceTextError,
    canonicalize_concept,
    canonicalize_fresh_concept,
    normalize_preference_text,
    stored_concept_identity,
)


MAX_INVITATION_TOPIC_CHARS = 80
MAX_QUERY_TEXT_CHARS = 600
MAX_SOURCE_MESSAGE_ID_CHARS = 128

_SEARCH_CONTEXT_KEYS = (
    "invitation_topic",
    "query_text",
    "source_message_id",
    "search_intent",
    "normalized_topic",
    "canonical_preference_key",
)
_SEARCH_INTENTS = frozenset({"activity", "recent_context", "preference", "generic"})

_NEGATED_TOPIC_PREFIX_RE = re.compile(
    r"(?:沒有|不要|不想|不用|不再|先不|別|不願(?:意)?|無意|不是|不找|不考慮|先跳過)"
    r"[^。！？!?，,；;]{0,16}$",
)

_BARE_CONFIRMATION_RE = re.compile(
    r"(?:好的?|好啊|好喔|好哦|可以(?:啊|喔|哦)?|確認|確定|願意|同意|沒問題|"
    r"yes(?:\s+please)?|sure|ok(?:ay)?)\s*[。.!！?？]*$",
    re.IGNORECASE,
)


def bounded_search_text(value: Any, limit: int) -> str:
    """Normalize and cap one user supplied search-context string."""
    text = re.sub(r"\s+", " ", normalize_zh_tw(str(value or ""))).strip()
    return text[:limit].rstrip()


def context_embedding_source_hash(value: Any) -> str:
    """Hash the normalized durable context used for a profile embedding."""
    text = bounded_search_text(value, MAX_QUERY_TEXT_CHARS)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def safe_search_context(value: Any) -> dict[str, str]:
    """Return only the optional, bounded fields owned by one search.

    Legacy clients may still send ``allow_adjacent`` or ``widened``.  They are
    deliberately ignored here; accepting them must never change qualification
    or safety gates.
    """
    if not isinstance(value, dict):
        return {}
    # Preference is semantic input, not a presentation hint. Validate before
    # any legacy display/context shortening and never downgrade an invalid
    # explicit preference request into a generic search.
    if str(value.get("search_intent") or "").strip() == "preference":
        if value.get("canonicalization_version") == "v2":
            # Already-bound requests may predate the fresh-input script policy.
            # Verify and preserve their source; never re-normalize a stored ID.
            record = dict(value)
            record.setdefault("canonical_key", value.get("canonical_preference_key"))
            identity = stored_concept_identity(record)
            if not identity or value.get("canonical_preference_key") != identity.key:
                raise PreferenceTextError("preference_search_identity_invalid")
        else:
            identity = canonicalize_fresh_concept(
                value.get("semantic_text") or value.get("normalized_topic")
                or value.get("invitation_topic"),
            )
        if not identity:
            raise PreferenceTextError("preference_search_invalid")
        if value.get("semantic_text") and value.get("normalized_topic"):
            repeated_identity = (canonicalize_concept if value.get("canonicalization_version") == "v2"
                                 else canonicalize_fresh_concept)(value["normalized_topic"])
            if not repeated_identity or repeated_identity.key != identity.key:
                raise PreferenceTextError("preference_search_topic_mismatch")
        result = {
            "search_intent": "preference",
            "normalized_topic": identity.semantic_text,
            "semantic_text": identity.semantic_text,
            "display_label": identity.display_label,
            "canonical_preference_key": identity.key,
            "canonicalization_version": identity.canonicalization_version,
            "semantic_input_hash": identity.semantic_input_hash,
        }
        if value.get("query_text"):
            result["query_text"] = normalize_preference_text(
                value["query_text"], max_length=MAX_QUERY_TEXT_CHARS,
            )
        source_id = bounded_search_text(
            value.get("source_message_id"), MAX_SOURCE_MESSAGE_ID_CHARS,
        )
        if source_id:
            result["source_message_id"] = source_id
        return result
    limits = {
        "invitation_topic": MAX_INVITATION_TOPIC_CHARS,
        "query_text": MAX_QUERY_TEXT_CHARS,
        "source_message_id": MAX_SOURCE_MESSAGE_ID_CHARS,
        "search_intent": 24,
        "normalized_topic": MAX_INVITATION_TOPIC_CHARS,
        "canonical_preference_key": 52,
    }
    result: dict[str, str] = {}
    for key in _SEARCH_CONTEXT_KEYS:
        text = bounded_search_text(value.get(key), limits[key])
        if text:
            result[key] = text
    intent = result.get("search_intent", "")
    if intent not in _SEARCH_INTENTS:
        result.pop("search_intent", None)
        intent = ""
    result.pop("canonical_preference_key", None)
    if intent != "activity":
        result.pop("normalized_topic", None)
    elif result.get("invitation_topic"):
        result["normalized_topic"] = result["invitation_topic"]
    return result


def validate_persisted_search_context(value: Any) -> dict[str, str]:
    """Validate replay evidence, never upgrade a legacy/display-only request.

    Fresh entry points use safe_search_context to create server-owned metadata.
    Durable job and confirmation executors must use this stricter boundary:
    equality of a preview/request fingerprint does not prove semantic fidelity.
    """
    if not isinstance(value, dict) or str(value.get("search_intent") or "").strip() != "preference":
        return safe_search_context(value)
    try:
        record = dict(value)
        record.setdefault("canonical_key", value.get("canonical_preference_key"))
        identity = stored_concept_identity(record)
        if (not identity
                or value.get("canonical_preference_key") != identity.key
                or value.get("normalized_topic") != identity.semantic_text):
            raise PreferenceTextError("preference_search_reconfirmation_required")
        return safe_search_context(value)
    except PreferenceTextError:
        raise PreferenceTextError("preference_search_reconfirmation_required") from None


def _topic_is_negated(text: str, topic: str) -> bool:
    """Reject an activity when the same turn explicitly rules it out.

    Match search meaning is request scoped.  A sentence such as
    ``沒有要滑雪，依近期情境就好`` must clear the old topic even though the
    activity word is still present in the sentence.
    """
    safe_topic = bounded_search_text(topic, MAX_INVITATION_TOPIC_CHARS)
    if not safe_topic:
        return False
    for occurrence in re.finditer(re.escape(safe_topic), text, flags=re.IGNORECASE):
        prefix = text[: occurrence.start()].strip()
        if _NEGATED_TOPIC_PREFIX_RE.search(prefix[-24:]):
            return True
    return False


def extract_invitation_topic(value: Any) -> str:
    """Extract a conservative activity phrase from an explicit search ask.

    This is only a convenience for the Public Ayue confirmation boundary.  It
    is intentionally conservative: if no activity cue is present, the caller
    falls back to the durable recent profile context.
    """
    text = bounded_search_text(value, MAX_QUERY_TEXT_CHARS)
    if not text:
        return ""
    # Keep this parser deliberately small and deterministic.  It is used at a
    # confirmation boundary, so a filler such as ``新的人`` must never become
    # the invitation topic.  The full user sentence remains the query text;
    # this value is only the short, readable label shown on a confirmation or
    # Hub card.
    patterns = (
        # ``再找一個滑雪的`` is an explicit continuation of the topic.  The
        # suffix is intentionally narrow so ``再找一個新的朋友`` stays a
        # generic search and cannot inherit a prior activity.
        r"(?:幫我\s*)?(?:再|繼續|還要)\s*(?:找|配到|配對)(?:一個|一位|個|位)?"
        r"(?P<topic>[^。！？!?，,；;]{1,60}?)(?:的人|的對象|的伴|的朋友|的)$",
        # ``幫我找一個也會衝浪的人`` / ``想配到會衝浪的人``
        r"(?:幫我\s*)?(?:想\s*)?(?:找|配到|配對到|配對|介紹)(?:一個|一位|個|位)?"
        r"(?P<topic>(?:也?會|懂得)\s*[^。！？!?，,]{1,60}?)"
        r"(?:的人|的對象|的伴|的朋友)",
        # ``我要找對攝影有興趣的人``.  This pattern is intentionally before
        # the broader together/陪伴 pattern so ``對…有興趣`` is normalized to
        # the activity rather than returned verbatim.
        r"(?:找|配對|介紹)[^。！？!?，,]{0,24}?對\s*(?P<topic>[^。！？!?，,]{1,40}?)"
        r"(?:有興趣|感興趣|有點興趣)(?:的人|的對象|的伴|的朋友)?",
        r"(?:找人|找個人|找一位|找個對象|想找|想要找|我要找)"
        r"[^。！？!?，,]{0,24}?(?:一起|陪我)(?P<topic>[^。！？!?，,]{1,80})",
        # ``找人去吃生魚片`` / ``幫我找個人去看電影`` describe the
        # requested activity even without the literal word ``一起``.  Stop
        # before a trailing request to invite/contact the person so that text
        # such as ``叫它幫我邀請`` never becomes part of the topic label.
        r"(?:找人|找個人|找一位|找個對象)\s*(?:一起\s*|去\s*)?"
        r"(?P<topic>(?:吃|喝|看|逛|玩|參加|學|練|爬|走)[^。！？!?，,]{1,60}?)"
        r"(?=(?:叫|請|讓)(?:阿月|它|他|她|你)?幫我(?:邀請|問問|牽線)|[。！？!?，,]|$)",
        r"(?:一起|陪我)(?P<topic>[^。！？!?，,]{1,80})",
        r"(?:想|要)(?:去|做|玩|看|逛|參加|學|練)(?P<topic>[^。！？!?，,]{1,80})",
        # A short follow-up such as ``是對攝影有興趣的`` commonly arrives
        # after the user has already said they want a new match.
        r"對\s*(?P<topic>[^。！？!?，,]{1,40}?)(?:有興趣|感興趣)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        topic = bounded_search_text(match.group("topic"), MAX_INVITATION_TOPIC_CHARS)
        topic = re.sub(r"(?:的人|的伴|的朋友|的對象|嗎|吧)$", "", topic).strip()
        topic = re.sub(r"^(?:去|一起|陪我|跟我|和我)\s*", "", topic).strip()
        topic = re.sub(r"^(?:也?會|能|可以|懂得|有)\s*", "", topic).strip()
        topic = re.sub(r"^(?:對|關於)\s*", "", topic).strip()
        topic = re.sub(r"(?:有興趣|感興趣|興趣|愛好)$", "", topic).strip()
        # Search scope words describe *who* to find, never *what* to do.
        topic = re.sub(r"^(?:新的人|新人|新的朋友|新朋友|一個人|一位朋友)\s*", "", topic).strip()
        topic = re.sub(r"^(?:跟我|和我)\s*", "", topic).strip()
        topic = re.sub(r"(?:找人|找個人|找一位|找個伴)$", "", topic).strip()
        topic = re.sub(r"^(?:一起|陪我)\s*", "", topic).strip()
        # Use a stable human label for common wording variants.  The original
        # query remains untouched for embedding, so this does not discard any
        # nuance from the request.
        topic = {
            "拍人像": "人像攝影",
            "拍人像照": "人像攝影",
            "拍照": "攝影",
            "拍照片": "攝影",
        }.get(topic, topic)
        if (
            topic
            and topic not in {"新", "新的", "新人", "新朋友", "新的人", "一個人", "一位朋友"}
            and not _topic_is_negated(text, topic)
        ):
            return topic
    # A message can be the activity itself rather than a full ``找人``
    # sentence (for example ``拍人像，可以幫我約人嗎``).  Only accept a
    # bounded activity phrase and leave the caller to use conversation history
    # to decide whether this means a new introduction or an existing contact.
    short_activity = re.search(
        r"(?P<topic>人像攝影|拍人像|攝影|衝浪|游泳|潛水|水上活動|登山|爬山|健行|跑步|看展|逛展|咖啡|桌遊)",
        text,
    )
    if short_activity and not _topic_is_negated(text, short_activity.group("topic")):
        topic = short_activity.group("topic")
        return {"拍人像": "人像攝影", "拍照": "攝影"}.get(topic, topic)
    return ""


def search_context_for_turn(
    value: Any = None, *, message: Any = "", message_id: Any = None,
    history: Any = None,
) -> dict[str, str]:
    """Bind explicit request context to one Public Ayue turn.

    A supplied query is authoritative for this one search.  If a caller only
    supplies a topic, the topic is also a bounded query.  When neither is
    supplied, only a message with a conservative activity cue becomes a
    query; generic ``start_search`` language remains an ordinary search.
    """
    result = safe_search_context(value)
    if result.get("search_intent") == "preference":
        if result.get("query_text") and message_id and not result.get("source_message_id"):
            result["source_message_id"] = bounded_search_text(
                message_id, MAX_SOURCE_MESSAGE_ID_CHARS,
            )
        return safe_search_context(result)
    recovered_source_id = ""
    query_text = result.get("query_text", "")
    if result.get("invitation_topic"):
        raw_topic = result["invitation_topic"]
        normalized_topic = extract_invitation_topic(raw_topic)
        if normalized_topic:
            result["invitation_topic"] = normalized_topic
        elif raw_topic in {"新", "新的", "新人", "新朋友", "新的人", "一個人", "一位朋友"}:
            result.pop("invitation_topic", None)
    if query_text and not result.get("invitation_topic"):
        topic = extract_invitation_topic(query_text)
        if topic:
            result["invitation_topic"] = topic
    message_text = bounded_search_text(message, MAX_QUERY_TEXT_CHARS)
    explicit_message_negates_topic = bool(
        result.get("invitation_topic")
        and message_text
        and not _BARE_CONFIRMATION_RE.fullmatch(message_text)
        and _topic_is_negated(message_text, result["invitation_topic"])
    )
    if explicit_message_negates_topic:
        # A correction in the current turn supersedes a stale caller hint.  In
        # particular, ``沒有要滑雪，依近期情境就好`` must not retain either
        # the old topic or its old query text.
        result.pop("invitation_topic", None)
        result.pop("query_text", None)
        result.pop("source_message_id", None)
        query_text = ""

    if not query_text:
        message_topic = extract_invitation_topic(message)
        topic = result.get("invitation_topic") or message_topic
        source_message = bounded_search_text(message or topic, MAX_QUERY_TEXT_CHARS)
        # A typed confirmation normally says only ``好``/``確認``.  Recover the
        # original request from the bounded turn history instead of embedding
        # the confirmation word.  The history is read-only and is never sent to
        # Matchmaker as private conversation data.
        if (
            isinstance(history, (list, tuple))
            and not message_topic
            and _BARE_CONFIRMATION_RE.fullmatch(message_text)
        ):
            # A button confirmation has already persisted its own search
            # context.  This recovery path is only for old callers that send a
            # bare acknowledgement: accept the immediately preceding assistant
            # search offer and the user turn directly before that offer.  Never
            # scan an arbitrary historical activity and attach it to a new
            # search.
            items = [item for item in history if isinstance(item, dict)]
            if items:
                latest = items[-1]
                latest_role = str(latest.get("role") or "")
                latest_sender = str(latest.get("sender_id") or "")
                latest_room = str(latest.get("room_id") or "")
                latest_text = bounded_search_text(
                    latest.get("content") or latest.get("message") or "", MAX_QUERY_TEXT_CHARS,
                )
                latest_is_assistant = (
                    (latest_role == "assistant" or latest_sender in {"ai_assistant", "assistant"})
                    and (not latest_room or not str(getattr(history, "room_id", "") or ""))
                )
                is_search_offer = bool(re.search(
                    r"(?:要我(?:現在)?開始(?:找|搜尋)|可能對「[^」]+」有興趣).*開始",
                    latest_text,
                ))
                if latest_is_assistant and is_search_offer:
                    for item in reversed(items[:-1]):
                        role = str(item.get("role") or "")
                        sender_id = str(item.get("sender_id") or "")
                        room_id = str(item.get("room_id") or "")
                        if room_id and latest_room and room_id != latest_room:
                            continue
                        if role and role != "user":
                            continue
                        if not role and sender_id in {"ai_assistant", "assistant", "system"}:
                            continue
                        candidate_text = item.get("content") or item.get("message") or ""
                        candidate_topic = extract_invitation_topic(candidate_text)
                        if candidate_topic:
                            topic = candidate_topic
                            source_message = bounded_search_text(candidate_text, MAX_QUERY_TEXT_CHARS)
                            recovered_source_id = bounded_search_text(
                                item.get("message_id") or item.get("id") or "",
                                MAX_SOURCE_MESSAGE_ID_CHARS,
                            )
                        break
        if topic:
            result["invitation_topic"] = bounded_search_text(
                topic, MAX_INVITATION_TOPIC_CHARS,
            )
            result["query_text"] = source_message or bounded_search_text(topic, MAX_QUERY_TEXT_CHARS)
    if result.get("query_text") and recovered_source_id and not result.get("source_message_id"):
        result["source_message_id"] = recovered_source_id
    elif result.get("query_text") and message_id and not result.get("source_message_id"):
        result["source_message_id"] = bounded_search_text(
            message_id, MAX_SOURCE_MESSAGE_ID_CHARS,
        )
    return safe_search_context(result)


def provider_search_context(value: Any) -> dict[str, str]:
    """Project request meaning for Matchmaker without source-message identity."""
    context = safe_search_context(value)
    return {
        key: context[key]
        for key in ("search_intent", "normalized_topic", "invitation_topic", "query_text")
        if context.get(key)
    }
