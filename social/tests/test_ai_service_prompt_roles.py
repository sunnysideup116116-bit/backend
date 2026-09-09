import unittest
from unittest.mock import patch

from services import ai_service


class AiServicePromptRoleTests(unittest.TestCase):
    def _response(self, *, tool_calls=None):
        return {
            "message": {"content": "ok", "tool_calls": tool_calls or []},
            "prompt_eval_count": 1,
            "eval_count": 1,
        }

    def test_chat_without_system_prompt_keeps_single_user_message(self):
        with patch.object(ai_service, "OLLAMA_API_KEY", "test"), \
             patch.object(ai_service.ollama_client, "chat", return_value=self._response()) as chat:
            ai_service.generate_chat_completion("user data")
        self.assertEqual(
            chat.call_args.kwargs["messages"],
            [{"role": "user", "content": "user data"}],
        )

    def test_chat_with_system_prompt_sends_separate_roles(self):
        with patch.object(ai_service, "OLLAMA_API_KEY", "test"), \
             patch.object(ai_service.ollama_client, "chat", return_value=self._response()) as chat:
            ai_service.generate_chat_completion(
                "user data", system_prompt="hard policy",
            )
        self.assertEqual(chat.call_args.kwargs["messages"], [
            {"role": "system", "content": "hard policy"},
            {"role": "user", "content": "user data"},
        ])

    def test_tools_with_system_prompt_keeps_tools_and_roles(self):
        tools = [{"type": "function", "function": {"name": "demo"}}]
        with patch.object(ai_service, "OLLAMA_API_KEY", "test"), \
             patch.object(ai_service.ollama_client, "chat", return_value=self._response()) as chat:
            ai_service.generate_chat_completion_with_tools(
                "user data", tools, system_prompt="tool policy",
            )
        self.assertEqual(chat.call_args.kwargs["tools"], tools)
        self.assertEqual(chat.call_args.kwargs["messages"][0]["role"], "system")
        self.assertEqual(chat.call_args.kwargs["messages"][1]["role"], "user")

    def test_fast_tier_uses_fast_model_but_runtime_override_wins(self):
        with patch.object(ai_service, "OLLAMA_API_KEY", "test"), \
             patch.object(ai_service, "OLLAMA_CHAT_MODEL", "main-model"), \
             patch.object(ai_service, "OLLAMA_FAST_CHAT_MODEL", "fast-model"), \
             patch.object(ai_service, "_RUNTIME_MODEL_OVERRIDE", None), \
             patch.object(ai_service.ollama_client, "chat", return_value=self._response()) as chat:
            ai_service.generate_chat_completion_with_tools(
                "fast request", [], prefer_fast_model=True,
            )
            self.assertEqual(chat.call_args.kwargs["model"], "fast-model")

        with patch.object(ai_service, "OLLAMA_API_KEY", "test"), \
             patch.object(ai_service, "OLLAMA_FAST_CHAT_MODEL", "fast-model"), \
             patch.object(ai_service, "_RUNTIME_MODEL_OVERRIDE", "override-model"), \
             patch.object(ai_service.ollama_client, "chat", return_value=self._response()) as chat:
            ai_service.generate_chat_completion_with_tools(
                "override request", [], prefer_fast_model=True,
            )
            self.assertEqual(chat.call_args.kwargs["model"], "override-model")

    def test_ollama_owner_model_and_empty_fallback(self):
        with patch.object(ai_service, "OLLAMA_API_KEY", "test"), \
             patch.object(ai_service, "OLLAMA_CHAT_MODEL", "main-model"), \
             patch.object(ai_service, "OLLAMA_FAST_CHAT_MODEL", "fast-model"), \
             patch.object(ai_service, "_RUNTIME_MODEL_OVERRIDE", None), \
             patch.dict(ai_service.os.environ, {
                 "AYUE_OLLAMA_PLANNER_MODEL": "planner-model",
                 "AYUE_OLLAMA_WEB_MODEL": "",
             }, clear=False), \
             patch.object(ai_service.ollama_client, "chat", return_value=self._response()) as chat:
            ai_service.generate_chat_completion_with_tools(
                "planner", [], prefer_fast_model=True, model_owner="planner",
            )
            self.assertEqual(chat.call_args.kwargs["model"], "planner-model")
            ai_service.generate_chat_completion_with_tools(
                "web", [], model_owner="web",
            )
            self.assertEqual(chat.call_args.kwargs["model"], "main-model")

    def test_explicit_ollama_model_precedes_owner_model(self):
        with patch.object(ai_service, "OLLAMA_API_KEY", "test"), \
             patch.object(ai_service, "_RUNTIME_MODEL_OVERRIDE", None), \
             patch.dict(ai_service.os.environ, {
                 "AYUE_OLLAMA_PLANNER_MODEL": "planner-model",
             }, clear=False), \
             patch.object(ai_service.ollama_client, "chat", return_value=self._response()) as chat:
            ai_service.generate_chat_completion_with_tools(
                "planner", [], model="explicit-model", model_owner="planner",
            )
            self.assertEqual(chat.call_args.kwargs["model"], "explicit-model")

    def test_unknown_owner_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "Unknown LLM owner"):
            ai_service.get_effective_chat_model(model_owner="unknown")
