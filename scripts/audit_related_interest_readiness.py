#!/usr/bin/env python3
"""Operator-only read-only API audit; never enables a feature or writes vectors."""
import json
import requests


def main():
    try:
        response = requests.get("http://127.0.0.1:9001/api/preferences/related-interest-readiness", timeout=(1, 10))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError()
        # Only metadata/counts from this narrow internal endpoint, not arbitrary payloads.
        output = {key: payload.get(key) for key in ("status", "index", "counts", "embedding_fingerprint",
            "provider_revision", "historical_embedding_fingerprint", "runtime_enabled")}
        output["activation_approved"] = False
        print(json.dumps(output, ensure_ascii=False))
        return 0 if payload.get("status") == "success" else 1
    except Exception:
        print(json.dumps({"status": "unavailable", "activation_approved": False})); return 1


if __name__ == "__main__":
    raise SystemExit(main())
