"""Exercise env provisioning with disposable fixtures, never real credentials."""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from dotenv import dotenv_values


SERVER_ROOT = Path(__file__).resolve().parents[1]


class ProvisionAyueEnvironmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ayue-env-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "Server fixture"
        for folder in ("scripts", "social", "matchmaker_agent"):
            (self.root / folder).mkdir(parents=True)
        self.script = self.root / "scripts" / "provision_ayue_v3_env.sh"
        shutil.copyfile(SERVER_ROOT / "scripts" / self.script.name, self.script)
        for folder in ("social", "matchmaker_agent"):
            shutil.copyfile(
                SERVER_ROOT / folder / ".env.example",
                self.root / folder / ".env.example",
            )
        self.social = self.root / "social" / ".env"
        self.matchmaker = self.root / "matchmaker_agent" / ".env"

    def _write(self, name, content):
        path = self.root / name
        path.write_text(content, encoding="utf-8")
        return path

    def _run(self, **sources):
        environment = {
            key: value for key, value in os.environ.items()
            if key not in {
                "AYUE_SOCIAL_ENV_SOURCE", "AYUE_MATCHMAKER_ENV_SOURCE",
                "AYUE_SERVER_ENV_SOURCE",
            }
        }
        environment.update({key: str(value) for key, value in sources.items()})
        return subprocess.run(
            ["bash", str(self.script)], cwd=self.root, env=environment,
            capture_output=True, text=True, timeout=10,
        )

    def test_social_event_and_memory_settings_are_not_dropped(self):
        source = self._write("social attachment.env", (
            "EVENT_WEEKLY_CYCLE_ENABLED=on\n"
            "EVENT_DISCOVERY_REGION=高雄\n"
            "EVENT_DISCOVERY_CATEGORIES=市集,音樂,運動,節慶,美食\n"
            "EVENT_DISCOVERY_TARGET_PER_CATEGORY=5\n"
            "AYUE_CONVERSATION_COMPACTION_MODE=shadow\n"
            "AYUE_CONVERSATION_CONTEXT_MODE=on\n"
            "AYUE_CONVERSATION_CONTEXT_USER_ALLOWLIST=*\n"
            "AYUE_AGENT_V2_MODE=on\n"
            "NEO4J_PASSWORD=graph-fixture-secret\n"
        ))
        result = self._run(AYUE_SOCIAL_ENV_SOURCE=source)
        self.assertEqual(result.returncode, 0, result.stderr)
        values = dotenv_values(self.social)
        self.assertEqual(values["EVENT_WEEKLY_CYCLE_ENABLED"], "on")
        self.assertEqual(values["EVENT_DISCOVERY_TARGET_PER_CATEGORY"], "5")
        self.assertEqual(values["AYUE_CONVERSATION_COMPACTION_MODE"], "shadow")
        self.assertEqual(values["AYUE_CONVERSATION_CONTEXT_MODE"], "on")
        self.assertEqual(values["AYUE_CONVERSATION_CONTEXT_USER_ALLOWLIST"], "*")
        self.assertNotIn("AYUE_AGENT_V2_MODE", values)
        self.assertNotIn("NEO4J_PASSWORD", values)
        self.assertNotIn("graph-fixture-secret", result.stdout + result.stderr)

    def test_matchmaker_attachment_and_graph_thresholds_use_correct_service(self):
        social_source = self._write("social.env", (
            "EVENT_RELEVANCE_MIN_SIMILARITY=0.69\n"
            "EVENT_AVOIDANCE_MIN_SIMILARITY=0.75\n"
        ))
        matchmaker_source = self._write("env (1)", (
            "LLM_API_KEY=match-fixture-secret\n"
            "EVENT_EXTRACTION_MODEL_ID=event-fixture-model\n"
            "EVENT_RELEVANCE_MAX_PER_USER=2\n"
            "AURA_INSTANCEID=unused-provider-metadata\n"
        ))
        result = self._run(
            AYUE_SOCIAL_ENV_SOURCE=social_source,
            AYUE_MATCHMAKER_ENV_SOURCE=matchmaker_source,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        values = dotenv_values(self.matchmaker)
        self.assertEqual(values["LLM_API_KEY"], "match-fixture-secret")
        self.assertEqual(values["EVENT_EXTRACTION_MODEL_ID"], "event-fixture-model")
        self.assertEqual(values["EVENT_RELEVANCE_MAX_PER_USER"], "2")
        self.assertEqual(values["EVENT_RELEVANCE_MIN_SIMILARITY"], "0.69")
        self.assertEqual(values["EVENT_AVOIDANCE_MIN_SIMILARITY"], "0.75")
        self.assertNotIn("EVENT_RELEVANCE_MIN_SIMILARITY", dotenv_values(self.social))
        self.assertNotIn("AURA_INSTANCEID", values)
        self.assertNotIn("match-fixture-secret", result.stdout + result.stderr)

    def test_existing_values_and_extra_local_keys_survive_repeated_import(self):
        self._write("social/.env", (
            "EVENT_WEEKLY_CYCLE_ENABLED=on\n"
            "OLLAMA_CHAT_MODEL=current-model\n"
            "TAVILY_API_KEY=current-search-secret\n"
            "LOCAL_CUSTOM_SETTING=keep-me\n"
        ))
        self._write("matchmaker_agent/.env", "LLM_API_KEY=current-match-secret\n")
        source = self._write("incoming.env", (
            "EVENT_WEEKLY_CYCLE_ENABLED=off\n"
            "OLLAMA_CHAT_MODEL=old-model\n"
            "TAVILY_API_KEY=old-search-secret\n"
            "LLM_API_KEY=old-match-secret\n"
        ))
        for _ in range(2):
            result = self._run(AYUE_SOCIAL_ENV_SOURCE=source)
            self.assertEqual(result.returncode, 0, result.stderr)
        values = dotenv_values(self.social)
        self.assertEqual(values["EVENT_WEEKLY_CYCLE_ENABLED"], "on")
        self.assertEqual(values["OLLAMA_CHAT_MODEL"], "current-model")
        self.assertEqual(values["TAVILY_API_KEY"], "current-search-secret")
        self.assertEqual(values["LOCAL_CUSTOM_SETTING"], "keep-me")
        self.assertEqual(dotenv_values(self.matchmaker)["LLM_API_KEY"], "current-match-secret")
        self.assertEqual(self.social.read_text().count("EVENT_WEEKLY_CYCLE_ENABLED="), 1)

    def test_new_example_keys_are_imported_without_another_allowlist(self):
        example = self.root / "social" / ".env.example"
        example.write_text(example.read_text() + "\nEVENT_FUTURE_SETTING=default\n")
        source = self._write("incoming.env", "EVENT_FUTURE_SETTING=custom\n")
        result = self._run(AYUE_SOCIAL_ENV_SOURCE=source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(dotenv_values(self.social)["EVENT_FUTURE_SETTING"], "custom")

    def test_crlf_export_and_shell_like_values_are_data_not_commands(self):
        marker = self.root / "must-not-exist"
        source = self._write("windows.env", (
            "export EVENT_WEEKLY_CYCLE_ENABLED=on\r\n"
            f"TAVILY_API_KEY='$(touch \"{marker}\")'\r\n"
        ))
        result = self._run(AYUE_SOCIAL_ENV_SOURCE=source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(dotenv_values(self.social)["EVENT_WEEKLY_CYCLE_ENABLED"], "on")
        self.assertIn("$(touch", dotenv_values(self.social)["TAVILY_API_KEY"])
        self.assertNotIn(b"\r", self.social.read_bytes())
        self.assertFalse(marker.exists())

    def test_missing_explicit_source_fails_before_changing_either_target(self):
        self._write("social/.env", "EVENT_WEEKLY_CYCLE_ENABLED=on\n")
        self._write("matchmaker_agent/.env", "LLM_API_KEY=keep-secret\n")
        before = (self.social.read_bytes(), self.matchmaker.read_bytes())
        result = self._run(AYUE_MATCHMAKER_ENV_SOURCE=self.root / "missing.env")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.social.read_bytes(), self.matchmaker.read_bytes()), before)
        self.assertNotIn("keep-secret", result.stdout + result.stderr)

    def test_fresh_defaults_are_safe_and_output_permissions_are_private(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        values = dotenv_values(self.social)
        self.assertEqual(values["EVENT_WEEKLY_CYCLE_ENABLED"], "off")
        self.assertEqual(values["EVENT_OPPORTUNITY_AUTO_SCAN_ENABLED"], "off")
        for path in (self.social, self.matchmaker):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
