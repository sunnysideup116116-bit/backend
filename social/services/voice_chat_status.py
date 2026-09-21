"""Owner-only delivery receipts and current cooldown for the voice adapter."""
import hashlib
import os
import requests

from app_voice_assistant.status_contract import (
    iso_now, public_cooldown, public_delivery, status_result,
)


def read_delivery_receipt(owner, room, attempt, *, collection=None):
    if not attempt:
        return None
    if collection is None:
        from database import messages_coll
        collection = messages_coll
    digest = hashlib.sha256(f'{room}:{owner}:{attempt}'.encode()).hexdigest()
    row = collection.find_one({'_id': f'pair-owner:{digest}', 'room_id': room, 'sender_id': owner})
    state = 'delivered'
    if row is None:
        digest = hashlib.sha256(f'{room}:blocked-notice:{attempt}'.encode()).hexdigest()
        row = collection.find_one({'_id': f'system-event:{digest}', 'room_id': room})
        state = 'blocked'
    if row is None:
        return None
    risk = (row.get('metadata') or {}).get('risk') or {}
    sender = risk.get('sender_directive') or {}
    # Historical notices without an explicit owner binding cannot prove ownership.
    if state == 'blocked' and sender.get('target_user_id') != owner and (row.get('metadata') or {}).get('sender_owner_id') != owner:
        return None
    return {'state': state, 'risk_available': risk.get('level') != 'unavailable',
            'sender_message': ((sender.get('content') or {}).get('body') or '')
            if sender.get('target_user_id') in {None, owner} else '',
            'checked_at': iso_now(), 'risk_assessment': risk}


def read_cooldown(owner, room):
    try:
        response = requests.get(
            f"{os.getenv('RISK_SERVICE_URL', 'http://127.0.0.1:8001').rstrip('/')}/api/v1/risk/state",
            params={'conversation_id': room, 'user_id': owner}, timeout=(2, 5),
        )
        response.raise_for_status()
        return public_cooldown(response.json().get('cooldown'))
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        return public_cooldown(None)


def read_chat_status(owner, room, attempt):
    cooldown = read_cooldown(owner, room)
    try:
        receipt = read_delivery_receipt(owner, room, attempt) if attempt else None
    except Exception:
        receipt = None
    return status_result(cooldown=cooldown,
                         delivery=public_delivery(receipt) if attempt else None)
