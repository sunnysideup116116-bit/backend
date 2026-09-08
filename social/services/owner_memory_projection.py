"""Bounded owner preference wording; cache freshness stays outside Context Builder."""
from services.language_service import normalize_zh_tw
from services.profile_projection import contains_internal_identifier, contains_protected_content

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
        label = normalize_zh_tw(str(item.get("label") or item.get("label_zh_tw") or ""), max_length=60).strip()
        if not label or contains_internal_identifier(label) or contains_protected_content(label):
            continue
        text = f"{stance}：{label}"
        if text not in result:
            result.append(text)
        if len(result) >= max(1, min(limit, 8)):
            break
    return result
