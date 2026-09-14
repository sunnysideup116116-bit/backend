from pathlib import Path

from scripts.validate_ayue_v3_environment import validate_environment


BASE_SOCIAL = """
MONGO_URI=mongodb://example
MONGO_DB_NAME=example
RISK_SERVICE_URL=http://127.0.0.1:8001
RISK_TIMEOUT_SEC=40
OLLAMA_HOST=http://127.0.0.1:11434
OLLAMA_CHAT_MODEL=model
GOOGLE_AI_STUDIO_API_KEY=test
GOOGLE_EMBEDDING_MODEL=models/test
"""

BASE_MATCHMAKER = """
LLM_API_KEY=test
LLM_BASE_URL=http://127.0.0.1:1
LLM_MODEL_ID=test
NEO4J_URI=bolt://127.0.0.1:7687
NEO4J_USERNAME=test
NEO4J_PASSWORD=test
NEO4J_DATABASE=test
"""


def _write(tmp_path: Path, name: str, value: str) -> Path:
    path = tmp_path / name
    path.write_text(value)
    return path


def test_google_calendar_configuration_is_optional_when_disabled(tmp_path):
    result = validate_environment(
        _write(tmp_path, "social.env", BASE_SOCIAL + "AYUE_GOOGLE_CALENDAR_ENABLED=off\n"),
        _write(tmp_path, "matchmaker.env", BASE_MATCHMAKER),
    )
    assert result.ok


def test_google_calendar_enabled_requires_credentials_key_and_canonical_callback(tmp_path):
    result = validate_environment(
        _write(tmp_path, "social.env", BASE_SOCIAL + "AYUE_GOOGLE_CALENDAR_ENABLED=on\n"),
        _write(tmp_path, "matchmaker.env", BASE_MATCHMAKER),
    )
    assert set(result.missing_social) >= {
        "GOOGLE_CALENDAR_CLIENT_ID",
        "GOOGLE_CALENDAR_CLIENT_SECRET",
        "GOOGLE_CALENDAR_TOKEN_KEY",
        "GOOGLE_CALENDAR_REDIRECT_URI",
    }
