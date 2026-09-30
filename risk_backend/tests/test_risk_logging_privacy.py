"""Bounded process logging and stable public error contracts."""

from pathlib import Path

import asyncio

import httpx
from fastapi import FastAPI

from app.api.risk_detection import _guardrail_log_view, _nlp_log_view


RISK_DETECTION_SOURCE = Path(__file__).parents[1] / "app" / "api" / "risk_detection.py"


def test_guardrail_log_view_exposes_counts_and_reason_codes_only():
    view = _guardrail_log_view({
        "is_blocked": True,
        "reason": "Hard-Block: secret phrase",
        "flagged_words": ["secret phrase"],
        "classifier_categories": ["S2"],
    })
    assert view == {
        "reason_code": "hard_block",
        "flagged_count": 1,
        "category_count": 1,
        "category_type": "list",
    }
    assert "secret" not in repr(view)


def test_nlp_log_view_exposes_fallback_and_feature_shape_only():
    view = _nlp_log_view({
        "reasoning": "Fallback: provider payload",
        "detected_features": [{"feature_name": "private"}],
    })
    assert view == {
        "reason_code": "fallback",
        "features_type": "list",
        "features_count": 1,
    }
    assert "provider payload" not in repr(view)


def test_detect_public_error_and_log_contracts_are_stable():
    source = RISK_DETECTION_SOURCE.read_text(encoding="utf-8")
    assert 'detail="risk_detect_internal_error"' in source
    assert "detail=str(e)" not in source
    assert "traceback.print_exc()" not in source
    assert "Sender: {req.sender_id}" not in source
    assert "Msg: {req.current_message}" not in source
    assert "Flagged Words       : {gr_result['flagged_words']}" not in source
    assert "NLP Reasoning       :" not in source
    assert "NLP Detected Feats  : {nlp_result.get('detected_features', [])}" not in source


def test_detect_asgi_error_response_and_stdout_are_redacted(monkeypatch, capsys):
    from app.api import risk_detection as api

    async def fail_guardrail(_text):
        raise RuntimeError("SECRET_PROVIDER_PAYLOAD")

    monkeypatch.setattr(api.guardrail_engine, "check", fail_guardrail)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1/risk")
    capsys.readouterr()

    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://risk.test") as client:
            return await client.post(
                "/api/v1/risk/detect",
                json={
                    "conversation_id": "room-secret",
                    "current_message": "private message SECRET_CURRENT",
                    "sender_id": "sender-secret",
                    "receiver_id": "receiver-secret",
                },
            )

    response = asyncio.run(request())

    assert response.status_code == 500
    assert response.json() == {"detail": "risk_detect_internal_error"}
    output = capsys.readouterr().out
    assert "SECRET_PROVIDER_PAYLOAD" not in output
    assert "SECRET_CURRENT" not in output
    assert "sender-secret" not in output
    assert "receiver-secret" not in output
