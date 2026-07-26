from types import SimpleNamespace

from fastapi import BackgroundTasks

from models import MediatorPrivateRequest
from routers import chat


class ProfileCollection:
    def __init__(self, profile):
        self.profile = profile
        self.updates = []

    def find_one(self, *_args, **_kwargs):
        return self.profile

    def update_one(self, *args, **kwargs):
        self.updates.append((args, kwargs))
        return SimpleNamespace(modified_count=1)


class MatchCollection:
    def __init__(self):
        self.updates = []

    def update_one(self, *args, **kwargs):
        self.updates.append((args, kwargs))
        return SimpleNamespace(modified_count=1)


def configure_private_chat(monkeypatch, match_doc):
    profiles = ProfileCollection({"user_id": "user_a", "deep_profile": {}})
    matches = MatchCollection()
    saved = []
    monkeypatch.setattr(chat, "profiles_coll", profiles)
    monkeypatch.setattr(chat, "matches_coll", matches)
    monkeypatch.setattr(chat, "find_accepted_match", lambda *_args: match_doc)
    monkeypatch.setattr(chat, "save_message", lambda *args, **kwargs: saved.append((args, kwargs)))
    monkeypatch.setattr(
        chat,
        "save_private_mediator_reply",
        lambda *args, **kwargs: saved.append((args, kwargs)),
    )
    monkeypatch.setattr(chat, "observe_user_memory", lambda *_args, **_kwargs: None)
    return profiles, matches, saved


def test_quick_action_starts_revision_shared_date_coordination(monkeypatch):
    match_doc = {
        "_id": "match-1",
        "status": "accepted",
        "from_user": "user_a",
        "to_user": "user_b",
    }
    _profiles, matches, _saved = configure_private_chat(monkeypatch, match_doc)
    monkeypatch.setattr(
        chat,
        "get_relationship_semantic_context",
        lambda *_args: {"semantic_plan": {}, "knowledge_graph_triples": []},
        raising=False,
    )
    monkeypatch.setattr(
        chat,
        "orchestrate_date_coordination",
        lambda _message, _state, _context: {
            "reply": "我先幫你們整理時間。",
            "show_form": False,
            "form": {
                "date": "",
                "time": "",
                "activity": "",
                "budget": "",
            },
        },
        raising=False,
    )

    response = chat.mediator_private_chat(
        MediatorPrivateRequest(
            user_id="user_a",
            other_id="user_b",
            message="幫我們協調約會",
        ),
        BackgroundTasks(),
    )

    assert response["reply"] == "我先幫你們整理時間。"
    persisted_states = [
        args[1]["$set"]["date_coordination"]
        for args, _kwargs in matches.updates
        if "date_coordination" in args[1].get("$set", {})
    ]
    assert persisted_states
    assert persisted_states[0]["status"] == "gathering"
    assert persisted_states[0]["form"] == {
        "date": "",
        "time": "",
        "activity": "",
        "budget": "",
    }


def test_private_prompt_contains_revision_semantic_context(monkeypatch):
    match_doc = {
        "_id": "match-1",
        "status": "accepted",
        "from_user": "user_a",
        "to_user": "user_b",
        "reason": "都喜歡電影",
    }
    configure_private_chat(monkeypatch, match_doc)
    monkeypatch.setattr(chat, "mediator_style", lambda _user_id: "自然")
    monkeypatch.setattr(chat, "mediator_profile_context", lambda user_id, _message: {"user_id": user_id})
    monkeypatch.setattr(chat, "latest_shared_chat", lambda *_args: [])
    monkeypatch.setattr(
        chat,
        "get_relationship_semantic_context",
        lambda *_args: {
            "semantic_plan": {"current_role": "ADVISER"},
            "knowledge_graph_triples": [
                {"subject": "user_b", "predicate": "LIKES", "object": "電影"}
            ],
        },
        raising=False,
    )
    captured = {}

    def fake_completion(prompt, **_kwargs):
        captured["prompt"] = prompt
        return "可以從電影聊起。"

    monkeypatch.setattr(chat, "generate_chat_completion", fake_completion)

    response = chat.mediator_private_chat(
        MediatorPrivateRequest(
            user_id="user_a",
            other_id="user_b",
            message="最近不知道聊什麼",
        ),
        BackgroundTasks(),
    )

    assert response["reply"] == "可以從電影聊起。"
    assert "共同朋友" in captured["prompt"]
    assert "ADVISER" in captured["prompt"]
    assert "LIKES" in captured["prompt"]


def test_legacy_pending_date_is_migrated_to_revision_state(monkeypatch):
    match_doc = {
        "_id": "match-1",
        "status": "accepted",
        "from_user": "user_a",
        "to_user": "user_b",
    }
    profiles, matches, _saved = configure_private_chat(monkeypatch, match_doc)
    profiles.profile["pending_date_coordination"] = {
        "match_id": "match-1",
        "other_id": "user_b",
        "stage": "activity",
        "data": {"availability": ["週末"]},
    }
    monkeypatch.setattr(
        chat,
        "get_relationship_semantic_context",
        lambda *_args: {"semantic_plan": {}, "knowledge_graph_triples": []},
    )
    monkeypatch.setattr(
        chat,
        "orchestrate_date_coordination",
        lambda _message, _state, _context: {
            "reply": "我們改用共用表單繼續。",
            "show_form": False,
            "form": {"date": "", "time": "", "activity": "", "budget": ""},
        },
    )

    response = chat.mediator_private_chat(
        MediatorPrivateRequest(
            user_id="user_a",
            other_id="user_b",
            message="我想看電影",
        ),
        BackgroundTasks(),
    )

    assert response["date_coordination"]["version"] == 2
    assert any(
        "pending_date_coordination" in args[1].get("$unset", {})
        for args, _kwargs in profiles.updates
    )
