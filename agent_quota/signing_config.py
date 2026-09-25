"""One cold-start credential contract for both sides of internal accounting.

Only APPWRITE_API_KEY is imported; no database/provider configuration is loaded.
Call before service-specific dotenv loading. Injected process environment wins,
then the shared root .env, then the historical shared social/.env fallback.
"""
import os
from pathlib import Path

from dotenv import dotenv_values

SERVER_ROOT = Path(__file__).resolve().parents[1]
KEY_NAME = "APPWRITE_API_KEY"


class QuotaSigningConfigurationError(RuntimeError):
    """Static, secret-safe startup/configuration failure."""


def load_signing_config() -> None:
    """Resolve once into process memory, before service-specific env imports."""
    if KEY_NAME in os.environ:
        return  # An explicit empty override must fail closed, not fall back.
    if os.getenv("AYUE_SKIP_DOTENV", "").lower() in {"1", "true", "on"}:
        return
    if os.getenv("DOTENV_DISABLED", "").lower() in {"1", "true", "on"}:
        return
    try:
        for path in (SERVER_ROOT / ".env", SERVER_ROOT / "social" / ".env"):
            values = dotenv_values(path, interpolate=False)
            if KEY_NAME in values:
                os.environ[KEY_NAME] = values[KEY_NAME] or ""
                return
    except (OSError, UnicodeError):
        raise QuotaSigningConfigurationError("quota_signing_config_unreadable") from None


def quota_signing_key() -> bytes:
    load_signing_config()
    value = os.getenv(KEY_NAME, "")
    if not value.strip():
        raise QuotaSigningConfigurationError("quota_signing_key_unavailable")
    return value.encode("utf-8")


def validate_signing_config() -> None:
    """Startup-only local validation: no remote calls, values, or key hashes."""
    quota_signing_key()


if __name__ == "__main__":
    try:
        validate_signing_config()
    except QuotaSigningConfigurationError:
        raise SystemExit("Internal quota signing configuration unavailable; configure APPWRITE_API_KEY in the shared environment.") from None
