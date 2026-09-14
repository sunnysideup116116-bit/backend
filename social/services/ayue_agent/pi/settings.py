"""Runtime requirements for the production Pi public agent."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


PI_ROOT = Path(__file__).resolve().parents[4] / "pi_agent"


def pi_available() -> bool:
    """Return whether the pinned local Pi bridge can serve requests."""
    return bool(
        os.environ.get("AYUE_PI_RUNTIME_AVAILABLE") != "0"
        and shutil.which("node")
        and (PI_ROOT / "bridge.mjs").is_file()
        and (PI_ROOT / "node_modules/@earendil-works/pi-agent-core/package.json").is_file()
    )


class PiRuntimeUnavailable(RuntimeError):
    status_code = 503

    def __init__(self) -> None:
        super().__init__("Pi 暫時無法使用，請稍後再試；這次沒有執行任何操作。")
