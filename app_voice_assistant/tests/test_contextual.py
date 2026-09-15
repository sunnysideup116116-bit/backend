import asyncio
import json
from types import SimpleNamespace

import pytest
from google.genai import types

from app_voice_assistant.capabilities import ACTIONS
from app_voice_assistant.contextual import bind_target, safe_result
from app_voice_assistant.contracts import safe_context, validate_proposal
from app_voice_assistant.duplex_session import _live_tools
from app_voice_assistant.duplex_runtime import run_duplex_session
from .test_duplex_runtime import FakeLive, FakeWebSocket, FakeProvider, FakeLimiter, message, wait_until


def screen_context(kind='contact', **permissions):
    action = {'contact': 'chat.request_send', 'calendar_event': 'calendar.update',
              'match_invitation': 'ayue.public_query'}[kind]
    items = [{'ref': f'surface-1-2-{i}', 'kind': kind, 'label': '小安' if kind == 'contact' else '晚餐',
              'actions': [action, 'ui.target.select'],
              'attributes': {'status': 'confirmed', 'date': '2026-09-13', 'user_id': 'secret'},
              'user_id': f'private-id-{i}'} for i in (1, 2)]
    return safe_context({'scope': {'contact':'chat', 'calendar_event':'calendar', 'match_invitation':'match_hub'}[kind], 'revision': 2,
                         'permissions': {'screen_read': True, 'chat_list': True, 'chat_send': True,
                                         'calendar_read': True, 'calendar_write': True,
                                         'match_read': True, 'public_ayue': True, 'match_actions': True, **permissions},
                         'screen': {'surface_id': 'surface-1', 'ready': True, 'items': items,
                                    'selected_ref': items[1]['ref'], 'available_actions': [action, 'ui.target.select']}})


def test_context_redacts_ids_and_respects_each_data_permission():
    context = screen_context()
    assert 'secret' not in json.dumps(context)
    assert 'private-id' not in json.dumps(context)
    assert screen_context(chat_list=False)['screen']['items'] == []
    assert screen_context(chat_list=False)['screen']['selected_ref'] == ''
    assert screen_context(screen_read=False)['screen'] == {}


def test_screen_content_is_bounded_and_requires_its_own_permission():
    raw = {
        'scope': 'chat',
        'permissions': {
            'screen_read': True,
            'chat_list': True,
            'chat_content': True,
        },
        'screen': {
            'surface_id': 'surface-1',
            'ready': True,
            'items': [],
            'available_actions': [],
            'content': {
                'kind': 'chat_messages',
                'content_permission': 'chat_content',
                'title': '小安',
                'item_count': 1,
                'items': [{
                    'role': 'contact',
                    'text': '這是可以讀取的內容',
                    'message_id': 'secret-message-id',
                    'metadata': {'prompt': 'do not leak'},
                }],
            },
        },
    }
    allowed = safe_context(raw)
    content = allowed['screen']['content']
    assert content['items'] == [{'role': 'contact', 'text': '這是可以讀取的內容'}]
    assert 'secret-message-id' not in json.dumps(allowed)
    assert 'do not leak' not in json.dumps(allowed)

    raw['permissions']['chat_content'] = False
    redacted = safe_context(raw)['screen']['content']
    assert redacted == {
        'kind': 'chat_messages',
        'content_permission': 'chat_content',
        'redacted': True,
        'item_count': 1,
    }


def test_ref_freezes_selected_contact_even_when_names_are_identical():
    args, error = bind_target('chat.request_send', {'contact_name': '他', 'message': '我可以'}, screen_context())
    assert error is None
    assert args == {'contact_name': '小安', 'message': '我可以', 'target_ref': 'surface-1-2-2'}


@pytest.mark.parametrize('target, expected', [('第二個', 'surface-1-2-2'), ('第二筆', 'surface-1-2-2'), ('second', 'surface-1-2-2'), ('這個', 'surface-1-2-2')])
def test_calendar_relative_target_uses_page_order(target, expected):
    args, error = bind_target('calendar.update', {'target': target, 'date': '2026-09-14'}, screen_context('calendar_event'))
    assert error is None and args['target_ref'] == expected


def test_stale_or_wrong_kind_ref_never_falls_back_to_matching_name():
    for ref in ['surface-99-1-1', 'surface-1-2-1']:
        args, error = bind_target('calendar.update', {'target': '晚餐', 'target_ref': ref}, screen_context())
        assert error == 'stale_target'


def test_ambiguous_missing_selection_and_out_of_range_require_input():
    context = screen_context('calendar_event')
    context['screen']['selected_ref'] = ''
    assert bind_target('calendar.update', {'target': '這個'}, context)[1] == 'ambiguous_target'
    assert bind_target('calendar.update', {'target': '第九個'}, context)[1] == 'ambiguous_target'


def test_normal_named_commands_remain_compatible_without_screen_access():
    args = {'contact_name': '小安', 'message': '我可以'}
    assert bind_target('chat.request_send', args, safe_context({}))[0] == args


def test_target_permission_denial_does_not_inherit_display_permission():
    context = screen_context(chat_send=False)
    assert bind_target('chat.request_send', {'target_ref': 'surface-1-2-2'}, context)[1] == 'permission_denied'


def test_direct_hub_target_uses_match_permissions_without_public_chat_permission():
    context = screen_context('match_invitation', public_ayue=False)
    args, error = bind_target('ayue.public_query', {'domain': 'matching', 'question': '接受第二個'}, context)
    assert error is None and args['target_ref'] == 'surface-1-2-2'


def test_ref_arguments_are_allowlisted_and_database_ids_are_not_refs():
    assert validate_proposal({'intent': 'calendar.update', 'arguments': {
        'target_ref': 'surface-1-2-1', 'date': '2026-09-14'}}, base_revision=2)
    for intent, args in [('calendar.cancel', {'target_ref': 'database-event-id'}),
                         ('settings.set', {'key': 'ui.dark_mode', 'enabled': True, 'target_ref': 'surface-1-2-1'})]:
        assert validate_proposal({'intent': intent, 'arguments': args}, base_revision=2) is None


def test_result_protocol_keeps_legacy_messages_and_bounds_new_data():
    assert safe_result({'success': True, 'message': '完成'}) == {'status': 'success', 'message': '完成'}
    result = safe_result({'result_version': 1, 'success': False, 'status': 'success',
                          'error_code': 'stale_target', 'message': '已過期',
                          'data': {'user_id': 'secret', 'candidates': [{'label': '晚餐', 'id': 'secret'}]}})
    assert result['status'] == 'failed' and result['error_code'] == 'stale_target'
    assert result['data'] == {'candidates': [{'label': '晚餐'}]}


def test_target_schemas_follow_shared_catalog():
    declarations = {d.name: d for d in _live_tools(types)[0].function_declarations}
    for action in ACTIONS.values():
        if action['target_kinds']:
            schema = declarations[action['tool']].parameters_json_schema
            assert 'target_ref' in schema['properties']
            assert not {'contact_name', 'target'} & set(schema.get('required', []))


@pytest.mark.parametrize('change_before_confirm', [False, 'revision', 'surface'])
def test_live_screen_target_confirmation_and_structured_result(change_before_confirm):
    async def scenario():
        live, socket, events = FakeLive(), FakeWebSocket(), []
        async def emit(event): events.append(event)
        task = asyncio.create_task(run_duplex_session(socket, provider=FakeProvider(live),
            limiter=FakeLimiter(), identity='test', initial_context=screen_context(),
            max_session_seconds=30, send_event=emit))
        async def call(id, name, args):
            await live.incoming.put(message(tool_calls=[SimpleNamespace(id=id, name=name, args=args)]))
        async def control(value):
            await socket.incoming.put({'type': 'websocket.receive', 'text': json.dumps(value)})
        try:
            await call('screen', 'describe_current_screen', {})
            await wait_until(lambda: len(live.tool_responses) == 1)
            assert len(live.tool_responses[0][2]['screen']['items']) == 2
            await call('send', 'send_chat_message', {'target_ref': 'surface-1-2-2', 'message': '我可以'})
            await wait_until(lambda: any(e['type'] == 'confirmation_required' for e in events))
            assert not any(e['type'] == 'action_proposal' for e in events)
            if change_before_confirm:
                context = screen_context()
                if change_before_confirm == 'revision':
                    context['revision'] = 3
                else:
                    context['screen']['items'] = []
                    context['screen']['surface_id'] = 'surface-2'
                await control({'type': 'context_changed', 'context': context})
                await asyncio.sleep(0.01)
            await call('confirm', 'confirm_pending_action', {'spoken_phrase': '確認'})
            if change_before_confirm:
                await wait_until(lambda: any(r[0] == 'confirm' for r in live.tool_responses))
                assert not any(e['type'] == 'action_proposal' for e in events)
            else:
                await wait_until(lambda: any(e['type'] == 'action_proposal' for e in events))
                action = next(e for e in events if e['type'] == 'action_proposal')
                assert action['arguments']['target_ref'] == 'surface-1-2-2'
                await control({'type': 'action_result', 'action_id': action['action_id'],
                               'result_version': 1, 'success': False, 'error_code': 'stale_target',
                               'message': '資料已更新', 'data': {'candidates': [{'label': '小安'}]}})
                await wait_until(lambda: any(r[0] == 'confirm' for r in live.tool_responses))
                assert live.tool_responses[-1][2]['error_code'] == 'stale_target'
                assert live.tool_responses[-1][2]['data']['candidates'][0]['label'] == '小安'
        finally:
            await control({'type': 'stop'})
            await task
    asyncio.run(scenario())


def test_malformed_optional_wire_fields_are_ignored():
    context = screen_context()
    context['screen']['selected_ref'] = {'unexpected': True}
    assert safe_context(context)['screen']['selected_ref'] == ''
    assert safe_result({'status': [], 'error_code': {}})['status'] == 'failed'
    assert safe_result([])['status'] == 'failed'
