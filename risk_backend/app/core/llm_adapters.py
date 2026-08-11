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


def call_with_retry(fn: Callable[[], str], label: str = "LLM") -> str:
    """執行 fn，對暫時性錯誤做指數退避重試。

    退避加入隨機抖動（jitter），避免多個併發請求在同一時刻一起重試而再次撞上限額。
    """
    last_exc = None
    for attempt in range(1, LLM_MAX_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — 需依內容分類，故先全捕捉
            last_exc = e
            if not _is_transient(e):
                print(f"   [ {label} ] 永久性錯誤，不重試：{type(e).__name__}: {e}")
                raise
            if attempt >= LLM_MAX_ATTEMPTS:
                print(f"   [ {label} ] 重試 {LLM_MAX_ATTEMPTS} 次仍失敗：{type(e).__name__}")
                raise
            wait = LLM_BACKOFF_BASE * (2 ** (attempt - 1)) * (1 + random.random() * 0.25)
            print(f"   [ {label} ] 暫時性錯誤（{type(e).__name__}），"
                  f"{wait:.1f}s 後重試（第 {attempt}/{LLM_MAX_ATTEMPTS} 次）")
            time.sleep(wait)
    raise last_exc  # pragma: no cover — 迴圈內必定 return 或 raise


class GeminiAdapter:
    """Google Generative AI 包裝（新版 google.genai SDK）"""

    def __init__(self):
        from google import genai
        from google.genai import types

        api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        self._client = genai.Client(api_key=api_key) if api_key else None
        self._types = types

    def generate(self, prompt: str, model: str) -> str:
        if not self._client:
            raise RuntimeError("GeminiAdapter 缺少 GOOGLE_API_KEY / GEMINI_API_KEY")

        # temperature=0（2026-08-05 新增）：原本未指定，沿用模型預設值。
        # 實測同一則案例連跑三次，sexual_boundary 出現 0.75／0.75／0.65 的擺盪；
        # 整個 test 集重跑一次，22 則中有 15 則（68%）NLP 分數改變，
        # 3 則（14%）連最終等級都翻掉——大於我們想量測的多數效果量，
        # 使「修正前 vs 修正後」的比較無法解讀（見 known-issues #23）。
        # 設為 0 後三次結果完全相同。
        # 這同時也是風險系統該有的性質：同樣的訊息與脈絡，應得到同樣的判斷。
        def _once() -> str:
            response = self._client.models.generate_content(
                model=model,
                contents=prompt,
                config=self._types.GenerateContentConfig(
                    temperature=0.0,
                    http_options=self._types.HttpOptions(timeout=LLM_TIMEOUT_SECONDS * 1000),
                ),
            )
            return response.text

        return call_with_retry(_once, label="Gemini")


class OpenAICompatAdapter:
    """OpenAI 相容 chat completion 包裝（適用 Groq / OpenRouter / Together / 等等）"""

    def __init__(self, base_url: str, api_key: str):
        from openai import OpenAI

        if not base_url:
            raise ValueError("OpenAICompatAdapter requires base_url")
        if not api_key:
            raise ValueError("OpenAICompatAdapter requires api_key")
        # max_retries=0：關閉 SDK 內建重試，改由 call_with_retry 統一處理，
        # 以免兩層重試相乘（3 × 2 = 6 次）使失敗情境的延遲不可預期。
        self._client = OpenAI(base_url=base_url, api_key=api_key,
                              timeout=LLM_TIMEOUT_SECONDS, max_retries=0)

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

        return call_with_retry(_once, label="OpenAICompat")


class OllamaAdapter:
    """直接使用 Ollama 的原生 /api/chat 端點，效能極高（解決 OpenAI 相容端點解析與推論卡頓問題）"""

    def __init__(self, base_url: str):
        # 將 /v1 端點轉換為原生 Ollama REST 端點
        self._url = base_url.replace("/v1", "").rstrip("/") + "/api/chat"

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
            resp = requests.post(self._url, json=data, timeout=LLM_TIMEOUT_SECONDS)
            resp.raise_for_status()
            return resp.json()["message"]["content"]

        return call_with_retry(_once, label="Ollama")


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
