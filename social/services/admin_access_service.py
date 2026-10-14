"""HTTP gates for opt-in maintenance and local-only private diagnostics.

Three independent authorities:

* ``require_event_ops_admin`` -- Event discovery / scan / maintenance. Its own
  ``EVENT_OPS_ADMIN_TOKEN`` (header ``x-ayue-event-ops-token``). It must not
  depend on the destructive demo flag and must not authenticate any
  demo/reset/seed route.
* ``require_demo_admin`` -- destructive demo maintenance (flag + separate
  ``AYUE_DEMO_ADMIN_TOKEN``). A feature flag alone never grants permission to
  erase data.
* ``require_local_operator`` -- destructive maintenance is additionally
  restricted to a true local operator connection; the public tunnel terminates
  on loopback, so both the client address and the Host header must be local.
"""

import hashlib
import hmac
import ipaddress
import os

from fastapi import HTTPException, Request

import config
from services.ayue_agent.shared.debug_trace import local_debug_enabled


def _constant_time_token_match(expected: str, supplied: str) -> bool:
    """Digest comparison: no length-dependent branch, no secret in any message."""
    expected = (expected or "").strip()
    if len(expected) < 32 or not supplied:
        return False
    return hmac.compare_digest(
        hashlib.sha256(expected.encode("utf-8")).digest(),
        hashlib.sha256(supplied.encode("utf-8")).digest(),
    )


def _local_origin(request: Request) -> bool:
    """True only for a loopback client that did not arrive through the tunnel."""
    if request.client is None:
        return False
    try:
        client_local = ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        client_local = request.client.host == "localhost"
    host = (request.url.hostname or "").lower()
    try:
        host_local = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        host_local = host == "localhost"
    return bool(client_local and host_local)


def require_event_ops_admin(request: Request) -> None:
    """Event operations gate: independent token, no destructive-tools dependency."""
    expected = os.environ.get("EVENT_OPS_ADMIN_TOKEN", "")
    supplied = request.headers.get("x-ayue-event-ops-token", "")
    if not _constant_time_token_match(expected, supplied):
        raise HTTPException(status_code=403, detail={"code": "event_ops_admin_required"})


def require_demo_admin(request: Request) -> None:
    """A demo feature flag alone never grants permission to erase data."""
    expected = os.environ.get("AYUE_DEMO_ADMIN_TOKEN", "")
    supplied = request.headers.get("x-ayue-admin-token", "")
    if not config.DEMO_DESTRUCTIVE_TOOLS_ENABLED or not _constant_time_token_match(expected, supplied):
        raise HTTPException(status_code=403, detail={"code": "demo_admin_required"})


def require_local_operator(request: Request) -> None:
    """Destructive maintenance must never be reachable through the public tunnel.

    The tunnel terminates on loopback, so a client-IP check alone is not enough:
    a public Host header is rejected even when a valid admin token is present.
    """
    if not _local_origin(request):
        raise HTTPException(status_code=403, detail={"code": "local_operator_required"})


def require_local_debug(request: Request) -> None:
    """Do not expose private snapshots through a public reverse proxy."""
    if not local_debug_enabled() or not _local_origin(request):
        raise HTTPException(status_code=404, detail="Debug tools unavailable")
