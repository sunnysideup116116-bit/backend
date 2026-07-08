import json
from app import app, semantic_plans
from datetime import datetime, timedelta, timezone

def run_tests():
    client = app.test_client()
    session_id = "test_session_1"
    
    print("--- Test 1: Brand New Chat (Fresh Start) ---")
    data1 = {
        "sessionId": session_id,
        "messages": [
            {"sender": "A", "text": "Hello, how are you doing?"},
            {"sender": "B", "text": "I'm good, just stressed about exams."}
        ],
        "manual_ai_triggers": 0
    }
    resp1 = client.post('/api/semantic-plan', json=data1)
    plan1 = resp1.get_json()
    print("Generated Plan 1 Summary:", plan1.get("macro_summary"))
    print("Generated Plan 1 Role:", plan1.get("current_role"))
    print("$updatedAt present:", "$updatedAt" in plan1)
    
    print("\n--- Test 2: Continuation (Read-Revise-Rewrite) ---")
    # Send another request shortly after, should use revision
    data2 = {
        "sessionId": session_id,
        "messages": [
            {"sender": "A", "text": "Hello, how are you doing?"},
            {"sender": "B", "text": "I'm good, just stressed about exams."},
            {"sender": "A", "text": "I understand, exams are tough. Have you tried studying in groups?"},
            {"sender": "B", "text": "No, I usually study alone. Maybe I should try it."}
        ],
        "manual_ai_triggers": 0
    }
    resp2 = client.post('/api/semantic-plan', json=data2)
    plan2 = resp2.get_json()
    print("Generated Plan 2 Summary:", plan2.get("macro_summary"))
    
    print("\n--- Test 3: Context Decay (> 12 hours later) ---")
    # Manually backdate the $updatedAt in the DB by 13 hours to simulate decay
    old_time = datetime.now(timezone.utc) - timedelta(hours=13)
    semantic_plans[session_id]['$updatedAt'] = old_time.isoformat()
    
    data3 = {
        "sessionId": session_id,
        "messages": [
            {"sender": "A", "text": "Hey, it's been a while, how did the exams go?"}
        ],
        "manual_ai_triggers": 0
    }
    resp3 = client.post('/api/semantic-plan', json=data3)
    plan3 = resp3.get_json()
    print("Generated Plan 3 Summary (should be fresh start):", plan3.get("macro_summary"))

    print("\n--- Test 4: Agent 2 Generate Suggestion ---")
    data4 = {
        "sessionId": session_id,
        "messages": [
            {"sender": "A", "text": "Hey, it's been a while, how did the exams go?"},
            {"sender": "B", "text": "Honestly, they were brutally hard. I'm feeling really burnt out right now."}
        ]
    }
    resp4 = client.post('/api/generate-suggestion', json=data4)
    suggestion4 = resp4.get_json()
    print("Agent 2 Suggestion:")
    print(json.dumps(suggestion4, indent=2))

if __name__ == '__main__':
    run_tests()
