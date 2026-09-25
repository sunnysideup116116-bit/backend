"""Cold-start regression: synthetic settings only; no DB/provider/network I/O."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from dotenv import dotenv_values
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from agent_quota import internal, signing_config
from agent_quota.service import SCOPE, task_scope

ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC = "synthetic-shared-signing-value"


@pytest.fixture
def config_root(tmp_path, monkeypatch):
    monkeypatch.setattr(signing_config, "SERVER_ROOT", tmp_path)
    monkeypatch.delenv("APPWRITE_API_KEY", raising=False)
    monkeypatch.delenv("AYUE_SKIP_DOTENV", raising=False)
    monkeypatch.delenv("DOTENV_DISABLED", raising=False)
    (tmp_path / "social").mkdir()
    (tmp_path / "matchmaker_agent").mkdir()
    return tmp_path


def write_root(root, text=None):
    (root / ".env").write_text(text or f"APPWRITE_API_KEY={SYNTHETIC}\n")


def service_load(root, service):
    """Actual dotenv precedence, independent of the offline suite's global stub."""
    for name, value in dotenv_values(root / service / ".env").items():
        if value is not None:
            os.environ.setdefault(name, value)


def invoke(header, path="/api/match"):
    observed, messages = [], []
    async def app(_scope, _receive, _send):
        observed.append(SCOPE.get())
    async def receive():
        return {"type": "http.request", "body": b""}
    async def send(message):
        messages.append(message)
    scope = {"type": "http", "path": path, "headers": [(b"x-agent-quota", header.encode())] if header else []}
    asyncio.run(internal.MatchmakerQuotaMiddleware(app)(scope, receive, send))
    return observed, messages


@pytest.mark.parametrize("path", ["/api/match", "/api/preferences/related-interest-candidates"])
def test_cold_social_signature_matches_cold_matchmaker(config_root, monkeypatch, path):
    write_root(config_root)
    # Divergent service-local values must not override the shared startup key.
    for service in ("social", "matchmaker_agent"):
        (config_root / service / ".env").write_text(f"APPWRITE_API_KEY=synthetic-{service}-obsolete\n")
    signing_config.load_signing_config()
    service_load(config_root, "social")
    with task_scope("synthetic-owner", "matching", "synthetic-task"):
        header = internal.signed_headers()["X-Agent-Quota"]
    monkeypatch.delenv("APPWRITE_API_KEY")  # Fresh verifier process, not a warm store.
    signing_config.load_signing_config()
    service_load(config_root, "matchmaker_agent")
    observed, _ = invoke(header, path)
    assert observed == [("synthetic-owner", "matching", "synthetic-task")]
    assert SCOPE.get() is None


def test_root_loader_does_not_import_other_settings(config_root, monkeypatch, capsys, caplog):
    monkeypatch.delenv("UNRELATED_SYNTHETIC_DB", raising=False)
    write_root(config_root, f"APPWRITE_API_KEY={SYNTHETIC}\nUNRELATED_SYNTHETIC_DB=do-not-import\n")
    signing_config.validate_signing_config()
    assert "UNRELATED_SYNTHETIC_DB" not in os.environ
    assert internal.secret() == SYNTHETIC.encode()
    assert SYNTHETIC not in caplog.text + str(capsys.readouterr())


def test_common_injected_environment_wins(config_root, monkeypatch):
    write_root(config_root)
    monkeypatch.setenv("APPWRITE_API_KEY", "synthetic-injected-shared-value")
    assert internal.secret() == b"synthetic-injected-shared-value"


def test_shared_social_legacy_fallback(config_root):
    (config_root / "social/.env").write_text(f"APPWRITE_API_KEY={SYNTHETIC}\n")
    assert internal.secret() == SYNTHETIC.encode()


@pytest.mark.parametrize("override", ["", "   "])
def test_empty_explicit_override_fails_closed(config_root, monkeypatch, override):
    write_root(config_root)
    monkeypatch.setenv("APPWRITE_API_KEY", override)
    with pytest.raises(signing_config.QuotaSigningConfigurationError, match="^quota_signing_key_unavailable$"):
        signing_config.validate_signing_config()


@pytest.mark.parametrize("flag", ["AYUE_SKIP_DOTENV", "DOTENV_DISABLED"])
def test_offline_flags_prevent_secret_file_loading(config_root, monkeypatch, flag):
    write_root(config_root)
    monkeypatch.setenv(flag, "1")
    with pytest.raises(signing_config.QuotaSigningConfigurationError):
        internal.secret()


def test_unreadable_configuration_error_is_secret_safe(config_root, monkeypatch, caplog, capsys):
    def fail(*args, **kwargs):
        raise OSError(SYNTHETIC)
    monkeypatch.setattr(signing_config, "dotenv_values", fail)
    with pytest.raises(signing_config.QuotaSigningConfigurationError) as failure:
        internal.secret()
    assert str(failure.value) == "quota_signing_config_unreadable"
    assert failure.value.__suppress_context__
    assert SYNTHETIC not in str(failure.value) + caplog.text + str(capsys.readouterr())


def test_missing_key_startup_rejects_before_worker_or_health(config_root):
    started = []
    app = FastAPI()
    app.router.add_event_handler("startup", signing_config.validate_signing_config)
    app.router.add_event_handler("startup", lambda: started.append(True))
    @app.get("/health")
    def health():
        raise AssertionError("must never serve")
    with pytest.raises(signing_config.QuotaSigningConfigurationError):
        with TestClient(app):
            pytest.fail("missing key must not finish startup")
    assert started == []


def test_valid_key_startup_is_local_and_secret_safe(config_root):
    write_root(config_root)
    app = FastAPI()
    app.router.add_event_handler("startup", signing_config.validate_signing_config)
    @app.get("/health")
    def health():
        return {"status": "ok"}
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize("missing", [False, True])
def test_invalid_signature_and_missing_key_still_403(config_root, missing):
    if not missing:
        write_root(config_root)
    observed, messages = invoke("invalid.signature")
    assert not observed and messages[0]["status"] == 403
    assert json.loads(messages[1]["body"]) == {"detail": "invalid_quota_context"}
    assert SYNTHETIC.encode() not in messages[1]["body"]


def test_unsigned_background_semantics_unchanged(config_root):
    observed, messages = invoke("")
    assert observed == [None] and not messages


def test_entrypoints_share_early_loader_and_startup_validation():
    social = (ROOT / "social/main.py").read_text()
    matcher = (ROOT / "matchmaker_agent/agent_api.py").read_text()
    assert social.index("load_signing_config()") < social.index("from routers import")
    assert matcher.index("load_signing_config()") < matcher.index("load_dotenv(dotenv_path=env_path)")
    for source, worker in ((social, "start_quota_worker"), (matcher, "start_worker")):
        # Validate ASGI startup order independent of full app imports/DB workers.
        assert source.index("add_event_handler(\"startup\", validate_signing_config)" if source is social else "add_event_handler('startup', validate_signing_config)") < source.index("add_event_handler(\"startup\", start_quota_worker)" if source is social else "add_event_handler('startup', start_worker)")


def test_start_all_missing_key_fails_before_port_cleanup(config_root):
    shutil.copy(ROOT / "start_all.sh", config_root / "start_all.sh")
    package = config_root / "agent_quota"
    package.mkdir()
    (package / "__init__.py").write_text("")
    shutil.copy(ROOT / "agent_quota/signing_config.py", package / "signing_config.py")
    # Isolate the preceding, unrelated normalizer preflight.
    (config_root / "matchmaker_agent/__init__.py").write_text("")
    (config_root / "matchmaker_agent/concept_identity.py").write_text("def check_fresh_preference_normalizer(): pass\n")
    for service in ("social", "matchmaker"):
        bin_dir = config_root / ".local-venv" / service / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "python").symlink_to(sys.executable)
    environment = {k: v for k, v in os.environ.items() if k not in {"APPWRITE_API_KEY", "AYUE_SKIP_DOTENV", "DOTENV_DISABLED", "PYTHONPATH"}}
    result = subprocess.run(["bash", "start_all.sh", "ollama"], cwd=config_root, env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert "Internal quota signing configuration unavailable" in result.stderr
    assert "Checking and cleaning up network ports" not in result.stdout
    assert not (config_root / ".runtime-logs").exists()
    assert SYNTHETIC not in result.stdout + result.stderr
