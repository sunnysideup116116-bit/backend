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
import random
import threading
import time
from typing import Callable, Optional, Protocol


class LLMAdapter(Protocol):
    def generate(self, prompt: str, model: str) -> str: ...


# ---------------------------------------------------------------------------
# 重試與逾時（2026-08-05 新增，known-issues #2）
# ---------------------------------------------------------------------------
# 為何需要：呼叫失敗時 `nlp_engine._fallback_result()` 回傳全 0 的 delta，
# 對一則不具行為異常的訊息即等同判 `safe`（known-issues #17）。
# 也就是說**網路抖動會直接變成安全破口**——這不是理論風險：
# 2026-08-05 驗證 temperature 修正時連續發送請求，即因速率限制觸發此路徑。
#
# ⚠️ 重試只對「呼叫失敗」有效，對「答案不好」無效。
#    temperature 已固定為 0，重試拿到的是同一個答案。
#    這是正確的行為，但別誤以為重試能改善判斷品質。
#
# 只重試暫時性錯誤：金鑰錯誤、參數錯誤等永久性失敗應立刻放棄，
# 否則只是把一次失敗變成三次失敗、延遲三倍。

LLM_MAX_ATTEMPTS = int(os.getenv("LLM_MAX_ATTEMPTS", "3"))
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))
LLM_BACKOFF_BASE = float(os.getenv("LLM_BACKOFF_BASE", "1.0"))

# Pair chat waits at most 40 seconds for the complete risk request.  The NLP
# provider therefore needs its own, smaller budget: a primary attempt plus one
# fallback attempt must leave time for guardrail and persistence work.  Summary
# generation keeps the more generous global budget because it runs in the
# background and is not on the message-send path.
NLP_LLM_MAX_ATTEMPTS = int(os.getenv("NLP_LLM_MAX_ATTEMPTS", "1"))
# Google GenAI rejects manually supplied deadlines below 10 seconds.
NLP_LLM_TIMEOUT_SECONDS = float(os.getenv("NLP_LLM_TIMEOUT_SECONDS", "10"))
NLP_LLM_FALLBACK_TIMEOUT_SECONDS = float(
    os.getenv("NLP_LLM_FALLBACK_TIMEOUT_SECONDS", "15")
)
GEMINI_PRIMARY_COOLDOWN_SECONDS = float(
    os.getenv("GEMINI_PRIMARY_COOLDOWN_SECONDS", "120")
)

# Guardrail classifier is optional and must not consume the NLP engine's much
# larger retry budget. Rule and NLP evaluation remain available when it fails.
GUARDRAIL_MAX_ATTEMPTS = int(os.getenv("GUARDRAIL_MAX_ATTEMPTS", "1"))
GUARDRAIL_TIMEOUT_SECONDS = float(os.getenv("GUARDRAIL_TIMEOUT_SECONDS", "3"))

# 依例外類別名稱判斷是否暫時性。跨 SDK 通用，不必逐一 import 各家的例外型別
# （google.api_core、openai、httpx 的類別名稱皆涵蓋於下）。
_TRANSIENT_NAME_HINTS = (
    "ratelimit", "resourceexhausted", "toomanyrequests",
    "serviceunavailable", "unavailable",
    "timeout", "deadlineexceeded",
    "internalservererror", "internalerror", "apiconnection",
    "connectionerror", "remoteprotocol",
)
_TRANSIENT_STATUS = {408, 409, 429, 500, 502, 503, 504}


def _is_transient(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    if any(h in name for h in _TRANSIENT_NAME_HINTS):
        return True
    for attr in ("status_code", "code", "http_status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int) and val in _TRANSIENT_STATUS:
            return True
    return False


def call_with_retry(
    fn: Callable[[], str],
    label: str = "LLM",
    max_attempts: Optional[int] = None,
) -> str:
    """執行 fn，對暫時性錯誤做指數退避重試。

    退避加入隨機抖動（jitter），避免多個併發請求在同一時刻一起重試而再次撞上限額。
    """
    attempts = LLM_MAX_ATTEMPTS if max_attempts is None else max(1, max_attempts)
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — 需依內容分類，故先全捕捉
            last_exc = e
            if not _is_transient(e):
                print(f"   [ {label} ] 永久性錯誤，不重試：{type(e).__name__}: {e}")
                raise
            if attempt >= attempts:
                print(f"   [ {label} ] 重試 {attempts} 次仍失敗：{type(e).__name__}")
                raise
            wait = LLM_BACKOFF_BASE * (2 ** (attempt - 1)) * (1 + random.random() * 0.25)
            print(f"   [ {label} ] 暫時性錯誤（{type(e).__name__}），"
                  f"{wait:.1f}s 後重試（第 {attempt}/{attempts} 次）")
            time.sleep(wait)
    raise last_exc  # pragma: no cover — 迴圈內必定 return 或 raise


def _collect_google_api_keys() -> list[str]:
    keys: list[str] = []
    for prefix in ("GOOGLE_API_KEYS", "GOOGLE_API_KEY_"):
        idx = 1
        while idx <= 50:
            val = os.getenv(f"{prefix}{idx}", "").strip().strip("\"'")
            if val and val not in keys:
                keys.append(val)
            elif not val and idx > 3:
                break
            idx += 1

    for var in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_AI_STUDIO_API_KEY"):
        val = os.getenv(var, "").strip().strip("\"'")
        if val and val not in keys:
            keys.append(val)

    return keys


class GeminiAdapter:
    """Google Generative AI 包裝（新版 google.genai SDK），支援多 Key 自動分流與容錯切換"""

    def __init__(
        self,
        fallback_model: Optional[str] = None,
        timeout: Optional[float] = None,
        fallback_timeout: Optional[float] = None,
        max_attempts: Optional[int] = None,
    ):
        from google import genai
        from google.genai import types

        self._types = types
        self._keys = _collect_google_api_keys()
        self._clients = [
            (k, genai.Client(api_key=k)) for k in self._keys
        ]
        self._client = self._clients[0][1] if self._clients else None
        self._client_lock = threading.Lock()
        self._client_index = 0
        self._key_cooldown_until: dict[str, float] = {}

        self._fallback_model = fallback_model or os.getenv(
            "GEMINI_FALLBACK_MODEL",
            "gemini-2.5-flash-lite",
        )
        self._timeout = LLM_TIMEOUT_SECONDS if timeout is None else timeout
        self._fallback_timeout = (
            self._timeout if fallback_timeout is None else fallback_timeout
        )
        self._max_attempts = max_attempts
        self._primary_unavailable_until = 0.0

    def generate(self, prompt: str, model: str) -> str:
        if not self._client and not self._clients:
            raise RuntimeError("GeminiAdapter 缺少 GOOGLE_API_KEY / GEMINI_API_KEY")

        def _call_model(target_model: str, timeout: float) -> str:
            # 若測試或外部手動替換了 self._client，直接使用
            if self._client and not any(c is self._client for _, c in self._clients):
                active_clients = [("mock", self._client)]
            else:
                active_clients = self._clients or [("default", self._client)]

            with self._client_lock:
                total = len(active_clients)
                start_idx = self._client_index
                self._client_index = (self._client_index + 1) % total

            now = time.monotonic()
            ordered = [active_clients[(start_idx + i) % total] for i in range(total)]

            last_err = None
            for key, client in ordered:
                if total > 1 and now < self._key_cooldown_until.get(key, 0.0):
                    continue

                def _once() -> str:
                    response = client.models.generate_content(
                        model=target_model,
                        contents=prompt,
                        config=self._types.GenerateContentConfig(
                            temperature=0.0,
                            http_options=self._types.HttpOptions(timeout=timeout * 1000),
                        ),
                    )
                    return response.text

                try:
                    return call_with_retry(
                        _once,
                        label=f"Gemini({target_model})",
                        max_attempts=self._max_attempts,
                    )
                except Exception as exc:
                    last_err = exc
                    if _is_transient(exc):
                        self._key_cooldown_until[key] = time.monotonic() + 60.0
                        masked = (key[:6] + "..." + key[-4:]) if len(key) > 10 else "***"
                        print(f"⚠️ [GeminiAdapter] Key {masked} 觸發配額/暫時限制，切換下一個 Key")
                        continue
                    raise exc

            if last_err:
                raise last_err
            raise RuntimeError("GeminiAdapter 無可用 Client")

        fallback = self._fallback_model
        primary_available = time.monotonic() >= self._primary_unavailable_until
        if primary_available or not fallback or fallback == model:
            try:
                return _call_model(model, self._timeout)
            except Exception as primary_err:
                if _is_transient(primary_err):
                    self._primary_unavailable_until = (
                        time.monotonic() + GEMINI_PRIMARY_COOLDOWN_SECONDS
                    )
                if fallback and fallback != model:
                    print(f"   [ GeminiAdapter ] 主要模型 {model} 呼叫失敗 ({primary_err})，啟動 Fallback 至 {fallback}...")
                    try:
                        return _call_model(fallback, self._fallback_timeout)
                    except Exception as fb_err:
                        print(f"   [ GeminiAdapter ] Fallback 模型 {fallback} 亦失敗: {fb_err}")
                        raise fb_err
                raise primary_err
        else:
            print(
                f"   [ GeminiAdapter ] 主要模型 {model} 暫時停用，"
                f"直接使用 Fallback {fallback}"
            )
            try:
                return _call_model(fallback, self._fallback_timeout)
            except Exception as fb_err:
                print(f"   [ GeminiAdapter ] Fallback 模型 {fallback} 亦失敗: {fb_err}")
                raise fb_err


class OpenAICompatAdapter:
    """OpenAI 相容 chat completion 包裝（適用 Groq / OpenRouter / Together / 等等）"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: Optional[float] = None,
        max_attempts: Optional[int] = None,
    ):
        from openai import OpenAI

        if not base_url:
            raise ValueError("OpenAICompatAdapter requires base_url")
        if not api_key:
            raise ValueError("OpenAICompatAdapter requires api_key")
        self._max_attempts = max_attempts
        # max_retries=0：關閉 SDK 內建重試，改由 call_with_retry 統一處理，
        # 以免兩層重試相乘（3 × 2 = 6 次）使失敗情境的延遲不可預期。
        self._client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=LLM_TIMEOUT_SECONDS if timeout is None else timeout,
            max_retries=0,
        )

    def generate(self, prompt: str, model: str, extra_kwargs: Optional[dict] = None) -> str:
        def _once() -> str:
            kwargs = dict(extra_kwargs or {})
            resp = self._client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                # 與 GeminiAdapter 一致設為 0（2026-08-05）：判斷的可重現性是本系統的設計性質，
                # 不因走哪條 provider 路徑而異。原值 0.1 使 provider 切換會重新引入不確定性。
                # 已實測 llama-guard3:1b 與 gemma4:e2b 在 0.0 下三次輸出完全相同，未見小模型退化。
                temperature=0.0,
                # llama.cpp 專用參數（如 chat_template_kwargs）需走 extra_body，
                # openai SDK 會拒絕未知的頂層參數。
                extra_body=kwargs,
            )
            return resp.choices[0].message.content or ""

        return call_with_retry(
            _once,
            label="OpenAICompat",
            max_attempts=self._max_attempts,
        )


class OllamaAdapter:
    """直接使用 Ollama 的原生 /api/chat 端點，效能極高（解決 OpenAI 相容端點解析與推論卡頓問題）"""

    def __init__(self, base_url: str, timeout: Optional[float] = None, max_attempts: Optional[int] = None):
        # 將 /v1 端點轉換為原生 Ollama REST 端點
        self._url = base_url.replace("/v1", "").rstrip("/") + "/api/chat"
        # 預設退回全域 LLM_TIMEOUT_SECONDS（30s）與 call_with_retry 的全域預設（3 次）。
        # guardrail classifier 走此類別時，呼叫端應傳入緊縮預算
        # （GUARDRAIL_TIMEOUT_SECONDS=3, GUARDRAIL_MAX_ATTEMPTS=1），否則會回到
        # 3 次 × 30s 的寬鬆預算——正是 8/9 事故把 /detect 拖慢的原因。
        self._timeout = LLM_TIMEOUT_SECONDS if timeout is None else timeout
        self._max_attempts = max_attempts

    def generate(self, prompt: str, model: str) -> str:
        import requests

        def _once() -> str:
            data = {
                "model": model,
                "messages": [
                    {"role": "user", "content": prompt}
                ],
                "stream": False,
                "think": False,
                "options": {
                    "temperature": 0.0
                }
            }
            resp = requests.post(self._url, json=data, timeout=self._timeout)
            resp.raise_for_status()
            return resp.json()["message"]["content"]

        return call_with_retry(_once, label="Ollama", max_attempts=self._max_attempts)


def _make_openai_compat(
    base_url: Optional[str],
    api_key: Optional[str],
    timeout: Optional[float] = None,
    max_attempts: Optional[int] = None,
) -> OpenAICompatAdapter:
    if not base_url or not api_key:
        raise RuntimeError("openai_compat provider 需要 base_url 與 api_key 環境變數")
    return OpenAICompatAdapter(
        base_url=base_url,
        api_key=api_key,
        timeout=timeout,
        max_attempts=max_attempts,
    )


def get_nlp_adapter() -> LLMAdapter:
    provider = os.getenv("NLP_PROVIDER", "gemini").lower()
    if provider == "openai_compat":
        return _make_openai_compat(
            os.getenv("NLP_OPENAI_BASE_URL"),
            os.getenv("NLP_OPENAI_API_KEY"),
            timeout=NLP_LLM_TIMEOUT_SECONDS,
            max_attempts=NLP_LLM_MAX_ATTEMPTS,
        )
    return GeminiAdapter(
        timeout=NLP_LLM_TIMEOUT_SECONDS,
        fallback_timeout=NLP_LLM_FALLBACK_TIMEOUT_SECONDS,
        max_attempts=NLP_LLM_MAX_ATTEMPTS,
    )


def get_nlp_model_name(kb_model_hint: Optional[str]) -> str:
    """Resolve NLP model name from provider-specific source."""

    provider = os.getenv("NLP_PROVIDER", "gemini").lower()
    if provider == "openai_compat":
        model = os.getenv("NLP_MODEL")
        if not model:
            raise RuntimeError("openai_compat 需要 NLP_MODEL 環境變數")
        return model
    return os.getenv("GEMINI_MODEL") or kb_model_hint or "gemini-3.1-flash-lite"


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
    return os.getenv("GEMINI_MODEL") or kb_model_hint or "gemini-3.1-flash-lite"


def get_guardrail_classifier_adapter() -> LLMAdapter:
    """只在 GUARDRAIL_PROVIDER=llm_classifier 時被呼叫"""
    base_url = os.getenv("GUARDRAIL_BASE_URL")
    if base_url and ("11434" in base_url or "ollama" in base_url.lower()):
        return OllamaAdapter(
            base_url,
            timeout=GUARDRAIL_TIMEOUT_SECONDS,
            max_attempts=GUARDRAIL_MAX_ATTEMPTS,
        )
    return _make_openai_compat(
        base_url,
        os.getenv("GUARDRAIL_API_KEY"),
        timeout=GUARDRAIL_TIMEOUT_SECONDS,
        max_attempts=GUARDRAIL_MAX_ATTEMPTS,
    )


def get_guardrail_classifier_model() -> str:
    model = os.getenv("GUARDRAIL_MODEL")
    if not model:
        raise RuntimeError("llm_classifier 需要 GUARDRAIL_MODEL 環境變數")
    return model
