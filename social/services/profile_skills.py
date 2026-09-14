"""Owner-scoped Profile Router for recent context and durable memory."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any, Callable

from bson.objectid import ObjectId
from pymongo.errors import DuplicateKeyError

from database import db, profiles_coll, messages_coll
from services.ai_service import generate_chat_completion, get_embedding
from services.profile_contracts import ProfileExtractionDecision
from services.profile_projection import (
    INTERNAL_ID_RE as ID_RE,
    PROTECTED_CONTENT_RE as PROTECTED_RE,
    clean_profile_text,
    contains_internal_identifier,
    contains_protected_content,
    render_recent_context,
    safe_recent_context,
)
from services.message_use_service import MessageUse, message_use
from services.match_search_context import context_embedding_source_hash
from services.proactive_followup_service import (
    apply_followup_proposal,
    followup_candidate_snapshot,
    followup_mode_for_user,
    is_proactive_care_enabled,
    validate_followup_proposal,
)
from services.skill_loader import load_profile_skill

PROFILE_RUNS = db["profile_skill_runs"]
NO_STORE_RE = re.compile(r"(?:不要記|別記|不用記|不必記)")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,50}$")
TRAVEL_ACTIVITY_ALIASES = {"travel", "travelling", "traveling", "trip", "旅遊", "旅行", "出國玩"}
RECENT_FIELD_NAMES = ("activity", "destination", "timing", "companion_intent", "temporal_status")
RECENT_TIMINGS = {"昨天", "前天", "今天", "明天", "後天", "最近", "近期", "本週", "下週", "本月", "下個月"}
RECENT_TEMPORAL_STATUSES = {"past", "current", "planned"}
RECENT_EPISODE_TTL_SECONDS = 30 * 60
PROFILE_MAX_ATTEMPTS = 3
PROFILE_LEASE_SECONDS = 90
PROFILE_RETRY_DELAYS = (30, 120)


def _activity_from_owner_message(activity: str, message: str) -> str:
    """Keep recent-context activity in Traditional Chinese and recover a stated destination."""
    cleaned = _clean(activity, 28)
    lowered = cleaned.lower()
    if lowered in TRAVEL_ACTIVITY_ALIASES:
        destination = re.search(r"(?:想|要|計畫|打算|最近想)?去(?P<place>[\u4e00-\u9fff]{1,12})(?:玩|旅行|旅遊)", message)
        return f"去{destination.group('place')}旅行" if destination else "旅行"
    # Context summaries are user-facing; reject untranslated model placeholders.
    if re.search(r"[a-z]", lowered):
        return ""
    # A model may preserve whitespace between activities from the owner text.
    # Display it as a list without guessing any extra activity.
    return re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "、", cleaned)


def _field_value(name: str, value: Any, owner_message: str) -> str:
    value = _clean(value, 40)
    if not value or ID_RE.search(value) or PROTECTED_RE.search(value):
        return ""
    if name == "timing":
        return value if value in RECENT_TIMINGS else ""
    if name == "temporal_status":
        return value if value in RECENT_TEMPORAL_STATUSES else ""
    if name == "companion_intent":
        aliases = {"solo": "自己", "自己去": "自己", "獨自": "自己", "social": "找人同行", "一起": "找人同行", "想找人同行": "找人同行"}
        return aliases.get(value.lower(), value) if aliases.get(value.lower(), value) in {"自己", "找人同行"} else ""
    if name == "activity":
        if value.lower() in TRAVEL_ACTIVITY_ALIASES:
            return "旅行"
        return _activity_from_owner_message(value, owner_message)
    return value


def _compose_recent_context_summary(fields: dict[str, Any]) -> str:
    """Let the LLM phrase typed owner fields, then validate its short result."""
    fallback = render_recent_context(fields)
    typed_fields = {
        name: _clean((fields.get(name) or {}).get("value"), 40)
        for name in RECENT_FIELD_NAMES
        if _clean((fields.get(name) or {}).get("value"), 40)
    }
    if not typed_fields:
        return ""
    prompt = f"""把以下已驗證的本人近期情境欄位整理成一句自然、精簡的繁體中文。
只能重組提供的欄位，不可補充地點、日期、人物、偏好或配對狀態。
依 temporal_status 保留時態：past 是已做過、current 是正在做、planned 才能使用「想／規劃」。沒有 temporal_status 時使用中性「近期活動」，不可擅自改成未來計畫。
避免「前往市集去市集」這類重複語意；多個活動以「、」自然分隔。
最多 32 個中文字，不加引號、不加解釋，只輸出句子。
欄位：{json.dumps(typed_fields, ensure_ascii=False)}"""
    try:
        summary = _clean(generate_chat_completion(prompt, temperature=0.2).content, 48)
    except Exception:
        return fallback
    if (
        not summary
        or ID_RE.search(summary)
        or PROTECTED_RE.search(summary)
        or safe_recent_context(summary, "") != summary
    ):
        return fallback
    destination = typed_fields.get("destination")
    activity = typed_fields.get("activity")
    if destination and destination not in summary:
        return fallback
    if not destination and activity and activity not in summary:
        return fallback
    verified_chars = set("".join(typed_fields.values()))
    structural_chars = set("近期最近昨天前天今天明天後天本週下週本月下個月活動有在做過想要打算計畫規劃準備前往去：，、")
    allowed_chars = verified_chars | structural_chars
    if any("\u4e00" <= char <= "\u9fff" and char not in allowed_chars for char in summary):
        return fallback
    return summary


def _source_is_newer(candidate: dict[str, Any], existing: dict[str, Any] | None) -> bool:
    if not existing:
        return True
    candidate_key = (float(candidate.get("source_timestamp", 0) or 0), str(candidate.get("evidence_message_id") or ""))
    existing_key = (float(existing.get("source_timestamp", 0) or 0), str(existing.get("evidence_message_id") or ""))
    return candidate_key > existing_key


def _timestamp(value: Any) -> float:
    if hasattr(value, "timestamp"):
        try:
            return float(value.timestamp())
        except (TypeError, ValueError, OSError):
            return 0.0
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _active_recent_episode(profile: dict[str, Any], *, now: float | None = None) -> dict[str, Any] | None:
    """Return a bounded typed episode; never expose evidence or raw history."""
    reference = time.time() if now is None else float(now)
    state = profile.get("recent_context_state") or {}
    fields = state.get("fields") or {}
    if isinstance(fields, dict) and fields:
        updated_at = _timestamp(state.get("updated_at") or profile.get("recent_context_updated_at"))
        if updated_at and reference - updated_at <= RECENT_EPISODE_TTL_SECONDS:
            episode_id = str(state.get("episode_id") or "")
            if not episode_id:
                plan_ids = {
                    str((fields.get(name) or {}).get("plan_id") or "")
                    for name in RECENT_FIELD_NAMES if isinstance(fields.get(name), dict)
                }
                plan_ids.discard("")
                if len(plan_ids) == 1:
                    episode_id = plan_ids.pop()
            projection = {
                name: _clean((fields.get(name) or {}).get("value"), 40)
                for name in RECENT_FIELD_NAMES
                if isinstance(fields.get(name), dict) and _clean((fields.get(name) or {}).get("value"), 40)
            }
            if episode_id and projection:
                return {"episode_id": episode_id[:128], "fields": projection}
    draft = profile.get("recent_context_draft") or {}
    created_at = _timestamp(draft.get("created_at")) if isinstance(draft, dict) else 0.0
    if (
        created_at and reference - created_at <= RECENT_EPISODE_TTL_SECONDS
        and draft.get("goal") == "activity_or_destination"
    ):
        return {
            "episode_id": f"draft:{int(created_at * 1000)}"[:128],
            "fields": {},
            "goal": "activity_or_destination",
        }
    return None


def profile_skills_mode_for_user(user_id: str) -> str:
    mode = os.getenv("AYUE_PROFILE_SKILLS_MODE", "off").strip().lower()
    if mode not in {"off", "shadow", "on"}:
        mode = "off"
    allowlist = {item.strip() for item in os.getenv("AYUE_PROFILE_SKILLS_USER_ALLOWLIST", "").split(",") if item.strip()}
    return mode if not allowlist or user_id in allowlist else "off"


def ensure_profile_skill_indexes() -> None:
    try:
        PROFILE_RUNS.create_index("created_at", expireAfterSeconds=14 * 86400)
        PROFILE_RUNS.create_index("message_id", unique=True, sparse=True)
        PROFILE_RUNS.create_index([("user_id", 1), ("created_at", -1)])
        PROFILE_RUNS.create_index(
            [("status", 1), ("next_attempt_at", 1)],
            name="profile_retry_queue",
        )
    except Exception as exc:
        print(f"Profile skill index setup skipped: {exc}")


def _trace(user_id: str, mode: str, payload: dict[str, Any]) -> None:
    payload = dict(payload or {})
    run_status = str(payload.pop("_run_status", "completed") or "completed")
    next_attempt_at = payload.pop("_next_attempt_at", None)
    lease_token = payload.pop("_lease_token", None)
    doc = {
        "user_id": user_id,
        "skill": "profile_router",
        "mode": mode,
        "status": run_status,
        "updated_at": time.time(),
        "created_at": time.time(),
        **payload,
    }
    if next_attempt_at is not None:
        doc["next_attempt_at"] = float(next_attempt_at)
    try:
        if doc.get("message_id"):
            update: dict[str, Any] = {
                "$set": doc,
                "$unset": {"lease_until": "", "lease_token": ""},
            }
            if next_attempt_at is None:
                update["$unset"]["next_attempt_at"] = ""
            query = {"message_id": doc["message_id"]}
            if lease_token:
                query["lease_token"] = lease_token
            PROFILE_RUNS.update_one(query, update, upsert=not bool(lease_token))
        else:
            PROFILE_RUNS.insert_one(doc)
    except Exception as exc:
        print(f"Profile skill trace skipped: {exc}")


def _claim_profile_message(user_id: str, message_id: str, mode: str) -> str | None:
    """Atomically claim a source, allowing only bounded transient retries."""
    now = time.time()
    lease_token = uuid.uuid4().hex
    try:
        result = PROFILE_RUNS.update_one(
            {
                "message_id": message_id,
                "$or": [
                    {"status": {"$exists": False}},
                    {
                        "status": "processing",
                        "attempt_count": {"$lt": PROFILE_MAX_ATTEMPTS},
                        "lease_until": {"$lte": now},
                    },
                    {
                        "status": "processing",
                        "attempt_count": {"$lt": PROFILE_MAX_ATTEMPTS},
                        "lease_until": {"$exists": False},
                    },
                    {
                        "status": "failed",
                        "attempt_count": {"$lt": PROFILE_MAX_ATTEMPTS},
                        "$or": [
                            {"next_attempt_at": {"$lte": now}},
                            {"next_attempt_at": {"$exists": False}},
                        ],
                    },
                ],
            },
            {
                "$set": {
                    "user_id": user_id,
                    "skill": "profile_router",
                    "mode": mode,
                    "status": "processing",
                    "lease_until": now + PROFILE_LEASE_SECONDS,
                    "lease_token": lease_token,
                    "attempt_started_at": now,
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "message_id": message_id,
                    "created_at": now,
                },
                "$inc": {"attempt_count": 1},
            },
            upsert=True,
        )
        return lease_token if (
            getattr(result, "upserted_id", None) is not None
            or getattr(result, "modified_count", 0)
        ) else None
    except DuplicateKeyError:
        return False
    except Exception as exc:
        print(f"Profile skill claim failed: {type(exc).__name__}")
        return None


def _profile_claim_is_current(message_id: str, lease_token: str) -> bool:
    try:
        return bool(PROFILE_RUNS.find_one({
            "message_id": message_id,
            "status": "processing",
            "lease_token": lease_token,
            "lease_until": {"$gt": time.time()},
        }, {"_id": 1}))
    except Exception:
        return False


def _renew_profile_claim(message_id: str, lease_token: str) -> bool:
    """Extend a live claim after model work and before owner-data writes."""
    now = time.time()
    try:
        result = PROFILE_RUNS.update_one(
            {
                "message_id": message_id,
                "status": "processing",
                "lease_token": lease_token,
                "lease_until": {"$gt": now},
            },
            {"$set": {
                "lease_until": now + PROFILE_LEASE_SECONDS,
                "updated_at": now,
            }},
        )
        return bool(
            getattr(result, "matched_count", 0)
            or getattr(result, "modified_count", 0)
        )
    except Exception:
        return False


def _clean(value: Any, limit: int = 60) -> str:
    return clean_profile_text(value, limit)


def _safe_context_text(value: Any) -> str:
    return safe_recent_context(value, "")


def _confidence(value: Any) -> float:
    try:
        result = float(value or 0)
        return max(0.0, min(result / 100 if result > 1 else result, 1.0))
    except (TypeError, ValueError):
        return 0.0


def _valid_evidence_span(value: Any, owner_message: str) -> str:
    span = _clean(value, 160)
    return span if span and span in owner_message else ""


def _validate_memory(item: dict[str, Any], owner_message: str) -> tuple[dict[str, Any] | None, str]:
    key = str(item.get("key", "")).strip().lower().replace(" ", "_")
    label = _clean(item.get("label_zh_tw") or item.get("label"), 40)
    stance = str(item.get("stance", "")).strip()
    category = str(item.get("category", "lifestyle")).strip()[:30]
    confidence = _confidence(item.get("confidence"))
    evidence_span = _valid_evidence_span(item.get("evidence_span"), owner_message)
    minimum = 0.90
    if not KEY_RE.match(key):
        return None, "invalid_key"
    if not label or ID_RE.search(label) or PROTECTED_RE.search(label):
        return None, "unsafe_or_protected_label"
    if stance not in {"like", "dislike", "require", "avoid"}:
        return None, "invalid_stance"
    if confidence < minimum:
        return None, "low_confidence"
    if not evidence_span:
        return None, "invalid_evidence_span"
    if item.get("subject") != "owner":
        return None, "not_owner_attribution"
    return {"key": key, "label": label, "stance": stance, "category": category,
            "confidence": confidence, "evidence_span": evidence_span,
            "reason_code": str(item.get("reason_code") or "accepted")[:60]}, "accepted"


def _validated_recent_proposal(raw_recent: Any, message: str, plan_id: str | None) -> tuple[dict[str, Any], bool]:
    """Validate recent-context fields independently and report retry eligibility."""
    patches: dict[str, dict[str, Any]] = {}
    rejected_field = False
    invalid_evidence = False
    unsafe_value = False
    for name, candidate in raw_recent.fields.items():
        if name not in RECENT_FIELD_NAMES or candidate.subject != "owner":
            rejected_field = True
            continue
        evidence_span = _valid_evidence_span(candidate.evidence_span, message)
        raw_value = _clean(candidate.value, 40)
        if raw_value and (ID_RE.search(raw_value) or PROTECTED_RE.search(raw_value)):
            # An internal identifier or protected-content value is not a
            # harmless missing sibling. Reject this whole model proposal so a
            # malformed field cannot piggyback unrelated inferred patches.
            unsafe_value = True
            rejected_field = True
            continue
        value = _field_value(name, candidate.value, message)
        field_confidence = _confidence(candidate.confidence)
        if candidate.operation == "clear":
            if evidence_span and field_confidence >= 0.90:
                patches[name] = {"operation": "clear", "evidence_span": evidence_span, "confidence": field_confidence}
            else:
                rejected_field = True
                invalid_evidence = True
        elif value and evidence_span and field_confidence >= 0.90:
            patches[name] = {"operation": "set", "value": value, "evidence_span": evidence_span, "confidence": field_confidence}
        else:
            rejected_field = True
            invalid_evidence = invalid_evidence or bool(candidate.value) and not evidence_span

    recent_confidence = _confidence(raw_recent.confidence)
    should_update = (
        raw_recent.action in {"update", "clear"}
        and raw_recent.message_kind == "real_world_update"
        and raw_recent.episode_relation != "unrelated"
        and recent_confidence >= 0.90 and bool(patches) and not unsafe_value
    )
    if unsafe_value:
        patches = {}
    if raw_recent.action == "clear" and any(patch.get("operation") != "clear" for patch in patches.values()):
        should_update = False
        rejected_field = True
    compatibility = {name: (patches.get(name) or {}).get("value") for name in RECENT_FIELD_NAMES}
    preview_fields = {
        name: {"value": patch["value"]}
        for name, patch in patches.items()
        if patch.get("operation") == "set" and patch.get("value")
    }
    reason_code = str(raw_recent.reason_code or ("accepted" if should_update else "invalid_recent_patch"))[:60]
    if unsafe_value:
        reason_code = "unsafe_recent_value"
    elif invalid_evidence and not should_update:
        reason_code = "invalid_evidence_span"
    elif rejected_field and should_update:
        reason_code = "partial_fields_rejected"
    if not should_update and raw_recent.action == "none":
        reason_code = str(raw_recent.reason_code or "no_recent_context")[:60]
    recent = {
        "should_update": should_update, "confidence": recent_confidence, **compatibility,
        "fields": patches, "message_kind": raw_recent.message_kind,
        "context_action": raw_recent.action,
        "episode_relation": raw_recent.episode_relation,
        "evidence_span": next((item.get("evidence_span") for item in patches.values() if item.get("evidence_span")), ""),
        "summary_zh_tw": render_recent_context(preview_fields), "plan_id": plan_id or "",
        "reason_code": reason_code,
    }
    retry_needed = (
        raw_recent.action == "update"
        and raw_recent.message_kind == "real_world_update"
        and recent_confidence >= 0.90
        and not patches and not unsafe_value
    )
    return recent, retry_needed


def _retry_recent_context_contract(
    message: str, recent_skill: dict[str, Any], active_episode: dict[str, Any] | None,
) -> Any | None:
    """One bounded retry for a real activity whose first extraction omitted fields."""
    prompt = f"""你是阿月的近期情境欄位抽取器。只能讀取一則已儲存的本人原始訊息，不能讀取歷史、助理回覆、工具、配對或行事曆。
【recent-context skill v{recent_skill['version']}】
{recent_skill['instructions']}
這句已被初步判定為本人真實活動。請只輸出 ProfileExtractionDecision JSON，memories 必須是 []；recent_context 必須保留 action=update、message_kind=real_world_update，並從 activity、destination、timing、companion_intent、temporal_status 中輸出所有有明確原文依據的欄位。episode_relation 必須依語意輸出 continue、new 或 unrelated。每個 evidence_span 必須是原句連續子字串，subject 必須是 owner，confidence 至少 0.90；沒有原文依據的欄位不要輸出。
目前有效的 typed episode（只能判斷是否延續，不是新 evidence）：{json.dumps(active_episode or {}, ensure_ascii=False)}
本人原始訊息：{message}"""
    try:
        data = json.loads(generate_chat_completion(prompt, temperature=0, json_output=True).content)
        return ProfileExtractionDecision.model_validate(data).recent_context
    except Exception:
        return None


def analyze_profile_message(
    message: str, previous_context: str = "", *, plan_id: str | None = None,
    active_episode: dict[str, Any] | None = None,
    follow_up_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Extract from one saved owner message, then validate deterministically.

    ``previous_context`` is intentionally retained only for call compatibility.
    Continuity may use a bounded typed episode, never raw conversation text.
    """
    del previous_context
    message = _clean(message, 800)
    blank = {"should_update": False, "confidence": 0.0, "activity": None, "destination": None,
             "timing": None, "companion_intent": None, "temporal_status": None, "fields": {}, "message_kind": "other",
             "summary_zh_tw": "", "reason_code": "skipped", "plan_id": plan_id or "",
             "episode_relation": "unrelated", "active_episode_id": ""}
    if not message or NO_STORE_RE.search(message) or contains_internal_identifier(message):
        return {"recent_context": {**blank, "reason_code": "blocked_input"}, "memories": [], "follow_up": None, "memory_codes": ["blocked_input"], "policy_versions": {}, "contract": {}}
    if contains_protected_content(message):
        return {"recent_context": {**blank, "reason_code": "protected_attribute"}, "memories": [], "follow_up": None, "memory_codes": ["protected_attribute"], "policy_versions": {}, "contract": {}}
    try:
        recent_skill = load_profile_skill("recent-context")
        memory_skill = load_profile_skill("memory")
    except Exception as exc:
        return {"recent_context": {**blank, "reason_code": "skill_load_failed"}, "memories": [], "follow_up": None, "memory_codes": [type(exc).__name__], "policy_versions": {}, "contract": {}}
    safe_episode = active_episode if isinstance(active_episode, dict) else None
    safe_episode = {
        "episode_id": str((safe_episode or {}).get("episode_id") or "")[:128],
        "goal": "activity_or_destination" if (safe_episode or {}).get("goal") == "activity_or_destination" else "",
        "fields": {
            name: _clean(value, 40)
            for name, value in ((safe_episode or {}).get("fields") or {}).items()
            if name in RECENT_FIELD_NAMES and _clean(value, 40)
        },
    } if safe_episode else None
    prompt_episode = {
        "goal": (safe_episode or {}).get("goal") or "continue_current_activity",
        "fields": dict((safe_episode or {}).get("fields") or {}),
    } if safe_episode else None
    safe_follow_up_candidates = []
    for item in (follow_up_candidates or [])[:3]:
        if not isinstance(item, dict):
            continue
        try:
            slot = int(item.get("slot", 0) or 0)
        except (TypeError, ValueError):
            continue
        if not 1 <= slot <= 3:
            continue
        safe_follow_up_candidates.append({
            "slot": slot,
            "topic": _clean(item.get("topic"), 80),
            "question_goal": _clean(item.get("question_goal"), 120),
            "timing": str(item.get("timing") or "after_days")[:24],
            "status": str(item.get("status") or "pending")[:24],
        })
    prompt = f"""
你是阿月的 Profile Extractor。新欄位與 evidence 只能來自下方一則「已儲存的本人原始訊息」，不可使用對話歷史、助理回覆、工具結果、配對或行事曆資料。你可能收到一個已驗證的 typed active episode；它只用來判斷本句是延續還是新情境，不能當作本句新欄位的 evidence。

【recent-context skill v{recent_skill['version']}】
{recent_skill['instructions']}
【memory skill v{memory_skill['version']}】
{memory_skill['instructions']}

請輸出 ProfileExtractionDecision JSON。recent_context.action 必須為 update、clear 或 none。episode_relation 必須為 continue、new 或 unrelated：本句是在補充／修正 active episode 時用 continue，開始不同活動時用 new，無關時用 unrelated。短句沒有時間詞也能延續；不確定時用 unrelated，禁止硬合併。每個欄位都要有 value、evidence_span、confidence、subject；subject 只能是 owner。只有確實描述本人現實活動時才 update；訊息可同時包含找人要求，但只擷取本人活動，絕不把找人、配對、提案或等待回覆寫入欄位。時間詞（例如今天、下週）是活動的時間欄位，不是拒絕理由。若提到他人，但同時清楚表達「我喜歡／不喜歡這種類型」，只能提出本人偏好記憶，不能儲存他人的特徵。
不得自行杜撰摘要。欄位與記憶標籤使用繁體中文；每個 evidence_span 必須是原訊息的連續子字串。

待追問只針對本人有明確後續的生活活動或計畫。已有候選使用 existing_slot=1..3 做 update 或 close，不要重複建立；建立時必須有 topic、question_goal、evidence_span、confidence>=0.90、subject=owner。question_goal 只描述中性的後續確認，不得預設活動成功。行事曆、日曆、測驗、配對、他人資料與單純偏好不建立候選；沒有合適候選時 action=none。
現有待追問候選（只有安全摘要，不含 ID）：{json.dumps(safe_follow_up_candidates, ensure_ascii=False)}

JSON schema：{{"recent_context":{{"action":"update|clear|none","confidence":0.0,"message_kind":"real_world_update|match_operation|durable_preference|other","episode_relation":"continue|new|unrelated","fields":{{"activity":{{"operation":"set|clear","value":"","evidence_span":"","confidence":0.0,"subject":"owner"}},"destination":{{"operation":"set|clear","value":"","evidence_span":"","confidence":0.0,"subject":"owner"}},"timing":{{"operation":"set|clear","value":"","evidence_span":"","confidence":0.0,"subject":"owner"}},"companion_intent":{{"operation":"set|clear","value":"","evidence_span":"","confidence":0.0,"subject":"owner"}},"temporal_status":{{"operation":"set|clear","value":"past|current|planned","evidence_span":"","confidence":0.0,"subject":"owner"}}}},"reason_code":""}},"memories":[{{"key":"snake_case","label_zh_tw":"繁體中文標籤","stance":"like|dislike|require|avoid","category":"lifestyle|habit|personality|relationship|activity","confidence":0.0,"evidence_span":"","subject":"owner","reason_code":""}}]}}
follow_up 欄位：{{"action":"create|update|close|none","existing_slot":null,"topic":"","question_goal":"","timing":"after_days|after_activity|none","wait_days":3,"evidence_span":"","confidence":0.0,"subject":"owner","reason_code":""}}
目前有效的 typed episode：{json.dumps(prompt_episode or {}, ensure_ascii=False)}
本人原始訊息：{message}
"""
    try:
        data = json.loads(generate_chat_completion(prompt, temperature=0, json_output=True).content)
        contract = ProfileExtractionDecision.model_validate(data)
    except Exception as exc:
        return {"recent_context": {**blank, "reason_code": f"model_{type(exc).__name__}"}, "memories": [], "follow_up": None, "memory_codes": [f"model_{type(exc).__name__}"], "policy_versions": {"recent-context": recent_skill["version"], "memory": memory_skill["version"]}, "contract": {}}

    recent, retry_needed = _validated_recent_proposal(contract.recent_context, message, plan_id)
    recent["active_episode_id"] = str((safe_episode or {}).get("episode_id") or "")
    if retry_needed:
        retried_recent = _retry_recent_context_contract(message, recent_skill, prompt_episode)
        if retried_recent is not None:
            retry_result, _ = _validated_recent_proposal(retried_recent, message, plan_id)
            if retry_result["should_update"]:
                retry_result["reason_code"] = "retry_accepted"
                retry_result["active_episode_id"] = str((safe_episode or {}).get("episode_id") or "")
                recent = retry_result
    memories, codes = [], []
    for item in contract.memories[:3]:
        candidate, code = _validate_memory(item.model_dump(), message)
        codes.append(code)
        if candidate:
            memories.append(candidate)
    follow_up = validate_followup_proposal(contract.follow_up, message, recent)
    contract_payload = contract.model_dump()
    # Keep traces/provider output bounded to the deterministic, evidence-checked
    # proposal rather than persisting arbitrary model text.
    contract_payload["follow_up"] = follow_up
    return {"recent_context": recent, "memories": memories, "follow_up": follow_up,
            "memory_codes": codes or ["no_memory_candidate"],
            "policy_versions": {"recent-context": recent_skill["version"], "memory": memory_skill["version"]},
            "contract": contract_payload}


def apply_recent_context(
    user_id: str,
    proposal: dict[str, Any],
    *,
    message_id: str,
    source_timestamp: float,
    before_write: Callable[[], bool] | None = None,
) -> bool:
    """Atomically merge independently evidenced fields into V2 recent context."""
    if not proposal.get("should_update") or proposal.get("message_kind") != "real_world_update":
        return False
    patches = proposal.get("fields") or {}
    if not isinstance(patches, dict) or not patches:
        return False
    context_action = str(proposal.get("context_action") or "update")
    source_timestamp = _timestamp(source_timestamp)
    episode_relation = str(proposal.get("episode_relation") or "new")
    if context_action != "clear" and episode_relation not in {"continue", "new"}:
        return False
    proposed_episode_id = str(proposal.get("active_episode_id") or "")[:128]
    for _ in range(3):
        profile = profiles_coll.find_one(
            {"user_id": user_id},
            {"current_context": 1, "current_context_revision": 1, "recent_context_state": 1,
             "recent_context_updated_at": 1, "recent_context_draft": 1},
        ) or {}
        previous = _safe_context_text(profile.get("current_context"))
        state = profile.get("recent_context_state") or {"version": 4, "revision": 0, "fields": {}}
        state_fields = dict(state.get("fields") or {})
        changed = False
        active_episode = _active_recent_episode(profile)
        if context_action == "clear":
            episode_id = str(state.get("episode_id") or f"episode:{message_id}")[:128]
        elif episode_relation == "continue":
            # The analyzer saw one exact typed episode. If it expired or was
            # replaced before this CAS attempt, fail closed instead of joining
            # two unrelated plans.
            if not active_episode or active_episode.get("episode_id") != proposed_episode_id:
                return False
            episode_id = proposed_episode_id
        else:
            existing_sources = [
                item for item in state_fields.values() if isinstance(item, dict)
            ]
            newest_existing = max(
                existing_sources,
                key=lambda item: (_timestamp(item.get("source_timestamp")), str(item.get("evidence_message_id") or "")),
                default=None,
            )
            if newest_existing and not _source_is_newer(
                {"source_timestamp": source_timestamp, "evidence_message_id": message_id},
                newest_existing,
            ):
                return False
            episode_id = f"episode:{message_id}"[:128]
            state_fields = {}
            changed = bool(state.get("fields"))
        if context_action == "clear":
            changed = changed or bool(state_fields)
            state_fields = {}
        for name in RECENT_FIELD_NAMES:
            if context_action == "clear":
                break
            patch = patches.get(name)
            if not isinstance(patch, dict):
                continue
            candidate = {
                "evidence_message_id": message_id,
                "evidence_span": patch.get("evidence_span"),
                "source_timestamp": source_timestamp,
                "plan_id": episode_id,
            }
            existing = state_fields.get(name)
            if not _source_is_newer(candidate, existing):
                continue
            if patch.get("operation") == "clear":
                if name in state_fields:
                    state_fields.pop(name, None)
                    changed = True
            elif patch.get("operation") == "set" and patch.get("value"):
                candidate["value"] = patch["value"]
                state_fields[name] = candidate
                changed = True
        if not changed:
            return False
        state = {
            "version": 4,
            "revision": int(state.get("revision", 0)) + 1,
            "episode_id": episode_id,
            "updated_at": time.time(),
            "fields": state_fields,
        }
        summary = _compose_recent_context_summary(state_fields)
        projection = {
            "activity": (state_fields.get("activity") or {}).get("value"),
            "destination": (state_fields.get("destination") or {}).get("value"),
            "timing": (state_fields.get("timing") or {}).get("value"),
            "companion_intent": (state_fields.get("companion_intent") or {}).get("value"),
            "temporal_status": (state_fields.get("temporal_status") or {}).get("value"),
        }
        set_fields = {
            "recent_context_state": state,
            "current_context": summary,
            "previous_context": previous,
            "current_context_revision": int(profile.get("current_context_revision", 0)) + 1,
            "context_signals": projection,
            "recent_context_updated_at": time.time(),
            "recent_context_expires_at": time.time() + 30 * 86400,
        }
        if summary:
            try:
                set_fields["context_embedding"] = get_embedding(summary)
                set_fields["context_embedding_source_hash"] = context_embedding_source_hash(summary)
            except Exception as exc:
                print(f"Recent context embedding skipped: {exc}")
        old_revision = int(profile.get("current_context_revision", 0))
        query = {"user_id": user_id, "$or": [
            {"current_context_revision": old_revision},
            {"current_context_revision": {"$exists": False}, "$expr": {"$eq": [old_revision, 0]}},
        ]}
        if before_write is not None and not before_write():
            return False
        updated = profiles_coll.update_one(
            query, {"$set": set_fields, "$unset": {"recent_context_draft": ""}}, upsert=False,
        )
        if updated.modified_count:
            return True
    return False


def process_profile_message(user_id: str, message: str, message_id: str | None, surface: str, match_id: str | None = None) -> dict[str, Any]:
    profile_mode = profile_skills_mode_for_user(user_id)
    followup_mode = followup_mode_for_user(user_id)
    mode = profile_mode if profile_mode != "off" else followup_mode
    if mode == "off":
        return {"status": "disabled"}
    # Profile extraction is only allowed from the already-persisted owner message.
    if not message_id:
        return {"status": "skipped", "reason": "missing_message_id"}
    try:
        source_message_id = ObjectId(message_id)
    except Exception:
        return {"status": "skipped", "reason": "invalid_message_id"}
    source_projection = {
        "content": 1,
        "metadata.owner_raw_content": 1,
        "metadata.message_use": 1,
        "timestamp": 1,
    }
    if followup_mode != "off":
        source_projection["room_id"] = 1
    source = messages_coll.find_one(
        {"_id": source_message_id, "sender_id": user_id},
        source_projection,
    )
    stored_owner_message = ((source or {}).get("metadata") or {}).get("owner_raw_content")
    stored_owner_message = stored_owner_message if isinstance(stored_owner_message, str) else (source or {}).get("content", "")
    if not source or stored_owner_message != str(message):
        return {"status": "skipped", "reason": "owner_message_not_saved"}
    source_use = message_use(source)
    if source_use is not MessageUse.ORDINARY:
        _trace(
            user_id,
            mode,
            {
                "message_id": message_id,
                "surface": surface,
                "match_id": match_id,
                "_run_status": "excluded",
                "recent_context": {
                    "reason_code": f"message_use_{source_use.value}",
                    "changed": False,
                    "message_kind": "excluded",
                    "field_names": [],
                    "input_eligible": False,
                },
                "memory": {
                    "codes": [f"message_use_{source_use.value}"],
                    "candidate_count": 0,
                    "saved_count": 0,
                    "error_code": None,
                },
            },
        )
        return {"status": "skipped", "reason": f"message_use_{source_use.value}"}
    claim_token = _claim_profile_message(user_id, message_id, mode)
    if not claim_token:
        return {"status": "skipped", "reason": "already_processed"}
    if isinstance(claim_token, str) and not _profile_claim_is_current(message_id, claim_token):
        return {"status": "skipped", "reason": "stale_claim"}
    try:
        profile = profiles_coll.find_one(
            {"user_id": user_id},
            {
                "recent_context_state": 1,
                "recent_context_updated_at": 1,
                "recent_context_draft": 1,
                "proactive_care_enabled": 1,
                "proactive_frequency": 1,
            },
        ) or {}
    except Exception:
        profile = {}
    if followup_mode != "off" and not is_proactive_care_enabled(profile):
        followup_mode = "off"
    active_episode = _active_recent_episode(profile)
    room_id = str(source.get("room_id") or "")
    followup_candidates = (
        followup_candidate_snapshot(user_id, room_id, now=time.time())
        if followup_mode != "off" and room_id
        else []
    )
    decision = analyze_profile_message(
        message, plan_id=f"message:{message_id}", active_episode=active_episode,
        follow_up_candidates=followup_candidates,
    )
    reason_code = str(
        (decision.get("recent_context") or {}).get("reason_code") or ""
    )
    retryable = reason_code.startswith("model_") or reason_code in {
        "skill_load_failed",
    }
    try:
        run = PROFILE_RUNS.find_one({"message_id": message_id}, {"attempt_count": 1}) or {}
        attempt_count = max(1, int(run.get("attempt_count", 1) or 1))
    except Exception:
        attempt_count = 1
    if isinstance(claim_token, str) and not _renew_profile_claim(message_id, claim_token):
        return {"status": "skipped", "reason": "stale_claim"}
    followup_result: dict[str, Any] = {"status": "disabled"}
    if followup_mode != "off":
        followup_result = apply_followup_proposal(
            user_id,
            room_id,
            message_id,
            stored_owner_message,
            _timestamp(source.get("timestamp")),
            decision.get("follow_up"),
            decision.get("recent_context"),
            mode=followup_mode,
            candidate_snapshot=followup_candidates,
        )
        if followup_result.get("status") == "storage_unavailable":
            retryable = True
    run_status = "failed" if retryable and attempt_count < PROFILE_MAX_ATTEMPTS else "completed"
    next_attempt_at = None
    if run_status == "failed":
        next_attempt_at = time.time() + PROFILE_RETRY_DELAYS[min(attempt_count - 1, len(PROFILE_RETRY_DELAYS) - 1)]
    recent_changed, saved_memories, memory_error = False, [], None
    if profile_mode == "on":
        recent_changed = apply_recent_context(
            user_id, decision["recent_context"], message_id=message_id,
            source_timestamp=_timestamp(source.get("timestamp")),
            before_write=(
                lambda: _renew_profile_claim(message_id, claim_token)
                if isinstance(claim_token, str)
                else True
            ),
        )
        if isinstance(claim_token, str) and not _renew_profile_claim(message_id, claim_token):
            return {"status": "skipped", "reason": "stale_claim"}
        try:
            from services.memory_service import MemoryWriteError, apply_profile_memory_proposals
            saved_memories = apply_profile_memory_proposals(user_id, decision["memories"], surface, message_id, match_id)
        except MemoryWriteError as exc:
            memory_error = exc.error_code
        except Exception as exc:
            memory_error = type(exc).__name__
    _trace(user_id, mode, {"message_id": message_id, "surface": surface, "match_id": match_id,
                             "_run_status": run_status,
                             "_next_attempt_at": next_attempt_at,
                             "_lease_token": claim_token if isinstance(claim_token, str) else None,
                             "policy_versions": decision["policy_versions"],
                             "recent_context": {"reason_code": decision["recent_context"]["reason_code"], "changed": recent_changed,
                                                "message_kind": decision["recent_context"].get("message_kind"),
                                                "field_names": sorted((decision["recent_context"].get("fields") or {}).keys()),
                                                "input_eligible": True},
                             "memory": {"codes": decision["memory_codes"], "candidate_count": len(decision["memories"]), "saved_count": len(saved_memories), "error_code": memory_error},
                             "follow_up": {"status": followup_result.get("status", "disabled"),
                                           "action": (decision.get("follow_up") or {}).get("action"),
                                           "changed": followup_result.get("status") in {"created", "updated"}}})
    followup_changed = followup_result.get("status") in {"created", "updated"}
    return {
        "status": "retry_scheduled" if run_status == "failed" else ("applied" if recent_changed or saved_memories or followup_changed else "skipped"),
        "decision": decision,
        "recent_changed": recent_changed,
        "saved_memories": saved_memories,
        "memory_error": memory_error,
        "follow_up": followup_result,
        "retry_scheduled": run_status == "failed",
        "attempt_count": attempt_count,
        "next_attempt_at": next_attempt_at,
    }


# Compatibility shims for current callers and tests.
def analyze_recent_context(message: str, previous_context: str = "") -> dict[str, Any]:
    return analyze_profile_message(message, previous_context)["recent_context"]


def process_recent_context(user_id: str, message: str, message_id: str | None = None) -> dict[str, Any]:
    return process_profile_message(user_id, message, message_id, "legacy")
