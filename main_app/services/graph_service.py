from typing import List, Dict, Any
import time
from database import knowledge_graph_edges_coll, neo4j_driver
from config import NEO4J_DATABASE

ALLOWED_PREDICATES = {
    "IS_A", "HAS", "LIKES", "DISLIKES", "WANTS", "FEELS", "KNOWS", 
    "USES", "BELIEVES", "AGREES_WITH", "DISAGREES_WITH", "MENTIONED", "IS_INTERESTED_IN"
}

def normalize_triple(triple: dict) -> dict:
    """Normalize triple fields for consistent comparison."""
    return {
        "subject": triple.get("subject", "").strip().lower(),
        "predicate": triple.get("predicate", "").strip().upper(),
        "object": triple.get("object", "").strip().lower(),
        "significance_score": triple.get("significance_score", 0),
        "reasoning": triple.get("reasoning", "")
    }

def process_triples(room_id: str, extracted_triples: List[dict]) -> int:
    """
    Process new triples: normalize, filter by allowed predicates,
    deduplicate against existing Neo4j DB (and MongoDB), store new ones,
    and return the count of newly added triples.
    """
    new_triple_count = 0
    if not extracted_triples:
        return new_triple_count
        
    valid_triples = []
    for raw_triple in extracted_triples:
        triple = normalize_triple(raw_triple)
        if triple["predicate"] in ALLOWED_PREDICATES:
            valid_triples.append(triple)
            
    if not valid_triples:
        return 0

    # Parse room_id to get users. Fallback to just assigning them if parsing fails.
    parts = room_id.split("_")
    u1_id = parts[0] if len(parts) > 0 else "user1"
    u2_id = parts[1] if len(parts) > 1 else "user2"
    
    new_triples = []
    
    # --- TEMPORARY DEBUG: Print triples and bypass DB insertion ---
    print(f"\n=======================================================")
    print(f"🧠 EXTRACTED GRAPH TRIPLES FOR ROOM: {room_id}")
    print(f"=======================================================")
    for t in valid_triples:
        print(f"  [{t['subject']}] - ({t['predicate']}) -> [{t['object']}] (Score: {t['significance_score']})")
    print(f"=======================================================\n")
    
    # return len(valid_triples) # Early return to prevent any Neo4j/Mongo DB insertion for now
    # --------------------------------------------------------------
    
    if neo4j_driver:
        with neo4j_driver.session(database=NEO4J_DATABASE) as session:
            # 1. Filter out duplicates existing in this ChatRoom
            for t in valid_triples:
                query = """
                MATCH (c:ChatRoom {id: $room_id})-[:HAS_BATCH]->(:Batch)-[:HAS_TRIPLE|NEXT_TRIPLE*]->(existing:Triple)
                WHERE existing.subject = $subj AND existing.predicate = $pred AND existing.object = $obj
                RETURN existing LIMIT 1
                """
                res = session.run(query, room_id=room_id, subj=t["subject"], pred=t["predicate"], obj=t["object"])
                if not res.single():
                    new_triples.append(t)
            
            if not new_triples:
                return 0
                
            # 2. Build the graph topology for new triples
            batch_id = f"batch_{room_id}_{int(time.time()*1000)}"
            
            # Setup Users, ChatRoom, and Batch
            setup_query = """
            MATCH (u1:User {user_id: $u1_id})
            MATCH (u2:User {user_id: $u2_id})
            MERGE (c:ChatRoom {id: $room_id})
            MERGE (u1)-[:PARTICIPATES_IN]->(c)
            MERGE (u2)-[:PARTICIPATES_IN]->(c)
            CREATE (b:Batch {id: $batch_id, timestamp: timestamp()})
            MERGE (c)-[:HAS_BATCH]->(b)
            """
            session.run(setup_query, u1_id=u1_id, u2_id=u2_id, room_id=room_id, batch_id=batch_id)
            
            # Create linked list of triples
            query_parts = [f"MATCH (b:Batch {{id: $batch_id}})"]
            params = {"batch_id": batch_id}
            
            prev_node = "b"
            for i, t in enumerate(new_triples):
                node_var = f"t{i}"
                params[f"s_{i}"] = t["subject"]
                params[f"p_{i}"] = t["predicate"]
                params[f"o_{i}"] = t["object"]
                params[f"sc_{i}"] = t["significance_score"]
                params[f"r_{i}"] = t["reasoning"]
                
                query_parts.append(
                    f"CREATE ({node_var}:Triple {{subject: $s_{i}, predicate: $p_{i}, object: $o_{i}, score: $sc_{i}, reasoning: $r_{i}}})"
                )
                if i == 0:
                    query_parts.append(f"CREATE ({prev_node})-[:HAS_TRIPLE]->({node_var})")
                else:
                    query_parts.append(f"CREATE ({prev_node})-[:NEXT_TRIPLE]->({node_var})")
                
                prev_node = node_var
                
            full_query = "\\n".join(query_parts)
            session.run(full_query, **params)
            
            new_triple_count = len(new_triples)
    else:
        # Fallback to pure MongoDB if Neo4j is offline/unconfigured
        for t in valid_triples:
            existing = knowledge_graph_edges_coll.find_one({
                "room_id": room_id,
                "subject": t["subject"],
                "predicate": t["predicate"],
                "object": t["object"]
            })
            if not existing:
                new_triples.append(t)
                
        if not new_triples:
            return 0
            
        new_triple_count = len(new_triples)
        
    # Also save to MongoDB for UI compatibility if needed (since guidance_service reads it for provenance)
    for t in new_triples:
        edge_doc = {
            "room_id": room_id,
            "subject": t["subject"],
            "predicate": t["predicate"],
            "object": t["object"],
            "significance_score": t["significance_score"],
            "reasoning": t["reasoning"],
            "source_message_ids": [],
            "created_at": time.time(),
            "updated_at": time.time()
        }
        knowledge_graph_edges_coll.insert_one(edge_doc)
            
    return new_triple_count
