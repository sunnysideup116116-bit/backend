"""Bounded relation-only DeepSeek adapter; no embeddings, storage or raw logging."""
import json
import time
import uuid
import asyncio
from openai import AsyncOpenAI
from pathlib import Path

try:
    from .related_interest_contract import ACCEPTED, RELATIONS
except ImportError:
    from related_interest_contract import ACCEPTED, RELATIONS

PROMPT = Path(__file__).with_name("related_interest_relation_v1.txt").read_text(encoding="utf-8")


def _completion(client, *, timeout, **request):
    """Cancel the actual async transport, not a detached sync worker thread."""
    async def call():
        async with asyncio.timeout(timeout):
            async with AsyncOpenAI(api_key=client.api_key, base_url=str(client.base_url),
                    timeout=timeout, max_retries=0) as bounded:
                return await bounded.chat.completions.create(**request)
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


def validate_concepts(query, concepts, client, model, *, deadline, clock=time.monotonic, is_enabled=lambda: True):
    """At most 12 Concepts, batch 2, 2 attempts/batch, one shared wall budget.

    IDs sent to the model are transient batch positions, not Graph/user IDs.
    A valid negative/unknown never retries. Any final error rejects that batch.
    """
    if len(concepts) > 12:
        raise ValueError("concept_limit_exceeded")
    counts = {k: 0 for k in ("accepted", "rejected", "error", "attempts", "attempt_errors", "retries", *RELATIONS)}
    accepted = []
    for offset in range(0, len(concepts), 2):
        batch = concepts[offset:offset+2]
        pairs = [{"id": str(i), "Q": query, "C": c["semantic_text"]} for i, c in enumerate(batch)]
        relations = None
        for _attempt in range(2):
            remaining = deadline-clock()
            if remaining <= 0.1 or not is_enabled():
                break
            counts["attempts"] += 1
            counts["retries"] += int(_attempt == 1)
            try:
                if not str(model or "").startswith("deepseek"):
                    raise ValueError("validator_model_unavailable")
                response = _completion(client, timeout=min(6.0, remaining),
                    model=model, temperature=0, max_tokens=4096,
                    messages=[{"role": "system", "content": PROMPT},
                              {"role": "user", "content": json.dumps({"pairs": pairs}, ensure_ascii=False)}])
                usage = getattr(response, "usage", None)
                if usage:
                    try:
                        from agent_quota.service import record_usage_deferred
                        record_usage_deferred(uuid.uuid4().hex, usage.prompt_tokens, usage.completion_tokens)
                    except Exception:
                        # Accounting failure must not trigger more paid model
                        # calls or bypass the existing billable task boundary.
                        counts["error"] += len(concepts)-offset
                        counts["attempt_errors"] += 1
                        return [], counts
                if clock() >= deadline:
                    raise ValueError("validator_deadline")
                if getattr(response, "model", model) != model:
                    raise ValueError("validator_model_mismatch")
                choice = response.choices[0]
                relations = parse_relations(choice.message.content, choice.finish_reason, {p["id"] for p in pairs})
                break
            except Exception:
                # No provider exception/body/headers or input text is logged.
                relations = None
                counts["attempt_errors"] += 1
        if relations is None:
            counts["error"] += len(batch)
            continue
        for i, concept in enumerate(batch):
            relation = relations[str(i)]
            counts[relation] += 1
            counts["accepted" if relation in ACCEPTED else "rejected"] += 1
            if relation in ACCEPTED:
                accepted.append({**concept, "relation": relation})
    return accepted, counts
