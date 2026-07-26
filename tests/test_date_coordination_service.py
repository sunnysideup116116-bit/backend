import importlib
import importlib.util

import pytest


def load_service():
    module_name = "services.date_coordination_service"
    assert importlib.util.find_spec(module_name) is not None, (
        "date coordination service has not been integrated"
    )
    return importlib.import_module(module_name)


def test_new_coordination_uses_revision_shared_form_contract():
    service = load_service()

    state = service.new_date_coordination(now=123.0)

    assert state == {
        "version": 2,
        "status": "gathering",
        "form": {"date": "", "time": "", "activity": "", "budget": ""},
        "form_revision": 1,
        "confirmations": {},
        "established_at": 123.0,
    }


def test_form_update_keeps_known_fields_and_invalidates_confirmations():
    service = load_service()
    state = service.new_date_coordination(now=123.0)
    state["confirmations"] = {"user_a": {"revision": 1, "confirmed_at": 124.0}}

    updated = service.update_date_form(
        state,
        {
            "date": " 2026-08-01 ",
            "time": " 19:00 ",
            "activity": " 看電影 ",
            "budget": " 500 ",
            "unexpected": "must not persist",
        },
    )

    assert updated["form"] == {
        "date": "2026-08-01",
        "time": "19:00",
        "activity": "看電影",
        "budget": "500",
    }
    assert updated["form_revision"] == 2
    assert updated["confirmations"] == {}
    assert updated["status"] == "active"


def test_confirm_requires_both_participants_on_current_revision():
    service = load_service()
    state = service.update_date_form(
        service.new_date_coordination(now=123.0),
        {
            "date": "2026-08-01",
            "time": "19:00",
            "activity": "看電影",
            "budget": "500",
        },
    )

    waiting = service.confirm_date_form(
        state,
        user_id="user_a",
        participant_ids=("user_a", "user_b"),
        expected_revision=2,
        now=130.0,
    )
    completed = service.confirm_date_form(
        waiting,
        user_id="user_b",
        participant_ids=("user_a", "user_b"),
        expected_revision=2,
        now=131.0,
    )

    assert waiting["status"] == "active"
    assert completed["status"] == "completed"
    assert completed["completed_at"] == 131.0


def test_confirm_rejects_stale_form_revision():
    service = load_service()
    state = service.update_date_form(
        service.new_date_coordination(now=123.0),
        {"activity": "散步"},
    )

    with pytest.raises(service.StaleDateFormError):
        service.confirm_date_form(
            state,
            user_id="user_a",
            participant_ids=("user_a", "user_b"),
            expected_revision=1,
            now=130.0,
        )
