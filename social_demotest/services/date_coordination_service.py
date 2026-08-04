"""State machine for one shared date-coordination card per accepted match."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from bson import ObjectId
from fastapi import HTTPException
from pymongo import ReturnDocument

from database import calendar_events_coll, matches_coll, messages_coll
from services.calendar_service import (
    calendar_access_enabled, conflicts_for_viewer, normalize_form, _parse_local_interval,
    serialize_event,
)
from services.chat_service import generate_room_id, save_message
from services.match_state_service import verified_accepted_match_query
from services.mediator_event_service import queue_mediator_event


LIVE_STATUSES = {"pending_partner", "active"}


def _field(user_id: str) -> str:
    if not user_id or any(char in user_id for char in ".$"):
        raise HTTPException(status_code=400, detail="無效的使用者識別")
    return f"date_coordination.confirmations.{user_id}"


def find_accepted_match(user_id: str, other_id: str) -> dict:
    match = matches_coll.find_one(
        verified_accepted_match_query(user_id, other_id)
    )
    if not match:
        raise HTTPException(status_code=403, detail="只能在已接受配對中使用約會功能")
    return match


def public_coordination(coordination: dict | None) -> dict | None:
    if not coordination:
        return None
    return {
        "coordination_id": coordination.get("coordination_id"),
        "status": coordination.get("status"),
        "initiator_id": coordination.get("initiator_id"),
        "invitee_id": coordination.get("invitee_id"),
        "revision": coordination.get("revision", 1),
        "form": normalize_form(coordination.get("form", {})),
        "confirmations": coordination.get("confirmations", {}),
        "calendar_event_id": coordination.get("calendar_event_id"),
    }


def _card_metadata(coordination: dict, event_type: str = "date_coordination_form") -> dict:
    return {
        "event_type": event_type,
        "coordination_id": coordination["coordination_id"],
        "revision": coordination.get("revision", 1),
        "status": coordination.get("status"),
        "form": normalize_form(coordination.get("form", {})),
        "confirmations": coordination.get("confirmations", {}),
        "initiator_id": coordination.get("initiator_id"),
        "invitee_id": coordination.get("invitee_id"),
        "calendar_event_id": coordination.get("calendar_event_id"),
    }


def _sync_card(match: dict, coordination: dict) -> None:
    card_id = coordination.get("card_message_id")
    if not card_id:
        return
    event_type = "date_coordination_invite" if coordination.get("status") == "pending_partner" else "date_coordination_form"
    if coordination.get("status") == "completed":
        event_type = "date_coordination_success"
    if coordination.get("status") in {"declined", "cancelled"}:
        event_type = "date_coordination_cancelled"
    content = {
        "pending_partner": "阿月收到約會提議，正在等對方決定要不要一起協調。",
        "active": "阿月幫你們保留了一張共享約會表單；修改後請雙方重新確認。",
        "completed": "🎉 約會已成立，已加入雙方行事曆。",
        "declined": "這次約會協調沒有開始，先維持舒服的聊天節奏吧。",
        "cancelled": "這次約會已取消，行事曆會保留取消紀錄。",
    }.get(coordination.get("status"), "約會協調狀態已更新。")
    try:
        messages_coll.update_one(
            {"_id": ObjectId(card_id)},
            {"$set": {"content": content, "message_type": "mediator_card", "metadata": _card_metadata(coordination, event_type)}},
        )
    except Exception as exc:
        print(f"Date card sync skipped: {exc}")


def _other_participant(match: dict, user_id: str) -> str:
    return match["to_user"] if match.get("from_user") == user_id else match["from_user"]


def _notify_date_change(match: dict, actor_id: str, coordination: dict, event_type: str, message: str) -> None:
    """Queue one private, relationship-scoped notification for the other person."""
    other_id = _other_participant(match, actor_id)
    queue_mediator_event(
        other_id,
        message,
        event_type,
        event_key=(
            f"date:{coordination.get('coordination_id')}:{coordination.get('revision', 1)}:"
            f"{event_type}:{coordination.get('status')}"
        ),
        match_id=str(match["_id"]),
        other_id=actor_id,
        coordination_id=coordination.get("coordination_id"),
        revision=coordination.get("revision", 1),
    )


def create_invite(match: dict, initiator_id: str, invitee_id: str) -> dict | None:
    existing = match.get("date_coordination") or {}
    if existing.get("status") in LIVE_STATUSES:
        return None
    coordination = {
        "coordination_id": uuid4().hex,
        "status": "pending_partner",
        "initiator_id": initiator_id,
        "invitee_id": invitee_id,
        "revision": 1,
        "form": normalize_form({}),
        "confirmations": {},
        "created_at": datetime.now(timezone.utc),
    }
    claimed = matches_coll.find_one_and_update(
        {"_id": match["_id"], "$or": [
            {"date_coordination": {"$exists": False}},
            {"date_coordination.status": {"$nin": ["pending_partner", "active"]}},
            {"date_coordination.coordination_id": {"$exists": False}},
        ]},
        {"$set": {"date_coordination": coordination}},
        return_document=ReturnDocument.AFTER,
    )
    if not claimed:
        return None
    room_id = generate_room_id(match["from_user"], match["to_user"])
    card = save_message(room_id, "ai_assistant", "阿月收到約會提議，正在等對方決定要不要一起協調。", "mediator_card", _card_metadata(coordination, "date_coordination_invite"))
    coordination["card_message_id"] = card["message_id"]
    matches_coll.update_one({"_id": match["_id"], "date_coordination.coordination_id": coordination["coordination_id"]}, {"$set": {"date_coordination.card_message_id": card["message_id"]}})
    return coordination


def respond_to_invite(user_id: str, other_id: str, coordination_id: str, accepted: bool) -> dict:
    match = find_accepted_match(user_id, other_id)
    coordination = match.get("date_coordination") or {}
    if coordination.get("coordination_id") != coordination_id or coordination.get("status") != "pending_partner":
        raise HTTPException(status_code=409, detail="這個約會邀請已失效或已處理")
    if coordination.get("invitee_id") != user_id:
        raise HTTPException(status_code=403, detail="只有受邀者可以回覆此邀請")
    coordination["status"] = "active" if accepted else "declined"
    coordination["responded_at"] = datetime.now(timezone.utc)
    matches_coll.update_one({"_id": match["_id"], "date_coordination.coordination_id": coordination_id, "date_coordination.status": "pending_partner"}, {"$set": {"date_coordination": coordination}})
    _sync_card(match, coordination)
    return public_coordination(coordination)


def get_state(user_id: str, other_id: str) -> dict | None:
    return public_coordination(find_accepted_match(user_id, other_id).get("date_coordination"))


def update_form(user_id: str, other_id: str, coordination_id: str, revision: int, form: dict) -> dict:
    match = find_accepted_match(user_id, other_id)
    coordination = match.get("date_coordination") or {}
    if coordination.get("coordination_id") != coordination_id or coordination.get("status") != "active":
        raise HTTPException(status_code=409, detail="目前沒有可修改的約會協調")
    if int(coordination.get("revision", 1)) != revision:
        raise HTTPException(status_code=409, detail="表單已被更新，請重新整理後再修改")
    coordination["form"] = normalize_form(form)
    coordination["revision"] = revision + 1
    coordination["confirmations"] = {}
    coordination["updated_at"] = datetime.now(timezone.utc)
    updated = matches_coll.update_one(
        {"_id": match["_id"], "date_coordination.coordination_id": coordination_id, "date_coordination.revision": revision, "date_coordination.status": "active"},
        {"$set": {"date_coordination": coordination}},
    )
    if not updated.modified_count:
        raise HTTPException(status_code=409, detail="表單已被其他人更新")
    if coordination.get("rescheduling_event_id"):
        pending_result = calendar_events_coll.update_one(
            {
                "event_id": coordination["rescheduling_event_id"],
                "status": "pending_reconfirmation",
            },
            {
                "$set": {
                    "pending_change": coordination["form"],
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        if not pending_result.modified_count:
            # event 已不在「等重新確認」（例如被撤回改期還原成 confirmed）；
            # 清掉 rescheduling_event_id，避免後續 update_form 再誤把 pending_change 寫回已確認行程
            matches_coll.update_one(
                {"_id": match["_id"], "date_coordination.coordination_id": coordination_id},
                {"$unset": {"date_coordination.rescheduling_event_id": ""}},
            )
            coordination.pop("rescheduling_event_id", None)
    _sync_card(match, coordination)
    return public_coordination(coordination)


def _upsert_calendar_event(match: dict, coordination: dict) -> dict:
    form = normalize_form(coordination["form"])
    start_at, end_at, zone_name = _parse_local_interval(form)
    now = datetime.now(timezone.utc)
    participants = [match["from_user"], match["to_user"]]
    event = {
        "event_id": coordination.get("calendar_event_id") or uuid4().hex,
        "source_type": "date", "coordination_id": coordination["coordination_id"], "match_id": str(match["_id"]),
        "participants": participants, "title": f"與對方的約會", "start_at": start_at, "end_at": end_at,
        "timezone": zone_name, "activity": form["activity"], "location": form["location"],
        "budget": form["budget"], "notes": form["notes"], "status": "confirmed",
        "revision": coordination["revision"], "updated_at": now,
    }
    result = calendar_events_coll.find_one_and_update(
        {"coordination_id": coordination["coordination_id"]},
        {"$set": event, "$unset": {"pending_change": ""}, "$setOnInsert": {"created_at": now}}, upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return result


def confirm_form(user_id: str, other_id: str, coordination_id: str, revision: int) -> tuple[dict, dict | None]:
    match = find_accepted_match(user_id, other_id)
    coordination = match.get("date_coordination") or {}
    if coordination.get("coordination_id") != coordination_id or coordination.get("status") != "active":
        raise HTTPException(status_code=409, detail="目前沒有可確認的約會協調")
    if int(coordination.get("revision", 1)) != revision:
        raise HTTPException(status_code=409, detail="表單已變更，請重新確認")
    start_at, end_at, _ = _parse_local_interval(normalize_form(coordination.get("form", {})))
    participant_ids = [match["from_user"], match["to_user"]]
    conflicts = conflicts_for_viewer(
        user_id, participant_ids, start_at, end_at,
        exclude_event_id=coordination.get("calendar_event_id"),
    )
    if conflicts:
        raise HTTPException(status_code=409, detail={"message": "此時段已有行程衝突，請改期後再確認", "conflicts": conflicts})
    marked = matches_coll.find_one_and_update(
        {"_id": match["_id"], "date_coordination.coordination_id": coordination_id, "date_coordination.revision": revision, "date_coordination.status": "active"},
        {"$set": {_field(user_id): True, "date_coordination.updated_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )
    if not marked:
        raise HTTPException(status_code=409, detail="確認狀態已改變，請重新整理")
    coordination = marked["date_coordination"]
    if all(coordination.get("confirmations", {}).get(person) for person in participant_ids):
        completed = matches_coll.find_one_and_update(
            {"_id": match["_id"], "date_coordination.coordination_id": coordination_id, "date_coordination.revision": revision, "date_coordination.status": "active"},
            {"$set": {"date_coordination.status": "completed", "date_coordination.completed_at": datetime.now(timezone.utc)}},
            return_document=ReturnDocument.AFTER,
        )
        if completed:
            coordination = completed["date_coordination"]
    event = None
    if coordination.get("status") == "completed":
        event = _upsert_calendar_event(match, coordination)
        coordination["calendar_event_id"] = event["event_id"]
        matches_coll.update_one({"_id": match["_id"], "date_coordination.coordination_id": coordination_id}, {"$set": {"date_coordination.calendar_event_id": event["event_id"]}})
        _sync_card(match, coordination)
    else:
        _sync_card(match, coordination)
    return public_coordination(coordination), serialize_event(event, user_id) if event else None


def request_reschedule(
    user_id: str,
    other_id: str,
    event_id: str,
    proposed_form: dict,
    *,
    expected_revision: int | None = None,
    idempotency_key: str | None = None,
) -> tuple[dict, dict]:
    """Propose a shared-date change; the other participant must reconfirm it."""
    match = find_accepted_match(user_id, other_id)
    coordination = match.get("date_coordination") or {}
    if (
        coordination.get("calendar_event_id") != event_id
        or coordination.get("status") not in {"completed", "active"}
    ):
        raise HTTPException(status_code=409, detail="約會協調資料不一致")
    event = calendar_events_coll.find_one({
        "event_id": event_id,
        "source_type": "date",
        "participants": {"$all": [user_id, other_id]},
    })
    if not event:
        raise HTTPException(status_code=404, detail="找不到可改期的共同約會")
    if (
        idempotency_key
        and event.get("last_agent_action_key") == idempotency_key
        and coordination.get("last_action_key") == idempotency_key
    ):
        return public_coordination(coordination), serialize_event(event, user_id)
    current_revision = int(coordination.get("revision", 1) or 1)
    event_revision = int(event.get("revision", 1) or 1)
    # 呼叫端（agent 確認或 REST）都是以「行程版本」當錨點；協調單有自己的 CAS（下方 update）保護。
    # 不再強制協調單版本 == 行程版本，避免表單編輯導致兩者分歧時誤判 409。
    if expected_revision is not None and event_revision != expected_revision:
        raise HTTPException(status_code=409, detail="約會剛剛已變更，請重新確認")
    if event.get("status") != "confirmed":
        raise HTTPException(status_code=409, detail="這筆約會目前正在等待重新確認")

    proposed = normalize_form(proposed_form)
    _parse_local_interval(proposed)
    next_revision = current_revision + 1
    now = datetime.now(timezone.utc)
    action_key = idempotency_key or f"reschedule:{uuid4().hex}"
    coordination_set = {
        "date_coordination.status": "active",
        "date_coordination.revision": next_revision,
        "date_coordination.form": proposed,
        "date_coordination.confirmations": {},
        "date_coordination.rescheduling_event_id": event_id,
        "date_coordination.updated_at": now,
        "date_coordination.last_action_key": action_key,
    }
    updated_match = matches_coll.find_one_and_update(
        {
            "_id": match["_id"],
            "date_coordination.coordination_id": coordination["coordination_id"],
            "date_coordination.revision": current_revision,
            "date_coordination.status": {"$in": ["completed", "active"]},
        },
        {"$set": coordination_set},
        return_document=ReturnDocument.AFTER,
    )
    if not updated_match:
        latest = find_accepted_match(user_id, other_id)
        latest_coordination = latest.get("date_coordination") or {}
        latest_event = calendar_events_coll.find_one({"_id": event["_id"]}) or {}
        if (
            idempotency_key
            and latest_coordination.get("last_action_key") == idempotency_key
            and latest_event.get("last_agent_action_key") == idempotency_key
        ):
            return public_coordination(latest_coordination), serialize_event(latest_event, user_id)
        raise HTTPException(status_code=409, detail="約會剛剛已變更，請重新確認")

    updated_coordination = updated_match["date_coordination"]
    event_query: dict = {"_id": event["_id"], "status": "confirmed", "revision": event_revision}
    event_update: dict = {
        "status": "pending_reconfirmation",
        "pending_change": proposed,
        "revision": next_revision,
        "updated_at": now,
        "last_agent_action_key": action_key,
    }
    claimed_event = calendar_events_coll.update_one(event_query, {"$set": event_update})
    if not claimed_event.modified_count:
        matches_coll.update_one(
            {
                "_id": match["_id"],
                "date_coordination.coordination_id": coordination["coordination_id"],
                "date_coordination.last_action_key": action_key,
            },
            {"$set": {"date_coordination": coordination}},
        )
        raise HTTPException(status_code=409, detail="約會剛剛已變更，請重新確認")

    _sync_card(updated_match, updated_coordination)
    _notify_date_change(
        updated_match,
        user_id,
        updated_coordination,
        "date_coordination_result",
        (
            f"對方提出將你們的共同約會改到 {proposed['date']} "
            f"{proposed['start_time']}–{proposed['end_time']}；請到共同聊天室確認。"
        ),
    )
    updated_event = calendar_events_coll.find_one({"_id": event["_id"]}) or {**event, **event_update}
    return public_coordination(updated_coordination), serialize_event(updated_event, user_id)


def cancel_coordination_or_event(
    user_id: str,
    other_id: str,
    coordination_id: str,
    *,
    expected_revision: int | None = None,
    idempotency_key: str | None = None,
) -> dict:
    match = find_accepted_match(user_id, other_id)
    coordination = match.get("date_coordination") or {}
    if coordination.get("coordination_id") != coordination_id:
        raise HTTPException(status_code=404, detail="找不到約會協調")
    if coordination.get("status") == "cancelled":
        if not idempotency_key or coordination.get("last_action_key") == idempotency_key:
            return public_coordination(coordination)
        raise HTTPException(status_code=409, detail="這筆共同約會已經取消")
    # 不在此處用「協調單版本」當 CAS；呼叫端鎖的是行程版本。下方 event_query 有 event revision CAS，
    # match_query 有 status CAS，足以保護。避免表單編輯導致協調單與行程版本分歧時誤判 409。

    event = None
    if coordination.get("calendar_event_id"):
        event = calendar_events_coll.find_one({
            "event_id": coordination["calendar_event_id"],
            "source_type": "date",
            "participants": {"$all": [user_id, other_id]},
        })
        if not event:
            raise HTTPException(status_code=409, detail="共同約會與行事曆資料不一致")
        if expected_revision is not None and int(event.get("revision", 1) or 1) != expected_revision:
            raise HTTPException(status_code=409, detail="約會剛剛已變更，請重新確認")

    now = datetime.now(timezone.utc)
    action_key = idempotency_key or f"cancel:{uuid4().hex}"
    coordination_set = {
        "date_coordination.status": "cancelled",
        "date_coordination.cancelled_by": user_id,
        "date_coordination.cancelled_at": now,
        "date_coordination.updated_at": now,
        "date_coordination.last_action_key": action_key,
    }
    match_query: dict = {
        "_id": match["_id"],
        "date_coordination.coordination_id": coordination_id,
        "date_coordination.status": {"$ne": "cancelled"},
    }
    # 不在此處用「協調單版本」當 CAS（呼叫端鎖的是行程版本，二者可能因表單編輯而分歧）；
    # 行程版本已由上方 event revision 檢查保護。
    updated_match = matches_coll.find_one_and_update(
        match_query,
        {"$set": coordination_set},
        return_document=ReturnDocument.AFTER,
    )
    if not updated_match:
        latest = find_accepted_match(user_id, other_id)
        latest_coordination = latest.get("date_coordination") or {}
        if idempotency_key and latest_coordination.get("last_action_key") == idempotency_key:
            return public_coordination(latest_coordination)
        raise HTTPException(status_code=409, detail="約會剛剛已變更，請重新確認")

    updated_coordination = updated_match["date_coordination"]
    if event:
        event_query: dict = {
            "_id": event["_id"],
            "status": {"$ne": "cancelled"},
        }
        if expected_revision is not None:
            event_query["revision"] = expected_revision
        event_set = {
            "status": "cancelled",
            "cancelled_by": user_id,
            "cancelled_at": now,
            "updated_at": now,
            "last_agent_action_key": action_key,
        }
        event_result = calendar_events_coll.update_one(
            event_query,
            {"$set": event_set, "$inc": {"revision": 1}},
        )
        if not event_result.modified_count:
            latest_event = calendar_events_coll.find_one({"_id": event["_id"]}) or {}
            if not (
                idempotency_key
                and latest_event.get("status") == "cancelled"
                and latest_event.get("last_agent_action_key") == idempotency_key
            ):
                matches_coll.update_one(
                    {
                        "_id": match["_id"],
                        "date_coordination.coordination_id": coordination_id,
                        "date_coordination.last_action_key": action_key,
                    },
                    {"$set": {"date_coordination": coordination}},
                )
                raise HTTPException(status_code=409, detail="約會剛剛已變更，請重新確認")
    _sync_card(updated_match, updated_coordination)
    activity = str((updated_coordination.get("form") or {}).get("activity") or "共同約會")
    _notify_date_change(
        updated_match,
        user_id,
        updated_coordination,
        "date_coordination_cancelled",
        f"對方已取消你們的共同約會「{activity}」，雙方行事曆已同步更新。",
    )
    return public_coordination(updated_coordination)
