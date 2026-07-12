"""
LLM Adapter Layer - 統一不同 provider 的 chat completion / generate 介面

兩種 adapter：
- GeminiAdapter：包 google.genai SDK
- OpenAICompatAdapter：包 openai SDK，可指向任何 OpenAI 相容 endpoint
  (Groq / OpenRouter / Together / HuggingFace Inference / 自建 vLLM / Ollama)

工廠 function：
- get_nlp_adapter() 供 NLPEngine.analyze 用
- get_summary_adapter() 供 generate_rolling_summary 用
- get_guardrail_classifier_adapter() 供 GuardrailEngine 的 llm_classifier 模式用
"""

import os
from typing import Optional, Protocol


class LLMAdapter(Protocol):
    def generate(self, prompt: str, model: str) -> str: ...


class GeminiAdapter:
    """Google Generative AI 包裝"""

    def __init__(self):
        from google import genai
        from google.genai import types

        api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        self._client = genai.Client(api_key=api_key) if api_key else None
        self._types = types

    def generate(self, prompt: str, model: str) -> str:
        if not self._client:
            raise RuntimeError("GeminiAdapter 缺少 GOOGLE_API_KEY / GEMINI_API_KEY")
        try:
            response = self._client.models.generate_content(
                model=model,
                contents=prompt,
                config=self._types.GenerateContentConfig(
                    http_options=self._types.HttpOptions(timeout=10000),
                ),
            )
            return response.text
        except Exception as e:
            print(f"      [ Gemini SDK Error ] {e}")
            raise e


class OpenAICompatAdapter:
    """OpenAI 相容 chat completion 包裝（適用 Groq / OpenRouter / Together / 等等）"""

    def __init__(self, base_url: str, api_key: str):
        from openai import OpenAI

        if not base_url:
            raise ValueError("OpenAICompatAdapter requires base_url")
        if not api_key:
            raise ValueError("OpenAICompatAdapter requires api_key")
        self._client = OpenAI(base_url=base_url, api_key=api_key)

    def generate(self, prompt: str, model: str) -> str:
        resp = self._client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        return resp.choices[0].message.content or ""


class OllamaAdapter:
    """直接使用 Ollama 的原生 /api/chat 端點，效能極高（解決 OpenAI 相容端點解析與推論卡頓問題）"""

    def __init__(self, base_url: str):
        # 將 /v1 端點轉換為原生 Ollama REST 端點
        self._url = base_url.replace("/v1", "").rstrip("/") + "/api/chat"

    def generate(self, prompt: str, model: str) -> str:
        import requests
        data = {
            "model": model,
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "stream": False,
            "think": False,
            "options": {
                "temperature": 0.1
            }
        }
        try:
            resp = requests.post(self._url, json=data, timeout=15.0)
            resp.raise_for_status()
            return resp.json()["message"]["content"]
        except Exception as e:
            print(f"      [ Ollama Native Error ] {e}")
            raise e



def _make_openai_compat(base_url: Optional[str], api_key: Optional[str]) -> OpenAICompatAdapter:
    if not base_url or not api_key:
        raise RuntimeError("openai_compat provider 需要 base_url 與 api_key 環境變數")
    return OpenAICompatAdapter(base_url=base_url, api_key=api_key)


def get_nlp_adapter() -> LLMAdapter:
    provider = os.getenv("NLP_PROVIDER", "gemini").lower()
    if provider == "openai_compat":
        return _make_openai_compat(
            os.getenv("NLP_OPENAI_BASE_URL"),
            os.getenv("NLP_OPENAI_API_KEY"),
        )
    return GeminiAdapter()


def get_nlp_model_name(kb_model_hint: Optional[str]) -> str:
    """Resolve NLP model name from provider-specific source."""

    provider = os.getenv("NLP_PROVIDER", "gemini").lower()
    if provider == "openai_compat":
        model = os.getenv("NLP_MODEL")
        if not model:
            raise RuntimeError("openai_compat 需要 NLP_MODEL 環境變數")
        return model
    return kb_model_hint or "gemini-2.5-flash"


def get_summary_adapter() -> LLMAdapter:
    provider = (os.getenv("SUMMARY_PROVIDER") or os.getenv("NLP_PROVIDER", "gemini")).lower()
    if provider == "openai_compat":
        return _make_openai_compat(
            os.getenv("SUMMARY_OPENAI_BASE_URL") or os.getenv("NLP_OPENAI_BASE_URL"),
            os.getenv("SUMMARY_OPENAI_API_KEY") or os.getenv("NLP_OPENAI_API_KEY"),
        )
    return GeminiAdapter()


def get_summary_model_name(kb_model_hint: Optional[str]) -> str:
    provider = (os.getenv("SUMMARY_PROVIDER") or os.getenv("NLP_PROVIDER", "gemini")).lower()
    if provider == "openai_compat":
        model = os.getenv("SUMMARY_MODEL") or os.getenv("NLP_MODEL")
        if not model:
            raise RuntimeError("openai_compat summary 需要 SUMMARY_MODEL 或 NLP_MODEL 環境變數")
        return model
    return kb_model_hint or "gemini-2.5-flash"


def get_guardrail_classifier_adapter() -> LLMAdapter:
    """只在 GUARDRAIL_PROVIDER=llm_classifier 時被呼叫"""
    base_url = os.getenv("GUARDRAIL_BASE_URL")
    if base_url and ("11434" in base_url or "ollama" in base_url.lower()):
        return OllamaAdapter(base_url)
    return _make_openai_compat(
        base_url,
        os.getenv("GUARDRAIL_API_KEY"),
    )



def get_guardrail_classifier_model() -> str:
    model = os.getenv("GUARDRAIL_MODEL")
    if not model:
        raise RuntimeError("llm_classifier 需要 GUARDRAIL_MODEL 環境變數")
    return model
