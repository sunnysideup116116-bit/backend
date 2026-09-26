"""Fact-rendered, role-bound Ayue introductions for the related-interest pilot.

No LLM may upgrade a related Concept to shared preference. Complete semantic
phrases are used or omitted as a whole; never show a clipped qualifier as fact.
"""
import re
from matchmaker_agent.related_interest_contract import POLICY, validated_evidence
from services.profile_projection import contains_internal_identifier, contains_protected_content
from services.match_reason_service import FRIEND_COPY_VERSION, COUNTERPARTY_PLACEHOLDER


def _public_phrase(text):
    if (not isinstance(text, str) or not text or len(text) > 72
            or contains_internal_identifier(text) or contains_protected_content(text)
            or re.search(r"https?://|www\.|@|[<>]|\{\{|[\r\n]|\d[\d ()+-]{6,}", text)):
        return ""
    return text


def related_entry_text(entry, *, observation=None):
    if observation is not None:
        observation["mode"] = "neutral_fallback"
    basis = entry.get("related_interest_basis") if isinstance(entry, dict) else None
    if not isinstance(basis, dict):
        return ""
    evidence = validated_evidence(basis.get("evidence"))
    first, second = basis.get("requester_id"), basis.get("candidate_id")
    viewer, other = entry.get("viewer_id"), entry.get("counterparty_id")
    if (not evidence or not isinstance(first, str) or not isinstance(second, str)
            or not first or not second or first == second or {viewer, other} != {first, second}):
        return ""
    q, c = _public_phrase(evidence["query_preference"]), _public_phrase(evidence["candidate_preference"])
    if not q or not c:
        return "我找到一位興趣方向可能相關、但不代表偏好相同的人；想先認識看看嗎？"
    confirmed = basis.get("requester_prefers_query") is True
    if viewer == first:
        facts = (f"你喜歡「{q}」，對方喜歡「{c}」。" if confirmed else
                 f"你這次想找對「{q}」有興趣的人；對方保存的興趣是「{c}」。")
    else:
        facts = (f"你喜歡「{c}」，這位朋友喜歡「{q}」。" if confirmed else
                 f"你喜歡「{c}」，這位朋友這次想找對「{q}」有興趣的人。")
    bridge = ("主題相關、參與方式不同，可以交流各自的體驗；不代表適合一起做同一活動。"
              if evidence["relation"] == "role_mismatch" else
              "這是相關而非已確認相同的偏好，可以從彼此的不同體驗聊起。")
    text = facts + bridge + "想先認識看看嗎？"
    if observation is not None and len(text) <= 220:
        observation["mode"] = "fact_bound"
    return text if len(text) <= 220 else "我找到一位興趣方向可能相關、但不代表偏好相同的人；想先認識看看嗎？"


def related_friend_intro(base, requester, candidate, evidence, *, requester_prefers_query):
    valid = [packet for item in evidence if (packet := validated_evidence(item))]
    if not valid:
        raise ValueError("related_interest_evidence_invalid")
    packet = sorted(valid, key=lambda e: (-e["similarity"], e["concept_key"]))[0]
    result = {}
    for role, viewer, other in (("initiator_preview", requester, candidate),
                               ("receiver_invitation", candidate, requester)):
        entry = {**dict((base or {}).get(role) or {}), "copy_version": FRIEND_COPY_VERSION,
            "style_id": POLICY, "viewer_id": viewer["user_id"], "counterparty_id": other["user_id"],
            "counterparty_context_snapshot": "", "counterparty_public_personality": "",
            "viewer_public_personality": "", "tier": "exploratory",
            "related_interest_basis": {"evidence": packet,
                "requester_id": requester["user_id"], "candidate_id": candidate["user_id"],
                "requester_prefers_query": requester_prefers_query is True},
            "conversation_starter": "你平常是怎麼接觸這類興趣的？",
            "accepted_opening": f"{COUNTERPARTY_PLACEHOLDER}也點頭了！可以先聊聊各自接觸這類興趣的方式。"}
        observation = {}
        entry["viewer_text"] = related_entry_text(entry, observation=observation)
        entry["reason_render_mode"] = observation["mode"]
        result[role] = entry
    return result


def related_pair_opening(match_doc, first_label, second_label, *, observation=None):
    """Post-consent copy stays fact-bound even after the pilot is switched off."""
    projection = match_doc.get("friend_intro_v4") or {}
    if not isinstance(projection, dict):
        return None
    entry = projection.get("initiator_preview") or {}
    if not isinstance(entry, dict) or entry.get("style_id") != POLICY:
        return None
    if observation is not None:
        observation["mode"] = "neutral_fallback"
    greeting = (f"{first_label}、{second_label}，" if first_label and second_label
                and "對方" not in (first_label, second_label) else "")
    neutral = f"阿月：{greeting}你們都願意認識彼此了！可以先交流各自的興趣，不必有相同的參與方式。\n最近哪一次接觸自己的興趣，讓你特別有印象？"
    if (entry.get("viewer_id") != match_doc.get("from_user")
            or entry.get("counterparty_id") != match_doc.get("to_user")
            or not related_entry_text(entry)):
        return neutral
    basis = entry["related_interest_basis"]
    if (basis["requester_id"] != match_doc.get("from_user")
            or basis["candidate_id"] != match_doc.get("to_user")):
        return neutral
    packet = validated_evidence(basis["evidence"])
    q, c = _public_phrase(packet["query_preference"]), _public_phrase(packet["candidate_preference"])
    if not q or not c or not first_label or not second_label:
        return neutral
    first = (f"{first_label}喜歡「{q}」" if basis.get("requester_prefers_query") is True else
             f"{first_label}這次想找對「{q}」有興趣的人")
    bridge = ("主題相關但參與方式不同，不代表適合一起做同一活動。" if packet["relation"] == "role_mismatch"
              else "這次是從相關興趣牽線，不代表你們有完全相同的偏好。")
    if observation is not None:
        observation["mode"] = "fact_bound"
    return f"阿月：{first}；{second_label}喜歡「{c}」。{bridge}\n最近哪一次接觸自己的興趣，讓你特別有印象？"
