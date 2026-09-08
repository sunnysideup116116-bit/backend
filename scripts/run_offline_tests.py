#!/usr/bin/env python3
"""Run one isolated service test suite without developer secrets or network I/O."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import socket
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=("social", "risk", "matchmaker", "contracts"))
    args, pytest_args = parser.parse_known_args()
    root = Path(__file__).resolve().parents[1]
    os.environ.update({
        "AYUE_SKIP_DOTENV": "1", "DOTENV_DISABLED": "1",
        "MONGO_URI": "mongodb://127.0.0.1:27017/?serverSelectionTimeoutMS=50&connectTimeoutMS=50",
        "APPWRITE_PROJECT_ID": "", "APPWRITE_API_KEY": "",
        "LLM_API_KEY": "offline-test", "LLM_BASE_URL": "http://provider.invalid/v1",
        "LLM_MODEL_ID": "offline-test", "OLLAMA_API_KEY": "offline-test",
        "OPENAI_API_KEY": "offline-test", "GOOGLE_API_KEY": "",
        "GOOGLE_PLACES_SERVER_API_KEY": "", "TAVILY_API_KEY": "",
        "AYUE_RUN_PERSONA_LIVE": "0", "AYUE_RUN_PRIVATE_SCOPE_LIVE": "0",
    })
    import dotenv
    dotenv.load_dotenv = lambda *_args, **_kwargs: False

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_resolve = socket.getaddrinfo

    def offline_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise OSError("Network connections are disabled in offline tests")
        return original_connect(sock, address)

    def offline_connect_ex(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise OSError("Network connections are disabled in offline tests")
        return original_connect_ex(sock, address)

    def offline_resolve(host, *positional, **keywords):
        if host not in (None, "localhost", "127.0.0.1", "::1", b"localhost", b"127.0.0.1"):
            raise OSError("DNS is disabled in offline tests")
        return original_resolve(host, *positional, **keywords)

    socket.socket.connect = offline_connect
    socket.socket.connect_ex = offline_connect_ex
    socket.getaddrinfo = offline_resolve
    folders = {"social": "social", "risk": "risk_backend", "matchmaker": "matchmaker_agent", "contracts": "."}
    working = root / folders[args.suite]
    os.chdir(working)
    sys.path.insert(0, str(working))
    sys.path.insert(0, str(root / "social") if args.suite == "contracts" else str(working))
    tests = {
        "social": ["tests", "--ignore=tests/test_google_maps_live_smoke.py", "--ignore=tests/test_private_scope_live.py", "--ignore=tests/test_ayue_persona_live.py"],
        "risk": ["tests", "--ignore=tests/test_audit_pipeline_timing.py"],
        "matchmaker": [str(path) for path in sorted(working.glob("test_*.py"))],
        "contracts": ["tests"],
    }
    import pytest
    return pytest.main(["-q", *tests[args.suite], *pytest_args])


if __name__ == "__main__":
    raise SystemExit(main())
