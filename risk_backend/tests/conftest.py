"""Offline unit tests never load production credentials or contact services."""

import socket

import dotenv
import pytest
import requests


# This executable profiler intentionally calls deployed dependencies. Run it
# explicitly as a script when a live integration run has been authorized.
collect_ignore = ["test_audit_pipeline_timing.py"]
_isolation = pytest.MonkeyPatch()


def _deny_network(*args, **kwargs):
    raise AssertionError("Risk unit tests must mock external I/O")


def pytest_configure(config):
    _isolation.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    for name in ("OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
                 "NLP_API_KEY", "GUARDRAIL_API_KEY", "SUMMARY_API_KEY",
                 "APPWRITE_API_KEY", "MONGO_URI"):
        _isolation.setenv(name, "")
    _isolation.setenv("APPWRITE_ENDPOINT", "http://test.invalid/v1")
    _isolation.setenv("APPWRITE_PROJECT_ID", "offline-test")
    _isolation.setenv("APPWRITE_DB_ID", "offline-test")
    _isolation.setattr(requests.sessions.Session, "request", _deny_network)
    _isolation.setattr(socket, "create_connection", _deny_network)
    original_connect = socket.socket.connect

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            _deny_network()
        return original_connect(sock, address)

    _isolation.setattr(socket.socket, "connect", connect)


def pytest_unconfigure(config):
    _isolation.undo()
