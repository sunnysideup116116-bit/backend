import time
import uuid
import threading
from database import audit_logs_coll

def write_audit_log_async(audit_doc: dict):
    """Asynchronous write to audit_logs_coll"""
    def task():
        try:
            audit_logs_coll.insert_one(audit_doc)
        except Exception as e:
            print(f"Failed to write audit log: {e}")
    
    threading.Thread(target=task, daemon=True).start()

def build_provenance_and_dto(
    room_id: str,
    nudge_type: str, 
    nudge_text: str, 
    var_role: str,
    var_strategy: str,
    var_fact: str,
    var_graph_edge: str,
    var_model: str,
    var_t_invoke_ms: float
) -> dict:
    
    suggestion_id = f"sug_{uuid.uuid4().hex[:8]}"
    
    # Construct quick trail string
    edge_text = f" regarding {var_graph_edge}" if var_graph_edge and var_graph_edge != "None" else ""
    quick_trail_string = (
        f"Operating under the {var_role} framework, I used the {var_fact} context "
        f"and the plan to {var_strategy} to suggest a {nudge_type}{edge_text}."
    )
    
    # Construct W3C-style provenance JSON
    audit_doc = {
        "suggestion_id": suggestion_id,
        "room_id": room_id,
        "@context": "http://www.w3.org/ns/prov",
        "type": "Entity",
        "generatedAtTime": time.time(),
        "wasGeneratedBy": {
            "type": "Activity",
            "used": [
                {"type": "Entity", "role": "strategy", "value": var_strategy},
                {"type": "Entity", "role": "fact", "value": var_fact},
                {"type": "Entity", "role": "graph_edge", "value": var_graph_edge}
            ],
            "agent": {
                "type": "Agent",
                "model": var_model,
                "assigned_role": var_role,
                "latency_ms": var_t_invoke_ms
            }
        },
        "output": {
            "nudge_type": nudge_type,
            "nudge_text": nudge_text
        }
    }
    
    # Async write
    write_audit_log_async(audit_doc)
    
    # Return lean DTO
    return {
        "suggestion_id": suggestion_id,
        "nudge_type": nudge_type,
        "ui_nudge_text": nudge_text,
        "quick_trail_string": quick_trail_string
    }
