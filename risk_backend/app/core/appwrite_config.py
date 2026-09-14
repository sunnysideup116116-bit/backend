import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv


project_env = Path(__file__).resolve().parents[3] / ".env"
local_env = Path(__file__).resolve().parents[2] / ".env"
if project_env.exists():
    load_dotenv(dotenv_path=project_env)
if local_env.exists():
    load_dotenv(dotenv_path=local_env, override=True)
if not project_env.exists() and not local_env.exists():
    load_dotenv()

DEFAULT_APPWRITE_ENDPOINT = "https://appwrite.misproject.us.ci/v1"


def _normalize_appwrite_endpoint(value: str) -> str:
    """Avoid Appwrite's HTTP→HTTPS redirect, which breaks writes and queries.

    The local reverse proxy redirects ``http://127.0.0.1:80`` to the default
    HTTPS port. Appwrite POST requests then fail with
    ``general_protocol_unsupported`` and redirected GET requests lose their
    ``queries[]`` parameters. Connect to the canonical HTTPS origin directly.
    Other development endpoints (for example http://localhost:8080) are left
    unchanged.
    """
    endpoint = value.strip()
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme.lower() == "http"
        and parsed.hostname in {"127.0.0.1", "localhost"}
        and parsed.port == 80
    ):
        return urlunsplit(
            ("https", parsed.hostname, parsed.path, parsed.query, parsed.fragment)
        )
    return endpoint


@dataclass(frozen=True)
class AppwriteConfig:
    endpoint: str
    project_id: str
    api_key: str
    db_id: str
    kb_db_id: str


def get_appwrite_config() -> AppwriteConfig:
    return AppwriteConfig(
        endpoint=_normalize_appwrite_endpoint(
            os.getenv("APPWRITE_ENDPOINT") or DEFAULT_APPWRITE_ENDPOINT
        ),
        project_id=(os.getenv("APPWRITE_PROJECT_ID") or os.getenv("APPWRITE_PROJECT") or "").strip(),
        api_key=(os.getenv("APPWRITE_API_KEY") or "").strip(),
        db_id=(os.getenv("APPWRITE_DB_ID") or os.getenv("APPWRITE_DATABASE_ID") or "").strip(),
        kb_db_id=(os.getenv("APPWRITE_KB_DB_ID") or "kb").strip(),
    )


def configure_appwrite_client(client):
    config = get_appwrite_config()
    client.set_endpoint(config.endpoint)
    if "127.0.0.1" in config.endpoint or "localhost" in config.endpoint:
        client.set_self_signed(status=True)
    if config.project_id:
        client.set_project(config.project_id)
    if config.api_key:
        client.set_key(config.api_key)
    return config
