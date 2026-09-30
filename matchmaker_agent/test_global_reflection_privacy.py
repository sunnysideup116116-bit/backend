"""Provider payloads and raw exceptions must not reach general logs."""

import asyncio
from unittest.mock import MagicMock

import agent_api


def request():
    return agent_api.GlobalReflectionRequest(from_big_five={}, to_big_five={})


def test_malformed_provider_output_is_not_logged(monkeypatch, capsys):
    private = "private-profile-contents-and-provider-token"
    monkeypatch.setattr(agent_api.agent, "generate_global_reflection", MagicMock(return_value=private))
    response = asyncio.run(agent_api.global_reflection_endpoint(request()))
    assert response == {"status": "error", "message": "JSON parse failed"}
    assert private not in capsys.readouterr().out


def test_provider_exception_uses_stable_public_error(monkeypatch, capsys):
    private = "private-key-and-database-host"
    monkeypatch.setattr(agent_api.agent, "generate_global_reflection", MagicMock(side_effect=RuntimeError(private)))
    response = asyncio.run(agent_api.global_reflection_endpoint(request()))
    assert response == {"status": "error", "message": "global_reflection_failed"}
    assert private not in capsys.readouterr().out


def test_successful_rule_content_is_not_written_to_general_log(monkeypatch, capsys):
    private = "private profile topic"
    monkeypatch.setattr(agent_api.agent, "generate_global_reflection", MagicMock(return_value='{"abstract_rule":"private profile topic","category":"context"}'))
    driver, session = MagicMock(), MagicMock()
    driver.__enter__.return_value = driver
    driver.session.return_value.__enter__.return_value = session
    monkeypatch.setattr(agent_api.GraphDatabase, "driver", MagicMock(return_value=driver))
    monkeypatch.setattr(agent_api, "find_similar_global_rule", lambda *args: None)
    response = asyncio.run(agent_api.global_reflection_endpoint(request()))
    assert response["status"] == "success"
    assert response["abstract_rule"] == private
    assert private not in capsys.readouterr().out
