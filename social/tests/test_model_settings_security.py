import unittest
from unittest.mock import patch

from fastapi import HTTPException

from models import ModelSettingsRequest
from routers.system import update_model_settings


class RuntimeModelSettingsSecurityTests(unittest.TestCase):
    def test_process_wide_override_is_disabled_without_admin_token(self):
        with patch.dict("os.environ", {}, clear=False):
            with patch("routers.system.os.getenv", return_value=""):
                with self.assertRaises(HTTPException) as raised:
                    update_model_settings(
                        ModelSettingsRequest(model="model-a", thinking_level="off"),
                        x_ayue_admin_token=None,
                    )
        self.assertEqual(raised.exception.status_code, 403)

    def test_model_must_be_allowlisted_even_with_admin_token(self):
        def fake_getenv(key, default=""):
            return {
                "AYUE_RUNTIME_MODEL_SETTINGS_TOKEN": "admin-token",
                "AYUE_ALLOWED_RUNTIME_MODELS": "model-a,model-b",
            }.get(key, default)

        with patch("routers.system.os.getenv", side_effect=fake_getenv):
            with self.assertRaises(HTTPException) as raised:
                update_model_settings(
                    ModelSettingsRequest(model="unbounded-model", thinking_level="high"),
                    x_ayue_admin_token="admin-token",
                )
        self.assertEqual(raised.exception.status_code, 400)

    def test_allowlisted_admin_override_succeeds(self):
        def fake_getenv(key, default=""):
            return {
                "AYUE_RUNTIME_MODEL_SETTINGS_TOKEN": "admin-token",
                "AYUE_ALLOWED_RUNTIME_MODELS": "model-a,model-b",
            }.get(key, default)

        with patch("routers.system.os.getenv", side_effect=fake_getenv), \
             patch("services.ai_service.set_runtime_model_override") as setter, \
             patch("services.ai_service.get_runtime_model_override", return_value={"model": "model-a"}):
            result = update_model_settings(
                ModelSettingsRequest(model="model-a", thinking_level="low"),
                x_ayue_admin_token="admin-token",
            )
        setter.assert_called_once_with("model-a", "low")
        self.assertEqual(result["status"], "success")


if __name__ == "__main__":
    unittest.main()
