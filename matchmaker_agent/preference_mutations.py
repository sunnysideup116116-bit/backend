"""Shared validated Identity-v2 edge write, called inside an owner-fenced tx."""

def write_preference_edges(tx, owner, memories):
    # Both callers validate existing Concept identities in this same tx before
    # their idempotency marker/plan; do not repeat that indexed query here.
    tx.run("""
        UNWIND $memories AS item
        MERGE (u:User {id:$user_id})
        MERGE (c:Concept {key:item.key})
        ON CREATE SET c.label=item.semantic_text,c.kind='preference',
                      c.semantic_text=item.semantic_text,c.display_label=item.display_label,
                      c.canonicalization_version=item.canonicalization_version,
                      c.semantic_input_hash=item.semantic_input_hash,c.fidelity_status='complete'
        WITH u,c,item
        OPTIONAL MATCH (u)-[old:PREFERS|AVOIDS|CURRENTLY_WANTS]->(c)
        DELETE old
        WITH u,c,item
        FOREACH (_ IN CASE WHEN item.stance IN ['dislike','avoid'] THEN [1] ELSE [] END |
            MERGE (u)-[:AVOIDS]->(c))
        FOREACH (_ IN CASE WHEN item.stance IN ['like','require'] THEN [1] ELSE [] END |
            MERGE (u)-[:PREFERS]->(c))
    """, user_id=owner, memories=memories).consume()
