"""Shared public-reply presentation and safety checks for V3.

This module does not classify user intent.  It only validates model-produced
user-facing prose after Planner/Synthesizer semantic decisions have already
been made.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

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


def validate_public_reply(
    value: str | None,
    *,
    preserve_details: bool = False,
    reject_internal_identifiers: bool = False,
    reject_structured_output: bool = False,
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
    concise = _concise_public_reply(normalized, preserve_details=preserve_details)
    if not concise:
        return PublicReplyValidation(None, "empty_reply")
    if _INTERNAL_META_REPLY_RE.search(concise):
        return PublicReplyValidation(None, "internal_meta_reply")
    if reject_internal_identifiers and _INTERNAL_IDENTIFIER_RE.search(concise):
        return PublicReplyValidation(None, "internal_identifier")
    if contains_unsupported_random_match_claim(concise):
        return PublicReplyValidation(None, "unsupported_claim")
    return PublicReplyValidation(concise)
