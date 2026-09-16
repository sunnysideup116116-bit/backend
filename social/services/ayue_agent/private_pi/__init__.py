"""Private Ayue Pi surface.

The package owns the accepted-pair surface policy and context projection while
reusing the server's canonical relationship and date services.  It deliberately
does not import the Public Ayue orchestrator or Public tool registry.
"""

from .runtime import run_private_pi_turn

__all__ = ["run_private_pi_turn"]
