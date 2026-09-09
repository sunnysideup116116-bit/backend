"""Shared GPT dotenv policy for standalone preflight and Social imports."""
import os
from pathlib import Path

from dotenv import dotenv_values


LLM_OWNER_NAMES = frozenset({
    "planner", "calendar", "places", "match",
    "relationship", "profile", "web", "synthesizer",
})
FAST_MODEL_OWNERS = frozenset({
    "planner", "places", "match", "relationship", "profile",
})

GPT_ENV_KEYS = frozenset({
    "AYUE_CODEX_HOME", "AYUE_CODEX_BIN", "AYUE_GPT_MODEL",
    "AYUE_GPT_FAST_MODEL", "AYUE_GPT_SERVICE_TIER", "AYUE_GPT_TIMEOUT_SECONDS",
} | {f"AYUE_GPT_{owner.upper()}_MODEL" for owner in LLM_OWNER_NAMES})
SERVER_ENV = Path(__file__).resolve().parents[2] / ".env"


def load_gpt_settings(path=SERVER_ENV, *, environ=None):
    """Load only GPT keys; explicit environment (even empty) wins.

    No shell execution, interpolation, credential logging or provider selection.
    Call before Social's general dotenv loading to give Server/.env precedence.
    """
    env = os.environ if environ is None else environ
    if env.get("AYUE_SKIP_DOTENV", "").strip().lower() in {"1", "true", "on"}:
        return
    for key, value in dotenv_values(path, interpolate=False).items():
        if key in GPT_ENV_KEYS and value is not None:
            env.setdefault(key, value)
