import hashlib
import mongomock
from services.voice_chat_status import read_delivery_receipt


def test_delivered_receipt_is_owner_bound_and_uses_persisted_delivery():
    collection = mongomock.MongoClient().db.messages
    digest = hashlib.sha256(b'room:owner:voice-1').hexdigest()
    collection.insert_one({'_id': f'pair-owner:{digest}', 'room_id': 'room', 'sender_id': 'owner',
        'metadata': {'risk': {'level': 'blocked', 'delivery': 'delivered', 'sanction_exempted': True}}})
    assert read_delivery_receipt('owner', 'room', 'voice-1', collection=collection)['state'] == 'delivered'
    assert read_delivery_receipt('other', 'room', 'voice-1', collection=collection) is None


def test_blocked_notice_does_not_become_the_receivers_send_receipt():
    collection = mongomock.MongoClient().db.messages
    digest = hashlib.sha256(b'room:blocked-notice:voice-1').hexdigest()
    collection.insert_one({'_id': f'system-event:{digest}', 'room_id': 'room',
        'metadata': {'sender_owner_id': 'owner', 'risk': {'delivery': 'blocked'}}})
    assert read_delivery_receipt('owner', 'room', 'voice-1', collection=collection)['state'] == 'blocked'
    assert read_delivery_receipt('receiver', 'room', 'voice-1', collection=collection) is None
