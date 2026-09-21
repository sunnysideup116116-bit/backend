from types import SimpleNamespace
from fastapi import BackgroundTasks, HTTPException
from starlette.requests import Request
import pytest


@pytest.fixture
def gateway(monkeypatch):
    from routers import public_chat
    from services import voice_chat_status
    import agent_quota.api
    monkeypatch.setattr(public_chat, 'find_accepted_match', lambda *args: {'_id': 'match'})
    monkeypatch.setattr(public_chat.risk_block_service, 'is_pair_blocked', lambda *args: False)
    monkeypatch.setattr(agent_quota.api, 'owner_from_request', lambda request: 'owner')
    monkeypatch.setattr(voice_chat_status, 'read_delivery_receipt', lambda *args: None)
    def unexpected(*args, **kwargs): raise AssertionError('must not evaluate or persist')
    monkeypatch.setattr(public_chat.pair_message_risk_gate, 'evaluate', unexpected)
    monkeypatch.setattr(public_chat, 'save_pair_owner_message_once', unexpected)
    from models import DirectChatRequest
    request = DirectChatRequest(user_id='owner', contact_id='friend', message='合成測試', client_message_id='voice-test')
    def invoke():
        return public_chat.direct_chat(request, BackgroundTasks(), Request({'type': 'http', 'headers': []}))
    return public_chat, voice_chat_status, invoke


@pytest.mark.parametrize('state,status,code', [('active', 409, 'risk_cooldown'), ('unknown', 503, 'risk_unavailable')])
def test_voice_send_does_not_bypass_active_or_unknown_cooldown(gateway, monkeypatch, state, status, code):
    _, service, invoke = gateway
    monkeypatch.setattr(service, 'read_cooldown', lambda *args: {'state': state})
    with pytest.raises(HTTPException) as result: invoke()
    assert result.value.status_code == status
    assert result.value.detail['code'] == code
    assert result.value.detail['delivery']['state'] == 'not_sent'


def test_replayed_voice_attempt_returns_receipt_without_risk_or_persistence(gateway, monkeypatch):
    _, service, invoke = gateway
    monkeypatch.setattr(service, 'read_delivery_receipt', lambda *args: {
        'state': 'delivered', 'risk_assessment': {'level': 'warning', 'delivery': 'delivered'}})
    monkeypatch.setattr(service, 'read_cooldown', lambda *args: {'state': 'active'})
    result = invoke()
    assert result['duplicate'] is True
    assert result['is_blocked'] is False
    assert result['cooldown']['state'] == 'active'


def test_voice_attempt_owner_cannot_be_spoofed(gateway, monkeypatch):
    import agent_quota.api
    monkeypatch.setattr(agent_quota.api, 'owner_from_request', lambda request: 'other')
    with pytest.raises(HTTPException) as result: gateway[2]()
    assert result.value.status_code == 403
