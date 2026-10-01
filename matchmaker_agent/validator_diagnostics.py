"""Best-effort, allowlisted diagnostics. Never log source text or exception bodies."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import json
import logging
import math
import re
import socket
import ssl
import threading
import time
import uuid
from urllib.parse import urlsplit

# Canonical start_all.sh uses Uvicorn's existing error sink. Do not enable
# root/OpenAI/httpx logging, which may include request/header/body details.
LOG = logging.getLogger('uvicorn.error.validator_diagnostics')
LOG.setLevel(logging.INFO)
_CURRENT = ContextVar('safe_validator_diagnostic', default=None)
_LOCK = threading.Lock()
_INFLIGHT = 0
_PEAK = 0
_EVENTS = frozenset({'validator_start', 'attempt_start', 'attempt_result', 'attempt_skipped',
    'validator_result', 'transport_start', 'transport_http', 'transport_result', 'event_stage', 'event_result', 'breaker_transition'})
_ENUMS = {
    'stage': {'validator', 'client_setup', 'transport', 'client_close', 'parser', 'accounting', 'model',
        'deadline', 'cancellation', 'event_request', 'event_load', 'event_exact', 'event_vectors',
        'event_ann', 'event_relevance', 'breaker'},
    'category': {'success', 'provider_rate_limit', 'provider_5xx', 'provider_account_or_quota',
        'provider_4xx', 'provider_timeout', 'provider_connection', 'client_connection_pool', 'parser_failure',
        'model_mismatch', 'accounting_failure', 'shared_deadline_exhaustion', 'cancellation',
        'client_exception', 'provider_payload_failure', 'zero_trusted_decisions', 'policy_disabled', 'negative_unavailable',
        'evidence_unavailable', 'other'},
    'parser_result': {'valid', 'not_reached', 'malformed_json', 'invalid_relation_response',
        'invalid_relation_schema', 'invalid_relation_row', 'missing_relation', 'duplicate_json_field', 'parser_exception'},
    'retry_decision': {'not_needed', 'eligible_for_existing_retry', 'attempt_limit', 'no_paid_retry',
        'shared_deadline_exhausted', 'policy_disabled', 'started_existing_retry'},
    'final_typed_outcome': {'accepted', 'normal_no_match', 'unavailable', 'cancelled', 'propagated_exception'},
    'provider': {'ollama_cloud', 'other_provider'},
    'model_id': {'deepseek-v4.1-flash:cloud', 'other_configured_model'},
    'http_status_class': {'2xx', '3xx', '4xx', '5xx', 'unknown'},
    'http_status_source': {'sdk_success_boundary', 'sdk_exception', 'response_hook'},
    'provider_code': {'rate_limit_exceeded', 'insufficient_quota', 'invalid_api_key', 'model_not_found',
        'permission_denied', 'server_error', 'redacted_or_absent'},
    'exception_class': {'APIConnectionError', 'APITimeoutError', 'APIStatusError', 'RateLimitError',
        'InternalServerError', 'AuthenticationError', 'PermissionDeniedError', 'BadRequestError',
        'NotFoundError', 'UnprocessableEntityError', 'APIResponseValidationError', 'TimeoutError',
        'CancelledError', 'JSONDecodeError', 'ValueError', 'RuntimeError', 'IndexError', 'AttributeError',
        'TypeError', '_SystemicValidatorError', 'other_exception'},
    'cause_category': {'dns_failure', 'tls_verification', 'connection_reset', 'client_connection_pool',
        'connect_timeout', 'read_timeout', 'write_timeout', 'connection_failure', 'unknown'},
    'operation': {'event_request', 'semantic_validator', 'other'},
    'typed_code': {'semantic_validator_unavailable', 'semantic_retrieval_timeout', 'event_negative_unavailable',
        'event_candidate_evidence_unavailable', 'event_owner_eligibility_unavailable', 'event_owner_ineligible',
        'event_semantic_disabled', 'event_positive_embedding_pending', 'event_v2_index_unavailable',
        'event_graph_unavailable', 'event_query_embedding_unavailable', 'event_query_embedding_invalid',
        'event_v2_contract_unavailable', 'event_semantic_config_unavailable'},
}
_ENUMS['failure_stage'] = _ENUMS['stage']
_ENUMS['stage'] |= {'event_eligibility', 'event_owner_signals'}
_NUMBERS = frozenset({'batch_id', 'attempt', 'concept_count', 'elapsed_ms', 'timeout_budget_ms',
    'remaining_shared_ms', 'remaining_event_ms', 'http_status', 'accepted', 'rejected', 'error',
    'attempts', 'retries', 'process_inflight', 'process_peak_inflight', 'before_capped2', 'after_capped3'})
_BOOLEANS = frozenset({'job_unavailable', 'model_matches_expected', 'breaker_engaged', 'counter_before_is_lower_bound'})


class Trace:
    def __init__(self, operation, parent):
        self.correlation_id = parent.correlation_id if parent else uuid.uuid4().hex
        self.call_id = uuid.uuid4().hex
        self.operation = operation if operation in _ENUMS['operation'] else 'other'
        self.failure = {}
        self.remaining = None
        self.identity = {}
        self.attempt_fields = {}

    def emit(self, event, **fields):
        try:
            if event not in _EVENTS:
                return
            row = {'event': event, 'correlation_id': self.correlation_id, 'call_id': self.call_id,
                'operation': self.operation}
            row.update(self.identity)
            if event.startswith('transport_') or event == 'attempt_result':
                row.update(self.attempt_fields)
            for key, value in fields.items():
                if key in _ENUMS:
                    row[key] = value if isinstance(value, str) and value in _ENUMS[key] else (
                        'other_exception' if key == 'exception_class' else 'redacted_or_absent' if key == 'provider_code' else 'unknown')
                elif key in _NUMBERS and type(value) in (int, float) and math.isfinite(value):
                    row[key] = max(-3600000, min(3600000, value))
                elif key in _BOOLEANS and type(value) is bool:
                    row[key] = value
            if 'remaining_shared_ms' in row:
                self.remaining = row['remaining_shared_ms']
            if event == 'validator_start':
                self.identity = {k: row[k] for k in ('provider', 'model_id') if k in row}
            if event == 'attempt_start':
                self.attempt_fields = {k: row[k] for k in ('batch_id', 'attempt', 'timeout_budget_ms', 'remaining_shared_ms') if k in row}
            if event == 'transport_http':
                self.attempt_fields.update({k: row[k] for k in ('http_status', 'http_status_class', 'http_status_source') if k in row})
            if row.get('category') not in {None, 'success', 'unknown'}:
                self.failure = {k: row[k] for k in ('stage', 'category', 'parser_result') if k in row}
            LOG.info('validator_diagnostic %s', json.dumps(row, separators=(',', ':')))
        except Exception:
            # Observability cannot alter evidence, retry, cancellation or availability.
            pass


def current_trace():
    return _CURRENT.get()


@contextmanager
def diagnostic_scope(operation):
    trace = Trace(operation, current_trace())
    token = _CURRENT.set(trace)
    try:
        yield trace
    finally:
        _CURRENT.reset(token)


def emit(event, **fields):
    trace = current_trace()
    if trace is not None:
        trace.emit(event, **fields)


def remaining_ms(deadline, clock=time.monotonic):
    """Extra diagnostic sampling is never used in any decision or deadline."""
    try:
        return (deadline-clock())*1000
    except Exception:
        return None


def breaker_transition(correlation_id, latest, *, now, engaged):
    """Use only the already-read latest rows. No new DB read or policy action."""
    try:
        if not isinstance(correlation_id, str) or not re.fullmatch('[a-f0-9]{32}', correlation_id):
            return
        from .related_interest_contract import POLICY
        eligible = [row for row in latest if now-900 <= row.get('completed_at', 0) <= now
            and (row.get('related_interest_pilot') or {}).get('triggered') is True
            and (row.get('related_interest_pilot') or {}).get('policy_version') == POLICY
            and (row.get('related_interest_pilot') or {}).get('telemetry_version') in {2, 3}]
        eligible.sort(key=lambda row: row['completed_at'], reverse=True)
        def count(rows):
            number = 0
            for row in rows:
                if row.get('status') != 'failed' or row.get('error_code') not in {
                        'semantic_validator_unavailable', 'semantic_retrieval_timeout'}:
                    break
                number += 1
            return number
        before, after = count(eligible[1:]), count(eligible)
        with diagnostic_scope('event_request') as trace:
            trace.correlation_id = correlation_id
            trace.emit('breaker_transition', stage='breaker', before_capped2=min(2,before),
                after_capped3=min(3,after), counter_before_is_lower_bound=before>=2,
                breaker_engaged=bool(engaged))
    except Exception:
        pass


def provider_identity(client, model):
    try:
        host = urlsplit(str(client.base_url)).hostname
    except Exception:
        host = None
    return {'provider': 'ollama_cloud' if host == 'ollama.com' else 'other_provider',
        'model_id': model if model == 'deepseek-v4.1-flash:cloud' else 'other_configured_model'}


def _exception_metadata(exc, stage):
    """Only exception types, numeric status and fixed public error categories."""
    from openai import APIConnectionError, APIStatusError, APITimeoutError
    import asyncio
    import httpx
    data = {'stage': stage, 'failure_stage': stage, 'exception_class': type(exc).__name__, 'category': 'client_exception'}
    if isinstance(exc, APIStatusError):
        status = exc.status_code
        data.update(http_status=status, http_status_class=f'{status//100}xx', http_status_source='sdk_exception',
            category='provider_rate_limit' if status == 429 else 'provider_5xx' if status >= 500
            else 'provider_account_or_quota' if status in {401, 403} else 'provider_4xx', stage='transport')
        code = getattr(exc, 'code', None)
        data['provider_code'] = code if isinstance(code, str) and code in _ENUMS['provider_code'] else 'redacted_or_absent'
    elif stage == 'deadline':
        data['category'] = 'shared_deadline_exhaustion'
    elif isinstance(exc, (TimeoutError, APITimeoutError)):
        data.update(stage='transport', category='provider_timeout')
    elif isinstance(exc, APIConnectionError):
        data.update(stage='transport', category='provider_connection')
    elif isinstance(exc, asyncio.CancelledError):
        data.update(stage='cancellation', category='cancellation')
    elif stage == 'transport' and (isinstance(exc, json.JSONDecodeError) or type(exc).__name__ == 'APIResponseValidationError'):
        data.update(category='provider_payload_failure', parser_result='not_reached')
    elif stage == 'parser':
        code = 'malformed_json' if isinstance(exc, json.JSONDecodeError) else str(exc)
        data.update(category='parser_failure', parser_result=code if code in _ENUMS['parser_result'] else 'parser_exception')
    elif stage == 'model':
        data['category'] = 'model_mismatch'
    elif stage == 'accounting':
        data['category'] = 'accounting_failure'
    cause = exc
    for _ in range(8):
        if cause is None:
            break
        category = ('dns_failure' if isinstance(cause, socket.gaierror) else
            'tls_verification' if isinstance(cause, ssl.SSLCertVerificationError) else
            'connection_reset' if isinstance(cause, (ConnectionResetError, BrokenPipeError)) else
            'client_connection_pool' if isinstance(cause, httpx.PoolTimeout) else
            'connect_timeout' if isinstance(cause, httpx.ConnectTimeout) else
            'read_timeout' if isinstance(cause, httpx.ReadTimeout) else
            'write_timeout' if isinstance(cause, httpx.WriteTimeout) else None)
        if category:
            data['cause_category'] = category
            break
        cause = cause.__cause__ or cause.__context__
    return data


def exception_metadata(exc, stage):
    try:
        return _exception_metadata(exc, stage)
    except Exception:
        return {'stage': 'validator', 'category': 'other', 'exception_class': 'other_exception'}


@contextmanager
def transport_observation():
    global _INFLIGHT, _PEAK
    with _LOCK:
        _INFLIGHT += 1
        _PEAK = max(_PEAK, _INFLIGHT)
        inflight, peak = _INFLIGHT, _PEAK
    emit('transport_start', stage='client_setup', process_inflight=inflight, process_peak_inflight=peak)
    try:
        yield
    finally:
        with _LOCK:
            _INFLIGHT -= 1


def diagnosed_validator(function):
    @wraps(function)
    def call(query, concepts, client, model, **kwargs):
        with diagnostic_scope('semantic_validator') as trace:
            started = time.monotonic()
            trace.emit('validator_start', stage='validator', concept_count=len(concepts), **provider_identity(client, model))
            try:
                accepted, counts = function(query, concepts, client, model, **kwargs)
            except BaseException as exc:
                import asyncio
                trace.emit('validator_result', final_typed_outcome='cancelled' if isinstance(exc, asyncio.CancelledError)
                    else 'propagated_exception', elapsed_ms=(time.monotonic()-started)*1000,
                    **exception_metadata(exc, 'validator'))
                raise
            unavailable = bool(counts.get('job_unavailable') or counts.get('error') and not
                (counts.get('accepted') or counts.get('rejected')))
            meta = trace.failure if unavailable else {'stage': 'validator', 'category': 'success'}
            trace.emit('validator_result', **meta, elapsed_ms=(time.monotonic()-started)*1000,
                remaining_shared_ms=trace.remaining, job_unavailable=bool(counts.get('job_unavailable')),
                final_typed_outcome='unavailable' if unavailable else 'accepted' if accepted else 'normal_no_match',
                **{k: counts.get(k, 0) for k in ('accepted', 'rejected', 'error', 'attempts', 'retries')})
            return accepted, counts
    return call
