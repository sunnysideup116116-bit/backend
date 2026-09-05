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
    assert adapter._timeout == 10
    assert adapter._fallback_timeout == 15
    assert adapter._max_attempts == 1
    assert adapter._fallback_model == "gemini-2.5-flash-lite"


def test_openai_compat_provider_returns_compat(monkeypatch):
    monkeypatch.setenv("NLP_PROVIDER", "openai_compat")
    monkeypatch.setenv("NLP_OPENAI_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("NLP_OPENAI_API_KEY", "fake-key")
    from app.core.llm_adapters import OpenAICompatAdapter, get_nlp_adapter

    adapter = get_nlp_adapter()

    assert isinstance(adapter, OpenAICompatAdapter)
    assert adapter._max_attempts == 1


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


# --- 重試與逾時（known-issues #2）---

def _transient(name):
    """造一個「名稱看起來像暫時性錯誤」的例外類別。"""
    return type(name, (Exception,), {})


def test_retry_gives_up_immediately_on_permanent_error():
    """金鑰／參數錯誤屬永久性，重試只會把一次失敗變三次、延遲三倍。"""
    from app.core import llm_adapters as la

    calls = []

    def fn():
        calls.append(1)
        raise ValueError("invalid api key")

    with pytest.raises(ValueError):
        la.call_with_retry(fn)
    assert len(calls) == 1


def test_retry_retries_transient_then_succeeds(monkeypatch):
    from app.core import llm_adapters as la
    monkeypatch.setattr(la, "LLM_BACKOFF_BASE", 0.0)   # 測試不實際等待

    RateLimitError = _transient("RateLimitError")
    calls = []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise RateLimitError("429")
        return "ok"

    assert la.call_with_retry(fn) == "ok"
    assert len(calls) == 3


def test_retry_raises_after_max_attempts(monkeypatch):
    from app.core import llm_adapters as la
    monkeypatch.setattr(la, "LLM_BACKOFF_BASE", 0.0)
    monkeypatch.setattr(la, "LLM_MAX_ATTEMPTS", 3)

    ServiceUnavailable = _transient("ServiceUnavailable")
    calls = []

    def fn():
        calls.append(1)
        raise ServiceUnavailable("503")

    with pytest.raises(ServiceUnavailable):
        la.call_with_retry(fn)
    assert len(calls) == 3


def test_component_retry_budget_can_disable_retries(monkeypatch):
    from app.core import llm_adapters as la
    monkeypatch.setattr(la, "LLM_BACKOFF_BASE", 0.0)

    ServiceUnavailable = _transient("ServiceUnavailable")
    calls = []

    def fn():
        calls.append(1)
        raise ServiceUnavailable("503")

    with pytest.raises(ServiceUnavailable):
        la.call_with_retry(fn, label="Guardrail", max_attempts=1)
    assert len(calls) == 1


def test_gemini_nlp_budget_switches_to_fallback_without_retrying_primary(monkeypatch):
    """The foreground risk path must not exhaust the chat gateway's 20s timeout."""
    monkeypatch.setenv("GOOGLE_API_KEY", "fake")
    from app.core import llm_adapters as la

    ServiceUnavailable = _transient("ServiceUnavailable")
    calls = []

    class FakeModels:
        def generate_content(self, *, model, contents, config):
            calls.append(model)
            if model == "primary-model":
                raise ServiceUnavailable("503")
            return type("Response", (), {"text": "ok"})()

    adapter = la.GeminiAdapter(
        fallback_model="fallback-model",
        timeout=6,
        max_attempts=1,
    )
    adapter._client = type("Client", (), {"models": FakeModels()})()

    assert adapter.generate("prompt", "primary-model") == "ok"
    assert calls == ["primary-model", "fallback-model"]
    assert adapter.generate("prompt", "primary-model") == "ok"
    assert calls == ["primary-model", "fallback-model", "fallback-model"]


def test_transient_detected_by_status_code():
    """類別名稱看不出來時，改以 status_code 判斷。"""
    from app.core import llm_adapters as la

    class WeirdError(Exception):
        status_code = 503

    class AuthError(Exception):
        status_code = 401

    assert la._is_transient(WeirdError()) is True
    assert la._is_transient(AuthError()) is False


def test_transient_covers_common_sdk_exception_names():
    """涵蓋 google.api_core 與 openai 兩家 SDK 的常見暫時性例外名稱。"""
    from app.core import llm_adapters as la

    for name in ("RateLimitError", "ResourceExhausted", "ServiceUnavailable",
                 "APITimeoutError", "DeadlineExceeded", "InternalServerError",
                 "APIConnectionError"):
        assert la._is_transient(_transient(name)()) is True, name
    for name in ("PermissionDenied", "InvalidArgument", "NotFound"):
        assert la._is_transient(_transient(name)()) is False, name


# --- guardrail classifier 緊縮預算（整合進度確認 2026-08-23 第 7 項）---
# B5 的緊縮預算（GUARDRAIL_TIMEOUT_SECONDS=3, GUARDRAIL_MAX_ATTEMPTS=1）原本只在
# openai_compat 路徑生效；OllamaAdapter 分支沒吃 timeout/max_attempts，會退回
# 3 次 × 30s 的寬鬆預算——8/9 事故把 /detect 拖慢的原因。

def test_guardrail_classifier_uses_tight_budget_ollama(monkeypatch):
    """GUARDRAIL_BASE_URL 指向 Ollama 時，adapter 應帶緊縮的 timeout/max_attempts。"""
    monkeypatch.setenv("GUARDRAIL_BASE_URL", "http://localhost:11434/v1")
    from app.core import llm_adapters as la

    adapter = la.get_guardrail_classifier_adapter()

    assert isinstance(adapter, la.OllamaAdapter)
    assert adapter._timeout == la.GUARDRAIL_TIMEOUT_SECONDS
    assert adapter._max_attempts == la.GUARDRAIL_MAX_ATTEMPTS


def test_guardrail_classifier_uses_tight_budget_openai_compat(monkeypatch):
    """openai_compat 路徑同樣應帶緊縮重試預算（既有行為，確保未回退）。

    OpenAICompatAdapter 將 timeout 傳入 OpenAI SDK client（內部屬性），
    故這裡只驗 _max_attempts（重試次數）——這是 8/9 事故拖慢 /detect 的
    主因：3 次 × 30s vs 緊縮的 1 次。
    """
    monkeypatch.setenv("GUARDRAIL_BASE_URL", "http://test/v1")
    monkeypatch.setenv("GUARDRAIL_API_KEY", "test-key")
    from app.core import llm_adapters as la

    adapter = la.get_guardrail_classifier_adapter()

    assert isinstance(adapter, la.OpenAICompatAdapter)
    assert adapter._max_attempts == la.GUARDRAIL_MAX_ATTEMPTS


def test_ollama_adapter_defaults_to_global_budget(monkeypatch):
    """未傳 timeout/max_attempts 時退回全域預設（30s / 3 次），不炸既有 5 個 import 處。"""
    from app.core import llm_adapters as la

    adapter = la.OllamaAdapter("http://localhost:11434/v1")

    assert adapter._timeout == la.LLM_TIMEOUT_SECONDS
    assert adapter._max_attempts is None  # call_with_retry 會退回 LLM_MAX_ATTEMPTS
