from db_setup.apply_20260905_risk_repair import intervention_from_message


def test_intervention_recovery_uses_explicit_directive_targets():
    message = {
        "room_id": "alice_bob",
        "sender_id": "ai_assistant",
        "timestamp": 1788537773.5,
        "metadata": {
            "risk": {
                "level": "blocked",
                "triggered_by_msg_id": "risk-1",
                "sender_directive": {
                    "action": "block_message",
                    "target_user_id": "alice",
                    "cooldown_seconds": 1800,
                },
                "receiver_directive": {
                    "action": "show_blocked_notice",
                    "target_user_id": "bob",
                    "content": {"primary_risk_type": "coercion"},
                },
            }
        },
    }

    result = intervention_from_message(
        message,
        {"risk_state": {"coercion": 1.0}},
    )

    assert result is not None
    assert result["sender_id"] == "alice"
    assert result["receiver_id"] == "bob"
    assert result["cooldown_seconds"] == 1800
    assert result["risk_state_snapshot"] == '{"coercion": 1.0}'


def test_intervention_recovery_skips_unattributable_legacy_system_notice():
    assert intervention_from_message({
        "room_id": "alice_bob",
        "sender_id": "ai_assistant",
        "metadata": {
            "risk": {
                "level": "blocked",
                "triggered_by_msg_id": "legacy",
                "receiver_directive": {"action": "show_blocked_notice"},
            }
        },
    }) is None
