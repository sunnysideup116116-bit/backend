import time
from database import messages_coll

def generate_room_id(u1, u2):
    return "_".join(sorted([u1, u2]))

def save_message(room_id, sender_id, content):
    msg = {
        "room_id": room_id,
        "sender_id": sender_id,
        "content": content,
        "timestamp": time.time()
    }
    messages_coll.insert_one(msg)
    return msg
