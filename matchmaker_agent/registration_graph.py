"""Insert-only registration projection; account IDs remain the graph identity."""
import hashlib
import math
import re
import time

from pydantic import BaseModel, Field
from concept_identity import (
    canonicalize_concept,
    durable_memory_limit,
    has_mixed_preference_polarity,
    split_compound_concept_label,
    split_explicit_preference_enumeration,
)

VERSION = "registration-bootstrap-v1"


class RegistrationProjection(BaseModel):
    user_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,35}$")
    name: str = Field(default="", max_length=80)
    observed_at: float = Field(ge=0, allow_inf_nan=False)


def registration_message_id(user_id):
    return "registration-interest:" + hashlib.sha256(user_id.encode()).hexdigest()


def project_identity(tx, req):
    # Taking a node write lock also serializes simultaneous registration jobs.
    row = tx.run("""
        MERGE (u:User {id:$user_id})
        SET u.registration_projection_lock=coalesce(u.registration_projection_lock,0)+1
        WITH u
        FOREACH (_ IN CASE WHEN $observed_at >= coalesce(u.name_updated_at,0)
                          THEN [1] ELSE [] END |
            SET u.name=$name,u.name_updated_at=$observed_at)
        WITH u
        OPTIONAL MATCH (u)-[r:PREFERS|AVOIDS|CURRENTLY_WANTS|MEMORY_DISABLED]->(:Concept)
        WITH u,count(r) AS memory_count
        RETURN memory_count=0 AND u.registration_seed_finished_at IS NULL AS seed_allowed
    """, user_id=req.user_id, name=req.name.strip(), observed_at=req.observed_at).single()
    return {"status": "success", "version": VERSION,
            "registration_seed_allowed": bool(row and row["seed_allowed"])}


def seed_registration(tx, user_id, message_id, memories):
    if message_id != registration_message_id(user_id):
        raise ValueError("invalid_registration_observation")
    now = time.time()
    row = tx.run("""
        MERGE (u:User {id:$user_id})
        SET u.registration_projection_lock=coalesce(u.registration_projection_lock,0)+1
        WITH u
        OPTIONAL MATCH (u)-[r:PREFERS|AVOIDS|CURRENTLY_WANTS|MEMORY_DISABLED]->(:Concept)
        WITH u,count(r) AS memory_count
        RETURN memory_count=0 AND u.registration_seed_finished_at IS NULL AS seed_allowed
    """, user_id=user_id).single()
    if not row or not row["seed_allowed"]:
        return []
    clean_by_key = {}
    memory_limit = durable_memory_limit()
    protected = re.compile(r"種族|族裔|宗教|信仰|性傾向|性別認同|疾病|政治立場|國籍|殘障|黑人|白人|穆斯林|基督教|同性戀|跨性別")
    for item in memories[:durable_memory_limit()]:
        label = str(item.get("label") or item.get("label_zh_tw") or "").strip()[:40]
        label = re.sub(r"^(?:喜歡|偏好|興趣)\s*[:：,，]?\s*", "", label).strip()
        evidence = str(item.get("evidence_span") or "").strip()[:120]
        confidence = float(item.get("confidence") or 0)
        if (item.get("stance") != "like" or not label or not evidence
                or not math.isfinite(confidence) or confidence < .9 or protected.search(label)
                or has_mixed_preference_polarity(label)
                or item.get("category") not in {"activity", "habit", "lifestyle"}):
            continue
        labels = (
            split_explicit_preference_enumeration(label, limit=memory_limit)
            or split_compound_concept_label(label, limit=memory_limit)
            or [label]
        )
        for atomic_label in labels:
            identity = canonicalize_concept(atomic_label, item.get("key"))
            if not identity:
                continue
            clean_by_key.setdefault(identity.key, {
                "key": identity.key, "label": identity.label, "stance": "like",
                "category": "interest", "confidence": confidence,
                "evidence_span": evidence, "last_seen_at": now,
            })
            if len(clean_by_key) >= memory_limit:
                break
        if len(clean_by_key) >= memory_limit:
            break
    clean = list(clean_by_key.values())
    if not clean:
        return []
    tx.run("""
        MATCH (u:User {id:$user_id})
        SET u.registration_seed_finished_at=$now
        MERGE (o:MemoryObservation {message_id:$message_id})
        ON CREATE SET o.owner_user_id=$user_id,o.created_at=$now,o.source='registration_interest'
        WITH u
        UNWIND $memories AS item
        MERGE (c:Concept {key:item.key})
        ON CREATE SET c.label=item.label,c.kind='interest'
        ON MATCH SET c.label=coalesce(c.label,item.label),c.kind=coalesce(c.kind,'interest')
        MERGE (u)-[r:PREFERS]->(c)
        ON CREATE SET r.source='registration_interest',r.evidence_span=item.evidence_span,
                      r.confidence=item.confidence,r.last_seen_at=$now
    """, user_id=user_id, message_id=message_id, memories=clean, now=now).consume()
    return clean
