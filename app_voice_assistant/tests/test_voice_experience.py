import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app_voice_assistant.contracts import validate_proposal, requires_confirmation
from app_voice_assistant.contextual import safe_result
from app_voice_assistant.drafts import spoken_draft_patch
from app_voice_assistant.spoken_commands import cancellation_focus
from app_voice_assistant.voice_experience import app_help
from app_voice_assistant.tests.test_conversation_drafts import Conversation, calendar_values
from app_voice_assistant.tests.test_duplex_runtime import wait_until


def test_plan_contract_rejects_ids_invalid_dates_and_unissued_plan_refs():
    def proposal(args): return validate_proposal({'intent':'date.plan','arguments':args},base_revision=1)
    assert proposal({'city':'台北','date':'2030-01-01','budget':500})
    assert proposal({'user_id':'another'}) is None
    assert proposal({'date':'2030-02-30'}) is None
    assert proposal({'operation':'select','plan_ref':'another-user-id'}) is None
    assert proposal({'start_time':'25:00'}) is None
    assert requires_confirmation(validate_proposal({'intent':'memory.disable','arguments':{'label':'安靜'}},base_revision=0))


def test_app_help_comes_from_catalog_and_reports_unavailable_permissions():
    help = app_help({'permissions':{}}, '比較約會方案')
    assert help['features']
    assert any(not action['available'] for guide in help['features'] for action in guide['actions'])
    assert '查到地點不代表已訂位' in help['limits']


def test_structured_options_drop_private_data_and_keep_selection_drafts():
    result = safe_result({'success':True,'data':{'date_plan':{'options':[{
        'plan_ref':'plan-'+'a'*32, 'title':'咖啡A', 'private_user_id':'secret',
        'calendar_draft':{'title':'咖啡A','date':'2030-01-01'},
        'preferences':[{'label':'安靜','stance':'like','key':'private-memory-key'}],
    }]}}})
    option=result['data']['date_plan']['options'][0]
    assert 'private_user_id' not in option
    assert 'key' not in option['preferences'][0]
    assert option['calendar_draft']['date']=='2030-01-01'


def test_relative_time_preserves_duration_and_unchanged_fields():
    values=calendar_values()
    assert spoken_draft_patch('時間往後半小時，地點不變','calendar.create',values)=={'start_time':'18:30','end_time':'20:30'}
    assert spoken_draft_patch('提早一小時，其他不變','calendar.create',values)=={'start_time':'17:00','end_time':'19:00'}
    assert spoken_draft_patch('延後十小時','calendar.create',values) is None


def test_cancellation_does_not_choose_the_negated_date():
    focus=cancellation_focus('不是取消星期六，是取消星期日那個')
    assert datetime.fromisoformat(focus['date']).weekday()==6
    focus=cancellation_focus('取消的是星期日那個，星期六的保留')
    assert datetime.fromisoformat(focus['date']).weekday()==6
    assert 'question' in cancellation_focus('不是取消星期六，星期日也保留')


def test_preview_request_keeps_draft_without_dispatching_write():
    async def scenario():
        async with Conversation() as c:
            await c.tool('write_app_action', {'action':'calendar_create',**calendar_values()})
            task=c.confirmations()[0]['task_id']
            await c.control(type='utterance',text='先給我看內容，還不要傳')
            await wait_until(lambda:c.tasks.get_task('owner',task)['stage']=='draft_paused')
            assert not any(e['type']=='action_proposal' for e in c.events)
    asyncio.run(scenario())


def test_app_explanation_never_sends_device_action_and_plan_is_version_gated():
    async def scenario():
        async with Conversation() as c:
            answer=await c.tool('explain_app',{'query':'這個App可以做什麼'})
            assert answer['features']
            assert not any(e['type']=='action_proposal' for e in c.events)
            await c.control(type='context_changed',context={'scope':'global','revision':1,
                'feature_status':{'conversation_drafts':True},
                'permissions':{'places':True,'public_ayue':True,'calendar_read':True}})
            await c.control(type='utterance',text='測試權限')
            await wait_until(lambda:any(e['type']=='user_transcript' for e in c.events))
            answer=await c.tool('plan_date',{'city':'台北'})
            assert answer['status']=='failed'
    asyncio.run(scenario())


def test_new_client_planner_result_reaches_model_and_forgetting_requires_confirmation():
    async def scenario():
        async with Conversation() as c:
            await c.control(type='context_changed',context={'scope':'global','revision':1,
                'feature_status':{'conversation_drafts':True,'voice_experience':True},
                'permissions':{'places':True,'public_ayue':True,'calendar_read':True,'memory_read':True,'memory_write':True}})
            await c.control(type='utterance',text='請規劃約會')
            await wait_until(lambda:any(e['type']=='user_transcript' for e in c.events))
            call=asyncio.create_task(c.tool('plan_date',{'city':'台北'}))
            await wait_until(lambda:any(e['type']=='action_proposal' for e in c.events))
            action=next(e for e in c.events if e['type']=='action_proposal')
            assert action['intent']=='date.plan'
            await c.control(type='action_result',action_id=action['action_id'],success=True,message='已整理方案',data={
                'date_plan':{'options':[{'plan_ref':'plan-'+'a'*32,'title':'咖啡A','calendar_draft':{'title':'咖啡A'}}]}})
            answer=await call
            assert answer['data']['date_plan']['options'][0]['title']=='咖啡A'
            response=await c.tool('forget_preference',{'label':'安靜'})
            assert response['status']=='confirmation_required'
            assert '安靜' in c.confirmations()[-1]['spoken_prompt']
            assert len([e for e in c.events if e['type']=='action_proposal'])==1
    asyncio.run(scenario())
