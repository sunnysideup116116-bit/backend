"""Small, permission-filtered screen updates for the persistent voice model."""
import json

from .contracts import safe_context, safe_reply

MAX_SCREEN_CONTEXT_CHARS = 4000


def screen_context_payload(context: dict) -> str:
    safe = safe_context(context)
    screen = safe.get('screen') or {}
    content = screen.get('content') or {}
    raw_items = content.get('items') or []
    # Chat projections are oldest-first; other surfaces use displayed order.
    recent = raw_items[-4:] if 'messages' in str(content.get('kind')) else raw_items[:4]
    if screen.get('ready') is not True:
        # Do not repeatedly inject a half-written streaming reply. The final
        # ready snapshot restores the text; busy/controls remain available.
        recent = []
    targets = [dict(item, position=index + 1)
               for index, item in enumerate(screen.get('items') or [])]
    data = {
        'scope': safe['scope'], 'revision': safe['revision'],
        'screen_read_allowed': safe['permissions'].get('screen_read') is True,
        'screen': {
            'surface_id': screen.get('surface_id', ''),
            'ready': screen.get('ready', False),
            'operation_in_progress': screen.get('operation_in_progress', False),
            'selected_ref': screen.get('selected_ref', ''),
            'truncated': screen.get('truncated', False) or len(targets) > 5,
            'available_actions': screen.get('available_actions', [])[:12],
            'controls': screen.get('controls', []),
            'composer': screen.get('composer', {}),
            'content': {
                'title': content.get('title', ''),
                'kind': content.get('kind', ''),
                'coverage': 'recent_messages' if 'messages' in str(content.get('kind')) else 'page_items',
                'redacted': content.get('redacted', False),
                'truncated': content.get('truncated', False) or len(raw_items) > len(recent),
                'items': recent,
                'recommendations': content.get('recommendations', []),
            },
            'items': targets[:5],
        },
    }
    selected = next((item for item in targets
                     if item.get('ref') == screen.get('selected_ref')), None)
    if selected and selected not in data['screen']['items']:
        data['screen']['items'] = [selected, *data['screen']['items'][:4]]

    def redact(value):
        if isinstance(value, str):
            return safe_reply(value)
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, dict):
            return {key: redact(item) for key, item in value.items()}
        return value

    data = redact(data)
    while True:
        encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        if len(encoded) <= MAX_SCREEN_CONTEXT_CHARS:
            return encoded
        screen = data['screen']
        if screen['content']['items']:
            screen['content']['items'].pop(0)
            screen['content']['truncated'] = True
        elif screen['items']:
            screen['items'].pop()
            screen['truncated'] = True
        elif screen['content']['recommendations']:
            screen['content']['recommendations'].pop()
            screen['content']['truncated'] = True
        elif screen['controls']:
            screen['controls'].pop()
        else:
            # Remaining fields have fixed limits well below the total budget.
            return json.dumps({'scope': safe['scope'], 'screen': {'truncated': True}})
