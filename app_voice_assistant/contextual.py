"""Bounded screen/result wire format. Model references are never database IDs."""
import re
from .capabilities import ACTIONS, TARGET_PERMISSIONS, available_actions

REF = re.compile(r'surface-\d{1,12}-\d{1,12}-\d{1,2}')
ALIASES = {'他', '她', '對方', '這個人', '目前對象', '這個', '這一個', '這則',
           '這張', '這筆', '那筆', '那個', '目前這個', '剛才那個', 'it', 'him', 'her', 'this', 'that', 'current'}


def _text(value, limit=120):
    text = str(value or '').strip()[:limit]
    text = re.sub(r'[^\s@]+@[^\s@]+\.[^\s@]+', '[Email 已隱藏]', text)
    return re.sub(r'(?<!\d)09\d{8}(?!\d)', '[電話已隱藏]', text)


def _safe_content(value, permissions):
    if not isinstance(value, dict):
        return {}
    kind = _text(value.get('kind'), 80)
    content_permission = str(
        value.get('content_permission') or value.get('permission') or ''
    ).strip()[:80]
    raw_items = value.get('items')
    raw_count = value.get('item_count')
    try:
        item_count = max(
            0,
            min(
                10000,
                int(raw_count) if raw_count is not None else (
                    len(raw_items) if isinstance(raw_items, list) else 0
                ),
            ),
        )
    except (TypeError, ValueError):
        item_count = len(raw_items) if isinstance(raw_items, list) else 0
    if content_permission and permissions.get(content_permission) is not True:
        return {
            **({'kind': kind} if kind else {}),
            'content_permission': content_permission,
            'redacted': True,
            'item_count': item_count,
        }

    allowed = {
        'role', 'speaker', 'text', 'title', 'summary', 'date', 'start_time',
        'end_time', 'location', 'source_type', 'status', 'stage',
        'progress_percent', 'timestamp',
    }
    long_text = {'text', 'summary'}
    items = []
    for raw in (raw_items[:20] if isinstance(raw_items, list) else []):
        if not isinstance(raw, dict):
            continue
        item = {}
        for key in allowed:
            if key not in raw:
                continue
            if key == 'progress_percent':
                try:
                    item[key] = max(0, min(100, int(raw[key])))
                except (TypeError, ValueError):
                    continue
            else:
                limit = 500 if key in long_text else 160
                text = _text(raw.get(key), limit)
                if text:
                    item[key] = text
        if item:
            items.append(item)
    title = _text(value.get('title'), 160)
    content = {
        **({'kind': kind} if kind else {}),
        **({'content_permission': content_permission} if content_permission else {}),
        **({'title': title} if title else {}),
        'item_count': item_count,
        'truncated': value.get('truncated') is True or (
            isinstance(raw_items, list) and len(raw_items) > len(items)
        ),
        'items': items,
    }
    # Keep a malicious or accidentally oversized page projection bounded even
    # after field-level truncation.
    while len(str(content)) > 8000 and items:
        items.pop()
    return content


def safe_screen(value, permissions):
    if not isinstance(value, dict) or permissions.get('screen_read') is not True:
        return {}
    items = []
    seen = set()
    raw_items = value.get('items')
    for raw in (raw_items[:20] if isinstance(raw_items, list) else []):
        if not isinstance(raw, dict):
            continue
        kind, ref = str(raw.get('kind') or ''), str(raw.get('ref') or '')
        permission = TARGET_PERMISSIONS.get(kind)
        if not permission or permissions.get(permission) is not True or not REF.fullmatch(ref) or ref in seen:
            continue
        seen.add(ref)
        attrs = raw.get('attributes') if isinstance(raw.get('attributes'), dict) else {}
        actions = raw.get('actions') if isinstance(raw.get('actions'), list) else []
        items.append({'ref': ref, 'kind': kind, 'label': _text(raw.get('label')),
                      'permission': permission,
                      'attributes': {key: _text(attrs[key]) for key in ('date', 'start_time', 'end_time', 'status', 'source_type') if key in attrs},
                      'actions': available_actions(actions, permissions, targets=True)})
    requested = value.get('available_actions')
    selected = value.get('selected_ref')
    safe = {'surface_id': _text(value.get('surface_id'), 80),
            'ready': value.get('ready') is True, 'items': items,
            'selected_ref': selected if isinstance(selected, str) and selected in seen else '',
            'available_actions': available_actions(requested if isinstance(requested, list) else [], permissions, targets=True),
            'truncated': value.get('truncated') is True}
    content = _safe_content(value.get('content'), permissions)
    if content:
        safe['content'] = content
    return safe


def _ordinal(text):
    english = {'first':1, 'second':2, 'third':3, 'fourth':4, 'fifth':5}
    if text in english:
        return english[text]
    match = re.fullmatch(r'(?:選|選擇|選取|就|接受|婉拒|拒絕|撤回)?第([一二兩三四五六七八九十]{1,3}|\d{1,2})(?:個|則|張|位|項|筆)?', text)
    if not match:
        return None
    value = match.group(1)
    digits = {'一':1,'二':2,'兩':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
    if value.isdigit():
        return int(value)
    if value.count('十') == 1:
        left, right = value.split('十')
        if (not left or left in digits) and (not right or right in digits):
            return (digits.get(left, 1) * 10) + digits.get(right, 0)
    return digits.get(value)


def bind_target(intent, arguments, context):
    """Freeze a current reference before the existing confirmation is created.

    Returns (arguments, error). Missing/ambiguous references never fall back to
    substring name matching or a different list after confirmation.
    """
    args = dict(arguments)
    kinds = ACTIONS.get(intent, {}).get('target_kinds', [])
    explicit = args.get('target_ref')
    if explicit and intent == 'ayue.public_query' and args.get('domain') != 'matching':
        return args, 'permission_denied'
    field = 'contact_name' if 'contact' in kinds else 'target'
    phrase = str(args.get(field) or '').strip().lower()
    if 'match_invitation' in kinds and intent != 'ui.target.select':
        phrase = str(args.get('question') or '').strip().lower()
    compact = re.sub(r'[\s，,。.!！?？、]', '', phrase)
    ordinal = _ordinal(compact)
    relative = compact in ALIASES or ordinal is not None
    if 'match_invitation' in kinds:
        relative = relative or bool(re.fullmatch(r'(接受|婉拒|拒絕|撤回)(這個|這張|這則|它)', compact))
    if not explicit and not relative and intent != 'ui.target.select':
        return args, None
    screen = context.get('screen') or {}
    if not kinds or not screen.get('ready'):
        return args, 'stale_target'
    items = [item for item in screen.get('items', []) if item.get('kind') in kinds]
    ref = explicit or (items[ordinal-1]['ref'] if ordinal and 0 < ordinal <= len(items) else '')
    if not ref and ordinal is None:
        ref = screen.get('selected_ref') or (items[0]['ref'] if len(items) == 1 else '')
    selected = next((item for item in items if item['ref'] == ref), None)
    if selected is None:
        return args, 'stale_target' if explicit else 'ambiguous_target'
    if intent not in selected['actions'] or intent not in screen.get('available_actions', []):
        return args, 'permission_denied'
    args['target_ref'] = selected['ref']
    if field in {'contact_name', 'target'} and intent != 'ui.target.select' and 'match_invitation' not in kinds:
        args[field] = selected['label']
    return args, None


def safe_result(value):
    if not isinstance(value, dict):
        value = {}
    success = value.get('success') is True
    status = value.get('status')
    statuses = {'success', 'failed', 'needs_input', 'awaiting_confirmation'}
    status = status if isinstance(status, str) and status in statuses else ('success' if success else 'failed')
    if not success and status == 'success':
        status = 'failed'
    errors = {'permission_denied', 'stale_target', 'ambiguous_target', 'not_found',
              'not_ready', 'unauthenticated', 'network_error', 'validation_failed', 'unsupported', 'operation_failed'}
    def item(raw):
        if not isinstance(raw, dict):
            return {}
        result = {key: _text(raw[key], 500) for key in ('ref', 'kind', 'label', 'title', 'name', 'date', 'start_time', 'end_time', 'status', 'source_type')
                  if isinstance(raw.get(key), str)}
        attrs = raw.get('attributes')
        if isinstance(attrs, dict):
            result['attributes'] = {key: _text(attrs[key]) for key in ('date', 'start_time', 'end_time', 'status')
                                    if isinstance(attrs.get(key), str)}
        if isinstance(raw.get('actions'), list):
            result['actions'] = [name for name in raw['actions'][:40] if isinstance(name, str) and name in ACTIONS]
        return result
    def clean(raw):
        result = {key: raw[key] for key in ('count', 'remaining', 'revision') if type(raw.get(key)) is int}
        for key in ('items', 'candidates', 'events'):
            if isinstance(raw.get(key), list):
                result[key] = [item(value) for value in raw[key][:20] if isinstance(value, dict)]
        if isinstance(raw.get('selected'), dict):
            result['selected'] = item(raw['selected'])
        return result
    result = {'status': status,
              'message': str(value.get('message') or '')[:12000] or ('操作已完成。' if success else '操作沒有完成。')}
    if value.get('result_version') == 1:
        result['result_version'] = 1
    if isinstance(value.get('error_code'), str) and value['error_code'] in errors:
        result['error_code'] = value['error_code']
    if isinstance(value.get('data'), dict):
        result['data'] = clean(value['data'])
    return result
