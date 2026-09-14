"""Offline dotenv and catalog/protocol regression tests."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from services import gpt_settings
from services import codex_chat_provider as provider
from .test_codex_chat_provider import FakeRpc, mock_session


def test_allowlist_and_explicit_environment_precedence(tmp_path):
    path = tmp_path / ".env"
    path.write_text('AYUE_GPT_MODEL="file-model"\nAYUE_GPT_FAST_MODEL=file-fast\n'
                    'AYUE_GPT_PLANNER_MODEL=planner-model\n'
                    'AYUE_GPT_SERVICE_TIER=fast\nAYUE_CODEX_BIN=${HOME}/codex\n'
                    'AYUE_CODEX_HOME=$(touch forbidden)\nOTHER_SECRET=hidden\n'
                    'AYUE_LLM_PROVIDER=gpt\n')
    env = {"AYUE_GPT_MODEL": "exported-model", "AYUE_GPT_FAST_MODEL": ""}
    gpt_settings.load_gpt_settings(path, environ=env)
    assert env == {"AYUE_GPT_MODEL": "exported-model", "AYUE_GPT_FAST_MODEL": "",
                   "AYUE_GPT_PLANNER_MODEL": "planner-model",
                   "AYUE_GPT_SERVICE_TIER": "fast", "AYUE_CODEX_BIN": "${HOME}/codex",
                   "AYUE_CODEX_HOME": "$(touch forbidden)"}
    gpt_settings.load_gpt_settings(tmp_path / "missing", environ=env)
    assert env["AYUE_GPT_MODEL"] == "exported-model"
    skipped = {"AYUE_SKIP_DOTENV": "on"}
    gpt_settings.load_gpt_settings(path, environ=skipped)
    assert skipped == {"AYUE_SKIP_DOTENV": "on"}


@pytest.mark.parametrize("exported", [None, "shell-model"])
def test_preflight_and_social_load_same_root_settings(tmp_path, exported):
    root = Path(__file__).resolve().parents[2]
    services = tmp_path / "social/services"
    services.mkdir(parents=True)
    for name in ("gpt_settings.py", "codex_chat_provider.py"):
        (services / name).write_bytes((root / "social/services" / name).read_bytes())
    (tmp_path / "social/config.py").write_bytes((root / "social/config.py").read_bytes())
    (tmp_path / ".env").write_text("AYUE_GPT_MODEL=root-model\nAYUE_GPT_SERVICE_TIER=fast\n")
    (tmp_path / "social/.env").write_text("AYUE_GPT_MODEL=wrong-social-model\n")
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(tmp_path / "social")}
    if exported is not None:
        env["AYUE_GPT_MODEL"] = exported
    # Both import paths, no preflight execution, subprocess or remote calls.
    for imports in ("import config; from services import codex_chat_provider as p",
                    "import sys; sys.path.insert(0, 'social/services'); import codex_chat_provider as p"):
        result = subprocess.run([sys.executable, "-c", imports + "; print(p.selected_model())"],
                                cwd=tmp_path, env=env, text=True, capture_output=True, timeout=5)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == (exported or "root-model")


class TierRpc(FakeRpc):
    def __init__(self, tiers, returned_tier="priority"):
        super().__init__()
        self.tiers = tiers
        self.returned_tier = returned_tier

    def call(self, method, params):
        result = super().call(method, params)
        if method == "model/list":
            result["data"][0]["serviceTiers"] = self.tiers
        if method == "thread/start":
            result["serviceTier"] = self.returned_tier
        return result


@pytest.mark.parametrize("requested", ["fast", "priority"])
def test_fast_catalog_id_and_default_reasoning(monkeypatch, requested):
    monkeypatch.setenv("AYUE_GPT_SERVICE_TIER", requested)
    rpc = TierRpc([{"id": "priority", "name": "Fast"}])
    mock_session(monkeypatch, rpc)
    provider.generate("offline", model="test-gpt")
    for method in ("thread/start", "turn/start"):
        params = dict(rpc.calls)[method]
        assert params["serviceTier"] == "priority"
        assert "effort" not in params and "reasoningEffort" not in params


@pytest.mark.parametrize("tiers", [[], [{"id": "flex", "name": "Flex"}],
                                    [{"id": "priority", "name": "Fast"}, {"id": "other", "name": "Fast"}]])
def test_unsupported_fast_fails_before_thread(monkeypatch, tiers):
    monkeypatch.setenv("AYUE_GPT_SERVICE_TIER", "fast")
    rpc = TierRpc(tiers)
    mock_session(monkeypatch, rpc)
    with pytest.raises(provider.CodexProviderError, match="service tier is unavailable"):
        provider.generate("offline", model="test-gpt")
    assert "thread/start" not in dict(rpc.calls)


def test_changed_tier_fails_before_turn(monkeypatch):
    monkeypatch.setenv("AYUE_GPT_SERVICE_TIER", "fast")
    rpc = TierRpc([{"id": "priority", "name": "Fast"}], returned_tier=None)
    mock_session(monkeypatch, rpc)
    with pytest.raises(provider.CodexProviderError, match="changed requested service tier"):
        provider.generate("offline", model="test-gpt")
    assert "turn/start" not in dict(rpc.calls)


def test_unset_tier_leaves_protocol_default(monkeypatch):
    monkeypatch.delenv("AYUE_GPT_SERVICE_TIER", raising=False)
    rpc = TierRpc([])
    mock_session(monkeypatch, rpc)
    provider.generate("offline", model="test-gpt")
    assert "serviceTier" not in dict(rpc.calls)["thread/start"]
    assert "serviceTier" not in dict(rpc.calls)["turn/start"]


def test_preflight_checks_fast_routing_model_tier_without_starting_thread(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setenv("AYUE_GPT_MODEL", "main-model")
    monkeypatch.setenv("AYUE_GPT_FAST_MODEL", "fast-model")
    monkeypatch.setenv("AYUE_GPT_SERVICE_TIER", "fast")
    rpc = Mock()
    rpc.call.side_effect = [
        {"account": {"type": "chatgpt"}},
        {"data": [{"model": "main-model", "serviceTiers": [{"id": "priority", "name": "Fast"}]}],
         "nextCursor": "page2"},
        {"data": [{"model": "fast-model", "serviceTiers": []}]},
    ]
    mock_session(monkeypatch, rpc)
    with pytest.raises(provider.CodexProviderError, match="service tier is unavailable"):
        provider.preflight()
    assert [call.args[0] for call in rpc.call.call_args_list] == ["account/read", "model/list", "model/list"]


def test_preflight_checks_configured_owner_models(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setenv("AYUE_GPT_MODEL", "main-model")
    monkeypatch.setenv("AYUE_GPT_FAST_MODEL", "fast-model")
    monkeypatch.setenv("AYUE_GPT_WEB_MODEL", "web-model")
    rpc = Mock()
    rpc.call.side_effect = [
        {"account": {"type": "chatgpt"}},
        {"data": [
            {"model": "main-model"}, {"model": "fast-model"},
            {"model": "web-model"},
        ]},
        {"data": []},
    ]
    mock_session(monkeypatch, rpc)
    provider.preflight()
    assert [call.args[0] for call in rpc.call.call_args_list] == [
        "account/read", "model/list", "mcpServerStatus/list",
    ]
