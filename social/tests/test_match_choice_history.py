from copy import deepcopy
import time

from services.ayue_agent.v3.confirmation import project_match_choice_history
from tests.match_flow_store import Collection


def test_existing_message_gets_explicit_labels_without_writes_or_authority_leaks():
    collection = Collection([{
        "_id": "choice", "user_id": "owner", "room_id": "room", "surface": "public_ayue",
        "interaction_mode": "bubble_buttons_v1", "tool_name": "match.decide_active_proposal",
        "arguments": {"decision": "declined"}, "status": "pending", "expires_at": time.time() + 300,
        "payload": {"match_id": "private-match", "proposal_revision": 7},
    }])
    messages = [{"content": "old preview", "metadata": {"choice_prompt": {"id": "choice", "state": "pending"}}}]
    before = deepcopy(messages)
    result = project_match_choice_history(messages, user_id="owner", room_id="room", collection=collection)
    assert result[0]["metadata"]["choice_prompt"]["cancel_label"] == "保留提案"
    assert result[0]["metadata"]["choice_prompt"]["confirm_label"] == "確認放棄"
    assert "private-match" not in str(result)
    assert collection.writes == 0 and messages == before
    assert project_match_choice_history(messages, user_id="another", room_id="room", collection=collection) == messages
    assert project_match_choice_history(messages, user_id="owner", room_id="another", collection=collection) == messages


def test_expired_choice_is_projected_but_not_mutated():
    record = {"_id": "choice", "user_id": "owner", "room_id": "room", "surface": "public_ayue",
              "interaction_mode": "bubble_buttons_v1", "tool_name": "match.start_search",
              "arguments": {}, "status": "pending", "expires_at": 1}
    collection = Collection([record])
    result = project_match_choice_history([{"metadata": {"choice_prompt": {"id": "choice"}}}], user_id="owner", room_id="room", collection=collection)
    assert result[0]["metadata"]["choice_prompt"]["state"] == "expired"
    assert collection.rows[0]["status"] == "pending" and collection.writes == 0
