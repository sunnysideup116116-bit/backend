from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any


TAIWAN_CITIES = (
    "台北市", "新北市", "桃園市", "台中市", "台南市", "高雄市", "基隆市",
    "新竹市", "嘉義市", "新竹縣", "苗栗縣", "彰化縣", "南投縣", "雲林縣",
    "嘉義縣", "屏東縣", "宜蘭縣", "花蓮縣", "台東縣", "澎湖縣", "金門縣",
    "連江縣",
)
VOICE_FIELDS = ("nickname", "email", "gender", "phone", "age", "region", "interest")
CONFIRM_FIELDS = {"nickname", "email", "phone"}
WARNING_CODES = {
    "password_spoken",
    "district_not_stored",
    "ambiguous_value",
    "unsupported_language",
}
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_PHONE_RE = re.compile(r"^09\d{8}$")
_SPOKEN_PASSWORD_RE = re.compile(
    r"((?:我的)?(?:密碼|密码)\s*(?:是|為|为|叫|[:：])?\s*)[^\s，,。；;]{1,128}",
    re.IGNORECASE,
)
_ENGLISH_PASSWORD_RE = re.compile(
    r"(\bpassword\s*(?:is|=|:)?\s*)[^\s,.;]{1,128}",
    re.IGNORECASE,
)


def safe_form(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    result: dict[str, Any] = {}
    for field in VOICE_FIELDS:
        item = source.get(field)
        if item is None:
            result[field] = None
        elif field == "age":
            try:
                result[field] = int(item)
            except (TypeError, ValueError):
                result[field] = None
        else:
            result[field] = str(item)[:500]
    return result


def _clean_text(value: Any, limit: int) -> str:
    # Keep human-facing Traditional Chinese punctuation intact.  Identifier-like
    # fields (email, phone, region) apply NFKC explicitly at their boundary.
    text = unicodedata.normalize("NFC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def redact_sensitive_transcript(value: Any) -> str:
    """Hide spoken password values before a transcript reaches Flutter."""

    text = str(value or "")
    text = _SPOKEN_PASSWORD_RE.sub(r"\1[已隱藏]", text)
    return _ENGLISH_PASSWORD_RE.sub(r"\1[hidden]", text)


def _normalize_phone(value: Any) -> str:
    phone = unicodedata.normalize("NFKC", str(value or ""))
    phone = re.sub(r"[\s\-().]", "", phone)
    if phone.startswith("+8869"):
        phone = "0" + phone[4:]
    elif phone.startswith("8869"):
        phone = "0" + phone[3:]
    return phone


def _normalize_region(value: Any) -> tuple[str | None, bool]:
    text = _clean_text(value, 80).replace("臺", "台")
    if text in TAIWAN_CITIES:
        return text, False
    for city in TAIWAN_CITIES:
        stem = city.removesuffix("市").removesuffix("縣")
        if city in text or (len(stem) >= 2 and stem in text):
            has_district = text != city and text != stem
            return city, has_district
    return None, False


@dataclass(frozen=True)
class PatchDecision:
    base_revision: int
    changes: dict[str, Any]
    rejected: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]
    stale: bool = False


def validate_function_patch(args: Any, *, current_revision: int) -> PatchDecision:
    payload = args if isinstance(args, dict) else {}
    try:
        base_revision = int(payload.get("base_revision"))
    except (TypeError, ValueError):
        base_revision = -1
    if base_revision != current_revision:
        return PatchDecision(
            base_revision=base_revision,
            changes={},
            rejected=(),
            warnings=("stale_revision",),
            stale=True,
        )

    changes: dict[str, Any] = {}
    rejected: list[dict[str, Any]] = []
    warnings = [
        str(code)
        for code in payload.get("warning_codes", [])
        if str(code) in WARNING_CODES
    ] if isinstance(payload.get("warning_codes"), list) else []

    if "password" in payload:
        warnings.append("password_spoken")

    if "nickname" in payload:
        nickname = _clean_text(payload["nickname"], 80)
        if nickname:
            changes["nickname"] = nickname
        else:
            rejected.append({"field": "nickname", "code": "empty_value"})

    if "email" in payload:
        email = unicodedata.normalize(
            "NFKC", _clean_text(payload["email"], 254),
        ).replace(" ", "")
        if _EMAIL_RE.fullmatch(email):
            local, domain = email.rsplit("@", 1)
            changes["email"] = f"{local}@{domain.lower()}"
        else:
            rejected.append({"field": "email", "code": "invalid_email"})

    if "gender" in payload:
        gender = _clean_text(payload["gender"], 16).lower()
        aliases = {"男": "male", "男性": "male", "女": "female", "女性": "female"}
        gender = aliases.get(gender, gender)
        if gender in {"male", "female"}:
            changes["gender"] = gender
        else:
            rejected.append({"field": "gender", "code": "unsupported_gender"})

    if "phone" in payload:
        phone = _normalize_phone(payload["phone"])
        if _PHONE_RE.fullmatch(phone):
            changes["phone"] = phone
        else:
            rejected.append({"field": "phone", "code": "invalid_phone"})

    if "age" in payload:
        try:
            age = int(payload["age"])
        except (TypeError, ValueError):
            age = -1
        if 18 <= age <= 120:
            changes["age"] = age
        else:
            rejected.append({"field": "age", "value": age, "code": "age_out_of_range"})

    if "region" in payload:
        region, had_district = _normalize_region(payload["region"])
        if region:
            changes["region"] = region
            if had_district:
                warnings.append("district_not_stored")
        else:
            rejected.append({"field": "region", "code": "unsupported_region"})

    if "interest" in payload:
        interest = _clean_text(payload["interest"], 500)
        if interest:
            changes["interest"] = interest
        else:
            rejected.append({"field": "interest", "code": "empty_value"})

    clear_fields = payload.get("clear_fields")
    if isinstance(clear_fields, list):
        for field in clear_fields:
            if field in VOICE_FIELDS:
                changes[str(field)] = None

    return PatchDecision(
        base_revision=base_revision,
        changes=changes,
        rejected=tuple(rejected),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def requires_confirmation(changes: dict[str, Any]) -> list[str]:
    return [field for field in VOICE_FIELDS if field in changes and field in CONFIRM_FIELDS]
