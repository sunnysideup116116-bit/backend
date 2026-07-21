import time
from database import messages_coll

def generate_room_id(u1, u2):
    return "_".join(sorted([u1, u2]))

def save_message(room_id, sender_id, content, message_type="text", metadata=None, risk_assessment=None, is_blocked=False, delivery_status="delivered"):
    msg = {
        "room_id": room_id,
        "sender_id": sender_id,
        "content": content,
        "message_type": message_type,
        "metadata": metadata or {},
        "risk_assessment": risk_assessment,
        "is_blocked": is_blocked,
        "delivery_status": delivery_status,
        "timestamp": time.time()
    }
    messages_coll.insert_one(msg)
    return msg
