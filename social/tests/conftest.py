"""Prevent pytest collection from loading developer or production secrets."""

import os

import pytest


os.environ["AYUE_SKIP_DOTENV"] = "1"
os.environ["MONGO_URI"] = "mongodb://127.0.0.1:27017"


@pytest.fixture(autouse=True)
def default_empty_risk_block_state(monkeypatch):
    """Keep unrelated unit tests hermetic after block checks became fail-closed.

    Tests that exercise block behavior override these methods explicitly.
    """
    from services.risk_block_service import risk_block_service

    monkeypatch.setattr(risk_block_service, "excluded_user_ids", lambda _user_id: set())
    monkeypatch.setattr(
        risk_block_service,
        "is_pair_blocked",
        lambda _first_user_id, _second_user_id: False,
    )
