"""
LLM Adapter factory 測試，只驗證 provider 選擇邏輯。
不打網路、不打真實 LLM。
"""

import pytest


def test_default_provider_returns_gemini(monkeypatch):
    monkeypatch.delenv("NLP_PROVIDER", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "fake")
    from app.core.llm_adapters import GeminiAdapter, get_nlp_adapter

    adapter = get_nlp_adapter()

    assert isinstance(adapter, GeminiAdapter)


def test_openai_compat_provider_returns_compat(monkeypatch):
    monkeypatch.setenv("NLP_PROVIDER", "openai_compat")
    monkeypatch.setenv("NLP_OPENAI_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("NLP_OPENAI_API_KEY", "fake-key")
    from app.core.llm_adapters import OpenAICompatAdapter, get_nlp_adapter

    adapter = get_nlp_adapter()

    assert isinstance(adapter, OpenAICompatAdapter)


def test_openai_compat_missing_credentials_raises(monkeypatch):
    monkeypatch.setenv("NLP_PROVIDER", "openai_compat")
    monkeypatch.delenv("NLP_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("NLP_OPENAI_API_KEY", raising=False)
    from app.core.llm_adapters import get_nlp_adapter

    with pytest.raises(RuntimeError):
        get_nlp_adapter()


def test_summary_provider_falls_back_to_nlp(monkeypatch):
    monkeypatch.setenv("NLP_PROVIDER", "openai_compat")
    monkeypatch.setenv("NLP_OPENAI_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("NLP_OPENAI_API_KEY", "fake-key")
    monkeypatch.delenv("SUMMARY_PROVIDER", raising=False)
    from app.core.llm_adapters import OpenAICompatAdapter, get_summary_adapter

    adapter = get_summary_adapter()

    assert isinstance(adapter, OpenAICompatAdapter)


def test_summary_provider_can_override_to_gemini(monkeypatch):
    monkeypatch.setenv("NLP_PROVIDER", "openai_compat")
    monkeypatch.setenv("NLP_OPENAI_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("NLP_OPENAI_API_KEY", "fake-key")
    monkeypatch.setenv("SUMMARY_PROVIDER", "gemini")
    monkeypatch.setenv("GOOGLE_API_KEY", "fake")
    from app.core.llm_adapters import GeminiAdapter, get_summary_adapter

    adapter = get_summary_adapter()

    assert isinstance(adapter, GeminiAdapter)


def test_nlp_model_name_openai_compat_requires_env(monkeypatch):
    monkeypatch.setenv("NLP_PROVIDER", "openai_compat")
    monkeypatch.delenv("NLP_MODEL", raising=False)
    from app.core.llm_adapters import get_nlp_model_name

    with pytest.raises(RuntimeError):
        get_nlp_model_name("gemini-2.5-flash")


def test_nlp_model_name_openai_compat_uses_env(monkeypatch):
    monkeypatch.setenv("NLP_PROVIDER", "openai_compat")
    monkeypatch.setenv("NLP_MODEL", "openai/gpt-oss-20b")
    from app.core.llm_adapters import get_nlp_model_name

    assert get_nlp_model_name("gemini-2.5-flash") == "openai/gpt-oss-20b"


def test_nlp_model_name_gemini_uses_kb_hint(monkeypatch):
    monkeypatch.delenv("NLP_PROVIDER", raising=False)
    from app.core.llm_adapters import get_nlp_model_name

    assert get_nlp_model_name("gemini-2.5-flash") == "gemini-2.5-flash"


def test_guardrail_classifier_model_required(monkeypatch):
    monkeypatch.delenv("GUARDRAIL_MODEL", raising=False)
    from app.core.llm_adapters import get_guardrail_classifier_model

    with pytest.raises(RuntimeError):
        get_guardrail_classifier_model()
