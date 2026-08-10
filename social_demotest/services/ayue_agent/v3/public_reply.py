"""Shared public-reply presentation and safety checks for V3.

This module does not classify user intent.  It only validates model-produced
user-facing prose after Planner/Synthesizer semantic decisions have already
been made.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

import config
from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.ayue_agent.capabilities import (
    contains_unsupported_random_match_claim,
    normalize_public_language,
)
from services.ayue_agent.router import _INTERNAL_META_REPLY_RE, _concise_public_reply
from services.language_service import normalize_public_reply


_INTERNAL_IDENTIFIER_RE = re.compile(
    r"(?:seed_user_[A-Za-z0-9_-]+|\b(?:user_id|event_id|match_id|revision)\b|"
    r"\b[0-9a-f]{32}\b|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PublicReplyValidation:
    reply: str | None
    reason: str | None = None


PresentationClass = Literal[
    "conversation", "social_opportunity", "product_info", "transaction",
    "capability", "fallback", "onboarding", "grounded_recommendation",
]


def public_place_cards_enabled() -> bool:
    """Return whether the public UI may render place-card projections.

    This presentation switch intentionally does not disable Places retrieval
    or the server-owned candidate/ref projection used by Web and Synthesizer.
    """
    return bool(getattr(config, "AYUE_PUBLIC_PLACE_CARDS_ENABLED", False))


class AyueReplyPresentation(BaseModel):
    """Typed, bounded multi-bubble presentation for one public turn."""

    model_config = ConfigDict(extra="forbid")
    presentation_class: PresentationClass
    messages: list[str] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def _validate_messages(self) -> "AyueReplyPresentation":
        limits = {
            "conversation": (1, 2, 160, 3),
            "social_opportunity": (1, 2, 200, 4),
            "product_info": (1, 2, 240, 5),
            "transaction": (1, 1, 240, 5),
            "capability": (1, 1, 160, 3),
            "fallback": (1, 1, 160, 3),
            "onboarding": (3, 3, 240, 5),
            # A generous but finite editorial envelope for Web/Places
            # discovery. Length is presentation budget, not a domain state.
            "grounded_recommendation": (1, 3, 1400, 28),
        }
        low, high, max_chars, max_sentences = limits[self.presentation_class]
        if not low <= len(self.messages) <= high:
            raise ValueError("invalid message count for presentation class")
        normalized: list[str] = []
        for message in self.messages:
            validation = validate_public_reply(
                message,
                preserve_details=self.presentation_class in {
                    "product_info", "transaction", "onboarding",
                    "grounded_recommendation",
                },
                reject_internal_identifiers=True,
                reject_structured_output=True,
                max_chars=max_chars,
                max_sentences=max_sentences,
            )
            if validation.reply is None or len(validation.reply) > max_chars:
                raise ValueError(f"invalid presentation message: {validation.reason or 'too_long'}")
            if validation.reply in normalized:
                raise ValueError("duplicate presentation message")
            normalized.append(validation.reply)
        if (
            self.presentation_class == "grounded_recommendation"
            and sum(len(message) for message in normalized) > 3_600
        ):
            raise ValueError("grounded recommendation envelope too long")
        self.messages = normalized
        return self


def build_presentation(
    messages: list[str] | tuple[str, ...],
    presentation_class: PresentationClass,
) -> AyueReplyPresentation | None:
    """Validate an entire presentation atomically; invalid output is rejected."""
    try:
        return AyueReplyPresentation(
            presentation_class=presentation_class,
            messages=list(messages),
        )
    except Exception:
        return None


def validate_public_reply(
    value: str | None,
    *,
    preserve_details: bool = False,
    reject_internal_identifiers: bool = False,
    reject_structured_output: bool = False,
    max_chars: int | None = None,
    max_sentences: int | None = None,
) -> PublicReplyValidation:
    """Normalize and validate one human-facing model reply.

    Direct Planner and general-conversation Synthesizer replies use the
    ordinary Public envelope.  Grounded Synthesizer replies use
    ``preserve_details=True`` so verified observations keep their larger
    presentation limits while sharing the same language and metadata checks.
    """
    raw = str(value or "").strip()
    if not raw:
        return PublicReplyValidation(None, "empty_reply")

    if reject_structured_output:
        stripped = raw.lstrip()
        if "```" in raw or stripped.startswith("`"):
            return PublicReplyValidation(None, "structured_reply")
        if stripped.startswith(("{", "[")):
            try:
                json.loads(stripped)
            except (TypeError, ValueError):
                pass
            else:
                return PublicReplyValidation(None, "structured_reply")

    normalized = normalize_public_language(normalize_public_reply(raw))
    concise = _concise_public_reply(
        normalized,
        preserve_details=preserve_details,
        max_chars=max_chars,
        max_sentences=max_sentences,
    )
    if not concise:
        return PublicReplyValidation(None, "empty_reply")
    if _INTERNAL_META_REPLY_RE.search(concise):
        return PublicReplyValidation(None, "internal_meta_reply")
    if reject_internal_identifiers and _INTERNAL_IDENTIFIER_RE.search(concise):
        return PublicReplyValidation(None, "internal_identifier")
    if contains_unsupported_random_match_claim(concise):
        return PublicReplyValidation(None, "unsupported_claim")
    return PublicReplyValidation(concise)
