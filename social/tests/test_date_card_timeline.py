from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from bson import ObjectId

from services.date_coordination_service import _sync_card


@pytest.mark.parametrize("status", ["active", "completed", "declined", "cancelled"])
def test_updated_date_card_moves_after_chat_messages_and_retains_identity(status):
    card_id = ObjectId()
    stored_card = {"_id": card_id, "timestamp": 1.0}
    coordination = {
        "coordination_id": "date",
        "card_message_id": str(card_id),
        "status": status,
        "revision": 2,
        "confirmations": {"owner": True},
    }
    before_update = datetime.now(timezone.utc).timestamp()
    with patch("services.date_coordination_service.messages_coll.update_one") as update:
        _sync_card({}, coordination)
    query, mutation = update.call_args.args
    assert query == {"_id": card_id}
    stored_card.update(mutation["$set"])
    assert stored_card["_id"] == card_id
    assert before_update <= stored_card["timestamp"] <= datetime.now(timezone.utc).timestamp()
    assert stored_card["metadata"]["status"] == status
    assert stored_card["metadata"]["confirmations"] == {"owner": True}
    recent_chat = {"_id": "text", "timestamp": before_update - 1}
    assert sorted([stored_card, recent_chat], key=lambda row: row["timestamp"])[-1] is stored_card
