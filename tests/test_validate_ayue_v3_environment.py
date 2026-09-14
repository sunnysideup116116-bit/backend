import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "validate_ayue_v3_environment.py"


def load_validator():
    spec = importlib.util.spec_from_file_location("validate_ayue_v3_environment", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class AyueEnvironmentValidatorTests(unittest.TestCase):
    def setUp(self):
        self.validator = load_validator()

    def _write_env(self, directory: Path, name: str, values: dict[str, str]) -> Path:
        path = directory / name
        path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
        return path

    def _valid_social(self) -> dict[str, str]:
        return {
            "MONGO_URI": "mongodb://mongo.invalid:27017",
            "MONGO_DB_NAME": "profiling_db",
            "RISK_SERVICE_URL": "http://127.0.0.1:8001",
            "RISK_TIMEOUT_SEC": "20",
            "OLLAMA_HOST": "https://llm.invalid",
            "OLLAMA_API_KEY": "social-secret",
            "OLLAMA_CHAT_MODEL": "model-a",
            "GOOGLE_AI_STUDIO_API_KEY": "google-secret",
            "GOOGLE_EMBEDDING_MODEL": "embedding-a",
        }

    def test_risk_timeout_below_twenty_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            social_values = self._valid_social()
            social_values["RISK_TIMEOUT_SEC"] = "3"
            social = self._write_env(directory, "social.env", social_values)
            matchmaker = self._write_env(directory, "match.env", self._valid_matchmaker())
            result = self.validator.validate_environment(social, matchmaker)
        self.assertFalse(result.ok)
        self.assertIn("RISK_TIMEOUT_SEC", result.missing_social)

    def _valid_matchmaker(self) -> dict[str, str]:
        return {
            "LLM_API_KEY": "match-secret",
            "LLM_BASE_URL": "https://match-llm.invalid/v1",
            "LLM_MODEL_ID": "model-b",
            "NEO4J_URI": "neo4j://neo4j.invalid:7687",
            "NEO4J_USERNAME": "neo4j",
            "NEO4J_PASSWORD": "graph-secret",
            "NEO4J_DATABASE": "neo4j",
        }

    def test_missing_keys_are_reported_by_name_only(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            social = self._write_env(directory, "social.env", {"MONGO_URI": "secret-uri"})
            matchmaker = self._write_env(directory, "match.env", {})
            result = self.validator.validate_environment(social, matchmaker)
        self.assertFalse(result.ok)
        self.assertIn("OLLAMA_CHAT_MODEL", result.missing_social)
        self.assertIn("LLM_API_KEY", result.missing_matchmaker)
        rendered = result.render()
        self.assertNotIn("secret-uri", rendered)

    def test_valid_files_pass_without_printing_secret_values(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            social_values = self._valid_social()
            match_values = self._valid_matchmaker()
            social = self._write_env(directory, "social.env", social_values)
            matchmaker = self._write_env(directory, "match.env", match_values)
            result = self.validator.validate_environment(social, matchmaker)
        self.assertTrue(result.ok, result.render())
        rendered = result.render()
        for secret in ("social-secret", "google-secret", "match-secret", "graph-secret"):
            self.assertNotIn(secret, rendered)

    def test_mongo_and_neo4j_connectors_are_closed(self):
        social = self._valid_social()
        matchmaker = self._valid_matchmaker()
        mongo_client = MagicMock()
        mongo_factory = MagicMock(return_value=mongo_client)
        neo4j_driver = MagicMock()
        neo4j_factory = MagicMock(return_value=neo4j_driver)
        checks = self.validator.check_datastores(
            social,
            matchmaker,
            mongo_factory=mongo_factory,
            neo4j_factory=neo4j_factory,
        )
        self.assertTrue(all(check.ok for check in checks), checks)
        mongo_client.admin.command.assert_called_once_with("ping")
        mongo_client.close.assert_called_once_with()
        neo4j_driver.verify_connectivity.assert_called_once_with()
        neo4j_driver.close.assert_called_once_with()

    def test_http_checks_never_send_a_chat_prompt(self):
        response = MagicMock()
        response.status = 200
        response.__enter__.return_value = response
        opener = MagicMock(return_value=response)
        checks = self.validator.check_http_services(
            self._valid_social(),
            self._valid_matchmaker(),
            risk_url="http://127.0.0.1:8001/health",
            opener=opener,
        )
        self.assertTrue(all(check.ok for check in checks), checks)
        requested_urls = [call.args[0].full_url for call in opener.call_args_list]
        self.assertIn("https://llm.invalid/api/tags", requested_urls)
        self.assertIn("https://match-llm.invalid/v1/models", requested_urls)
        self.assertIn("http://127.0.0.1:8001/health", requested_urls)
        for call in opener.call_args_list:
            request = call.args[0]
            self.assertIsNone(request.data)

    def test_network_failures_are_bounded_and_redacted(self):
        errors = [
            TimeoutError("secret timeout detail"),
            HTTPError("https://secret.invalid", 401, "private auth", {}, io.BytesIO()),
            URLError("[Errno -2] secret host"),
        ]
        categories = ["timeout", "authentication", "dns"]
        for error, expected in zip(errors, categories):
            opener = MagicMock(side_effect=error)
            checks = self.validator.check_http_services(
                self._valid_social(),
                self._valid_matchmaker(),
                risk_url="http://127.0.0.1:8001/health",
                opener=opener,
            )
            self.assertEqual(checks[0].category, expected)
            self.assertNotIn("secret", checks[0].render().lower())


if __name__ == "__main__":
    unittest.main()
