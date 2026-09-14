"""Prevent pytest collection from loading developer or production secrets."""

import os

import pytest


os.environ["AYUE_SKIP_DOTENV"] = "1"
os.environ["MONGO_URI"] = "mongodb://127.0.0.1:27017/?serverSelectionTimeoutMS=50&connectTimeoutMS=50"
# A local root .env can still be loaded by legacy mirroring modules. Explicit
# empty values keep unit-test nickname reads off the real Appwrite service.
os.environ["APPWRITE_PROJECT_ID"] = ""
os.environ["APPWRITE_API_KEY"] = ""


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
