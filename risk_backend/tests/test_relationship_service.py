import pytest


@pytest.fixture
def rel_service(monkeypatch):
    monkeypatch.setenv("APPWRITE_ENDPOINT", "http://test/v1")
    monkeypatch.setenv("APPWRITE_PROJECT_ID", "test-proj")
    monkeypatch.setenv("APPWRITE_API_KEY", "test-key")
    monkeypatch.setenv("APPWRITE_DB_ID", "test-db")
    from app.services.relationship_service import RelationshipService

    return RelationshipService()


def test_familiarity_score_zero_baseline(rel_service):
    score = rel_service.compute_familiarity_score(0, 1, 0.5)

    assert 0.20 < score < 0.30


def test_familiarity_score_saturation(rel_service):
    score = rel_service.compute_familiarity_score(500, 90, 0.5)

    assert score > 0.95


def test_familiarity_score_imbalance_penalty(rel_service):
    balanced = rel_service.compute_familiarity_score(100, 30, 0.5)
    skewed = rel_service.compute_familiarity_score(100, 30, 0.9)

    assert balanced > skewed
    assert balanced - skewed >= 0.10


def test_familiarity_score_log_saturation_curve(rel_service):
    s500 = rel_service.compute_familiarity_score(500, 90, 0.5)
    s1000 = rel_service.compute_familiarity_score(1000, 90, 0.5)

    assert abs(s1000 - s500) < 0.05
