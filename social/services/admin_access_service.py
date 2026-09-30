"""HTTP gates for opt-in maintenance and local-only private diagnostics."""

import hmac
import ipaddress
import os

from fastapi import HTTPException, Request

import config
from services.ayue_agent.shared.debug_trace import local_debug_enabled


def require_demo_admin(request: Request) -> None:
    """A demo feature flag alone never grants permission to erase data."""
    expected = os.environ.get("AYUE_DEMO_ADMIN_TOKEN", "").strip()
    supplied = request.headers.get("x-ayue-admin-token", "")
    if (not config.DEMO_DESTRUCTIVE_TOOLS_ENABLED or len(expected) < 32
            or not supplied or not hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))):
        raise HTTPException(status_code=403, detail={"code": "demo_admin_required"})


def require_local_debug(request: Request) -> None:
    """Do not expose private snapshots through a public reverse proxy."""
    if not local_debug_enabled() or request.client is None:
        raise HTTPException(status_code=404, detail="Debug tools unavailable")
    try:
        client_local = ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        client_local = request.client.host == "localhost"
    host = (request.url.hostname or "").lower()
    try:
        host_local = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        host_local = host == "localhost"
    if not client_local or not host_local:
        raise HTTPException(status_code=404, detail="Debug tools unavailable")
