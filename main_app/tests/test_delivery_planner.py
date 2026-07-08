import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.delivery_planner import plan_delivery, DeliveryPlan

MSG = {"sender_id": "s1", "content": "hi", "timestamp": 1.0, "_id": "m1"}

def _ra(level, with_cmd=True):
    cmd = {"triggered_by_msg_id": "tbm_123"} if with_cmd else None
    return {"risk_level": level, "intervention_command": cmd}

def test_blocked_intercepts_no_tbm():
    p = plan_delivery("blocked", _ra("blocked"), MSG)
    assert p.deliver_original is False
    assert p.triggered_by_msg_id is None
    assert p.response["is_blocked"] is True
    assert p.response["warning_msg"]
    assert p.response["ui_priority"] == "risk"

def test_restricted_delivers_original_with_tbm():
    p = plan_delivery("restricted", _ra("restricted"), MSG)
    assert p.deliver_original is True
    assert p.triggered_by_msg_id == "tbm_123"
    assert p.response["is_blocked"] is False
    assert p.response["message"] == MSG
    assert p.response["ui_priority"] == "risk"

def test_warning_delivers_original_with_tbm():
    p = plan_delivery("warning", _ra("warning"), MSG)
    assert p.deliver_original is True
    assert p.triggered_by_msg_id == "tbm_123"
    assert p.response["ui_priority"] == "risk"

def test_observation_delivers_original_with_tbm():
    p = plan_delivery("observation", _ra("observation"), MSG)
    assert p.deliver_original is True
    assert p.triggered_by_msg_id == "tbm_123"
    assert p.response["ui_priority"] == "coach"

def test_safe_delivers_no_tbm():
    p = plan_delivery("safe", _ra("safe", with_cmd=False), MSG)
    assert p.deliver_original is True
    assert p.triggered_by_msg_id is None
    assert p.response["is_blocked"] is False
    assert p.response["ui_priority"] == "coach"

def test_missing_intervention_command_treats_no_tbm():
    p = plan_delivery("restricted", {"risk_level": "restricted"}, MSG)
    assert p.triggered_by_msg_id is None  # 容錯：缺指令就不帶 tbm
    assert p.deliver_original is True