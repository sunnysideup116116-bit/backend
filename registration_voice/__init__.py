"""Server-root registration voice assistant.

All Google Live API access for registration stays inside this package.  The
Flutter client only talks to the Social API and never receives a Google key.
"""

from .router import router

__all__ = ["router"]
