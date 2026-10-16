"""Only isolated script copies and fake preflight; no real service operations."""
import os
import pty
from pathlib import Path
import subprocess
import time

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


def _prefix_script(tmp_path):
    prefix = (ROOT / "start_all.sh").read_text().split('LOG_DIR="', 1)[0]
    script = tmp_path / "start_all.sh"
    script.write_text(prefix + '\nprintf "selected=%s\\n" "$AYUE_LLM_PROVIDER"\n')
    _fake_normalizer_runtimes(tmp_path)
    pi_package = tmp_path / "pi_agent/node_modules/@earendil-works/pi-agent-core/package.json"
    pi_package.parent.mkdir(parents=True)
    pi_package.write_text('{}')
    (tmp_path / "pi_agent/bridge.mjs").write_text('process.exit(0);')
    return script


def _run_tty_menu(tmp_path, *, feed=None, menu_timeout="15", env_extra=None, timeout=15):
    """Run the menu on a real pty so [[ -t 0 ]] is true for the interactive branch."""
    script = _prefix_script(tmp_path)
    env = {"PATH": os.environ["PATH"], "AYUE_MENU_TIMEOUT_SECONDS": menu_timeout}
    env.update(env_extra or {})
    master_fd, slave_fd = pty.openpty()
    process = subprocess.Popen(
        ["bash", str(script)], stdin=slave_fd, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=env)
    os.close(slave_fd)
    try:
        if feed is not None:
            time.sleep(0.4)
            os.write(master_fd, feed.encode())
        stdout, stderr = process.communicate(timeout=timeout)
    finally:
        os.close(master_fd)
    return stdout, stderr, process.returncode


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


@pytest.mark.parametrize("args,env_provider,expected", [
    ([], "", "ollama"), ([], "ollama", "ollama"),
    (["ollama"], "gpt", "ollama"), (["gpt"], "ollama", "gpt"),
    (["openrouter"], "ollama", "openrouter"), ([], "openrouter", "openrouter"),
])
def test_selection_and_successful_preflight_before_startup(tmp_path, args, env_provider, expected):
    script = _prefix_script(tmp_path)
    result = subprocess.run(
        ["bash", str(script), *args], capture_output=True, text=True, input="",
        env={"PATH": os.environ["PATH"], "AYUE_LLM_PROVIDER": env_provider}, timeout=5)
    assert result.returncode == 0
    assert f"selected={expected}" in result.stdout
    assert ("preflight=gpt" in result.stdout) == (expected == "gpt")
    assert ("preflight=openrouter" in result.stdout) == (expected == "openrouter")


def test_non_interactive_stdin_falls_back_to_ollama_instantly(tmp_path):
    """Closed stdin never enters the menu; the later r/q loop owns that stream."""
    script = _prefix_script(tmp_path)
    result = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, input="",
        env={"PATH": os.environ["PATH"]}, timeout=5)
    assert result.returncode == 0
    assert "selected=ollama" in result.stdout
    assert "Non-interactive stdin" in result.stderr


@pytest.mark.parametrize("feed,expected", [("2\n", "gpt"), ("3\n", "openrouter"), ("\n", "ollama")])
def test_menu_choice_routes_provider_before_preflight_on_tty(tmp_path, feed, expected):
    stdout, stderr, code = _run_tty_menu(tmp_path, feed=feed, menu_timeout="5")
    assert code == 0
    assert f"selected={expected}" in stdout
    assert ("preflight=gpt" in stdout) == (expected == "gpt")
    assert ("preflight=openrouter" in stdout) == (expected == "openrouter")


def test_menu_invalid_choice_falls_back_to_ollama_on_tty(tmp_path):
    stdout, stderr, code = _run_tty_menu(tmp_path, feed="9\n", menu_timeout="5")
    assert code == 0
    assert "selected=ollama" in stdout
    assert "無效選項" in stderr


def test_menu_idle_timeout_falls_back_to_ollama_on_tty(tmp_path):
    stdout, stderr, code = _run_tty_menu(tmp_path, feed=None, menu_timeout="1", timeout=20)
    assert code == 0
    assert "selected=ollama" in stdout
    assert "秒未選擇" in stderr
    assert "preflight=openrouter" not in stdout


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
