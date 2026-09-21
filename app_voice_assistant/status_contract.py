"""Small, public operational projections. Never expose raw risk/account rows."""
from datetime import datetime, timezone
import math

STATUS_ERRORS = frozenset({
    'agent_quota_exhausted', 'agent_quota_unavailable', 'provider_rate_limited',
    'risk_cooldown', 'risk_blocked', 'risk_unavailable', 'delivery_unknown',
    'contact_unavailable', 'status_rate_limited',
})


def provider_error_code(error):
    """Classify a provider failure without exposing keys or raw provider text."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        code = getattr(error, 'status_code', None) or getattr(error, 'code', None)
        if str(code) == '429' or 'RESOURCE_EXHAUSTED' in str(error) or '429' in str(error):
            return 'provider_rate_limited'
        error = error.__cause__ or error.__context__
    return 'gemini_live_connect_failed'


def iso_now():
    return datetime.now(timezone.utc).isoformat()


def safe_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def public_quota(raw):
    if not isinstance(raw, dict) or raw.get('state') == 'unknown':
        return {'state': 'unknown', 'shared': True, 'server_time': iso_now()}
    try:
        percent = float(raw['remaining_percent'])
        if not math.isfinite(percent):
            raise ValueError()
        unlimited = raw.get('infinity') is True or raw.get('unlimited') is True
        # A rounded 0% is NOT proof of exhaustion.
        exhausted = (raw.get('remaining_tokens', 1) <= 0
                     if 'remaining_tokens' in raw else raw.get('exhausted') is True)
        return {
            'state': 'available', 'shared': True, 'unlimited': unlimited,
            'exhausted': exhausted and not unlimited,
            'remaining_percent': max(0.0, min(percent, 100.0)),
            'next_refill_at': safe_time(raw.get('next_refill_at')),
            'refill_timezone': 'Asia/Taipei', 'server_time': iso_now(),
        }
    except (KeyError, TypeError, ValueError):
        return {'state': 'unknown', 'shared': True, 'server_time': iso_now()}


def public_cooldown(raw):
    unknown = {'state': 'unknown', 'remaining_seconds': None, 'until': None,
               'server_time': iso_now()}
    if not isinstance(raw, dict) or raw.get('state') not in {'clear', 'active'}:
        return unknown
    server_time = safe_time(raw.get('server_time'))
    until = safe_time(raw.get('until'))
    if not server_time or (raw['state'] == 'active' and not until):
        return unknown
    seconds = 0
    if until:
        seconds = max(0, math.ceil((datetime.fromisoformat(until)
                                  - datetime.fromisoformat(server_time)).total_seconds()))
    return {'state': 'active' if seconds else 'clear', 'remaining_seconds': seconds,
            'until': until, 'server_time': server_time}


def public_delivery(raw):
    if not isinstance(raw, dict):
        return {'state': 'unknown'}
    state = raw.get('state')
    return {
        'state': state if state in {'delivered', 'blocked', 'not_sent', 'unknown'} else 'unknown',
        'risk_available': raw.get('risk_available') is True,
        'sender_message': str(raw.get('sender_message') or '')[:500],
        'checked_at': safe_time(raw.get('checked_at')) or iso_now(),
    }


def quota_message(quota):
    if quota.get('state') != 'available':
        return '暫時無法確認 Agent 額度，請稍後再查。'
    if quota.get('unlimited'):
        return '你的 Agent 共用額度為無限；配對阿月、阿月悄悄話與語音阿月共用同一份額度。'
    percent = quota['remaining_percent']
    remaining = '不到 1%' if 0 < percent < 1 else f'{percent:.0f}%'
    prefix = 'Agent 共用額度已用完。' if quota.get('exhausted') else f'你的 Agent 共用額度還剩 {remaining}。'
    return prefix + '下次額度更新是台灣時間午夜十二點；三種阿月共用餘額。'


def status_result(*, quota=None, cooldown=None, delivery=None):
    data, messages, error = {}, [], None
    if quota is not None:
        quota = public_quota(quota)
        data['quota'] = quota
        messages.append(quota_message(quota))
        if quota['state'] == 'unknown':
            error = 'agent_quota_unavailable'
    if delivery is not None:
        delivery = public_delivery(delivery)
        data['delivery'] = delivery
        state = delivery['state']
        messages.append({'delivered': '這則訊息已送出。', 'blocked': '這則訊息內容被攔截，沒有送出。',
                         'not_sent': '這則訊息尚未送出。', 'unknown': '目前還無法確認這則訊息是否送出，請勿直接重送。'}[state])
        if state == 'unknown':
            error = 'delivery_unknown'
        elif state == 'blocked':
            error = 'risk_blocked'
        if delivery['sender_message']:
            messages.append(delivery['sender_message'])
    if cooldown is not None:
        cooldown = public_cooldown(cooldown)
        data['cooldown'] = cooldown
        if cooldown['state'] == 'unknown':
            messages.append('暫時無法確認冷卻狀態。')
            error = error or 'risk_unavailable'
        elif cooldown['state'] == 'active':
            seconds = cooldown['remaining_seconds']
            messages.append(f'目前冷卻還剩 {seconds // 60} 分 {seconds % 60} 秒，結束後仍需確認才能再次傳送。')
            error = error or 'risk_cooldown'
        else:
            messages.append('目前沒有生效中的冷卻；再次傳送仍需通過安全檢查。')
    return {'result_version': 1, 'success': error is None,
            'status': 'success' if error is None else 'failed',
            'message': ''.join(messages), 'data': data,
            **({'error_code': error} if error else {})}
