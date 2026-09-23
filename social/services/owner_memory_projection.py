"""Bounded owner preference wording; cache freshness stays outside Context Builder."""
from services.language_service import normalize_zh_tw
from services.profile_projection import contains_internal_identifier, contains_protected_content
from matchmaker_agent.concept_identity import (
    PreferenceTextError, normalize_preference_text, stored_concept_identity, is_v2_preference_key,
)

STANCE_LABELS = {"like": "喜歡", "dislike": "不喜歡", "avoid": "避免", "require": "需要"}


def preference_wording(items, *, owner_id=None, limit=8):
    result = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            # Old untyped entries have no trustworthy polarity.
            continue
        if owner_id and item.get("owner_user_id") not in (None, "", owner_id):
            continue
        stance = STANCE_LABELS.get(item.get("stance"))
        if not stance or item.get("active") is False:
            continue
        if item.get("canonicalization_version") == "v2" or is_v2_preference_key(item.get("key")):
            identity = stored_concept_identity(item)
            if not identity:
                continue
            # Owner context is semantic evidence, not a display label. Never
            # reintroduce a qualifier-losing prefix before the model sees it.
            label = identity.semantic_text
        else:
            # Existing legacy owner wording remains a display/read projection;
            # no key/version/ownership is inferred or written from this text.
            try:
                label = normalize_preference_text(item.get("label") or item.get("label_zh_tw") or "")
                label = normalize_preference_text(normalize_zh_tw(label))
            except PreferenceTextError:
                continue
        if not label or contains_internal_identifier(label) or contains_protected_content(label):
            continue
        text = f"{stance}：{label}"
        if text not in result:
            result.append(text)
        if len(result) >= max(1, min(limit, 8)):
            break
    return result
