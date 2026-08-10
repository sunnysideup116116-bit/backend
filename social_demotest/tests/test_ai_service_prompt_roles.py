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

    def test_legacy_chat_keeps_single_user_message(self):
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
