"""Bounded relation-only DeepSeek adapter; no embeddings, storage or raw logging."""
import json
import time
import uuid
import asyncio
import httpx
from openai import AsyncOpenAI, APIConnectionError, APIStatusError, APITimeoutError
from pathlib import Path

try:
    from .related_interest_contract import ACCEPTED, RELATIONS
    from .validator_diagnostics import diagnosed_validator, emit, exception_metadata, remaining_ms, transport_observation
except ImportError:
    from related_interest_contract import ACCEPTED, RELATIONS
    from validator_diagnostics import diagnosed_validator, emit, exception_metadata, remaining_ms, transport_observation

PROMPT = Path(__file__).with_name("related_interest_relation_v1.txt").read_text(encoding="utf-8")


class _SystemicValidatorError(ValueError):
    """Configuration/provider identity failure, not a Concept relation."""


PRIMARY_MODEL_PREFIX = "deepseek"
SECONDARY_MODEL_PREFIX = "glm"
# R3.4 healthy batch median latency is ~1.8s; below this a secondary request
# on the shared budget is a doomed call, so fail closed without starting it.
MIN_SECONDARY_BUDGET_SECONDS = 2.0


def _systemic_failure(exc):
    # Per-attempt timeouts and invalid completions remain batch-local ERRORs.
    # Inspect only typed transport/status metadata, never provider body text.
    return (isinstance(exc, _SystemicValidatorError)
        or isinstance(exc, APIConnectionError) and not isinstance(exc, APITimeoutError)
        or isinstance(exc, APIStatusError) and (
            exc.status_code in {401, 403, 429} or exc.status_code >= 500))


def _failover_eligible(exc):
    """Provider availability failure only; a valid semantic decision never fails over.

    Auth/quota (401/403), parser, model-identity, accounting and local contract
    failures stay on the existing fail-closed path and never reach the secondary.
    """
    if isinstance(exc, APITimeoutError):
        return True
    if isinstance(exc, APIConnectionError):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    return isinstance(exc, (TimeoutError, httpx.TimeoutException))


def _completion(client, *, timeout, **request):
    """Cancel the actual async transport, not a detached sync worker thread."""
    async def call():
        stage = 'client_setup'
        started = time.monotonic()
        async def response_status(response):
            # Observe status only, before SDK parsing. Never read body, headers,
            # URL or request; preserve the existing client and all existing hooks.
            emit('transport_http', stage='transport', http_status=response.status_code,
                http_status_class=f'{response.status_code//100}xx', http_status_source='response_hook',
                elapsed_ms=(time.monotonic()-started)*1000)
        try:
            async with asyncio.timeout(timeout):
                async with AsyncOpenAI(api_key=client.api_key, base_url=str(client.base_url),
                        timeout=timeout, max_retries=0) as bounded:
                    try:
                        bounded._client.event_hooks.setdefault('response', []).append(response_status)
                    except Exception:
                        # Missing diagnostic capability never changes the call.
                        pass
                    stage = 'transport'
                    emit('transport_start', stage=stage, elapsed_ms=(time.monotonic()-started)*1000)
                    response = await bounded.chat.completions.create(**request)
                    stage = 'client_close'
                emit('transport_result', stage='transport', category='success', http_status_class='2xx',
                    http_status_source='sdk_success_boundary', elapsed_ms=(time.monotonic()-started)*1000)
                return response
        except BaseException as exc:
            emit('transport_result', **exception_metadata(exc, stage), elapsed_ms=(time.monotonic()-started)*1000)
            raise
    with transport_observation():
        return asyncio.run(call())


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_field")
        result[key] = value
    return result


def parse_relations(text, finish, ids):
    if finish != "stop" or not isinstance(text, str) or not text.strip() or len(text) > 4096:
        raise ValueError("invalid_relation_response")
    obj = json.loads(text, object_pairs_hook=_unique_object)
    if not isinstance(obj, dict) or set(obj) != {"results"} or not isinstance(obj["results"], list):
        raise ValueError("invalid_relation_schema")
    result = {}
    for row in obj["results"]:
        if (not isinstance(row, dict) or set(row) != {"id", "relation"}
                or not isinstance(row["id"], str) or row["id"] not in ids or row["id"] in result
                or not isinstance(row["relation"], str) or row["relation"] not in RELATIONS):
            raise ValueError("invalid_relation_row")
        result[row["id"]] = row["relation"]
    if set(result) != set(ids):
        raise ValueError("missing_relation")
    return result


@diagnosed_validator
def validate_concepts(query, concepts, client, model, *, deadline, clock=time.monotonic,
        is_enabled=lambda: True, fallback_model=None):
    """At most 12 Concepts, batch 2, 2 attempts/batch, one shared wall budget.

    IDs sent to the model are transient batch positions, not Graph/user IDs.
    A valid negative/unknown never retries. Any final error excludes that batch.
    job_unavailable is a separate, bounded job-level signal: no partial result
    may bypass an accounting/configuration/systemic provider failure.

    Optional secondary provider shares the exact same batch attempt budget:
    attempt 2 uses fallback_model only when attempt 1 failed for provider
    availability (timeout/connection/429/5xx). No extra attempt, no extra
    deadline, no retry x failover amplification. A valid primary decision is
    final even when the secondary model is configured.
    """
    if len(concepts) > 12:
        raise ValueError("concept_limit_exceeded")
    models = [model]
    if isinstance(fallback_model, str) and fallback_model.strip() and fallback_model != model:
        models.append(fallback_model.strip())
    counts = {k: 0 for k in ("accepted", "rejected", "error", "attempts", "attempt_errors", "retries",
        "failover_attempts", *RELATIONS)}
    accepted = []
    for offset in range(0, len(concepts), 2):
        batch = concepts[offset:offset+2]
        pairs = [{"id": str(i), "Q": query, "C": c["semantic_text"]} for i, c in enumerate(batch)]
        relations = None
        systemic_failure = False
        failover_armed = False
        failover_started = None
        for _attempt in range(2):
            remaining = deadline-clock()
            if remaining <= 0.1 or not is_enabled():
                emit('attempt_skipped', batch_id=offset//2+1, attempt=_attempt+1,
                    stage='deadline' if remaining <= .1 else 'validator',
                    category='shared_deadline_exhaustion' if remaining <= .1 else 'policy_disabled',
                    retry_decision='shared_deadline_exhausted' if remaining <= .1 else 'policy_disabled',
                    remaining_shared_ms=remaining*1000)
                break
            use_secondary = bool(_attempt == 1 and failover_armed and len(models) == 2)
            if use_secondary and remaining < MIN_SECONDARY_BUDGET_SECONDS:
                # Never start a doomed secondary request on the last sliver of
                # the shared budget; fail closed under the existing contract.
                emit('attempt_skipped', batch_id=offset//2+1, attempt=_attempt+1, stage='deadline',
                    category='shared_deadline_exhaustion', retry_decision='failover_insufficient_budget',
                    remaining_shared_ms=remaining*1000)
                break
            attempt_model = models[1] if use_secondary else models[0]
            counts["attempts"] += 1
            counts["retries"] += int(_attempt == 1)
            if use_secondary:
                counts["failover_attempts"] += 1
            attempt_started = time.monotonic()
            stage = 'model'
            emit('attempt_start', batch_id=offset//2+1, attempt=_attempt+1,
                stage='validator', remaining_shared_ms=remaining*1000, timeout_budget_ms=min(6.,remaining)*1000,
                retry_decision=('failover_secondary' if use_secondary
                    else 'started_existing_retry' if _attempt else 'not_needed'),
                provider_role='secondary' if use_secondary else 'primary',
                model_id=attempt_model,
                failover_elapsed_ms=(None if failover_started is None
                    else (attempt_started-failover_started)*1000))
            try:
                expected_prefix = SECONDARY_MODEL_PREFIX if use_secondary else PRIMARY_MODEL_PREFIX
                if not str(attempt_model or "").startswith(expected_prefix):
                    raise _SystemicValidatorError('validator_fallback_unavailable' if use_secondary
                        else 'validator_model_unavailable')
                stage = 'transport'
                response = _completion(client, timeout=min(6.0, remaining),
                    model=attempt_model, temperature=0, max_tokens=4096,
                    messages=[{"role": "system", "content": PROMPT},
                              {"role": "user", "content": json.dumps({"pairs": pairs}, ensure_ascii=False)}])
                usage = getattr(response, "usage", None)
                if usage:
                    try:
                        stage = 'accounting'
                        from agent_quota.service import record_usage_deferred
                        record_usage_deferred(uuid.uuid4().hex, usage.prompt_tokens, usage.completion_tokens)
                    except Exception as exc:
                        # Accounting failure must not trigger more paid model
                        # calls or bypass the existing billable task boundary.
                        counts["error"] += len(concepts)-offset
                        counts["attempt_errors"] += 1
                        counts["job_unavailable"] = 1
                        emit('attempt_result', **exception_metadata(exc, 'accounting'),
                            batch_id=offset//2+1, attempt=_attempt+1,
                            elapsed_ms=(time.monotonic()-attempt_started)*1000, retry_decision='no_paid_retry',
                            remaining_shared_ms=remaining_ms(deadline, clock))
                        return [], counts
                stage = 'deadline'
                completed_at = clock()
                if completed_at >= deadline:
                    raise ValueError("validator_deadline")
                stage = 'model'
                if getattr(response, "model", attempt_model) != attempt_model:
                    raise _SystemicValidatorError("validator_model_mismatch")
                stage = 'parser'
                choice = response.choices[0]
                relations = parse_relations(choice.message.content, choice.finish_reason, {p["id"] for p in pairs})
                emit('attempt_result', stage='parser', category='success', parser_result='valid',
                    batch_id=offset//2+1, attempt=_attempt+1,
                    retry_decision='failover_secondary' if use_secondary else 'not_needed',
                    elapsed_ms=(time.monotonic()-attempt_started)*1000, remaining_shared_ms=(deadline-completed_at)*1000,
                    http_status_class='2xx', http_status_source='sdk_success_boundary', model_matches_expected=True)
                break
            except Exception as exc:
                # No provider exception/body/headers or input text is logged.
                relations = None
                counts["attempt_errors"] += 1
                systemic_failure = systemic_failure or _systemic_failure(exc)
                if _attempt == 0 and len(models) == 2 and _failover_eligible(exc):
                    failover_armed = True
                    failover_started = time.monotonic()
                emit('attempt_result', **exception_metadata(exc, stage), batch_id=offset//2+1, attempt=_attempt+1,
                    elapsed_ms=(time.monotonic()-attempt_started)*1000,
                    retry_decision='eligible_for_existing_retry' if _attempt == 0 else 'attempt_limit',
                    remaining_shared_ms=remaining_ms(deadline, clock),
                    provider_role='secondary' if use_secondary else 'primary')
        if relations is None:
            if systemic_failure:
                counts["error"] += len(concepts)-offset
                counts["job_unavailable"] = 1
                return [], counts
            counts["error"] += len(batch)
            continue
        for i, concept in enumerate(batch):
            relation = relations[str(i)]
            counts[relation] += 1
            counts["accepted" if relation in ACCEPTED else "rejected"] += 1
            if relation in ACCEPTED:
                accepted.append({**concept, "relation": relation})
    return accepted, counts
