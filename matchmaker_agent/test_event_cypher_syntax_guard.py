"""Regression guard for the Event graph query syntax.

Production Neo4j (Aura ``5.27-aura``) rejects the postfix ``NOT IN`` form:
``<value> NOT IN <expression>`` fails with
``Neo.ClientError.Statement.SyntaxError: Invalid input 'NOT'``.
``NOT (<value> IN <expression>)`` is accepted.

The last production Cypher that used the rejected form lived in
``MatchmakerAgent.find_event_matches`` (two occurrences, one for the owner
relevance clause and one for the candidate relevance clause). That query was
replaced by the V2 signal adapter, so the Event match path no longer depends on
it. These guards keep the rejected syntax from being reintroduced and keep the
Event match path on the adapter.
"""
from __future__ import annotations

import ast
from pathlib import Path
import re

import matchmaker_agent.event_v2_api as event_v2_api
from matchmaker_agent.matchmaker import MatchmakerAgent

PACKAGE_ROOT = Path(__file__).resolve().parent

# Built with escapes so the guard source itself does not contain the raw token
# it is looking for. Cypher keywords are case-insensitive.
POSTFIX_NOT_IN = re.compile(r"\bNOT\s+IN\b", re.IGNORECASE)

CYPHER_MARKERS = ("MATCH ", "MATCH(", "MERGE ", "UNWIND ", "CALL ")


def _production_modules():
    """Production modules only; test modules may quote the rejected form."""
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if path.name.startswith("test"):
            continue
        yield path


def _cypher_literals():
    for path in _production_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
                if any(marker in text.upper() for marker in CYPHER_MARKERS):
                    yield f"{path.name}:{node.lineno}", text


def test_guard_detects_the_rejected_form():
    """Positive control for the guard itself."""
    rejected = (
        "MATCH (target:User)-[target_relevance:EVENT_RELEVANCE]->(event:Event)\n"
        "WHERE ('recent' NOT IN coalesce(target_relevance.source_kinds, []))\n"
        "RETURN target.id AS user_id"
    )
    assert POSTFIX_NOT_IN.search(rejected)
    accepted = rejected.replace(
        "'recent' NOT IN coalesce(target_relevance.source_kinds, [])",
        "NOT ('recent' IN coalesce(target_relevance.source_kinds, []))",
    )
    assert not POSTFIX_NOT_IN.search(accepted)


def test_no_production_cypher_uses_the_rejected_postfix_not_in():
    offenders = [
        where for where, text in _cypher_literals()
        if POSTFIX_NOT_IN.search(text)
    ]
    assert offenders == [], (
        "Neo4j 5.27 rejects postfix NOT IN in Cypher; use NOT (x IN ...): "
        f"{offenders}"
    )


def test_legacy_event_bridge_query_is_gone():
    """The legacy owner/candidate EVENT_RELEVANCE bridge is not reintroduced."""
    offenders = [
        where for where, text in _cypher_literals()
        if where.startswith("matchmaker.py:")
        and ("EVENT_RELEVANCE" in text or "EVENT_AVOIDANCE" in text)
    ]
    assert offenders == []


def test_find_event_matches_routes_to_the_v2_adapter(monkeypatch):
    calls = []
    sentinel = object()

    def fake_find_matches(agent, owner, excluded):
        calls.append((agent, owner, excluded))
        return sentinel

    monkeypatch.setattr(event_v2_api, "find_matches", fake_find_matches)
    agent = object.__new__(MatchmakerAgent)
    assert MatchmakerAgent.find_event_matches(agent, "owner-1", ["blocked-1"]) is sentinel
    assert calls == [(agent, "owner-1", ["blocked-1"])]
