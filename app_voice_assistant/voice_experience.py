"""Bounded contracts and catalog-grounded help for the voice experience."""
from datetime import date
import re
from typing import Any

EXPERIENCE_INTENTS = {'date.plan', 'memory.disable', 'app.help'}


def experience_arguments(intent: str, raw: dict[str, Any]) -> dict[str, Any] | None:
    from .capabilities import ARGUMENT_SCHEMAS
    schema = ARGUMENT_SCHEMAS[intent]
    if set(raw) - set(schema['properties']):
        return None
    result = {}
    for key, value in raw.items():
        rule = schema['properties'][key]
        kind = rule['type']
        if kind == 'string':
            if not isinstance(value, str) or len(value) > rule.get('maxLength', 500):
                return None
            value = value.strip()
            if rule.get('enum') and value not in rule['enum']:
                return None
        elif kind == 'integer':
            if type(value) is not int or not rule.get('minimum', 0) <= value <= rule.get('maximum', 100000):
                return None
        elif kind == 'boolean' and type(value) is not bool:
            return None
        result[key] = value
    if intent == 'memory.disable' and not result.get('label'):
        return None
    if intent == 'date.plan':
        if result.get('date'):
            try:
                if date.fromisoformat(result['date']).isoformat() != result['date']:
                    return None
            except ValueError:
                return None
        if result.get('start_time') and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', result['start_time']):
            return None
        if result.get('operation') == 'select' and not re.fullmatch(r'plan-[a-f0-9]{32}', result.get('plan_ref', '')):
            return None
    return result


def app_help(context: dict[str, Any], query: str = '') -> dict[str, Any]:
    from .capabilities import ACTIONS, GUIDES
    from .capability_proxy import action_permissions_allowed
    terms = set(re.findall(r'[a-z]+|[\u4e00-\u9fff]', query.lower()))
    guides = []
    for key, guide in GUIDES.items():
        text = ' '.join([guide.get('title', ''), guide.get('summary', ''), *guide.get('aliases', [])])
        score = len(terms & set(re.findall(r'[a-z]+|[\u4e00-\u9fff]', text.lower())))
        actions = []
        for name in guide.get('action_ids', []):
            spec = ACTIONS[name]
            actions.append({'name': spec['title'], 'available': action_permissions_allowed(name, context),
                            'confirmation_required': spec.get('confirmation', False)})
        guides.append((score, {'topic': key, 'title': guide['title'], 'summary': guide['summary'],
                               'steps': guide.get('steps', [])[:5], 'actions': actions}))
    guides.sort(key=lambda item: item[0], reverse=True)
    return {'status': 'success', 'features': [item[1] for item in guides[:(4 if query else 12)]],
            'message': '依這份實際 App 清單說明功能；available=false 表示尚未授權。先回答最相關的兩三項，使用者想操作時再使用對應工具。',
            'limits': ['查到地點不代表已訂位', '對方是否有空只依已授權資料，不推測私人行程',
                       '送出或變更資料必須確認', '額度、Risk 冷卻及送達狀態以查詢結果為準']}


def experience_tools(types: Any) -> list[Any]:
    from .capabilities import ARGUMENT_SCHEMAS
    return [types.FunctionDeclaration(
        name=name, description=description, parameters_json_schema=ARGUMENT_SCHEMAS[intent],
    ) for name, intent, description in [
        ('plan_date', 'date.plan', 'Prepare 2-3 date options using real places, authorized calendar and preferences. Partial fields are allowed; ask only the returned question. Set reset=true for a new outing; keep it false when filling this plan. Per-person budget is TWD. ignore_preferences/use_preferences/ignore_budget affect THIS plan only. Select ONLY a returned plan_ref; selection never sends or saves. Use selected calendar_draft or message_draft with manage_voice_draft and ask confirmation.'),
        ('forget_preference', 'memory.disable', 'Stop using one existing long-term preference identified by its exact visible label. Requires confirmation. Never supply database keys. For one-time exceptions use plan_date ignore_preferences, never delete memory.'),
        ('explain_app', 'app.help', 'Explain what this App can do, where a feature is, how to use it and what permissions are missing. Answers come from the actual shared catalog. This tool does not perform the operation.'),
    ]]


def safe_experience_data(raw: dict[str, Any]) -> dict[str, Any]:
    """Explicit projection; never forward arbitrary local IDs or source payloads."""
    def clean(value, depth=0):
        allowed = {'plan_ref', 'title', 'name', 'address', 'date', 'start_time', 'end_time', 'location',
                   'notes', 'contact_name', 'message', 'summary', 'reason', 'source', 'checked_at',
                   'cost', 'transport', 'opening_hours', 'availability', 'preferences', 'label', 'stance',
                   'options', 'selected', 'calendar_draft', 'message_draft', 'constraints',
                   'city', 'budget', 'duration_minutes', 'ignore_preferences', 'ignore_budget', 'use_preferences', 'requirements'}
        if depth > 5:
            return None
        if isinstance(value, dict):
            return {key: clean(item, depth+1) for key, item in value.items() if key in allowed}
        if isinstance(value, list):
            return [clean(item, depth+1) for item in value[:12]]
        if isinstance(value, str):
            return value[:1200]
        if isinstance(value, (int, bool)) or value is None:
            return value
        return None
    return {key: clean(raw[key]) for key in ('date_plan', 'preferences') if key in raw}
