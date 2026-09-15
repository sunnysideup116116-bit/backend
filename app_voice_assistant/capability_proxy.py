"""Bounded capability discovery and invocation for App Voice protocol v4."""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any

from .capabilities import ACTIONS, ARGUMENT_SCHEMAS, CATALOG
from .contracts import (
    VoiceProposal,
    context_allows_proposal,
    deterministic_proposal,
    requires_confirmation,
    validate_proposal,
)
from .experience import discovery_experience


CAPABILITY_REF_TTL_SECONDS = 120
MAX_CAPABILITY_MATCHES = 4
MAX_OPERATIONS = 8

NON_PROXY_ACTIONS = frozenset({
    "assistant.cancel", "assistant.close", "assistant.reply", "ui.choice.activate",
})
ACTION_LABELS = {
    action_id: str(spec["title"]) for action_id, spec in ACTIONS.items()
}
ACTION_ALIASES = {
    action_id: [str(item) for item in spec.get("aliases") or []]
    for action_id, spec in ACTIONS.items()
}


class CapabilityRefError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def _permission_hash(context: dict[str, Any]) -> str:
    permissions = sorted(
        str(key) for key, value in (context.get("permissions") or {}).items()
        if value is True
    )
    routines = [
        {
            "name": str(item.get("name") or "")[:30],
            "template_id": str(item.get("template_id") or "")[:40],
        }
        for item in (context.get("voice_config") or {}).get("routines") or []
        if isinstance(item, dict)
    ][:8]
    raw = json.dumps(
        {"permissions": permissions, "routines": routines},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def action_permissions_allowed(action_id: str, context: dict[str, Any]) -> bool:
    spec = ACTIONS.get(action_id)
    if not isinstance(spec, dict):
        return False
    permission = spec.get("permission")
    permissions = context.get("permissions") or {}
    required_permissions = [
        item for item in [permission, *(spec.get("discovery_permissions") or [])]
        if item is not None
    ]
    if any(permissions.get(item) is not True for item in required_permissions):
        return False
    return True


def action_allowed(action_id: str, context: dict[str, Any]) -> bool:
    if not action_permissions_allowed(action_id, context):
        return False
    if action_id in {"routine.run", "routine.delete"} and not (
        (context.get("voice_config") or {}).get("routines") or []
    ):
        return False
    if action_id == "post.request_publish" and context.get("can_publish") is not True:
        return False
    if action_id == "ui.target.select":
        screen = context.get("screen") if isinstance(context.get("screen"), dict) else {}
        if not (screen.get("items") or []):
            return False
    return True


def action_metadata(action_id: str) -> dict[str, Any]:
    spec = ACTIONS[action_id]
    result = {
        "capability_id": action_id,
        "title": str(spec.get("title") or action_id),
        "domain": str(spec.get("domain") or action_id.split(".", 1)[0]),
        "parameters": list(spec.get("parameters") or []),
        "arguments_schema": copy.deepcopy(ARGUMENT_SCHEMAS[action_id]),
        "confirmation_required": bool(spec.get("confirmation")),
        "risk": str(spec.get("risk") or "control"),
        "execution_kind": str(spec.get("execution_kind") or "inline_device"),
        "cancellable": bool(spec.get("cancellable")),
        "progress_percent_supported": bool(spec.get("progress_percent_supported")),
    }
    routine_templates = spec.get("routine_templates")
    if isinstance(routine_templates, dict):
        result["routine_templates"] = copy.deepcopy(routine_templates)
    return result


def suggested_arguments(
    query: str, action_id: str, context: dict[str, Any],
) -> dict[str, Any] | None:
    """Return server-derived arguments only when they are deterministic and safe.

    Dynamic schemas are data inside a tool result, so realtime models may omit
    an otherwise obvious read parameter. These defaults never invent a write
    value and are cached server-side against the signed capability ref.
    """
    proposal = deterministic_proposal(str(query or ""), context=context)
    if proposal is not None and proposal.intent == action_id:
        return copy.deepcopy(proposal.arguments)
    read_defaults: dict[str, dict[str, Any]] = {
        "app.digest.query": {},
        "calendar.query": {"source": "all", "range": "upcoming"},
        "contacts.query": {},
        "date.query": {"contact_name": ""},
        "match.query": {"view": "status"},
        "memory.query": {"query": ""},
        "self.query": {"detail": "summary"},
        "weather.query": {"location": ""},
        "workflow.daily_briefing": {},
    }
    default = read_defaults.get(action_id)
    return copy.deepcopy(default) if default is not None else None


def _candidate_score(normalized: str, raw: Any) -> int:
    candidate = _normalize(raw)
    if not candidate:
        return 0
    if candidate == normalized:
        return 24
    if candidate in normalized:
        return 12 + min(len(candidate), 10)
    if normalized in candidate:
        return 7
    query_chars = set(normalized)
    overlap = len(query_chars & set(candidate))
    return overlap if overlap >= 2 else 0


def _score(query: str, guide: dict[str, Any], action_id: str) -> int:
    normalized = _normalize(query)
    if not normalized:
        return 0
    action_fields = [
        action_id, ACTION_LABELS.get(action_id), *(ACTION_ALIASES.get(action_id) or []),
    ]
    guide_fields = [
        guide.get("title"), guide.get("summary"), *(guide.get("keywords") or []),
    ]
    normalized_label = _normalize(ACTION_LABELS.get(action_id))
    score = (
        100
        if normalized_label == normalized
        else 80
        if normalized_label and normalized_label in normalized
        else 0
    )
    for raw in action_fields:
        score += _candidate_score(normalized, raw)
    # Guide copy helps retrieval, but it must not make a broad domain phrase
    # look like a precise executable action.
    for raw in guide_fields:
        score += min(_candidate_score(normalized, raw), 12)
    return score


def _explicit_action_match(query: str, action_id: str) -> bool:
    normalized = _normalize(query)
    if not normalized:
        return False
    for raw in [ACTION_LABELS.get(action_id), *(ACTION_ALIASES.get(action_id) or [])]:
        candidate = _normalize(raw)
        if len(candidate) >= 2 and (candidate == normalized or candidate in normalized):
            return True
    return False


def _explicit_match_length(query: str, action_id: str) -> int:
    normalized = _normalize(query)
    if not normalized:
        return 0
    return max((
        len(candidate)
        for raw in [ACTION_LABELS.get(action_id), *(ACTION_ALIASES.get(action_id) or [])]
        if (candidate := _normalize(raw))
        and len(candidate) >= 2
        and (candidate == normalized or candidate in normalized)
    ), default=0)


@dataclass(frozen=True)
class CapabilityRefSigner:
    secret: bytes

    def issue(
        self, action_id: str, *, user_id: str, session_id: str,
        context: dict[str, Any], now: float | None = None,
    ) -> str:
        instant = int(time.time() if now is None else now)
        payload = {
            "v": 1,
            "a": action_id,
            "u": str(user_id)[:128],
            "s": str(session_id)[:128],
            "c": int(CATALOG["version"]),
            "p": _permission_hash(context),
            "o": str(context.get("scope") or "global")[:80],
            "r": int(context.get("revision") or 0),
            "e": instant + CAPABILITY_REF_TTL_SECONDS,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        signature = hmac.new(self.secret, encoded.encode("ascii"), hashlib.sha256).digest()
        signed = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        return f"cap1.{encoded}.{signed}"

    def verify(
        self, ref: str, *, user_id: str, session_id: str,
        context: dict[str, Any], now: float | None = None,
    ) -> str:
        try:
            prefix, encoded, signed = str(ref or "").split(".", 2)
            if prefix != "cap1":
                raise ValueError
            expected = base64.urlsafe_b64encode(
                hmac.new(self.secret, encoded.encode("ascii"), hashlib.sha256).digest()
            ).rstrip(b"=").decode("ascii")
            if not hmac.compare_digest(expected, signed):
                raise CapabilityRefError("capability_ref_invalid")
            padding = "=" * (-len(encoded) % 4)
            payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
        except CapabilityRefError:
            raise
        except Exception as exc:
            raise CapabilityRefError("capability_ref_invalid") from exc
        instant = int(time.time() if now is None else now)
        if int(payload.get("e", 0) or 0) < instant:
            raise CapabilityRefError("capability_ref_expired")
        if (
            payload.get("u") != str(user_id)[:128]
            or payload.get("s") != str(session_id)[:128]
        ):
            raise CapabilityRefError("capability_ref_owner_mismatch")
        if int(payload.get("c", 0) or 0) != int(CATALOG["version"]):
            raise CapabilityRefError("capability_ref_catalog_stale")
        if (
            payload.get("p") != _permission_hash(context)
            or payload.get("o") != str(context.get("scope") or "global")[:80]
            or int(payload.get("r", -1)) != int(context.get("revision") or 0)
        ):
            raise CapabilityRefError("capability_ref_context_stale")
        action_id = str(payload.get("a") or "")
        if action_id not in ACTIONS or not action_allowed(action_id, context):
            raise CapabilityRefError("capability_ref_permission_denied")
        return action_id


def find_capabilities(
    query: str, mode: str, *, context: dict[str, Any], signer: CapabilityRefSigner,
    user_id: str, session_id: str,
) -> dict[str, Any]:
    selected_mode = mode if mode in {"explain", "perform"} else "explain"
    query_text = str(query or "")
    deterministic = deterministic_proposal(query_text, context=context)
    normalized_query_text = _normalize(query_text)
    google_calendar_write = (
        any(marker in normalized_query_text for marker in (
            "google日曆", "google行事曆", "googlecalendar",
        ))
        and any(marker in normalized_query_text for marker in (
            "新增", "建立", "加入", "修改", "改成", "改到", "取消", "刪除",
            "create", "add", "update", "change", "cancel", "delete",
        ))
    )
    longest_explicit = max(
        (_explicit_match_length(query, action_id) for action_id in ACTIONS),
        default=0,
    )
    deterministic_authoritative = (
        deterministic is not None
        and (
            longest_explicit == 0
            or _explicit_match_length(query, deterministic.intent) == longest_explicit
            or deterministic.intent in {
                "ayue.private_query", "chat.open",
                "match.ayue_query",
            }
            or (
                deterministic.intent == "ayue.public_query"
                and deterministic.arguments.get("domain") == "places"
            )
        )
    )
    semantic_permission_denied = False
    matches: list[tuple[int, str, dict[str, Any], list[dict[str, Any]]]] = []
    for guide_id, raw_guide in (CATALOG.get("guides") or {}).items():
        guide = dict(raw_guide)
        action_rows: list[dict[str, Any]] = []
        best = 0
        for action_id in guide.get("action_ids") or []:
            if action_id not in ACTIONS:
                continue
            action_score = _score(query, guide, action_id)
            if (
                deterministic_authoritative
                and deterministic is not None
                and deterministic.intent == action_id
            ):
                action_score += 500
            best = max(best, action_score)
            metadata = action_metadata(action_id)
            available = action_allowed(action_id, context)
            blocked_google_action = (
                google_calendar_write
                and action_id in {
                    "calendar.create", "calendar.update", "calendar.cancel",
                }
            )
            if blocked_google_action:
                available = False
            permission = ACTIONS[action_id].get("permission")
            required_permissions = [
                item for item in [
                    permission,
                    *(ACTIONS[action_id].get("discovery_permissions") or []),
                ]
                if item is not None
            ]
            missing_permissions = [
                item for item in required_permissions
                if (context.get("permissions") or {}).get(item) is not True
            ]
            if (
                available
                and deterministic_authoritative
                and deterministic is not None
                and deterministic.intent == action_id
                and not context_allows_proposal(context, deterministic)
            ):
                available = False
                if (
                    action_id == "match.ayue_query"
                    and (context.get("permissions") or {}).get("match_actions")
                    is not True
                ):
                    missing_permissions.append("match_actions")
                    semantic_permission_denied = True
            if (
                deterministic is not None
                and deterministic.intent == action_id
                and requires_confirmation(deterministic)
            ):
                # Some confirmation rules depend on the actual utterance
                # (for example, starting a new match search). Keep discovery
                # metadata honest so the model does not treat a dynamic write
                # boundary as a read-only capability.
                metadata["confirmation_required"] = True
            row = {**metadata, "available": available, "_match_score": action_score}
            if missing_permissions:
                row["missing_permissions"] = list(dict.fromkeys(missing_permissions))
            if (
                deterministic_authoritative
                and deterministic is not None
                and deterministic.intent == action_id
                and missing_permissions
                and action_id in {
                    "ayue.private_query", "ayue.public_query", "chat.open",
                    "match.ayue_query",
                }
            ):
                semantic_permission_denied = True
            if blocked_google_action:
                row["unavailable_reason"] = "google_calendar_read_only"
            if action_id == "post.request_publish" and context.get("can_publish") is not True:
                row["unavailable_reason"] = "post_media_not_ready"
            if action_id == "ui.target.select" and not available:
                row["unavailable_reason"] = "screen_target_unavailable"
            if action_id in {"routine.run", "routine.delete"} and not available:
                row.setdefault("unavailable_reason", "no_saved_routines")
            action_rows.append(row)
        action_rows.sort(key=lambda row: (
            -int(row["_match_score"]), str(row["capability_id"]),
        ))
        if best:
            matches.append((best, str(guide_id), guide, action_rows))

    normalized_query = _normalize(query)
    for index, routine in enumerate(
        (context.get("voice_config") or {}).get("routines") or []
    ):
        if not isinstance(routine, dict):
            continue
        routine_name = str(routine.get("name") or "").strip()[:30]
        score = _candidate_score(normalized_query, routine_name)
        if not routine_name or score <= 0:
            continue
        metadata = action_metadata("routine.run")
        matches.append((
            score + 30,
            f"saved_routine_{index + 1}",
            {
                "title": f"個人捷徑：{routine_name}"[:80],
                "summary": "這是你之前儲存的安全語音捷徑。",
                "steps": [f"執行「{routine_name}」", "執行時會再檢查當下權限"],
                "requirements": ["捷徑需仍存在這個 App 裝置"],
                "limitations": ["捷徑不會跳過權限與確認邊界"],
            },
            [{
                **metadata,
                "available": True,
                "suggested_arguments": {"name": routine_name},
                "_match_score": score + 30,
                "_dynamic_exact": True,
            }],
        ))
    matches.sort(key=lambda item: (-item[0], item[1]))
    limit = MAX_CAPABILITY_MATCHES if selected_mode == "perform" else 3
    selected_matches = matches[:limit]
    executable_ids: set[str] = set()
    dynamic_executable_guides: set[str] = set()
    if selected_mode == "perform":
        dynamic_matches = [
            (score, guide_id)
            for score, guide_id, _guide, actions in selected_matches
            if any(row.get("_dynamic_exact") is True for row in actions)
        ]
        if dynamic_matches:
            best_dynamic = max(score for score, _guide_id in dynamic_matches)
            best_guides = {
                guide_id for score, guide_id in dynamic_matches
                if score == best_dynamic
            }
            if len(best_guides) == 1:
                dynamic_executable_guides = best_guides
        candidate_rows = [
            row
            for _score_value, _guide_id, _guide, actions in selected_matches
            for row in actions
            if row["available"] and row["capability_id"] not in NON_PROXY_ACTIONS
        ]
        candidates_by_id: dict[str, dict[str, Any]] = {}
        for row in candidate_rows:
            action_id = str(row["capability_id"])
            current = candidates_by_id.get(action_id)
            if current is None or int(row["_match_score"]) > int(current["_match_score"]):
                candidates_by_id[action_id] = row
        candidates = list(candidates_by_id.values())
        multi_intent = any(marker in query_text.lower() for marker in (
            "然後", "並且", "同時", "再查", "再打開", " and ", " then ",
        ))
        if deterministic_authoritative and deterministic is not None and not multi_intent:
            executable_ids = {
                deterministic.intent
                for row in candidates
                if str(row["capability_id"]) == deterministic.intent
            }
        else:
            executable_ids = {
                str(row["capability_id"])
                for row in candidates
                if (
                    row.get("_dynamic_exact") is True
                    and any(
                        guide_id in dynamic_executable_guides
                        for _score_value, guide_id, _guide, guide_actions in selected_matches
                        if row in guide_actions
                    )
                )
                or _explicit_action_match(query, str(row["capability_id"]))
                or (
                    deterministic_authoritative
                    and deterministic is not None
                    and deterministic.intent == str(row["capability_id"])
                )
            }

    projected = []
    for score, guide_id, guide, actions in selected_matches:
        projected_actions: list[dict[str, Any]] = []
        actions_to_project = actions
        if selected_mode == "perform":
            executable_actions = [
                row for row in actions
                if str(row["capability_id"]) in executable_ids
            ]
            # A perform response is an execution routing result, not the full
            # help catalog. Keep every explicit executable in this guide, or
            # only its best clarification candidate when none is executable.
            actions_to_project = executable_actions or actions[:1]
        for raw_action in actions_to_project:
            action = {
                key: value for key, value in raw_action.items()
                if key not in {"_match_score", "_dynamic_exact"}
            }
            action_id = str(action["capability_id"])
            can_issue_dynamic = (
                raw_action.get("_dynamic_exact") is not True
                or guide_id in dynamic_executable_guides
            )
            if (
                action_id == "routine.run"
                and raw_action.get("_dynamic_exact") is not True
                and dynamic_executable_guides
                and not _explicit_action_match(query, action_id)
            ):
                can_issue_dynamic = False
            if (
                selected_mode == "perform"
                and action_id in executable_ids
                and can_issue_dynamic
            ):
                capability_ref = signer.issue(
                    action_id, user_id=user_id, session_id=session_id, context=context,
                )
                action["capability_ref"] = capability_ref
                if "suggested_arguments" not in action:
                    defaults = suggested_arguments(query, action_id, context)
                    if defaults is not None:
                        action["suggested_arguments"] = defaults
            else:
                # How-to and ambiguous results need guidance, not every
                # executable schema. This keeps dynamic tool responses small.
                action.pop("arguments_schema", None)
            projected_actions.append(action)
        projected.append({
            "guide_id": guide_id,
            "title": str(guide.get("title") or guide_id)[:80],
            "summary": str(guide.get("summary") or "")[:500],
            "steps": [str(item)[:240] for item in (guide.get("steps") or [])[:6]],
            "requirements": [str(item)[:240] for item in (guide.get("requirements") or [])[:6]],
            "limitations": [str(item)[:240] for item in (guide.get("limitations") or [])[:6]],
            **(
                {"destination": str(guide.get("destination"))[:40]}
                if guide.get("destination") else {}
            ),
            "actions": projected_actions,
            "score": score,
        })
    has_executable_ref = any(
        "capability_ref" in action
        for match in projected for action in match["actions"]
    )
    result = {
        "status": (
            "ok" if projected and (selected_mode == "explain" or has_executable_ref)
            else "needs_clarification" if projected
            else "not_found"
        ),
        "mode": selected_mode,
        "catalog_version": CATALOG["version"],
        "matches": projected,
        "message": (
            "只使用搜尋結果說明或執行；沒有 capability_ref 的項目不可執行。"
            if projected and (selected_mode == "explain" or has_executable_ref)
            else "找到相關功能，但無法唯一確定要執行哪一項；請先追問。"
            if projected
            else "找不到可驗證的 App 功能，請要求使用者說得更具體。"
        ),
    }
    if selected_mode == "perform" and has_executable_ref:
        recommended_operations = [
            {
                "operation_key": f"op{index}",
                "capability_ref": action["capability_ref"],
                "arguments": copy.deepcopy(action.get("suggested_arguments") or {}),
            }
            for index, action in enumerate((
                action
                for match in projected
                for action in match["actions"]
                if action.get("capability_ref")
            ), start=1)
        ]
        result["recommended_operations"] = recommended_operations
        result["next_step"] = (
            "Call run_app_capabilities now and copy recommended_operations exactly."
        )
    if any(match.get("guide_id") == "routines" for match in projected):
        result["saved_routines"] = [
            {
                "name": str(item.get("name") or "")[:30],
                "template_id": str(item.get("template_id") or "")[:40],
            }
            for item in (context.get("voice_config") or {}).get("routines") or []
            if isinstance(item, dict)
        ][:8]
    result.update(discovery_experience(projected, mode=selected_mode))
    if selected_mode == "perform" and google_calendar_write:
        result["status"] = "not_supported"
        result["message"] = (
            "Google 日曆目前在語音助理中只能查詢，"
            "不會改寫 App 個人行事曆或共同約會。"
        )
        result.pop("recommended_operations", None)
        result.pop("next_step", None)
    if (
        selected_mode == "perform"
        and not has_executable_ref
        and semantic_permission_denied
        and isinstance(result.get("permission_repair"), dict)
    ):
        result["status"] = "permission_denied"
        result["message"] = str(result["permission_repair"].get("message") or "")
    return result


def proposal_from_capability(
    action_id: str, arguments: Any, *, revision: int,
) -> VoiceProposal | None:
    if action_id not in ACTIONS or action_id in NON_PROXY_ACTIONS:
        return None
    values = dict(arguments) if isinstance(arguments, dict) else {}
    allowed = set(ACTIONS[action_id].get("parameters") or [])
    if set(values) - allowed:
        return None
    return validate_proposal(
        {"intent": action_id, "arguments": values, "reply": "好的，我來處理。"},
        base_revision=revision,
    )
