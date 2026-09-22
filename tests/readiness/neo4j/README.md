# R1-L disposable local functional readiness

- **Local baseline = Neo4j 2026.08.1 Community Edition**, `neo4j:2026.08.1`.
- This is **not** the production version. **Production compatibility remains unknown.**
- **Production fingerprint remains unknown.** A local PASS never authorizes activation.
- `MATCH_PREFERENCE_SEMANTIC_MODE=off` and
  `MATCH_PREFERENCE_SEMANTIC_EMBEDDING_SPACE_CONFIRMED=off` remain unchanged.
- `0.82` remains provisional and unchanged. Deterministic geometry is not model calibration.

Official image/version references: [Neo4j Docker](https://neo4j.com/docs/operations-manual/current/docker/introduction/),
[release notes](https://neo4j.com/release-notes/).

## Isolation

Only this directory's tooling controls the disposable Graph. The runner forces the
local Docker Unix socket, a checkout-specific project, labeled private network and
fresh named volumes, loopback-only ports (17687/17474 by default), generated
synthetic credentials and the dedicated instance's `neo4j` database. No production
mount, URI, `.env`, Graph export, provider key, server process or Event worker is used.
The dedicated bridge disables inter-container communication and publishes only on
loopback; usage reporting, Fleet Manager and fleet discovery are disabled. The runner
permits Graph connections only to its verified loopback Bolt port. It never calls
the shared service on port 9001. This is not an OS-level egress firewall for the container.

## Run

From the readiness checkout, provision a local Python environment (not Server's venv):

```bash
python3 -m venv tests/readiness/neo4j/.venv
tests/readiness/neo4j/.venv/bin/pip install neo4j==6.2.0 pydantic==2.13.4 requests==2.34.2 mongomock==4.3.0 pymongo==4.7.2
tests/readiness/neo4j/.venv/bin/python tests/readiness/neo4j/run_readiness.py up
tests/readiness/neo4j/.venv/bin/python tests/readiness/neo4j/run_readiness.py check
tests/readiness/neo4j/.venv/bin/python tests/readiness/neo4j/run_readiness.py stop
```

`up` refuses unexpected existing containers/volumes or an unpinned/enterprise image.
Before a run, set `READINESS_NEO4J_IMAGE=neo4j:<explicit-version>` or put that setting
in ignored `.env.local` to test another approved baseline. There is no `latest`
default/fallback. Stop/destroy the old baseline using its old settings before
changing the image; no automatic in-place database upgrade is supported. Ports may be changed through
`READINESS_BOLT_PORT` / `READINESS_HTTP_PORT`, never the production service ports.

`stop` preserves synthetic data for reproduction. `destroy` requires explicit
confirmation and validates ownership before removing only these disposable
container/network/volumes. No pruning, broad filesystem deletion, or production
maintenance command is used. Generated state (including synthetic credentials),
vectors, JSON reports and plans are ignored under `artifacts/`.

### Cleanup and volume deletion

Run from the same checkout with the same image/port settings used for `up`:

```bash
# Stop only this container; keep synthetic data/logs for another run.
tests/readiness/neo4j/.venv/bin/python tests/readiness/neo4j/run_readiness.py stop

# Optional destructive cleanup: delete only this owned disposable container,
# network and its data/log volumes. Synthetic Graph contents are deleted.
tests/readiness/neo4j/.venv/bin/python tests/readiness/neo4j/run_readiness.py destroy --confirm-disposable-destroy

# Recreate after destruction: new synthetic credential, fresh volumes and fixtures.
tests/readiness/neo4j/.venv/bin/python tests/readiness/neo4j/run_readiness.py up
tests/readiness/neo4j/.venv/bin/python tests/readiness/neo4j/run_readiness.py check
tests/readiness/neo4j/.venv/bin/python tests/readiness/neo4j/run_readiness.py stop
```

Without `--confirm-disposable-destroy`, no deletion command is sent. Ownership,
network peers (also while stopped), and all Compose project members are checked
before cleanup. Unexpected peers/members fail closed. Volume data is not backed up;
`up`/`check` regenerate it from synthetic fixtures. `destroy` removes credential state
but deliberately retains ignored reports, local config and the venv. It never prunes
images or deletes other projects. Do not stage these generated files with `git add -f`.

## Test boundaries

The runner executes selected, unmodified source definitions through AST loading;
it does not import service startup code, dotenv, Mongo clients, or production
credentials. Local Neo4j queries, Pydantic bounds, Social filtering, qualification,
and the exact-to-semantic trigger use repository implementations. Profiles/history
are synthetic in-memory records; Risk state is a test double and LLM selection is
an inert capture. Proposal/quota/consent I/O is not performed or certified here.
Only the isolated function namespace supplies test mode/space evidence; no runtime
environment flag is enabled. Source definitions remain unmodified. Display-text
normalization is an identity test double for pre-normalized fixtures, and HTTP
transport is an in-process bridge. API authentication, HTTP latency, provider
precision, real Risk service and production Mongo behavior are not certified here.

The bootstrap reuses the required DDL, not `project_concept_embeddings`, whose
production writer also refreshes Event relationships. Synthetic AVOIDS exist only
to verify hard-conflict rejection. ANN expands PREFERS only.

The fixture has separate manually labeled positive/borderline/negative multilingual
concept pairs and deterministic alias/case/spacing cases. Geometry vectors carry
`r1l-deterministic-geometry-v1`, never a Gemini fingerprint. With no explicitly
authorized development credential, real-model embedding/calibration is **not run**.
No generic/production key is discovered, copied or used. Provider embedding latency
and semantic precision remain unmeasured.

## Result interpretation

`artifacts/report.json` records actual image/digest/edition/version, schema and vector
integrity, EXPLAIN/PROFILE operators/rows, bounds, fixture scenarios and representative
wall-clock durations. ANN procedure internals are opaque in PROFILE: evidence is
the named vector-index call, verified index metadata and bounded procedure output,
not an assertion that PROFILE exposes the ANN implementation's internal operations.
The hot-concept fixture exceeds every per-concept/candidate limit. Explain/PROFILE
use the **actual queries captured from production function execution**, not rewritten
lookalikes. The first call in a run and subsequent representative calls are separate;
the first call is not guaranteed to be cold on a reused server. This is not a p95
benchmark. All reports are local fixture data, not public runtime
diagnostics.

### Recorded run

2026-09-21: **local functional readiness PASS**, using Community 2026.08.1.
Image digest: `sha256:d8f4c156caa3af76499134947deb11d13042e471d9733060449f2a01eb7a248e`.

- 21 synthetic Concepts / 745 synthetic Users; no production data.
- Vector index ONLINE, dimension 768, cosine (server spells metadata `COSINE`).
- PREFERS-connected coverage 19/19, no missing vectors; all finite, unit norm
  0.9999999999999999–1.0. This is fixture coverage, not production coverage.
- EXPLAIN and PROFILE: `NodeUniqueIndexSeek` for exact lookup and expansion;
  `ProcedureCall(db.index.vector.queryNodes)` for ANN. No `AllNodesScan` or
  `NodeByLabelScan` in these retrieval plans. Procedure internals remain opaque.
- Default bounds 24 neighbours / 8 Concepts / 10 owners per Concept / 40 IDs
  saturated the 40-ID cap. Hard maxima 32/12/20/50 saturated the 50-ID cap.
  Hard-max PROFILE expanded exactly 240 rows (12 × 20), then returned 50.
- A 61-owner Concept returns 10 owners at limit 10. Excluding exactly that window
  returns zero, proving the limit precedes exclusions and does not widen/refill.
- Eight source-backed flow scenarios passed: zero exact, exact rejected by hard
  conflict, block/history, missing profile, wrong cohort; sufficient exact and
  deterministic-alias exact skip ANN; final pool capped at 20. Semantic hard
  conflicts never reached captured ranking; one multi-Concept candidate used
  one slot, and AVOIDS-only did not produce positive retrieval evidence.
- Five representative runs from the first successful invocation: ANN 3.395–12.598 ms;
  expansion 1.553–6.982 ms; local fallback adapter 11.805–26.523 ms. First adapter
  call in that invocation was 375.500 ms. These exclude real provider and HTTP
  transport latency and include test instrumentation. Latest values are in the
  ignored report; provider embedding latency and semantic precision are unmeasured.
- Final repeat also PASS (5 samples): ANN median 5.051 ms, range 2.588–53.189 ms;
  expansion median 2.827 ms, range 1.449–4.677 ms; local fallback adapter median
  14.197 ms, range 11.985–68.137 ms. First call in that repeat: 56.629 ms. The
  variation is retained rather than presented as a p95 or production latency claim.
- Eight negative safety checks passed: reject unpinned/Enterprise image and
  production service port; block non-fixture connect/connect_ex and DNS before
  their real syscalls. Reports change to FAIL on a failed rerun, never stale PASS.
- No authorized dev Gemini credential found. `0.82` is unchanged; no real-model
  score distribution or semantic precision claim is made.

Compatibility observations: 2026.08.1 warns that `db.index.vector.queryNodes` and
unscoped `CALL { WITH ... }` are deprecated. They work on this baseline. Runtime
queries were **not** rewritten. The baseline also enables BINARY quantization by
default, recorded in index metadata. Other server versions may need separately
reviewed local-only boot options (for example Fleet settings did not exist in older
versions); do not infer production compatibility from a tag override or this PASS.

Docker 29.1.3 observation: an `internal: true` bridge left all actual port bindings
null despite a healthy in-container Bolt check. The tooling therefore uses the
dedicated loopback-only bridge above and verifies actual, not just requested,
published bindings before any Graph operation. Only the test container/network
were recreated; no shared resources or volumes were modified.

## R2 preparation: labeled synthetic dataset, not calibration results

`fixtures.json` version 2 contains 25 **unscored** semantic pairs, with stable IDs,
language, and a manual reason for each label:

| Category | Count | Meaning |
| --- | ---: | --- |
| Clear semantic positive | 5 | Close paraphrases suitable for semantic retrieval, not canonical merging |
| Cross-language positive | 5 | EN/ZH descriptions, still separate Concept identities |
| Borderline related | 5 | Related but weaker/different scope; report separately, do not force into positive |
| Hard negative | 5 | Misleading lexical overlap / different word senses |
| Topic-adjacent incorrect negative | 5 | Related topic but incompatible qualifier or unsupported role inference |
| Deterministic alias/normalization | 4 groups / 13 forms | Separate exact-identity tests, **excluded** from semantic calibration |

Pairs are directed: `left` is the requested preference and `right` is candidate-saved
evidence. Human labels are provisional task judgments to review before scoring,
not a new matching policy/ontology. A positive label cannot establish confirmed
shared preference. Negative labels do not become AVOIDS edges or production rules.
No semantic label changes deterministic identity. The existing geometry coefficients
under `concepts` are R1 infrastructure inputs, **not** measured scores for these pairs.

An eventual independently approved dev-only run must record:

1. Dataset version/SHA, source commit, run ID/time, pair IDs/labels/languages/reasons.
2. Requested model, provider-resolved model revision if exposed (otherwise `unknown`),
   provider request IDs when available; never API keys or secret-bearing logs.
3. Logical task `semantic_similarity`, actual API task field or input prefix, requested
   768 dimensions and actual lengths for each returned vector.
4. Original synthetic labels, deterministic canonical labels, exact preprocessed
   model inputs and hashes, normalization/truncation rules and implementation version.
   At this checkout `get_embeddings` uses the prefix
   `task: sentence similarity | query: ` for `gemini-embedding-2`; record the actual
   chosen model's path rather than assuming the same preprocessing for all models.
5. Finite/nonzero checks, float representation, L2 norms before/after normalization,
   and the precise normalization policy. Do not compare mixed embedding spaces.
6. Unrounded **raw cosine** `dot(a,b)/(norm(a)*norm(b))` in [-1,1], formula/precision,
   with missing/failed pairs explicitly separate (never assigned score zero).
7. Per-label and per-language counts, min/median/mean/max, quantiles and histogram
   bins; retain each pair score so small-sample outliers can be reviewed.
8. If also measured locally, store Neo4j ANN scores, server version, index provider
   and quantization in **separate fields**. The existing 0.82 gate applies to that
   ANN score, not automatically to raw cosine. Do not assume score scaling or
   tune the threshold from this tiny preparation set. The [official vector-index
   contract](https://neo4j.com/docs/cypher-manual/current/indexes/semantic-indexes/vector-indexes/)
   reports ANN scores in [0,1]; raw cosine and ANN distributions must not be merged.

No explicitly dev/testing-authorized Gemini credential or approved dev project/config
is currently available in this checkout. Generic key-pool credentials are **not** a
substitute. The current R1 runner blocks provider networking and rejects a supplied
`READINESS_DEV_GEMINI_API_KEY`; it does not silently start calibration. A future
separate dev-only scoring path needs explicit authorization/model/quota boundaries.
All score distributions remain **unmeasured** and 0.82 remains unchanged.

## Historical production provenance: offline evidence inventory

This inventory reads repository source/documents only, not production Graph, live
logs, credentials, or deployment processes. `historical_embedding_fingerprint = unknown`.

| Artifact / potential source | What it establishes | Missing linkage / current assessment |
| --- | --- | --- |
| `docs/REGISTRATION_GRAPH_BOOTSTRAP.md` deployment record (2026-09-15) | Reported checkout/date and an aggregate 20 Concepts with 768-d embeddings | Does not identify per-vector model/task/input/output hash; insufficient |
| `.runtime-logs/registration-20260915/` referenced by that document | A possible operator-retained record source | Ignored/not present as a versioned artifact; not accessed. Would need immutable batch/request/model/input/vector linkage |
| `social/services/concept_embedding_service.py` and its Git history | Current task, 768-d request, label cleaning, L2 normalization and batching | Code intent is not proof of what a historical job executed; cache is process-local, no durable manifest |
| Worker `[CONCEPT_EMBEDDING]` log format in the same source | Aggregate embedded/pending/relevance counts | Format lacks per-vector model/task/hash lineage; counts/time alone insufficient |
| `social/config.py`, `scripts/provision_ayue_v3_env.sh`, `.env.example`, config history | Default/loading policy and model centralization (e.g. `fef723a`) | Real `.env` is ignored and env overrides are possible; current/default model is not historical evidence |
| `social/services/ai_service.py:get_embeddings` history | Provider adapter and model-specific effective task/prefix behavior | Model/task behavior must be linked to the actual executed build and input, not inferred from Git history |
| `matchmaker_agent/agent_api.py:project_concept_embeddings` write contract | Persists vector and `embedded_at` | Does not persist model/task/fingerprint/input hash/output hash/batch ID; timestamp/dimension cannot prove consistency |
| GitHub CI/build records or provider request logs, if retained elsewhere | Potential evidence of an immutable build or model request | No qualifying per-vector manifest found in this checkout; external records not fetched. A build pass or model name alone is insufficient |
| `artifacts/report.json` from R1-L | Synthetic local model, fixtures, plans and local version only | Deliberately **not** historical production provenance |

Do not fabricate `embedding_model`/`embedding_task` on legacy vectors from current
config, dimension, timestamps or release notes. A usable provenance record must link
the precise canonical input and preprocessing, resolved embedding space, output
vector hash, job/request and executed build to the stored vector. No such complete
evidence chain has been established here.

If reliable historical evidence cannot be supplied offline, retain separately
approved **versioned re-embedding** as the next option: pin an embedding spec, make
bounded/checkpointed synthetic-validated batches, write a manifest and versioned
vectors/index alongside old data, verify coverage/integrity/calibration, and retain
rollback. Do not overwrite the shared Event embedding, merge Concepts, create aliases
or modify PREFERS. Production mutation, reader switching and activation require
separate approval; nothing in this review performs them. Both activation flags stay OFF.
