from types import SimpleNamespace

from fastapi import BackgroundTasks

from models import DirectChatRequest
from routers import chat


class MatchCollection:
    def __init__(self, match_doc):
        self.match_doc = match_doc

    def find_one(self, *_args, **_kwargs):
        return self.match_doc


def test_delivered_pair_chat_schedules_semantic_plan(monkeypatch):
    match_doc = {
        "_id": "match-1",
        "status": "accepted",
        "from_user": "user_a",
        "to_user": "user_b",
    }
    background_tasks = BackgroundTasks()
    monkeypatch.setattr(
        chat.risk_client,
        "check_risk",
        lambda **_kwargs: {"risk_level": "low"},
    )
    monkeypatch.setattr(chat.risk_client, "is_blocked", lambda _result: False)
    monkeypatch.setattr(
        chat.risk_client,
        "attach_to_response",
        lambda response, _assessment: response,
    )
    monkeypatch.setattr(chat, "save_message", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        chat,
        "profiles_coll",
        SimpleNamespace(update_one=lambda *_args, **_kwargs: None),
    )
    monkeypatch.setattr(chat, "matches_coll", MatchCollection(match_doc))
    monkeypatch.setattr(chat, "observe_user_memory", lambda *_args: None)
    monkeypatch.setattr(chat, "mark_post_chat_activity", lambda *_args: 1)

    response = chat.direct_chat(
        DirectChatRequest(
            user_id="user_a",
            contact_id="user_b",
            message="最近想一起去看電影",
        ),
        background_tasks,
    )

    task_functions = [task.func for task in background_tasks.tasks]
    assert response["message_saved"] is True
    assert chat.process_relationship_semantic_plan in task_functions
