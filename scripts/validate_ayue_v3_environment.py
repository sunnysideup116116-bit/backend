#!/usr/bin/env python3
"""Validate Ayue V3 configuration without importing or mutating either service."""

from __future__ import annotations

import argparse
import socket
from pathlib import Path
from typing import Callable, NamedTuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


SOCIAL_REQUIRED = (
    "MONGO_URI",
    "MONGO_DB_NAME",
    "RISK_SERVICE_URL",
    "RISK_TIMEOUT_SEC",
    "OLLAMA_HOST",
    "OLLAMA_CHAT_MODEL",
    "GOOGLE_AI_STUDIO_API_KEY",
    "GOOGLE_EMBEDDING_MODEL",
)
MATCHMAKER_REQUIRED = (
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL_ID",
    "NEO4J_URI",
    "NEO4J_USERNAME",
    "NEO4J_PASSWORD",
    "NEO4J_DATABASE",
)


class ValidationResult(NamedTuple):
    missing_social: tuple[str, ...]
    missing_matchmaker: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.missing_social and not self.missing_matchmaker

    def render(self) -> str:
        if self.ok:
            return "environment: ok"
        parts: list[str] = []
        if self.missing_social:
            parts.append("social missing: " + ", ".join(self.missing_social))
        if self.missing_matchmaker:
            parts.append("matchmaker missing: " + ", ".join(self.missing_matchmaker))
        return "; ".join(parts)


class ServiceCheck(NamedTuple):
    service: str
    ok: bool
    category: str

    def render(self) -> str:
        state = "ok" if self.ok else "failed"
        return f"{self.service}: {state} ({self.category})"


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key.replace("_", "").isalnum() or not key[:1].isalpha():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def validate_environment(social_path: Path, matchmaker_path: Path) -> ValidationResult:
    social = parse_env(social_path)
    matchmaker = parse_env(matchmaker_path)
    missing_social = tuple(key for key in SOCIAL_REQUIRED if not social.get(key, "").strip())
    try:
        risk_timeout = float(social.get("RISK_TIMEOUT_SEC", ""))
    except ValueError:
        risk_timeout = 0.0
    if risk_timeout < 20 and "RISK_TIMEOUT_SEC" not in missing_social:
        missing_social += ("RISK_TIMEOUT_SEC",)
    remote_ollama = not _is_local_url(social.get("OLLAMA_HOST", ""))
    if remote_ollama and not social.get("OLLAMA_API_KEY", "").strip():
        missing_social += ("OLLAMA_API_KEY",)
    missing_matchmaker = tuple(
        key for key in MATCHMAKER_REQUIRED if not matchmaker.get(key, "").strip()
    )
    return ValidationResult(missing_social, missing_matchmaker)


def _is_local_url(value: str) -> bool:
    hostname = (urlsplit(value).hostname or "").lower()
    return hostname in {"localhost", "127.0.0.1", "::1"}


def _failure_category(error: BaseException) -> str:
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(error, HTTPError) and error.code in {401, 403}:
        return "authentication"
    if isinstance(error, URLError):
        reason = str(error.reason).lower()
        if "name or service" in reason or "errno -2" in reason or "nodename" in reason:
            return "dns"
        if "timed out" in reason:
            return "timeout"
    return "unavailable"


def check_datastores(
    social: dict[str, str],
    matchmaker: dict[str, str],
    *,
    mongo_factory: Callable | None = None,
    neo4j_factory: Callable | None = None,
) -> list[ServiceCheck]:
    if mongo_factory is None:
        from pymongo import MongoClient

        mongo_factory = MongoClient
    if neo4j_factory is None:
        from neo4j import GraphDatabase

        neo4j_factory = GraphDatabase.driver

    checks: list[ServiceCheck] = []
    mongo_client = None
    try:
        mongo_client = mongo_factory(
            social["MONGO_URI"],
            serverSelectionTimeoutMS=3000,
            connectTimeoutMS=3000,
        )
        mongo_client.admin.command("ping")
        checks.append(ServiceCheck("mongodb", True, "reachable"))
    except Exception as error:  # provider exception types vary by installed driver
        checks.append(ServiceCheck("mongodb", False, _failure_category(error)))
    finally:
        if mongo_client is not None:
            mongo_client.close()

    neo4j_driver = None
    try:
        neo4j_driver = neo4j_factory(
            matchmaker["NEO4J_URI"],
            auth=(matchmaker["NEO4J_USERNAME"], matchmaker["NEO4J_PASSWORD"]),
            connection_timeout=3,
        )
        neo4j_driver.verify_connectivity()
        checks.append(ServiceCheck("neo4j", True, "reachable"))
    except Exception as error:
        checks.append(ServiceCheck("neo4j", False, _failure_category(error)))
    finally:
        if neo4j_driver is not None:
            neo4j_driver.close()
    return checks


def _joined_url(base_url: str, suffix: str) -> str:
    parsed = urlsplit(base_url)
    base_path = parsed.path.rstrip("/")
    if base_path.endswith(suffix):
        path = base_path
    else:
        path = base_path + suffix
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def check_http_services(
    social: dict[str, str],
    matchmaker: dict[str, str],
    *,
    risk_url: str,
    opener: Callable = urlopen,
) -> list[ServiceCheck]:
    targets = (
        (
            "ollama",
            _joined_url(social["OLLAMA_HOST"], "/api/tags"),
            {"Authorization": f"Bearer {social.get('OLLAMA_API_KEY', '')}"},
        ),
        (
            "matchmaker-llm",
            _joined_url(matchmaker["LLM_BASE_URL"], "/models"),
            {"Authorization": f"Bearer {matchmaker.get('LLM_API_KEY', '')}"},
        ),
        ("risk-backend", risk_url, {}),
    )
    checks: list[ServiceCheck] = []
    for service, target, headers in targets:
        request = Request(target, method="GET", headers=headers)
        try:
            with opener(request, timeout=3) as response:
                status = int(getattr(response, "status", 200))
            if 200 <= status < 500:
                checks.append(ServiceCheck(service, True, "reachable"))
            else:
                checks.append(ServiceCheck(service, False, "unavailable"))
        except Exception as error:
            checks.append(ServiceCheck(service, False, _failure_category(error)))
    return checks


def _build_parser() -> argparse.ArgumentParser:
    server_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--social-env",
        type=Path,
        default=server_root / "ayue_for_demo" / "social_demotest" / ".env",
    )
    parser.add_argument(
        "--matchmaker-env",
        type=Path,
        default=server_root / "ayue_for_demo" / "matchmaker_agent" / ".env",
    )
    parser.add_argument("--check-services", action="store_true")
    parser.add_argument("--risk-url", default="http://127.0.0.1:8001/health")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    result = validate_environment(args.social_env, args.matchmaker_env)
    print(result.render())
    if not result.ok:
        return 1
    if not args.check_services:
        return 0
    social = parse_env(args.social_env)
    matchmaker = parse_env(args.matchmaker_env)
    checks = check_datastores(social, matchmaker)
    checks.extend(check_http_services(social, matchmaker, risk_url=args.risk_url))
    for check in checks:
        print(check.render())
    return 0 if all(check.ok for check in checks) else 2


if __name__ == "__main__":
    raise SystemExit(main())
