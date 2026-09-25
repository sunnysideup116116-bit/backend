# Relation Validator Backend Selection — prospective development protocol

R3.3 and R3.4 final results remain permanently FAIL.
R3.4 final evidence committed at a6b4b99. Neither retired final dataset is used
for backend selection, prompt tuning, or renewed final PASS claims.
Production STOP: no runtime integration, deployment, Graph, migration or P1-B.

## Frozen semantic task

Reuse exact r32c_prompt_v3.txt, r32c_relation.py taxonomy/parser/map and original
96-case full-input development labels. Model returns only id/relation.
Code maps equivalent/candidate_more_specific→YES; broader/sibling/role/constraint/
unrelated→NO; lexical_ambiguity/unknown→ABSTAIN. ERROR/ABSTAIN/NO reject.
No explanations, confidence or model acceptance policy. No prompt edits/sweeps.

## Preselected candidate profiles

1. Google gemini-3.1-flash-lite, native GenerateContent JSON schema;
   temperature0, thinkingLevel=minimal (documented default; not guaranteed off).
2. Google gemini-2.5-flash, native GenerateContent JSON schema;
   temperature0, fixed thinkingBudget=1024 (bounded reasoning, no sweep).
3. Existing local Ollama qwen3.5:0.8b, native format JSON schema;
   temperature0, think=false, num_predict4096, num_ctx8192,num_thread2.
   No pull/download or global service change; keep_alive30s.

All: batch2,max output4096,timeout60s,at most one eligible schema/transient retry,
no retries for valid NO/ABSTAIN. No fallback to another model inside a condition.
Google keys only from approved root GOOGLE_API_KEYS1..6, in process memory;
no whole .env loading, key identities/fragments or raw exception/headers saved.
Shared Google request pacing>=2.1s (excluded from provider latency; scheduling
latency separately disclosed). Local one outstanding request. API errors count,
including a first error that recovers on the one permitted retry.

## Capability gate, separate from semantic scoring

Official contract plus actual probes, not HTTP200 alone:
- normal frozen development pair, real relation schema;
- schema-only sentinel under contradictory formatting instructions;
- cardinality/required/enum/additionalProperties constraint challenge;
- deliberately invalid schema type should be rejected by the server.

Capability probes do not use retired cases or create semantic labels. They are
not prompt tuning: fixed independent transport/schema tests, never copied into
the relation prompt. Passing probes are finite observations, not a proof that all
JSON Schema features or semantic correctness are guaranteed. Duplicate/missing
IDs, incomplete/refusal/truncated output still fail closed in the frozen parser.
No model that ignores/rejects the required schema enters development.

## Same development comparison

96 cases ×3 rounds, seeds350101/350102/350103; batch2=144 first requests/model.
Interleave eligible cloud backends with schedule seed350100. Native local backend
runs serially to avoid resource contention. max288 attempts/model, no secret
provider wrapper auto-retries. No opportunistic extra retry or case exclusion.
All phases refuse overwrite and bind hashes before scoring.

Blocking: first validity>=99.5% (144/144 needed in this sample), eventual=100%,
precision>=99%,recall>=95%,binary consistency>=99%,broad/role/constraint false
acceptance=0,ERROR fail-open=0. Undefined precision fails. ERROR remains distinct.
Relation accuracy/consistency and NO↔ABSTAIN disagreements diagnostics only.
Report latency, tokens, requests and public-rate cost estimates when obtainable.

Select among eligible profiles in fixed reuse preference order: current Gemini
3.1 Flash-Lite, Gemini2.5Flash, installed Qwen0.8B. Do not add another config/model
after viewing semantic scores. If none qualify STOP, assess semantic fallback's
value; no further prompt polishing or hidden new final tests.

## Independent human label review and final boundary

User designated a separate teammate as independent blind reviewer.
Process design must be complete before authoring a third final dataset:
author creates cases +sealed proposed labels; reviewer independently sees Q/C
and frozen taxonomy only, not initial labels/category clues/model outputs;
a human adjudicator resolves disagreements before gold freeze. No self-approval
by the author/evaluated provider. Reviewer ID/date/independence declaration and
adjudication hashes required. Unresolved cases cannot be scored.
Only after development PASS freeze next final gate before new cases.
No third final scoring before actual human review is received and validated.
Do not endlessly create replacement final holdouts after another FAIL.

## Discovery sources / limitations

Existing Social/Risk Google pool and Risk GeminiAdapter confirm credential wiring.
Do not reuse their partial-key logging/multi-retry/model-fallback paths.
OpenAI key is legacy/inactive for current guardrail provider and model lookup401;
no generation run. OpenAI Docs consulted only for capability/reference.
Ollama Cloud officially does not support structured outputs; no further DeepSeek
experiments. Local qwen0.8B is an actual GGUF model (not cloud alias); Gemma4e2b
also exists but is not selected to bound memory/time; no new service.
Model aliases are not silently substituted. Runtime flags OFF and .82 unchanged.

## Prescoring availability resolution (before semantic evaluation)

Gemini3.1 returned503 for all positive probes, including one bounded retry.
Gemini2.5 initial404s were credential/model-access dependent; positive probes
passed using a confirmed available credential. Metadata listing alone is not
generation access. Google per-model pools will be capability-preflighted in memory
with one schema-only sentinel/key. Only aggregate counts/statuses are retained.
No failed development attempts are hidden by this setup step.

The configured gemini-flash-lite-latest alias actually resolved to
gemini-3.5-flash-lite. Add that pinned model as a separate capability candidate,
thinkingLevel=minimal; do not silently replace3.1 inside any condition.
After capability qualification, fixed preference order is gemini3.5,gemini3.1,
gemini2.5,local Qwen. Unavailable profiles are NOT_RUN, not semantic FAIL/PASS.
No development scores or retired final data informed this availability change.
