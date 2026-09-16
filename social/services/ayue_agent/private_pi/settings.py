"""Private Pi runtime requirements, isolated from Public Ayue settings."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


PI_ROOT = Path(__file__).resolve().parents[4] / "pi_agent"


def pi_available() -> bool:
    """Return whether the pinned local Pi bridge can serve Private turns."""
    enabled = os.getenv(
        "AYUE_PRIVATE_PI_RUNTIME_AVAILABLE",
        os.getenv("AYUE_PI_RUNTIME_AVAILABLE", "1"),
    )
    return bool(
        str(enabled).strip().lower() not in {"0", "false", "off"}
        and shutil.which("node")
        and (PI_ROOT / "bridge.mjs").is_file()
        and (PI_ROOT / "node_modules/@earendil-works/pi-agent-core/package.json").is_file()
    )
