"""Only isolated script copies and fake preflight; no real service operations."""
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


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
    fake_python = tmp_path / ".local-venv/social/bin/python"
    fake_python.parent.mkdir(parents=True)
    fake_python.write_text('#!/bin/sh\nprintf "preflight=%s\\n" "$AYUE_LLM_PROVIDER"\nexit 0\n')
    fake_python.chmod(0o755)
    result = subprocess.run(["bash", str(script), *args], capture_output=True, text=True,
                            env={"PATH": os.environ["PATH"], "AYUE_LLM_PROVIDER": env_provider}, timeout=3)
    assert result.returncode == 0
    assert f"selected={expected}" in result.stdout
    assert ("preflight=gpt" in result.stdout) == (expected == "gpt")
