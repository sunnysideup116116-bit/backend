import subprocess
import unittest
from pathlib import Path


SERVER_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SERVER_ROOT / "scripts"


class AyueLaunchScriptTests(unittest.TestCase):
    def test_social_launcher_uses_canonical_working_directory_and_venv(self):
        source = (SCRIPTS / "run_ayue_social.sh").read_text(encoding="utf-8")
        self.assertIn('social', source)
        self.assertIn('.local-venv/social/bin/python', source)
        self.assertIn('uvicorn', source)
        self.assertIn('main:app', source)
        self.assertIn('AYUE_SOCIAL_PORT', source)
        self.assertNotIn('kill ', source)
        self.assertNotIn('pkill', source)

    def test_matchmaker_launcher_uses_canonical_working_directory_and_venv(self):
        source = (SCRIPTS / "run_ayue_matchmaker.sh").read_text(encoding="utf-8")
        self.assertIn('matchmaker_agent', source)
        self.assertIn('.local-venv/matchmaker/bin/python', source)
        self.assertIn('agent_api:app', source)
        self.assertIn('AYUE_MATCHMAKER_PORT', source)
        self.assertNotIn('kill ', source)
        self.assertNotIn('pkill', source)

    def test_health_script_checks_all_four_services(self):
        source = (SCRIPTS / "check_ayue_services.sh").read_text(encoding="utf-8")
        self.assertIn('8000/api/health', source)
        self.assertIn('9001/health', source)
        self.assertIn('8001/health', source)
        self.assertIn('8081/v1/models', source)
        self.assertNotIn('/api/demo/', source)
        self.assertNotIn('/api/direct_chat', source)

    def test_shell_scripts_have_valid_syntax(self):
        for filename in (
            "run_ayue_social.sh",
            "run_ayue_matchmaker.sh",
            "run_ayue_risk.sh",
            "run_ayue_guardrail.sh",
            "start_ayue_services.sh",
            "check_ayue_services.sh",
        ):
            result = subprocess.run(
                ["bash", "-n", str(SCRIPTS / filename)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

        start_all_result = subprocess.run(
            ["bash", "-n", str(SERVER_ROOT / "start_all.sh")],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(start_all_result.returncode, 0, start_all_result.stderr)


if __name__ == "__main__":
    unittest.main()
