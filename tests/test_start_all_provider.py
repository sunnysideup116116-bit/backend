"""Only isolated script copies and fake preflight; no real service operations."""
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _fake_normalizer_runtimes(root, *, failure=None):
    for service in ("social", "matchmaker"):
        fake_python = root / f".local-venv/{service}/bin/python"
        fake_python.parent.mkdir(parents=True)
        status = 1 if service == failure else 0
        fake_python.write_text(
            '#!/bin/sh\nif [ "$1" = "-c" ]; then exit ' + str(status) + '; fi\n'
            'printf "preflight=%s\\n" "$AYUE_LLM_PROVIDER"\nexit 0\n'
        )
        fake_python.chmod(0o755)


@pytest.mark.parametrize("arguments,code", [(["--help"], 0), (["invalid"], 2), (["gpt", "extra"], 2), (["gpt"], 1)])
def test_early_exits_never_touch_ports_or_logs(tmp_path, arguments, code):
    script = tmp_path / "start_all.sh"
    script.write_bytes((ROOT / "start_all.sh").read_bytes())
    # GPT fails because the fake root has no Social python. All early branches
    # must exit before mkdir, lsof, fuser, curl or launch scripts.
    result = subprocess.run(["bash", str(script), *arguments], capture_output=True, text=True,
                            env={"PATH": os.environ["PATH"], "AYUE_LLM_PROVIDER": "ollama"}, timeout=3)
    assert result.returncode == code
    assert not (tmp_path / ".runtime-logs").exists()


@pytest.mark.parametrize("args,env_provider,expected", [([], "", "ollama"), (["ollama"], "gpt", "ollama"), (["gpt"], "ollama", "gpt")])
def test_selection_and_successful_preflight_before_startup(tmp_path, args, env_provider, expected):
    prefix = (ROOT / "start_all.sh").read_text().split('LOG_DIR="', 1)[0]
    script = tmp_path / "start_all.sh"
    script.write_text(prefix + '\nprintf "selected=%s\\n" "$AYUE_LLM_PROVIDER"\n')
    _fake_normalizer_runtimes(tmp_path)
    pi_package = tmp_path / "pi_agent/node_modules/@earendil-works/pi-agent-core/package.json"
    pi_package.parent.mkdir(parents=True)
    pi_package.write_text('{}')
    (tmp_path / "pi_agent/bridge.mjs").write_text('process.exit(0);')
    result = subprocess.run(["bash", str(script), *args], capture_output=True, text=True,
                            env={"PATH": os.environ["PATH"], "AYUE_LLM_PROVIDER": env_provider}, timeout=3)
    assert result.returncode == 0
    assert f"selected={expected}" in result.stdout
    assert ("preflight=gpt" in result.stdout) == (expected == "gpt")


def test_missing_pi_dependencies_fail_before_logs_or_ports(tmp_path):
    script = tmp_path / "start_all.sh"
    script.write_bytes((ROOT / "start_all.sh").read_bytes())
    _fake_normalizer_runtimes(tmp_path)
    result = subprocess.run(
        ["bash", str(script), "ollama"], capture_output=True, text=True,
        env={"PATH": os.environ["PATH"]}, timeout=3,
    )
    assert result.returncode == 1
    assert "Pi dependencies are missing" in result.stderr
    assert not (tmp_path / ".runtime-logs").exists()


@pytest.mark.parametrize("service", ["social", "matchmaker"])
def test_pinned_normalizer_failure_precedes_logs_ports_and_service_start(tmp_path, service):
    script = tmp_path / "start_all.sh"
    script.write_bytes((ROOT / "start_all.sh").read_bytes())
    _fake_normalizer_runtimes(tmp_path, failure=service)
    result = subprocess.run(
        ["bash", str(script), "ollama"], capture_output=True, text=True,
        env={"PATH": os.environ["PATH"]}, timeout=3,
    )
    assert result.returncode == 1
    assert f"Pinned preference normalizer unavailable for {service}" in result.stderr
    assert "Starting" not in result.stdout
    assert not (tmp_path / ".runtime-logs").exists()
