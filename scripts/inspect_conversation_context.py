"""Inspect local shadow compaction state without exposing it to the network."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request


def _request(url: str, method: str = "GET") -> dict:
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"status": "unavailable", "error": type(exc).__name__}


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect Public Ayue conversation compaction")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--user-id", default="demo_user")
    parser.add_argument("--metrics-only", action="store_true")
    parser.add_argument("--watch-seconds", type=float, default=0)
    parser.add_argument("--trigger-shadow", action="store_true")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    if args.trigger_shadow and not args.metrics_only:
        print(json.dumps(_request(f"{base}/api/debug/conversation-context/users/{args.user_id}/shadow", "POST"), ensure_ascii=False, indent=2))
    endpoint = f"{base}/api/debug/conversation-context/metrics" if args.metrics_only else f"{base}/api/debug/conversation-context/users/{args.user_id}"
    while True:
        print(json.dumps(_request(endpoint), ensure_ascii=False, indent=2))
        if args.watch_seconds <= 0:
            break
        time.sleep(args.watch_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
