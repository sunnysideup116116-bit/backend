from main import app
from routers import chat
from services.mediator_event_service import EVENT_PRIORITIES


def test_backend_web_ui_has_no_probe_controls_or_copy():
    with open("main_app/frontend.html", encoding="utf-8") as frontend:
        source = frontend.read()

    assert "probe-mode-select" not in source
    assert "updateProbeMode" not in source
    assert "探口風" not in source
    assert "幫我問一件他的有趣小事" not in source


def test_probe_api_is_not_registered():
    paths = app.openapi()["paths"]

    assert "/api/mediator/probe" not in paths


def test_probe_state_machine_is_not_exposed_by_chat_module():
    removed_names = {
        "participant_probe_state",
        "participant_probe_field",
        "probe_policy",
        "choose_probe_kind",
        "queue_due_feedback",
        "deliver_consented_signal",
        "request_relationship_probe",
        "is_relationship_probe_request",
        "mediator_probe",
    }

    assert not [name for name in removed_names if hasattr(chat, name)]


def test_probe_events_have_no_delivery_priority():
    removed_event_types = {
        "feedback_request",
        "feedback_consent_request",
        "probe_result",
        "gentle_closure",
        "mutual_interest",
        "probe_question",
    }

    assert removed_event_types.isdisjoint(EVENT_PRIORITIES)
