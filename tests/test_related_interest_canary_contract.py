"""Synthetic configuration only; never load accounts, credentials or Graph."""
import json
from pathlib import Path

import pytest

from matchmaker_agent.related_interest_canary import (
    COHORT_ENV, canary_cohort, canary_requester_enabled, canary_pair_enabled,
)


@pytest.fixture
def configured(monkeypatch, tmp_path):
    monkeypatch.setenv("MATCH_RELATED_INTEREST_ENABLED", "on")
    monkeypatch.setenv("MATCH_PREFERENCE_SEMANTIC_MODE", "active")
    monkeypatch.setenv(COHORT_ENV, '["synthetic-a","synthetic-b"]')
    path = tmp_path / "kill"
    monkeypatch.setenv("MATCH_RELATED_INTEREST_KILL_SWITCH_FILE", str(path))
    return path


@pytest.mark.parametrize("raw", [
    "", "not-json", "null", "true", "{}", "[]", '["a"]',
    '["a","a"]', '["a",null]', '["a",1]', '["a","*"]', '["a"," b"]',
    '["a","b@example.com"]', '["a","中文暱稱"]', json.dumps(["a", "b"*129]), " "*1400,
    json.dumps([f"synthetic-{i}" for i in range(11)]),
    json.dumps([f"synthetic-{i}" for i in range(9)]+["synthetic-1"]),
])
def test_invalid_cohort_never_means_everyone(configured, monkeypatch, raw):
    monkeypatch.setenv(COHORT_ENV, raw)
    assert canary_cohort() == frozenset()
    assert not canary_requester_enabled("synthetic-a")
    assert not canary_pair_enabled("synthetic-a", "synthetic-b")


def test_missing_cohort_fail_closed(configured, monkeypatch):
    monkeypatch.delenv(COHORT_ENV)
    assert not canary_requester_enabled("synthetic-a")


@pytest.mark.parametrize("count", [2, 3, 5, 8, 10])
def test_bounded_expansion_keeps_all_members_and_drops_outsiders(configured, monkeypatch, count):
    ids = [f"synthetic-{i}" for i in range(count)]
    monkeypatch.setenv(COHORT_ENV, json.dumps(ids))
    assert canary_cohort() == frozenset(ids)
    assert all(canary_pair_enabled(ids[0], other) for other in ids[1:])
    assert not canary_pair_enabled(ids[0], "outsider")
    assert not canary_requester_enabled("outsider")
    configured.touch()
    assert not canary_pair_enabled(ids[0], ids[-1])


def test_stable_ids_only_both_directions_no_self_or_outsider(configured):
    assert canary_pair_enabled("synthetic-a", "synthetic-b")
    assert canary_pair_enabled("synthetic-b", "synthetic-a")
    for requester, candidate in [("outsider", "synthetic-b"), ("synthetic-a", "outsider"),
                                  ("synthetic-a", "synthetic-a"), (None, "synthetic-b")]:
        assert not canary_pair_enabled(requester, candidate)


@pytest.mark.parametrize("name,value", [
    ("MATCH_RELATED_INTEREST_ENABLED", "off"),
    ("MATCH_PREFERENCE_SEMANTIC_MODE", "off"),
    ("MATCH_PREFERENCE_SEMANTIC_MODE", "shadow"),
])
def test_global_flags_are_necessary_not_sufficient(configured, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    assert not canary_pair_enabled("synthetic-a", "synthetic-b")


def test_kill_wins_over_all_flags_and_cohort(configured):
    assert canary_pair_enabled("synthetic-a", "synthetic-b")
    configured.touch()
    assert not canary_requester_enabled("synthetic-a")
    assert not canary_pair_enabled("synthetic-a", "synthetic-b")


def test_startup_shares_operator_cohort_without_service_dotenv_fallback():
    script = (Path(__file__).resolve().parents[1] / "start_all.sh").read_text()
    assert 'export MATCH_RELATED_INTEREST_CANARY_USER_IDS="${MATCH_RELATED_INTEREST_CANARY_USER_IDS:-}"' in script
    assert script.index("export MATCH_RELATED_INTEREST_CANARY_USER_IDS=") < script.index("# Validate provider")
