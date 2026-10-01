"""Bounded relation-only DeepSeek adapter; no embeddings, storage or raw logging."""
import json
import time
import uuid
import asyncio
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


def _systemic_failure(exc):
    # Per-attempt timeouts and invalid completions remain batch-local ERRORs.
    # Inspect only typed transport/status metadata, never provider body text.
    return (isinstance(exc, _SystemicValidatorError)
        or isinstance(exc, APIConnectionError) and not isinstance(exc, APITimeoutError)
        or isinstance(exc, APIStatusError) and (
            exc.status_code in {401, 403, 429} or exc.status_code >= 500))


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
def validate_concepts(query, concepts, client, model, *, deadline, clock=time.monotonic, is_enabled=lambda: True):
    """At most 12 Concepts, batch 2, 2 attempts/batch, one shared wall budget.

    IDs sent to the model are transient batch positions, not Graph/user IDs.
    A valid negative/unknown never retries. Any final error excludes that batch.
    job_unavailable is a separate, bounded job-level signal: no partial result
    may bypass an accounting/configuration/systemic provider failure.
    """
    if len(concepts) > 12:
        raise ValueError("concept_limit_exceeded")
    counts = {k: 0 for k in ("accepted", "rejected", "error", "attempts", "attempt_errors", "retries", *RELATIONS)}
    accepted = []
    for offset in range(0, len(concepts), 2):
        batch = concepts[offset:offset+2]
        pairs = [{"id": str(i), "Q": query, "C": c["semantic_text"]} for i, c in enumerate(batch)]
        relations = None
        systemic_failure = False
        for _attempt in range(2):
            remaining = deadline-clock()
            if remaining <= 0.1 or not is_enabled():
                emit('attempt_skipped', batch_id=offset//2+1, attempt=_attempt+1,
                    stage='deadline' if remaining <= .1 else 'validator',
                    category='shared_deadline_exhaustion' if remaining <= .1 else 'policy_disabled',
                    retry_decision='shared_deadline_exhausted' if remaining <= .1 else 'policy_disabled',
                    remaining_shared_ms=remaining*1000)
                break
            counts["attempts"] += 1
            counts["retries"] += int(_attempt == 1)
            attempt_started = time.monotonic()
            stage = 'model'
            emit('attempt_start', batch_id=offset//2+1, attempt=_attempt+1,
                stage='validator', remaining_shared_ms=remaining*1000, timeout_budget_ms=min(6.,remaining)*1000,
                retry_decision='started_existing_retry' if _attempt else 'not_needed')
            try:
                if not str(model or "").startswith("deepseek"):
                    raise _SystemicValidatorError("validator_model_unavailable")
                stage = 'transport'
                response = _completion(client, timeout=min(6.0, remaining),
                    model=model, temperature=0, max_tokens=4096,
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
                if getattr(response, "model", model) != model:
                    raise _SystemicValidatorError("validator_model_mismatch")
                stage = 'parser'
                choice = response.choices[0]
                relations = parse_relations(choice.message.content, choice.finish_reason, {p["id"] for p in pairs})
                emit('attempt_result', stage='parser', category='success', parser_result='valid',
                    batch_id=offset//2+1, attempt=_attempt+1, retry_decision='not_needed',
                    elapsed_ms=(time.monotonic()-attempt_started)*1000, remaining_shared_ms=(deadline-completed_at)*1000,
                    http_status_class='2xx', http_status_source='sdk_success_boundary', model_matches_expected=True)
                break
            except Exception as exc:
                # No provider exception/body/headers or input text is logged.
                relations = None
                counts["attempt_errors"] += 1
                systemic_failure = systemic_failure or _systemic_failure(exc)
                emit('attempt_result', **exception_metadata(exc, stage), batch_id=offset//2+1, attempt=_attempt+1,
                    elapsed_ms=(time.monotonic()-attempt_started)*1000,
                    retry_decision='eligible_for_existing_retry' if _attempt == 0 else 'attempt_limit',
                    remaining_shared_ms=remaining_ms(deadline, clock))
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
