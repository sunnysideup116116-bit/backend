"""Direct, low-latency tool surface for the official Gemini Live template.

The template mode deliberately keeps the provider-facing function list small,
but does not put capability discovery in front of every request.  The model
chooses a domain-level operation and this module converts it into the same
validated :class:`VoiceProposal` used by the existing App Voice gateway.

This is an adapter, not a second business-rule implementation.  Validation,
permissions, target binding, confirmation, and App execution remain owned by
the existing runtime and Flutter executor.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import VoiceProposal, deterministic_proposal, validate_proposal
from .catalog_tools import catalog_function_declarations


TEMPLATE_TOOL_COUNT = 21

_SPOKEN_NUMBERS = {
    "一": 1, "二": 2, "兩": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _spoken_position(value: str) -> int | None:
    raw = str(value or "").strip()
    if raw.isdigit():
        number = int(raw)
        return number if 1 <= number <= 20 else None
    if raw in _SPOKEN_NUMBERS:
        return _SPOKEN_NUMBERS[raw]
    if len(raw) == 2 and raw.startswith("十") and raw[1] in _SPOKEN_NUMBERS:
        return 10 + _SPOKEN_NUMBERS[raw[1]]
    if len(raw) == 2 and raw.endswith("十") and raw[0] in _SPOKEN_NUMBERS:
        return _SPOKEN_NUMBERS[raw[0]] * 10
    return None


def _requested_photo_positions(text: str) -> list[int]:
    positions: list[int] = []
    for match in re.finditer(
        r"第\s*([0-9]{1,2}|[一二兩两三四五六七八九十]{1,2})\s*(?:張|章)", text,
    ):
        position = _spoken_position(match.group(1))
        if position is not None and position not in positions:
            positions.append(position)
    return positions[:5]


def _clean_contact_name(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip(" ，,。！？!?")
    cleaned = re.sub(r"^(?:幫我|請|把|將|與|和|跟)\s*", "", cleaned)
    cleaned = re.sub(r"(?:這個人|這位使用者|的)$", "", cleaned).strip()
    return cleaned[:40]


def _extended_template_proposal(
    text: str, *, revision: int,
) -> VoiceProposal | None:
    """Handle narrow App-only intents without widening the shared router."""

    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    if not raw:
        return None

    positions = _requested_photo_positions(raw)
    if positions and any(word in raw for word in ("照片", "相片", "圖片", "相簿")):
        return _proposal(
            "post.select_recent_photos", {"positions": positions},
            revision=revision,
        )

    private_open = re.search(
        r"(?:打開|開啟|切到|切去|前往|進入|帶我到|我要到|到)\s*"
        r"(?:與|和|跟)?\s*([^，。！？!?]{1,40}?)\s*(?:的)?\s*"
        r"(?:阿月悄悄話|悄悄話)",
        raw,
    )
    if private_open:
        contact_name = _clean_contact_name(private_open.group(1))
        if contact_name:
            return _proposal(
                "ayue.private_open", {"contact_name": contact_name},
                revision=revision,
            )

    if any(phrase in raw for phrase in ("封鎖名單", "黑名單", "我封鎖了誰")):
        return _proposal("safety.blocked_users_query", {}, revision=revision)

    unblock = (
        re.search(r"(?:解除|取消)\s*封鎖\s*([^，。！？!?]{1,40})", raw)
        or re.search(r"(?:把|將)?\s*([^，。！？!?]{1,40}?)\s*(?:解除|取消)封鎖", raw)
    )
    if unblock:
        contact_name = _clean_contact_name(unblock.group(1))
        if contact_name:
            return _proposal(
                "safety.unblock_user", {"contact_name": contact_name},
                revision=revision,
            )

    block = (
        re.search(r"(?:幫我|請)?\s*封鎖\s*([^，。！？!?]{1,40})", raw)
        or re.search(r"(?:把|將)\s*([^，。！？!?]{1,40}?)\s*封鎖", raw)
    )
    if block:
        contact_name = _clean_contact_name(block.group(1))
        if contact_name:
            return _proposal(
                "safety.block_user", {"contact_name": contact_name},
                revision=revision,
            )
    return None


def authoritative_template_proposal(
    text: str, *, context: dict[str, Any], revision: int | None = None,
) -> VoiceProposal | None:
    """Return deterministic routing with complete natural-month intervals."""

    effective_revision = (
        int(context.get("revision") or 0) if revision is None else revision
    )
    proposal = _extended_template_proposal(text, revision=effective_revision)
    if proposal is None:
        proposal = deterministic_proposal(text, context=context)
    if proposal is None or proposal.intent != "calendar.query":
        return proposal
    raw = str(text or "")
    if not re.search(
        r"(?:這個月|这个月|本月|下個月|下个月|上個月|上个月|月底|近一個月|近一个月|"
        r"今年\s*\d{1,2}\s*月|\d{1,2}\s*月)",
        raw,
    ):
        return proposal
    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    if "下個月" in raw or "下个月" in raw:
        month = today.month + 1
        year = today.year + (1 if month == 13 else 0)
        month = 1 if month == 13 else month
    elif "上個月" in raw or "上个月" in raw:
        month = today.month - 1
        year = today.year - (1 if month == 0 else 0)
        month = 12 if month == 0 else month
    else:
        year_match = re.search(r"今年\s*(\d{1,2})\s*月", raw)
        month_match = re.search(r"(?<!\d)(\d{1,2})\s*月", raw)
        year = today.year
        month = (
            int(year_match.group(1)) if year_match
            else int(month_match.group(1)) if month_match else today.month
        )
        if not 1 <= month <= 12:
            return proposal
    start = date(year, month, 1)
    end = date(
        year + (1 if month == 12 else 0),
        1 if month == 12 else month + 1,
        1,
    ) - timedelta(days=1)
    arguments = {
        "source": proposal.arguments.get("source", "all"),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }
    return VoiceProposal(
        proposal.intent,
        arguments,
        proposal.reply,
        proposal.base_revision if revision is None else revision,
    )


def _schema(
    properties: dict[str, Any], *, required: list[str] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }
    if required:
        value["required"] = required
    return value


def _proposal(
    intent: str, arguments: dict[str, Any], *, revision: int,
) -> VoiceProposal | None:
    return validate_proposal(
        {
            "intent": intent,
            "arguments": arguments,
            "reply": "好的，我來處理。",
        },
        base_revision=revision,
    )


def _raw_call(call: Any) -> dict[str, Any]:
    raw = getattr(call, "args", {})
    return dict(raw) if isinstance(raw, dict) else {}


def _with_target(raw: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    target_ref = raw.get("target_ref")
    if isinstance(target_ref, str) and target_ref:
        arguments["target_ref"] = target_ref
    return arguments


def template_proposal_from_function(
    call: Any, *, revision: int,
) -> VoiceProposal | None:
    """Convert one template-mode call into an existing validated proposal."""

    raw = _raw_call(call)
    name = str(getattr(call, "name", "") or "")

    if name == "navigate_app":
        return _proposal(
            "app.navigate",
            _with_target(raw, {"destination": raw.get("destination")}),
            revision=revision,
        )

    if name == "open_chat":
        return _proposal(
            (
                "ayue.private_open"
                if str(raw.get("mode") or "chat") == "private_ayue"
                else "chat.open"
            ),
            _with_target(raw, {"contact_name": raw.get("contact_name")}),
            revision=revision,
        )

    if name == "read_app_data":
        domain = str(raw.get("domain") or "")
        if domain == "quota":
            return _proposal("quota.query", {}, revision=revision)
        if domain == "delivery":
            return _proposal("chat.status.query", _with_target(raw, {"contact_name": raw.get("contact_name", "")}), revision=revision)
        if domain == "calendar":
            arguments = {
                key: raw[key]
                for key in ("source", "range", "start_date", "end_date")
                if key in raw and raw[key] not in (None, "")
            }
            return _proposal("calendar.query", arguments, revision=revision)
        if domain == "matching":
            return _proposal(
                "match.query",
                {"view": str(raw.get("view") or "status")},
                revision=revision,
            )
        if domain == "dates":
            return _proposal(
                "date.query",
                {"contact_name": raw.get("contact_name", "")},
                revision=revision,
            )
        if domain == "contacts":
            return _proposal("contacts.query", {}, revision=revision)
        if domain == "blocked_users":
            return _proposal("safety.blocked_users_query", {}, revision=revision)
        if domain == "memory":
            return _proposal(
                "memory.query", {"query": raw.get("query", "")},
                revision=revision,
            )
        if domain == "profile":
            return _proposal(
                "self.query", {"detail": raw.get("detail") or "summary"},
                revision=revision,
            )
        if domain == "posts":
            return _proposal(
                "post.open",
                _with_target(raw, {}),
                revision=revision,
            )
        if domain == "chat_content":
            return _proposal(
                "ayue.private_query",
                _with_target(
                    raw,
                    {
                        "contact_name": raw.get("contact_name", ""),
                        "question": raw.get("query") or "請讀取目前聊天內容",
                    },
                ),
                revision=revision,
            )
        if domain == "help":
            query = str(raw.get("query") or "").strip()
            return _proposal(
                "app.search",
                {
                    "query": query,
                    "domains": [
                        "contacts", "calendar", "shared_dates", "memory", "matching",
                    ],
                },
                revision=revision,
            )
        return None

    if name == "ask_app_ayue":
        domain = str(raw.get("domain") or "")
        question = raw.get("question")
        if domain == "personality":
            return _proposal(
                "personality.explore", {"message": question}, revision=revision,
            )
        if domain == "matching":
            return _proposal(
                "match.ayue_query", {"question": question}, revision=revision,
            )
        if domain == "private":
            return _proposal(
                "ayue.private_query",
                _with_target(
                    raw,
                    {
                        "contact_name": raw.get("contact_name", ""),
                        "question": question,
                    },
                ),
                revision=revision,
            )
        if domain in {"places", "web", "memory", "profile"}:
            return _proposal(
                "ayue.public_query",
                {"domain": domain, "question": question},
                revision=revision,
            )
        return None

    if name == "write_app_action":
        action = str(raw.get("action") or "")
        if action == "profile_patch":
            arguments = {"changes": raw.get("changes")}
            intent = "profile.patch"
        elif action == "profile_save":
            arguments = {}
            intent = "profile.request_commit"
        elif action == "setting":
            arguments = {"key": raw.get("key"), "enabled": raw.get("enabled")}
            intent = "settings.set"
        elif action == "post_caption":
            arguments = {
                "caption": raw.get("caption"),
            }
            intent = {
                "open": "post.open_draft",
                "replace": "post.replace_caption",
                "append": "post.append_caption",
            }.get(str(raw.get("mode") or "replace"), "")
        elif action == "post_publish":
            arguments = {}
            intent = "post.request_publish"
        elif action == "select_recent_photos":
            positions = raw.get("positions")
            arguments = (
                {"positions": positions}
                if isinstance(positions, list) and positions
                else {"count": raw.get("count")}
            )
            intent = "post.select_recent_photos"
        elif action == "block_user":
            arguments = {"contact_name": raw.get("contact_name")}
            intent = "safety.block_user"
        elif action == "unblock_user":
            arguments = {"contact_name": raw.get("contact_name")}
            intent = "safety.unblock_user"
        elif action == "calendar_create":
            arguments = {
                key: raw.get(key)
                for key in ("title", "date", "start_time", "end_time", "location", "notes")
            }
            intent = "calendar.create"
        elif action == "calendar_update":
            arguments = {
                key: raw.get(key)
                for key in ("target", "title", "date", "start_time", "end_time", "location", "notes")
                if key in raw
            }
            intent = "calendar.update"
        elif action == "calendar_cancel":
            arguments = {
                key: raw.get(key)
                for key in ("target", "date")
                if key in raw
            }
            intent = "calendar.cancel"
        elif action == "date_respond":
            arguments = {
                "contact_name": raw.get("contact_name"),
                "accepted": raw.get("accepted"),
            }
            intent = "date.respond"
        elif action == "date_update":
            arguments = {
                "contact_name": raw.get("contact_name"),
                "changes": raw.get("changes"),
            }
            intent = "date.update"
        elif action == "date_confirm":
            arguments = {"contact_name": raw.get("contact_name")}
            intent = "date.confirm"
        elif action == "memory_add":
            arguments = {
                "label": raw.get("label"),
                "stance": raw.get("stance"),
            }
            intent = "memory.add"
        elif action == "chat_send":
            arguments = {
                "contact_name": raw.get("contact_name"),
                "message": raw.get("message"),
            }
            intent = "chat.request_send"
        elif action == "visible_choice":
            arguments = {"action": raw.get("choice") or raw.get("action")}
            intent = "ui.choice.activate"
        elif action == "match_start":
            arguments = {"question": raw.get("question")}
            intent = "match.ayue_query"
        else:
            return None
        return _proposal(
            intent,
            _with_target(raw, arguments),
            revision=revision,
        )

    return None


def template_tool_call_for_proposal(
    proposal: VoiceProposal,
) -> tuple[str, dict[str, Any]] | None:
    """Return the direct template tool shape for a deterministic proposal.

    This is used only as a guarded fallback when Gemini Live has delivered a
    final user transcript but did not emit a function call for an unambiguous
    App request.  It keeps the same proposal validation and executor boundary;
    it does not create an additional model round trip.
    """

    intent = proposal.intent
    arguments = dict(proposal.arguments)
    if intent == "app.navigate":
        return "navigate_app", arguments
    if intent == "ui.choice.activate":
        return "write_app_action", {"action": "visible_choice", "choice": arguments["action"]}
    if intent == "chat.open":
        return "open_chat", arguments
    if intent == "ayue.private_open":
        return "open_chat", {"mode": "private_ayue", **arguments}
    if intent == "calendar.query":
        return "read_app_data", {"domain": "calendar", **arguments}
    if intent == "quota.query":
        return "read_app_data", {"domain": "quota"}
    if intent == "chat.status.query":
        return "read_app_data", {"domain": "delivery", **arguments}
    if intent == "match.query":
        return "read_app_data", {"domain": "matching", **arguments}
    if intent == "date.query":
        return "read_app_data", {"domain": "dates", **arguments}
    if intent == "contacts.query":
        return "read_app_data", {"domain": "contacts", **arguments}
    if intent == "safety.blocked_users_query":
        return "read_app_data", {"domain": "blocked_users"}
    if intent == "memory.query":
        return "read_app_data", {"domain": "memory", **arguments}
    if intent == "self.query":
        return "read_app_data", {"domain": "profile", **arguments}
    if intent == "post.open":
        return "read_app_data", {"domain": "posts", **arguments}
    if intent == "ayue.private_query":
        return "ask_app_ayue", {"domain": "private", **arguments}
    if intent == "personality.explore":
        return "ask_app_ayue", {"domain": "personality", "question": arguments["message"]}
    if intent == "ayue.public_query":
        return "ask_app_ayue", {"domain": arguments.pop("domain", "web"), **arguments}
    if intent == "match.ayue_query":
        return "ask_app_ayue", {"domain": "matching", **arguments}
    if intent == "weather.query":
        return "read_weather", arguments
    if intent == "post.select_recent_photos":
        return "write_app_action", {"action": "select_recent_photos", **arguments}
    if intent == "safety.block_user":
        return "write_app_action", {"action": "block_user", **arguments}
    if intent == "safety.unblock_user":
        return "write_app_action", {"action": "unblock_user", **arguments}
    return None


def template_live_tools(types: Any) -> list[Any]:
    """Return the bounded direct-tool surface used by template mode."""

    empty = _schema({})
    declarations = [
        types.FunctionDeclaration(
            name="navigate_app",
            description=(
                "Open one Folks App page. Use for explicit page navigation only; "
                "for a named person's chat use open_chat."
            ),
            parameters_json_schema=_schema({
                "destination": {
                    "type": "string",
                    "enum": [
                        "chat", "matching", "profile", "settings", "profile_edit",
                        "voice_settings", "calendar", "matching_ayue", "match_hub",
                        "memory", "create_post", "blocked_users",
                    ],
                },
            }, required=["destination"]),
        ),
        types.FunctionDeclaration(
            name="read_app_data",
            description=(
                "Read live data from one Folks App domain without capability search. "
                "Use calendar for schedules, matching for match status, dates for shared "
                "dates, contacts for chat recipients, memory for long-term memory, profile "
                "for the signed-in user's profile, posts to open a post currently shown on "
                "that profile, chat_content for an authorized chat read, blocked_users for "
                "the signed-in user's safety list, "
                "and help for App operation instructions."
            ),
            parameters_json_schema=_schema({
                "domain": {
                    "type": "string",
                    "enum": [
                        "calendar", "matching", "dates", "contacts", "memory",
                        "profile", "posts", "chat_content", "blocked_users", "help", "quota", "delivery",
                    ],
                },
                "query": {"type": "string", "maxLength": 1000},
                "contact_name": {"type": "string", "maxLength": 80},
                "view": {"type": "string", "enum": ["status", "hub"]},
                "source": {"type": "string", "enum": ["all", "personal", "google"]},
                "range": {
                    "type": "string",
                    "enum": ["today", "tomorrow", "week", "weekend", "next_week", "upcoming"],
                },
                "start_date": {"type": "string", "description": "Inclusive YYYY-MM-DD."},
                "end_date": {"type": "string", "description": "Inclusive YYYY-MM-DD."},
                "detail": {"type": "string", "enum": ["name", "summary"]},
                "target_ref": {"type": "string", "maxLength": 80},
            }, required=["domain"]),
        ),
        types.FunctionDeclaration(
            name="read_weather",
            description=(
                "Read current weather and air quality. Pass location only when the user "
                "said a city or district; otherwise use the saved App location."
            ),
            parameters_json_schema=_schema({
                "location": {"type": "string", "maxLength": 120},
            }),
        ),
        types.FunctionDeclaration(
            name="ask_app_ayue",
            description=(
                "Ask the correct App Ayue domain to perform a reasoning or recommendation "
                "request. Use matching for new pairing, places for nearby recommendations, "
                "personality to start or continue personality exploration with Matching Ayue "
                "(including requests to get to know the user better); send every answer "
                "through personality and never invent exploration questions yourself. "
                "web for current public information, memory/profile for those App domains, "
                "and private for an accepted contact's private chat context. For places, "
                "when device location is disabled, still use the saved App profile location; "
                "ask for a location only if the App reports that no saved location exists."
            ),
            parameters_json_schema=_schema({
                "domain": {
                    "type": "string",
                    "enum": ["matching", "places", "web", "memory", "profile", "private", "personality"],
                },
                "question": {"type": "string", "maxLength": 1000},
                "contact_name": {"type": "string", "maxLength": 40},
                "target_ref": {"type": "string", "maxLength": 80},
            }, required=["domain", "question"]),
        ),
        types.FunctionDeclaration(
            name="write_app_action",
            description=(
                "Prepare one App change. For post_caption use mode=open, replace, or append; "
                "calendar_create needs title, date and time, not a street address. "
                "Pass the venue name and known area; the App automatically looks up Google Places. "
                "for select_recent_photos pass count for the newest N photos or positions "
                "for exact 1-based newest-photo positions; for block/unblock pass the spoken "
                "contact name; "
                "for calendar_update include target and any explicitly requested title, date, "
                "time, location, or notes. The Server validates all fields and requests spoken "
                "confirmation when required. Never claim success before the App result is ok."
            ),
            parameters_json_schema=_schema({
                "action": {
                    "type": "string",
                    "enum": [
                        "profile_patch", "profile_save", "setting", "post_caption",
                        "post_publish", "select_recent_photos", "calendar_create",
                        "calendar_update", "calendar_cancel", "date_respond", "date_update",
                        "date_confirm", "memory_add", "chat_send", "visible_choice", "match_start",
                        "block_user", "unblock_user",
                    ],
                },
                "changes": {"type": "object"},
                "key": {
                    "type": "string",
                    "enum": [
                        "notifications.global", "location.enabled", "ai.proactive_care",
                        "ui.liquid_glass", "ui.dark_mode",
                    ],
                },
                "enabled": {"type": "boolean"},
                "mode": {"type": "string", "enum": ["open", "replace", "append"]},
                "caption": {"type": "string", "maxLength": 2000},
                "count": {"type": "integer", "minimum": 1, "maximum": 5},
                "positions": {
                    "type": "array", "minItems": 1, "maxItems": 5,
                    "uniqueItems": True,
                    "items": {"type": "integer", "minimum": 1, "maximum": 20},
                    "description": "1-based newest-photo positions, for example [3].",
                },
                "title": {"type": "string", "maxLength": 80},
                "date": {"type": "string", "description": "YYYY-MM-DD."},
                "start_time": {"type": "string", "description": "HH:mm."},
                "end_time": {"type": "string", "description": "Optional HH:mm."},
                "location": {"type": "string", "maxLength": 120},
                "notes": {"type": "string", "maxLength": 500},
                "target": {"type": "string", "maxLength": 80},
                "contact_name": {"type": "string", "maxLength": 80},
                "accepted": {"type": "boolean"},
                "label": {"type": "string", "maxLength": 40},
                "stance": {"type": "string", "enum": ["like", "dislike", "require", "avoid"]},
                "message": {"type": "string", "maxLength": 500},
                "question": {"type": "string", "maxLength": 1000},
                "choice": {"type": "string", "enum": ["confirm", "cancel"]},
                "action_name": {"type": "string", "maxLength": 40},
                "target_ref": {"type": "string", "maxLength": 80},
            }, required=["action"]),
        ),
        types.FunctionDeclaration(
            name="open_chat",
            description=(
                "Open a named accepted contact's real chat, or set mode=private_ayue "
                "to open that contact's Ayue private conversation without asking or sending "
                "a question. Keep the spoken display name."
            ),
            parameters_json_schema=_schema({
                "contact_name": {"type": "string", "maxLength": 40},
                "target_ref": {"type": "string", "maxLength": 80},
                "mode": {"type": "string", "enum": ["chat", "private_ayue"]},
            }, required=["contact_name"]),
        ),
        types.FunctionDeclaration(
            name="get_voice_capabilities",
            description="Read the current voice permissions and feature states.",
            parameters_json_schema=empty,
        ),
        types.FunctionDeclaration(
            name="describe_current_screen",
            description="Read the safe current App screen and its available actions.",
            parameters_json_schema=empty,
        ),
        types.FunctionDeclaration(
            name="select_screen_target",
            description="Select one visible screen item using a target_ref from describe_current_screen.",
            parameters_json_schema=_schema({
                "target_ref": {"type": "string", "maxLength": 80},
            }, required=["target_ref"]),
        ),
        types.FunctionDeclaration(
            name="read_tasks",
            description="Read background task progress created by App Voice, not domain data.",
            parameters_json_schema=_schema({
                "filter": {"type": "string", "enum": ["active", "recent", "all"]},
            }, required=["filter"]),
        ),
        types.FunctionDeclaration(
            name="cancel_task",
            description="Cancel a verified cancellable background task or batch.",
            parameters_json_schema=_schema({
                "task_ref": {"type": "string", "maxLength": 240},
            }, required=["task_ref"]),
        ),
        types.FunctionDeclaration(
            name="confirm_pending_action",
            description="Use only after the Server has requested confirmation for the current action.",
            parameters_json_schema=_schema({
                "spoken_phrase": {"type": "string", "maxLength": 200},
            }, required=["spoken_phrase"]),
        ),
        types.FunctionDeclaration(
            name="resolve_pending_interaction",
            description="Confirm/cancel the current interaction or retry/dismiss/undo a verified task.",
            parameters_json_schema=_schema({
                "action": {
                    "type": "string",
                    "enum": ["confirm", "cancel", "retry", "dismiss", "undo"],
                },
                "spoken_phrase": {"type": "string", "maxLength": 200},
                "task_ref": {"type": "string", "maxLength": 240},
            }, required=["action"]),
        ),
        types.FunctionDeclaration(
            name="cancel_current_action",
            description="Cancel the current pending App action without closing voice mode.",
            parameters_json_schema=empty,
        ),
        types.FunctionDeclaration(
            name="close_voice_mode",
            description="Close voice mode only when the user explicitly asks to stop listening.",
            parameters_json_schema=empty,
        ),
        *catalog_function_declarations(types, direct_tools_available=True),
    ]
    return [types.Tool(function_declarations=declarations)]
