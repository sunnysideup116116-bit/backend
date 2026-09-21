#!/usr/bin/env python3
"""Read-only P1-A readiness audit; never creates indexes or mutates Graph data."""

from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "social"))

from services.preference_semantic_service import preference_semantic_readiness


def main() -> int:
    result = preference_semantic_readiness()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
