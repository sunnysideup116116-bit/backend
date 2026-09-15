import os
from pathlib import Path
import subprocess
import sys


SERVER_ROOT = Path(__file__).resolve().parents[2]
FLUTTER_CATALOG = (
    SERVER_ROOT.parent
    / "DatingApp"
    / "lib"
    / "services"
    / "app_voice"
    / "app_voice_capabilities.dart"
)


def test_catalog_check_does_not_boot_task_database():
    env = dict(os.environ)
    env["AYUE_SKIP_DOTENV"] = "1"
    env["VOICE_APP_TASKS_ENABLED"] = "on"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "app_voice_assistant.generate_catalog",
            "--check",
            "--output",
            str(FLUTTER_CATALOG),
        ],
        cwd=SERVER_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
