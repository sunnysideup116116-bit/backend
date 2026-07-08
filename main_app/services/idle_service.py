import time
from typing import Dict, Tuple

# In-memory track of last activity per (room_id, user_id)
# This could also be persisted to the DB, but in-memory is fine for this polling implementation.
# If scale is an issue, we move it to a cache or DB collection.
user_activity: Dict[Tuple[str, str], float] = {}
user_idle_state: Dict[Tuple[str, str], dict] = {}

IDLE_THRESHOLD_SECONDS = 300 # 5 minutes

IDLE_NOTIFICATION_BY_ROLE = {
    "FRIEND": "[AI Copilot] They seem away for a moment. I'll keep things light and help you brainstorm a warm re-entry.",
    "FACILITATOR": "[AI Copilot] They appear idle. I'll pause the flow and suggest low-pressure ways to continue.",
    "ADVISER": "[AI Copilot] They seem away. I'll hold the thread here and help you prepare a clear next move.",
    "MENTOR": "[AI Copilot] They're idle for now. I'll step back and offer reflective prompts while you wait."
}

def update_activity(room_id: str, user_id: str):
    """Called when user sends a message or types. Returns welcome back draft if they were idle."""
    key = (room_id, user_id)
    user_activity[key] = time.time()
    
    welcome_back_draft = None
    if key in user_idle_state and user_idle_state[key].get("is_idle"):
        # Clear idle state
        welcome_back_draft = user_idle_state[key].get("returning_user_welcome_back_draft")
        user_idle_state[key]["is_idle"] = False
        user_idle_state[key]["notified"] = False
        
    return welcome_back_draft

def check_idle_state(room_id: str, user_id: str, current_role: str) -> dict:
    """
    Called by the polling /status endpoint to evaluate idle state.
    Returns the idle info to be returned to the frontend.
    """
    key = (room_id, user_id)
    now = time.time()
    last_act = user_activity.get(key, now) # default to now if no activity recorded yet
    
    # Initialize state if not present
    if key not in user_idle_state:
        user_idle_state[key] = {"is_idle": False, "notified": False}
        
    state = user_idle_state[key]
    
    if now - last_act >= IDLE_THRESHOLD_SECONDS:
        if not state["is_idle"]:
            state["is_idle"] = True
            state["idle_started_at"] = now
            state["persona_role_at_idle"] = current_role
            state["notified"] = False # Flag to ensure idempotency of system notification
            
            # Generate drafts based on role (simple mock mapping here, can be expanded or LLM generated)
            role_prompts = {
                "FRIEND": ("How are things?", "Hey, glad you're back!"),
                "ADVISER": ("Have you considered the next steps?", "Welcome back. Let's resume our plan."),
                "MENTOR": ("What are your thoughts on this?", "Reflect on what we discussed and continue."),
                "FACILITATOR": ("Any other topics to cover?", "Ready to continue?")
            }
            drafts = role_prompts.get(current_role, role_prompts["FRIEND"])
            state["waiting_user_draft"] = drafts[0]
            state["returning_user_welcome_back_draft"] = drafts[1]
            state["system_notification_text"] = IDLE_NOTIFICATION_BY_ROLE.get(current_role, IDLE_NOTIFICATION_BY_ROLE["FRIEND"])
    else:
        state["is_idle"] = False
        state["notified"] = False
        state["system_notification_text"] = ""
        
    return state

def set_idle_notified(room_id: str, user_id: str):
    """Mark the notification as sent so it's idempotent."""
    key = (room_id, user_id)
    if key in user_idle_state:
        user_idle_state[key]["notified"] = True

def check_boundary_guard(message: str) -> str:
    """
    If either user asks the AI for sensitive profiling, trauma, or unshared partner background data, decline.
    """
    sensitive_keywords = ["trauma", "abuse", "secret", "profile data", "background data", "diagnose"]
    if any(k in message.lower() for k in sensitive_keywords):
        return "Warning: I cannot share sensitive profile information, diagnose trauma, or reveal unshared background data about your partner. Let's keep our conversation respectful and within healthy boundaries."
    return ""
