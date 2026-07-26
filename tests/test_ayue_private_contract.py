import importlib
from types import SimpleNamespace

import pytest
from pydantic import ValidationError


def test_date_update_request_rejects_unknown_form_fields():
    models = importlib.import_module("models")

    with pytest.raises(ValidationError):
        models.DateUpdateRequest(
            user_id="user_a",
            other_id="user_b",
            form={
                "date": "2026-08-01",
                "time": "19:00",
                "activity": "看電影",
                "budget": "500",
                "private_note": "must not persist",
            },
        )


def test_date_confirm_request_accepts_optional_form_revision():
    models = importlib.import_module("models")

    request = models.DateConfirmRequest(
        user_id="user_a",
        other_id="user_b",
        form_revision=3,
    )

    assert request.form_revision == 3


def test_revision_date_routes_are_registered():
    chat = importlib.import_module("routers.chat")
    routes = {
        (method, route.path)
        for route in chat.router.routes
        for method in route.methods
    }

    assert ("POST", "/api/relationship/date/update") in routes
    assert ("POST", "/api/relationship/date/confirm") in routes


def test_confirm_migrates_legacy_form_without_revision(monkeypatch):
    chat = importlib.import_module("routers.chat")
    models = importlib.import_module("models")
    match_doc = {
        "_id": "match-1",
        "status": "accepted",
        "from_user": "user_a",
        "to_user": "user_b",
        "date_coordination": {
            "status": "active",
            "form": {
                "date": "2026-08-01",
                "time": "19:00",
                "activity": "看電影",
                "budget": "500",
            },
            "confirmations": {},
        },
    }
    queries = []

    class Matches:
        def update_one(self, query, _update):
            queries.append(query)
            return SimpleNamespace(modified_count=1)

    monkeypatch.setattr(chat, "matches_coll", Matches())
    monkeypatch.setattr(
        chat, "find_accepted_match", lambda *_args: match_doc
    )
    monkeypatch.setattr(chat, "queue_mediator_event", lambda *_args, **_kwargs: None)

    response = chat.confirm_date_form(
        models.DateConfirmRequest(user_id="user_a", other_id="user_b")
    )

    assert response["status"] == "waiting"
    assert "date_coordination.form_revision" not in queries[0]


def test_revision_date_events_have_relationship_priority():
    events = importlib.import_module("services.mediator_event_service")

    assert events.event_priority("date_coordination_form") >= 70
    assert events.event_priority("date_coordination_success") >= 80
