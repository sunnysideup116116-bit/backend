"""Deterministic Pi reply boundary; no model calls or state transitions."""
from dataclasses import dataclass
import json
import re

from services.ayue_agent.capabilities import normalize_public_language
from services.ayue_agent.shared.public_reply import validate_public_reply
from services.ayue_agent.shared.model_context_text import strip_model_time_annotations
from services.ayue_agent.web_tools import is_safe_public_url
from services.language_service import normalize_public_reply


_WRITE_RECEIPT = re.compile(
    r"(?:已經|已经|已).{0,12}(?:新增|寫入|写入|送出|寄出|取消|刪除|删除|修改|排好|安排好|建立(?:約會|邀請|行程))"
    r"|\bI(?:'ve| have)?\s+(?:created|sent|deleted|updated|scheduled)\b", re.I,
)
_CARD_MARKER = re.compile(r"\[\[\s*(?:confirmation|selection)\s*\]\]", re.I)
_FAKE_CARD_HEADER = re.compile(r"[\[［【]\s*(?:確認卡|选人卡|選人卡)\s*[｜|].*?[\]］】]", re.I)
_UNBOUND_INTERACTION_CLAIM = re.compile(
    r"(?:確認卡|确认卡|選人卡|选人卡).{0,24}(?:已(?:經|经)?(?:準備|准备|建立|產生|生成)|"
    r"(?:準備|准备|建立|產生|生成)(?:好|完成)|請(?:查看|點選|选择|選擇)|如下)",
    re.I,
)
_URL = re.compile(r"https?://[^\s<>\]\[\"']+", re.I)


@dataclass(frozen=True)
class PiReplyValidation:
    reply: str | None
    code: str | None = None
    stage: str = "public_reply"
    public_reason: str | None = None
    repairable: bool = False


def _verified_urls(observations: list[dict]) -> set[str]:
    urls: set[str] = set()

    def visit(value) -> None:
        if isinstance(value, dict):
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str) and is_safe_public_url(value):
            urls.add(value.rstrip(".,，。!?！？:：;；)）"))

    for observation in observations:
        if isinstance(observation, dict) and observation.get("status") == "ok":
            visit(observation.get("result"))
    return urls


def unbound_interaction_code(text: str) -> str | None:
    """Classify model-authored card syntax independently of tool evidence."""
    if _CARD_MARKER.search(text) or _FAKE_CARD_HEADER.search(text):
        return "pi_historical_card_echo"
    if _UNBOUND_INTERACTION_CLAIM.search(text):
        return "pi_unbound_interaction_claim"
    return None


def confirmed_reply(results: list[dict]) -> str:
    replies = [strip_model_time_annotations((item.get("data") or {}).get("reply"))
               for item in results if isinstance(item, dict)]
    return "\n".join(reply for reply in replies if reply)[:3600] or "這次操作結果暫時無法確認，請先查看最新狀態，避免重複操作。"


def pending_preview(observations: list[dict]) -> str | None:
    for item in reversed(observations):
        value = item.get("result") or {}
        if isinstance(value, dict) and value.get("pending_confirmation"):
            return strip_model_time_annotations(value.get("preview")) or "請確認是否套用這次變更。"
    return None


def pending_reply(observations: list[dict]) -> str | None:
    for item in reversed(observations):
        value = item.get("result") or {}
        if not isinstance(value, dict):
            continue
        if value.get("pending_confirmation"):
            return strip_model_time_annotations(value.get("preview")) or "請查看確認卡；確認後才會執行變更。"
        if value.get("contact_selection"):
            return "請先在選人卡確認你指的是哪一位，還沒有送出邀請。"
    return None


def validate_pi_reply_result(reply: str, observations: list[dict]) -> PiReplyValidation:
    raw = strip_model_time_annotations(reply)
    validation = validate_public_reply(
        raw, preserve_details=True, reject_internal_identifiers=True,
        reject_structured_output=True, max_chars=3600,
    )
    text = validation.reply
    # The shared DAG validator historically treats ordinary phrases such as
    # "沒有這個功能" as orchestration metadata. Pi keeps the real reason but
    # accepts natural capability prose unless it exposes actual tool internals.
    if validation.reason == "internal_meta_reply" and not re.search(
        r"visible_tools|tool_call|(?:沒有|無|缺少|目前沒有).{0,10}(?:工具|函式)|"
        r"(?:工具|函式).{0,10}(?:無法|不能|限制)|內部能力",
        raw,
        re.I,
    ):
        text = normalize_public_language(normalize_public_reply(raw)).strip()[:3600]
    if not text:
        reason = validation.reason or "empty_reply"
        return PiReplyValidation(
            None, f"pi_public_{reason}", public_reason=reason, repairable=True,
        )
    if interaction_code := unbound_interaction_code(text):
        return PiReplyValidation(
            None, interaction_code, stage="interaction_binding",
            public_reason=interaction_code.removeprefix("pi_"), repairable=True,
        )
    reply_urls = {
        match.group(0).rstrip(".,，。!?！？:：;；)）")
        for match in _URL.finditer(text)
    }
    if reply_urls and not reply_urls <= _verified_urls(observations):
        return PiReplyValidation(
            None, "pi_unverified_url", stage="source_binding",
            public_reason="unverified_url", repairable=True,
        )
    if _WRITE_RECEIPT.search(text):
        for item in reversed(observations):
            if item.get("tool") != "calendar.verify_recent_mutation" or item.get("status") != "ok":
                continue
            verification = (item.get("result") or {}).get("calendar_mutation_verification") or {}
            action = {"create": "新增", "update": "修改", "cancel": "取消"}.get(verification.get("action"))
            if verification.get("status") == "verified_success" and action:
                return PiReplyValidation(f"我已查證上一筆行程{action}成功。")
        return PiReplyValidation(
            None, "pi_unverified_completion_claim", stage="authority_binding",
            public_reason="unverified_completion_claim", repairable=True,
        )
    return PiReplyValidation(text)


def validate_pi_reply(reply: str, observations: list[dict]) -> str | None:
    """Compatibility wrapper used by focused tests and legacy imports."""
    return validate_pi_reply_result(reply, observations).reply


def _text_values(value, *, limit: int = 10) -> list[str]:
    output: list[str] = []

    def visit(item) -> None:
        if len(output) >= limit:
            return
        if isinstance(item, dict):
            preferred = ["title", "name", "event_title", "summary", "snippet", "display_name"]
            for key in preferred:
                child = item.get(key)
                if isinstance(child, str) and child.strip() and child.strip() not in output:
                    output.append(child.strip()[:240])
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return output


def verified_observation_fallback(observations: list[dict]) -> str | None:
    """Produce useful prose when verified reads succeeded but model prose failed."""
    successful = [
        item for item in observations
        if isinstance(item, dict) and item.get("status") == "ok"
        and not (item.get("result") or {}).get("pending_confirmation")
        and not (item.get("result") or {}).get("contact_selection")
    ]
    if not successful:
        return None
    latest = successful[-1]
    tool = str(latest.get("tool") or "")
    result = latest.get("result") or {}
    values = _text_values(result, limit=6)
    if tool.startswith("web."):
        if not values:
            return "公開資料查詢已完成，但這次無法安全整理內容；你可以縮小活動、人物或地區範圍再查一次。"
        return "我查到的重點包括：\n" + "\n".join(f"- {value}" for value in values[:5])
    if tool.startswith("places."):
        if values:
            return "附近場所查詢已完成，可以先看看：\n" + "\n".join(f"- {value}" for value in values[:5])
    if tool == "product.get_info":
        facts = result.get("facts") if isinstance(result, dict) else None
        if facts:
            compact = json.dumps(facts, ensure_ascii=False, separators=(",", ":"))[:1800]
            return f"我已讀到正式產品說明，但自然語言整理失敗。以下是已驗證的功能資料：{compact}"
    if tool.startswith(("calendar.", "relationship.", "profile.", "memory.", "match.")) and values:
        return "我已查到資料，重點是：\n" + "\n".join(f"- {value}" for value in values[:5])
    return "查詢已成功，但這次自然語言整理失敗。已驗證資料仍保留；你可以要我換個方式整理。"


def failed_reply(code: str, *, observations: list[dict] | None = None,
                 has_pending_interaction: bool = False) -> str:
    if observations:
        fallback = verified_observation_fallback(observations)
        if fallback:
            return fallback
    if code in {"pi_provider_timeout", "pi_deadline_exceeded"}:
        return "這次模型回應逾時，還沒有建立新的確認或執行變更。你的需求不需要重新補欄位，可以稍後重試。"
    if code == "pi_provider_error":
        return "這次模型服務呼叫失敗，還沒有建立新的確認或執行變更。請稍後再試。"
    if code == "pi_tool_schema_invalid":
        return "Pi 這次產生的工具參數格式無法安全處理，沒有建立確認或執行變更。請直接重試原需求，不需要重新補欄位。"
    if has_pending_interaction:
        return "Pi 這次未能完成文案，但已保留同一個待處理項目，請使用畫面上的卡片。"
    return "Pi 這次未能完成有效回覆，也沒有建立或執行新的變更。請直接重試原需求。"
