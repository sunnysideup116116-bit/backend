import math

AGENT1_MAX_PROMPT_TOKENS = 6000
AGENT1_STATIC_PROMPT_TOKEN_ESTIMATE = 1800
AGENT1_MIN_BUFFER_MESSAGES = 3
AGENT1_MAX_BUFFER_MESSAGES = 30
DEFAULT_DYNAMIC_THRESHOLD = 1850.0

def estimate_tokens(text: str) -> int:
    """Roughly estimate tokens based on character count. A common rule of thumb is 1 token ~ 3 to 4 chars for English/code, and roughly 1-2 chars for Chinese."""
    return max(1, len(text) // 2)

def should_trigger_agent1(buffered_messages: list, current_threshold: float) -> bool:
    """Determine if Agent 1 should trigger based on estimated token volume and message limits."""
    msg_count = len(buffered_messages)
    
    if msg_count < AGENT1_MIN_BUFFER_MESSAGES:
        return False
        
    if msg_count >= AGENT1_MAX_BUFFER_MESSAGES:
        return True
        
    buffered_message_tokens = sum(estimate_tokens(m.get('content', '')) for m in buffered_messages)
    total_estimated_tokens = AGENT1_STATIC_PROMPT_TOKEN_ESTIMATE + buffered_message_tokens
    
    return total_estimated_tokens >= current_threshold

def adjust_dynamic_threshold(current_threshold: float, new_triple_count: int) -> float:
    """
    Rubber band rule:
    Low/no new triples: increase token threshold, so Agent 1 runs less often.
    High number of significant new triples: decrease token threshold, so Agent 1 runs sooner.
    Clamp threshold between safe min/max.
    """
    if new_triple_count == 0:
        new_threshold = current_threshold * 1.25
    elif new_triple_count >= 4:
        new_threshold = current_threshold * 0.75
    else:
        new_threshold = current_threshold * 1.0
        
    # Clamp between STATIC and MAX
    new_threshold = max(AGENT1_STATIC_PROMPT_TOKEN_ESTIMATE + 200, min(new_threshold, AGENT1_MAX_PROMPT_TOKENS))
    return float(new_threshold)
