import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class StartAyueLauncherTests(unittest.TestCase):
    def test_launcher_uses_typed_health_endpoints_not_page_title(self):
        source = (ROOT / "start_ayue.ps1").read_text(encoding="utf-8")
        self.assertIn('$readinessUrl = "http://127.0.0.1:$Port/api/health"', source)
        self.assertIn('$agentHealthUrl = "http://127.0.0.1:$AgentPort/health"', source)
        self.assertIn('$payload.service -eq "ayue"', source)
        self.assertIn('$payload.service -eq "matchmaker"', source)
        self.assertNotIn('<title>AI .*DEMO</title>', source)

    def test_launcher_has_migration_identity_and_longer_cold_start_budget(self):
        source = (ROOT / "start_ayue.ps1").read_text(encoding="utf-8")
        self.assertIn('$openApiUrl = "http://127.0.0.1:$Port/openapi.json"', source)
        self.assertIn('$payload.paths."/api/direct_chat"', source)
        self.assertIn('[int]$StartupTimeoutSeconds = 90', source)
        self.assertIn('$maxHealthAttempts', source)

    def test_double_click_wrapper_keeps_failure_visible(self):
        source = (ROOT / "start_ayue.cmd").read_text(encoding="utf-8")
        self.assertIn('if "%~1"=="" pause', source)
        self.assertIn('exit /b %AYUE_EXIT_CODE%', source)

    def test_main_service_exposes_process_readiness_without_external_probes(self):
        source = (
            ROOT / "social_demotest" / "routers" / "system.py"
        ).read_text(encoding="utf-8")
        self.assertIn('@router.get("/health")', source)
        self.assertIn('return {"status": "ok", "service": "ayue"}', source)


if __name__ == "__main__":
    unittest.main()
