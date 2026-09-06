"""Regression scenarios using real delivery functions and a stateful Mongo subset.

No model, network, mirroring, or push is called. The store implements only the
queries used here; these tests verify transitions, not MongoDB server behavior.
"""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from bson import ObjectId
from services import proactive_followup_service as f, chat_service as chat
from services.ayue_agent import proactive_scheduler as scheduler, proactive_care as care
from services.message_use_service import metadata_for_use

ABSENT = object()
def get(doc, path):
    keys = path.split('.')
    for index, key in enumerate(keys):
        if isinstance(doc, list):
            return [get(item, '.'.join(keys[index:])) for item in doc]
        if not isinstance(doc, dict) or key not in doc:
            return ABSENT
        doc = doc[key]
    return doc

def matches(doc, query):
    for key, condition in query.items():
        if key == '$or':
            if not any(matches(doc, q) for q in condition): return False
            continue
        value = get(doc, key)
        if isinstance(condition, dict):
            for op, rhs in condition.items():
                if op == '$exists': good = (value is not ABSENT) == rhs
                elif op == '$ne': good = rhs not in value if isinstance(value, list) else value != rhs
                elif op == '$in': good = value in rhs
                elif op == '$gt': good = value is not ABSENT and value > rhs
                elif op == '$lte': good = value is not ABSENT and value <= rhs
                else: raise AssertionError(op)
                if not good: return False
        elif isinstance(value, list):
            if condition not in value: return False
        elif value != condition: return False
    return True

class Cursor(list):
    def sort(self, *args):
        specs = args[0] if isinstance(args[0], list) else [args]
        for key, direction in reversed(specs):
            super().sort(key=lambda doc: doc.get(key, 0), reverse=direction == -1)
        return self
    def limit(self, n): return Cursor(self[:n])

class Collection:
    def __init__(self, rows=()):
        self.rows = deepcopy(list(rows))
        self.fail_once = None
    def find(self, query, *args, **kwargs):
        return Cursor(deepcopy(row) for row in self.rows if matches(row, query))
    def find_one(self, query, *args, **kwargs):
        return next(iter(self.find(query)), None)
    def update_one(self, query, update, *, upsert=False, **kwargs):
        if self.fail_once and self.fail_once(update):
            self.fail_once = None
            raise RuntimeError('injected write failure')
        row = next((row for row in self.rows if matches(row, query)), None)
        inserted = row is None and upsert
        if inserted:
            row = deepcopy(query)
            self.rows.append(row)
        if row is None:
            return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=None)
        before = deepcopy(row)
        if inserted: row.update(deepcopy(update.get('$setOnInsert', {})))
        for path, value in update.get('$set', {}).items():
            target = row
            keys = path.split('.')
            for key in keys[:-1]: target = target.setdefault(key, {})
            target[keys[-1]] = deepcopy(value)
        for key in update.get('$unset', {}): row.pop(key, None)
        for key, value in update.get('$inc', {}).items(): row[key] = row.get(key, 0) + value
        for key, value in update.get('$push', {}).items():
            row[key] = (row.get(key, []) + deepcopy(value['$each']))[value['$slice']:]
        return SimpleNamespace(matched_count=int(not inserted), modified_count=int(row != before), upserted_id=row.get('_id') if inserted else None)
    def update_many(self, query, update):
        count = sum(self.update_one({'_id':row['_id']}, update).modified_count for row in self.find(query))
        return SimpleNamespace(modified_count=count)
    def find_one_and_update(self, query, update, **kwargs):
        before = self.find_one(query)
        self.update_one(query, update)
        return self.find_one({'_id': before['_id']}) if before and kwargs.get('return_document') else before

T = datetime(2026, 9, 6, 12, tzinfo=ZoneInfo('Asia/Taipei')).timestamp()

def setup(stack):
    profiles = Collection([{'_id':'p', 'user_id':'owner', 'proactive_care_enabled':True, 'mediator_calendar_access':False,
        'proactive_care_delivery_claim_id':'delivery', 'proactive_care_delivery_claim_candidate_id':'c',
        'proactive_care_delivery_claim_until':T+90}])
    candidates = Collection([{'_id':'c', 'user_id':'owner', 'room_id':'room', 'status':'processing',
        'revision':1, 'lease_token':'candidate', 'lease_until':T+90, 'expires_at':T+7*86400,
        'available_at':T, 'active_slot':1}])
    messages = Collection()
    for module in [f, scheduler, care]: stack.enter_context(patch.object(module, 'profiles_coll', profiles))
    for module in [f, scheduler]: stack.enter_context(patch.object(module, 'PROACTIVE_FOLLOWUPS', candidates))
    for module in [f, scheduler, care, chat]: stack.enter_context(patch.object(module, 'messages_coll', messages))
    for module in [f, scheduler]: stack.enter_context(patch.object(module, 'is_owned_public_ai_room', return_value=True))
    stack.enter_context(patch.object(chat, 'mirror_message_to_appwrite_async'))
    stack.enter_context(patch.object(chat, 'queue_push_notification'))
    pending = f.stage_delivery_slot('owner', 'delivery', candidate_id='c', candidate_token='candidate',
        now=T, event_key='proactive-care:c:r1', message='考試如何？', origin_room_id='room', require_enabled_flag=True)
    assert pending
    return profiles, candidates, messages

def delivery(profile, now=T):
    with patch.object(chat.time, 'time', return_value=now):
        return scheduler._deliver_pending_slot(profile, now=now)


def test_expired_claim_cannot_replace_pending_delivery():
    with ExitStack() as stack:
        profiles, _, _ = setup(stack)
        assert f.claim_delivery_slot("owner", "another-candidate", now=T+200) is None
        assert profiles.rows[0]["proactive_care_delivery_claim_candidate_id"] == "c"


def test_commit_failure_recovers_after_extractor_closes_candidate():
    with ExitStack() as stack:
        profiles, candidates, messages = setup(stack)
        profiles.fail_once = lambda update: "$push" in update
        assert delivery(profiles.find_one({"user_id": "owner"})) == "retried"
        source = {"content": "我已經考完了", "metadata": {"message_use": metadata_for_use("ordinary")}}
        with patch.object(f, "_source_is_reusable", return_value=(True, source)):
            result = f.apply_followup_proposal(
                "owner", "room", str(ObjectId()), "我已經考完了", T+60,
                {"action": "close", "existing_slot": 1, "evidence_span": "我已經考完了",
                 "subject": "owner", "confidence": .95},
                {"message_kind": "real_world_update"}, now=T+60, mode="on",
                candidate_snapshot=[{"_candidate_id": "c", "_revision": 2}],
            )
        assert result["status"] == "updated"
        assert candidates.rows[0]["status"] == "completed"
        with patch.object(scheduler, "followup_mode_for_user", return_value="on"):
            stats = scheduler.run_due_proactive_care_once(now=T+120)
        assert stats["delivered"] == 1
        assert len(messages.rows) == 1
        assert profiles.rows[0]["proactive_care_delivery_times"] == [T]
        assert "proactive_care_pending_delivery" not in profiles.rows[0]


def test_consumed_notice_is_not_republished_by_a_stale_worker():
    with ExitStack() as stack:
        profiles, _, messages = setup(stack)
        snapshot = profiles.find_one({"user_id": "owner"})
        assert delivery(snapshot) == "delivered"
        assert care.consume_proactive_delivery("owner")
        delivery(snapshot, T+10)
        assert care.consume_proactive_delivery("owner") is None
        assert profiles.rows[0]["proactive_care_delivery_times"] == [T]
        assert len(messages.rows) == 1


def test_disabling_after_scan_blocks_first_save():
    with ExitStack() as stack:
        profiles, _, messages = setup(stack)
        snapshot = profiles.find_one({"user_id": "owner"})
        profiles.update_one({"user_id": "owner"}, {"$set": {"proactive_care_enabled": False}})
        f.cancel_pending_delivery_slot("owner")
        assert delivery(snapshot, T+10) == "skipped"
        assert not messages.rows


def test_disabling_during_policy_check_is_seen_by_atomic_authorization():
    with ExitStack() as stack:
        profiles, _, messages = setup(stack)
        def pause(_now):
            profiles.update_one({"user_id": "owner"}, {"$set": {"proactive_care_enabled": False}})
            return None
        with patch.object(scheduler, "next_quiet_end", side_effect=pause):
            assert delivery(profiles.find_one({"user_id": "owner"})) == "retried"
        assert not messages.rows


def test_unsent_recovery_waits_until_quiet_hours_end():
    with ExitStack() as stack:
        profiles, _, messages = setup(stack)
        late = datetime(2026, 9, 6, 23, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
        assert delivery(profiles.find_one({"user_id": "owner"}), late) == "skipped"
        assert not messages.rows
        assert "proactive_care_pending_delivery" in profiles.rows[0]
        morning = datetime(2026, 9, 7, 9, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
        assert delivery(profiles.find_one({"user_id": "owner"}), morning) == "delivered"
        assert messages.rows[0]["timestamp"] == morning
        assert profiles.rows[0]["proactive_care_last_sent_at"] == morning


def test_saved_message_can_finish_bookkeeping_during_quiet_hours():
    with ExitStack() as stack:
        profiles, _, messages = setup(stack)
        profiles.fail_once = lambda update: "$push" in update
        assert delivery(profiles.find_one({"user_id": "owner"})) == "retried"
        late = datetime(2026, 9, 6, 23, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
        assert delivery(profiles.find_one({"user_id": "owner"}), late) == "delivered"
        assert len(messages.rows) == 1
        assert profiles.rows[0]["proactive_care_last_sent_at"] == T


def test_48_hour_interval_starts_at_actual_first_save_after_outage():
    with ExitStack() as stack:
        profiles, _, messages = setup(stack)
        late = T+3*86400
        assert delivery(profiles.find_one({"user_id": "owner"}), late) == "delivered"
        messages.rows.append({"_id": "reply", "room_id": "room", "sender_id": "owner", "timestamp": late+60})
        assert care.consume_proactive_delivery("owner")
        assert f.claim_delivery_slot("owner", "c2", now=late+1800) is None
        assert profiles.rows[0]["proactive_care_delivery_history"][0]["asked_at"] == late


def test_failed_candidate_write_and_same_event_requeue_clears_pending():
    with ExitStack() as stack:
        profiles, candidates, messages = setup(stack)
        candidates.fail_once = lambda update: update.get("$set", {}).get("status") == "asked"
        assert delivery(profiles.find_one({"user_id": "owner"})) == "delivered"
        messages.rows.append({"_id": "reply", "room_id": "room", "sender_id": "owner", "timestamp": T+60})
        assert care.consume_proactive_delivery("owner")
        later = T+49*3600
        token = f.claim_delivery_slot("owner", "c", now=later)
        candidate_token, candidate = f.claim_followup_candidate(candidates.rows[0], now=later)
        assert token
        assert candidate["revision"] == 1
        assert f.stage_delivery_slot(
            "owner", token, candidate_id="c", candidate_token=candidate_token, now=later,
            event_key="proactive-care:c:r1", message="考試如何？", origin_room_id="room", require_enabled_flag=True,
        )
        assert delivery(profiles.find_one({"user_id": "owner"}), later) == "delivered"
        assert "proactive_care_pending_delivery" not in profiles.rows[0]
        assert care.consume_proactive_delivery("owner") is None
        assert profiles.rows[0]["proactive_care_delivery_times"] == [T]
        assert sum(row["sender_id"] == "ai_assistant" for row in messages.rows) == 1


@pytest.mark.parametrize("state", ["completed", "updated", "expired"])
def test_unsent_recovery_discards_no_longer_valid_candidate(state):
    with ExitStack() as stack:
        profiles, candidates, messages = setup(stack)
        if state == "expired":
            candidates.rows[0]["expires_at"] = T-1
        elif state == "updated":
            candidates.rows[0].update(status="pending", revision=2)
            candidates.rows[0].pop("lease_token")
        else:
            candidates.rows[0]["status"] = state
        assert delivery(profiles.find_one({"user_id": "owner"})) == "skipped"
        assert not messages.rows
        assert "proactive_care_pending_delivery" not in profiles.rows[0]


@pytest.mark.parametrize("blocked", ["busy", "calendar_unavailable", "active_chat"])
def test_unsent_recovery_respects_current_activity_and_calendar(blocked):
    with ExitStack() as stack:
        profiles, _, messages = setup(stack)
        profiles.rows[0]["mediator_calendar_access"] = True
        if blocked == "active_chat":
            profiles.rows[0]["last_user_activity_at"] = T-60
        calendar = (T+1800, True) if blocked == "busy" else (None, False)
        with patch.object(scheduler, "owner_busy_until", return_value=calendar):
            assert delivery(profiles.find_one({"user_id": "owner"})) == "skipped"
        assert not messages.rows


def test_old_worker_cancellation_cannot_remove_a_new_pending_event():
    with ExitStack() as stack:
        profiles, _, _ = setup(stack)
        profiles.rows[0]["proactive_care_pending_delivery"]["event_key"] = "new-event"
        f.cancel_pending_delivery_slot("owner", event_key="proactive-care:c:r1")
        assert profiles.rows[0]["proactive_care_pending_delivery"]["event_key"] == "new-event"


def test_atomic_authorization_keeps_legacy_opt_out_closed():
    with ExitStack() as stack:
        profiles, _, messages = setup(stack)
        profiles.rows[0].pop("proactive_care_enabled")
        profiles.rows[0]["proactive_frequency"] = "normal"
        def pause(_now):
            profiles.rows[0]["proactive_frequency"] = "none"
            return None
        with patch.object(scheduler, "next_quiet_end", side_effect=pause):
            assert delivery(profiles.find_one({"user_id": "owner"})) == "retried"
        assert not messages.rows


def test_old_event_receipt_survives_a_newer_delivery():
    with ExitStack() as stack:
        profiles, _, messages = setup(stack)
        old_pending = deepcopy(profiles.rows[0]["proactive_care_pending_delivery"])
        assert delivery(profiles.find_one({"user_id": "owner"})) == "delivered"
        assert care.consume_proactive_delivery("owner")
        profiles.rows[0].update({
            "proactive_care_last_delivery_message_id": "newer-message",
            "proactive_care_last_sent_at": T+49*3600,
            "proactive_care_pending_delivery": old_pending,
        })
        assert delivery(profiles.find_one({"user_id": "owner"}), T+50*3600) == "delivered"
        assert "proactive_care_pending_delivery" not in profiles.rows[0]
        assert profiles.rows[0]["proactive_care_last_sent_at"] == T+49*3600
        assert care.consume_proactive_delivery("owner") is None
        assert len(messages.rows) == 1
