"""The two persisted message ID formats supported by conversation compaction.

System-event IDs are legitimate idempotency keys, not user-authored evidence.
Unknown formats fail closed rather than being silently skipped.
"""
import re

from bson import ObjectId


class UnsupportedConversationMessageId(ValueError):
    pass


def decode_message_id(value: str) -> ObjectId | str:
    if not isinstance(value, str):
        raise UnsupportedConversationMessageId('source_invalid_id')
    if ObjectId.is_valid(value):
        return ObjectId(value)
    if re.fullmatch(r'system-event:[0-9a-f]{64}', value):
        return value
    raise UnsupportedConversationMessageId('source_invalid_id')


def message_id_order_key(value: str) -> tuple[int, str]:
    """Match BSON ordering for the supported types: string before ObjectId."""
    decoded = decode_message_id(value)
    return (7 if isinstance(decoded, ObjectId) else 2, str(decoded))
