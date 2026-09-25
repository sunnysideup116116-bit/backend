import asyncio
from types import SimpleNamespace
from unittest.mock import Mock
import json
import pytest

from agent_quota import internal
from agent_quota.service import SCOPE, task_scope
from matchmaker_agent.related_interest_validator import validate_concepts


@pytest.fixture(autouse=True)
def synthetic_completion_boundary(monkeypatch):
    from matchmaker_agent import related_interest_validator as validator
    def complete(client, *, timeout, **request):
        return client.with_options(timeout=timeout, max_retries=0).chat.completions.create(**request)
    monkeypatch.setattr(validator, "_completion", complete)


@pytest.mark.parametrize("path", ["/api/match", "/api/preferences/related-interest-candidates"])
def test_signed_matching_context_is_preserved_for_both_model_endpoints(monkeypatch, path):
    monkeypatch.setattr(internal, "secret", lambda: b"synthetic-signing-secret")
    with task_scope("synthetic-owner", "matching", "synthetic-job"):
        header = internal.signed_headers()["X-Agent-Quota"]
    seen = []
    async def app(_scope, _receive, _send):
        seen.append(SCOPE.get())
    async def receive(): return {"type": "http.request", "body": b""}
    async def send(_event): pass
    asyncio.run(internal.MatchmakerQuotaMiddleware(app)(
        {"type": "http", "path": path, "headers": [(b"x-agent-quota", header.encode())]}, receive, send))
    assert seen == [("synthetic-owner", "matching", "synthetic-job")]
    assert SCOPE.get() is None


def test_forged_context_cannot_reach_related_validator(monkeypatch):
    monkeypatch.setattr(internal, "secret", lambda: b"synthetic-signing-secret")
    seen = []
    async def app(*_args): raise AssertionError("must reject before model")
    async def receive(): return {"type": "http.request", "body": b""}
    async def send(event): seen.append(event)
    asyncio.run(internal.MatchmakerQuotaMiddleware(app)(
        {"type": "http", "path": "/api/preferences/related-interest-candidates",
         "headers": [(b"x-agent-quota", b"forged.context")]}, receive, send))
    assert seen[0]["status"] == 403 and SCOPE.get() is None


def completion(finish="stop"):
    return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
        model="deepseek-test", choices=[SimpleNamespace(finish_reason=finish,
            message=SimpleNamespace(content=json.dumps({"results": [{"id": "0", "relation": "role_mismatch"}]})))])


def test_validator_accounts_for_truncated_and_retried_completion(monkeypatch):
    from agent_quota import service
    recorder = Mock(); monkeypatch.setattr(service, "record_usage_deferred", recorder)
    client = Mock(); client.with_options.return_value = client
    client.chat.completions.create.side_effect = [completion("length"), completion()]
    accepted, counts = validate_concepts("Watching Football", [{"semantic_text": "Playing Football"}],
        client, "deepseek-test", deadline=100, clock=lambda: 0)
    assert accepted and counts["attempts"] == 2 and recorder.call_count == 2
    assert recorder.call_args_list[0].args[0] != recorder.call_args_list[1].args[0]
    assert all(call.args[1:] == (10, 20) for call in recorder.call_args_list)


def test_accounting_failure_does_not_retry_or_expand(monkeypatch):
    from agent_quota import service
    monkeypatch.setattr(service, "record_usage_deferred", Mock(side_effect=RuntimeError("storage unavailable")))
    client = Mock(); client.with_options.return_value = client
    client.chat.completions.create.return_value = completion()
    accepted, counts = validate_concepts("Watching Football", [{"semantic_text": "Playing Football"}],
        client, "deepseek-test", deadline=100, clock=lambda: 0)
    assert not accepted and counts["error"] == 1 and counts["attempts"] == 1
    assert client.chat.completions.create.call_count == 1


def test_deferred_usage_is_durable_and_default_delivery_is_unchanged(tmp_path):
    from agent_quota.service import QuotaService
    quota = QuotaService(store=Mock(), outbox=tmp_path / "quota.sqlite3")
    quota.replay = Mock()
    quota.record("owner", "matching", "job", "call", 10, 20, defer_delivery=True)
    quota.replay.assert_not_called()
    with quota._db() as db:
        rows = db.execute("SELECT payload FROM pending").fetchall()
    assert len(rows) == 1 and json.loads(rows[0][0])["output_tokens"] == 20
    quota.record("owner", "matching", "job", "call", 10, 20)
    quota.replay.assert_called_once_with(owner="owner")
    with quota._db() as db:
        assert db.execute("SELECT count(*) FROM pending").fetchone()[0] == 1
