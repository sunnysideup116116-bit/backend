# V2 Preference Bootstrap — internal pilot contract (DESIGN ONLY)

Base: backend `main@99098067f7d462e3f641bbdbc5d31b3436858975`.
Branch: `codex/preference-bootstrap-contract`.
Status: proposed contract + synthetic in-memory tests; **no production adapter,
endpoint, apply CLI, deployment, Graph connection, data mutation or migration**.
P0/P0.1 production smoke is accepted PASS. Personalized LLM opening validation
uses a functioning safe fallback; that accepted non-blocking issue is unchanged.

## 1. Product objective and boundary

First cohort: approximately 5–10 consenting team-member accounts, ordinarily
3–5 explicitly reconfirmed atomic preferences each (PREFERS and AVOIDS combined).
This creates new owner-grounded assertions, not reconstructed historical facts.
No old Concept is re-keyed, merged, repaired or declared equivalent to a new one.
An operator selects the pilot cohort but **cannot consent on behalf of owners**.
No real account list, source text or credential is in these fixtures.

Semantic runtime flags remain OFF. Historical embeddings are never inputs to
bootstrap. No embedding calls, ANN, relation validator, matching request, proposal
or automatic invitation is part of bootstrap. `embedding_v2` + explicit fingerprint
+ dedicated index is a separate later approval. P1-B is excluded.

## 2. Findings from current code, not assumptions

| Current component | Consequence for bootstrap |
| --- | --- |
| `concept_identity.canonicalize_fresh_concept` and `stored_concept_identity` | Reuse fresh OpenCC1.4.1/s2twp → complete normalized source → alias/digest. Persisted v2 reads never rerun fresh conversion. |
| `memory_service.validate_memory_proposals` / `apply_profile_memory_proposals` | Reuse bounded atomic/mixed-polarity validation. Existing HTTP facade can queue an ordinary memory retry and refresh only a bounded cache; it is not a full-set transaction coordinator. |
| `agent_api.apply_memory` | Graph marker + new owner edges share one transaction. It can skip inadmissible items and replace same-key PREFERS/AVOIDS/CURRENTLY_WANTS. Bootstrap must require exact all-item admission, not silently accept a subset or delete recent context. |
| `registration_graph.assert_existing_preference_identities` | Existing v2 Concept must be independently valid; normal key reuse does not overwrite source/label/hash. This protects globally shared nodes. |
| `agent_api.memory_action` disable/restore | MEMORY_DISABLED records only original relation/expiry and uses one merged edge per owner/Concept. It is not a lossless per-association, batch-bound rollback journal. Do not reuse it for this retirement. |
| `list_memories` / `_trait_stances` / context preview | Existing 30 /20 /12 /8 caps are read/display limits, **not a complete owner inventory**. Full-set retirement must read a separate owner-scoped complete snapshot. |
| `candidate_qualification` | Unknown legacy + potentially opposite verified evidence remains `indeterminate_legacy_conflict`. Do not relax it. |
| Writer locking | apply/registration increment `registration_projection_lock`; disable/restore/correction and other paths do not share a complete revision/fence protocol. That counter is NOT a universal preference revision. |
| Mongo projections | Active Graph relations power matching; previews power owner context. `preference_facts` additionally records explicit feedback provenance and is not populated by every ordinary write, despite its broad module docstring. Inspect actual Graph + owner facts/cache; do not infer one is a full copy of the other. |
| Existing manual memory route | `add_profile_memory` accepts a body user_id. Bootstrap must instead derive owner from authenticated Appwrite identity and use a protected internal write boundary; body owner/admin possession alone is not consent. |

No existing symbol is modified in this design. Graph-index results do not resolve
all HTTP/dynamic callers (`memory_action` impact UNKNOWN); direct source inspection
confirms the routes and Social caller. Any eventual writer/fence change needs a
new shared-boundary impact review before implementation.

## 3. Two deliberately different owner actions

### Add-only (default)

“只新增我這次確認的幾筆偏好；這不是完整清單。”

New rows use the normal Identity-v2 admission/write rules. No active legacy edge
is retired. Existing opposite v2 stance, a same-key CURRENTLY_WANTS collision or
duplicate canonical rows require an explicit edit/repreview, not implicit removal.
Add-only does not promise `v2_clean`; an unresolved legacy conflict may remain.

### Explicit full-set confirmation (separate action)

Owner-facing confirmation must say, in substance:

> 我已分別檢查「喜歡／偏好」與「避免／不喜歡」。以下兩欄合起來，是我目前
> 要讓阿月使用的完整偏好集合。我同意預覽列出的舊版連結停止作為目前偏好依據。
> 舊項目沒有列入，不代表它變成相反偏好；原始紀錄將保留供查核與還原。

- No prechecked full-set/retirement consent. A chat “OK”, model tool call, admin
  approval, opening this page, or confirming one item is insufficient.
- Both polarity panels need explicit review. Empty AVOIDS must explicitly read
  “我確認目前沒有要保留的避免項目”; omission/default/missing JSON is not this statement.
- New text is typed/reconfirmed by the owner. Legacy display text is labelled
  `legacy_unknown / 可能缺少限定詞`; it is NOT copied into a preaccepted new row.
- True complete v2 rows may be shown for review but still need confirmation.
  Full-set must include every currently active verified v2 identity + polarity.
  Omitted/reversed v2 items are blocked; use the normal explicit edit/disable first
  and repreview. This phase retires **legacy associations only**, not old v2 ones.
- An empty whole set is outside this pilot's add/bootstrap contract. Do not turn
  an empty form into clear-all; use a separately reviewed explicit deletion flow.
- With full-set confirmation only, retire *all and only* active legacy PREFERS /
  AVOIDS associations in the displayed owner snapshot, including negatives and
  separate duplicate legacy associations. This is set supersession, not a guessed
  one-old-Concept → one-new-Concept mapping.
- An invalid node claiming v2 is corruption, not “legacy to retire”. STOP for
  investigation; no inferred repair, suffix reconstruction or polarity conversion.

## 4. Preview and confirmation binding

Proposed phases (not implemented endpoints):

```text
authenticated owner enters two typed polarity lists
→ whole-input admission / canonicalization
→ complete bounded owner inventory + candidate Concept key checks
→ read-only preview
→ owner explicitly confirms this exact preview
→ revision/fence recheck → one Graph transaction + journal
→ projection/outbox acknowledgement → preference-state readiness
```

Resource envelope:

- New item limit `min(5, durable_memory_limit())` for this initial pilot; current
  normal default6/hard8 is not raised. Target3–5, minimum1. Larger actual complete
  sets STOP for separately reviewed handling, never truncate or split one complete
  confirmation into independent partial commits.
- Normal raw **Unicode code-point** bound ≤500, plus normalized/converted ≤500;
  ≥501 rejects the whole request. NFKC/converter expansion can reject ≤500 raw.
  Display labels stay presentation-only. No semantic source prefix slicing.
- Proposed inventory hard bound100 owner associations with101st-row overflow
  detection. This is an operational cap, not a Neo4j limit. Bounded direct owner
  traversal only; incomplete/paged/overflow inventory cannot authorize retirement.
- Proposed preview expiry10 minutes, unrelated to recent-context TTL. Any relevant
  owner mutation or normalizer/policy version change invalidates it immediately.
- Process one owner per transaction, at most10 explicitly selected pilot owners;
  no automatic user discovery, full Graph scan or bulk apply.

Private dry-run response MUST separate:

1. **Concepts to create:** full semantic_text, display_label, computed v2 key,
   version/source hash; distinguish safely reused existing verified Concepts.
2. **Associations to create:** owner, canonical key and explicit PREFERS / AVOIDS;
   unchanged same-polarity v2 edges are listed as unchanged, not rewritten.
3. **Legacy associations to retire:** exact owner/source node, original relation,
   physical snapshot locator + property hash, preserved original properties,
   stored legacy label with unknown-fidelity warning, operation reason.
4. **Untouched:** other owners, all legacy Concept properties/keys, inactive memory,
   existing unchanged v2 edges, recent context/expiry, profile interests/traits,
   history/chat/proposals/consents, vectors/indexes/flags.

Canonical duplicates/alias duplicates are a validation error requiring a visible
single-row repreview, not first/last-wins. Opposite relations for the same identity
are a whole-request error. No automatic enum/polarity inference from other rows.

Preflight must also verify the authenticated account/profile and exactly one
existing Graph User with that account ID. This first bootstrap does not create
accounts, repair nicknames or guess a missing/duplicate Graph identity. Resolve
those separately through the normal registration/identity lifecycle, then preview.
The in-memory model takes the authenticated owner/existing identity as a trusted
test precondition; it does not pretend to validate live Appwrite tokens or Graph
uniqueness. Labels are concept fields beside an explicit polarity selection;
polarity-bearing/ambiguous prose needs visible clarification/reconfirmation, not
LLM-inferred polarity. Normalized/alias-resolved full text is shown before consent.

The server issues an opaque, one-use confirmation capability bound to authenticated
owner, operation ID, policy + fresh-normalization version, full submitted item
digest, exact create/keep/retire plan hash, owner snapshot hash/revision and expiry.
The client submits this capability, not trusted “confirmed=true” or a freely
chosen owner ID. The model receipt in tests is a **trusted-test stand-in**, not an
authentication implementation. No API key/JWT is written into an audit record.
The protected Social→9001 bootstrap mutation must carry a distinct, server-owned
operation capability/scope; the existing matching quota-context header is not
authorization to retire preferences. Select the concrete transport adapter in
implementation review, retaining the newly fixed shared configuration contract.

## 5. Apply transaction, writer fencing and projection correctness

These are prerequisites for a future implementation, not capabilities this model
claims already exist:

1. A shared owner write fence + monotonic preference revision must cover manual
   add/action, extraction, correction, registration/bootstrap replay, decline
   feedback and memory retry workers (and any other identified Graph writer).
   Drain/park in-flight old-epoch work before preview. Pending/unknown work blocks
   confirmation. New owner messages after commit may write normally in the next
   epoch; stale pre-confirmation jobs cannot replay and resurrect old state.
2. Acquire the same owner serialization guard; re-read complete snapshot and all
   relevant existing Concept identities within the transaction. Any mismatch,
   new owner write, corrupt v2, incomplete inventory or expired receipt aborts.
   A Mongo-only lock or revision comparison without all writers participating is
   insufficient. External administrative writes require the same controlled window.
3. Reuse/refactor the *normal* admission/identity/Graph-write helper inside this
   transaction; do not build a parallel canonicalizer. Require **all** confirmed
   key+polarity pairs to be admitted, including protected-content/sensitive-memory
   policy. Current skip-invalid behavior must not commit a successful subset.
   If validation changes an item after preview, abort and repreview, not silent edit.
4. Create missing verified v2 Concepts with ON CREATE identity metadata; reuse
   validated existing nodes without rewriting their source. Create only listed new
   owner relations, preserving unchanged edges. No context or inactive-edge removal.
5. Journal each listed legacy association's original relation and lossless property
   before-image; move it out of active PREFERS/AVOIDS into a dedicated batch-bound
   retirement archive (`PREFERENCE_SUPERSEDED` conceptually, **not MEMORY_DISABLED**).
   Keep shared legacy nodes and every other owner's edges unchanged. Merely setting
   `active=false`/`retired=true` on a PREFERS edge is forbidden: existing readers
   do not universally check those properties.
6. Graph changes, receipt consumption, operation status, before/after images and a
   durable projection-repair marker commit atomically. An operation-ID uniqueness
   contract is needed; any new audit metadata constraint requires reviewed setup,
   not an automatic production schema change in this design.
7. Graph commit and Mongo projection are **not** one distributed transaction.
   Mark owner projection pending: old preview must not fall back into context or
   eligibility. Invalidate only this owner's cache and explicitly mapped legacy
   feedback facts, then rebuild from active verified Graph truth. No unrelated
   profile rewrite or blanket deletion of Mongo history. Unmapped active/orphan
   legacy facts or ambiguous mirror ownership block readiness for investigation.
8. Projection retries reuse the committed operation, not the ordinary memory
   outbox's apply call. Response loss uses operation-status reconciliation with the
   same idempotency key. Never create a new batch or reinterpret failure as “not
   committed”. Cancel is read-only before apply; after commit it requires rollback.

`preference_state_ready` requires active verified v2 only, at least one PREFERS,
no unresolved legacy/corrupt/mirror state, no pending old-epoch work and projection
acknowledgement. It is **not** automatic match eligibility or semantic readiness.
Existing blocks, history, hard conflicts, quotas, dedupe and mutual consent still
run normally. There is no `ignore_legacy_conflict` flag.

Inactive MEMORY_DISABLED rows remain inactive and unchanged. If a later normal
restore reintroduces legacy evidence, readiness must be invalidated/recomputed;
bootstrap is not a permanent exemption from safety gates.

Existing derived Event relevance links and in-flight match reasoning may reflect
the old preferences. No Event algorithm/proposal rewrite belongs here. Before an
actual rollout, coordinate the scoped owner fence with in-flight match jobs and
existing derived-projection invalidation/rebuild; if safe freshness cannot be
demonstrated without wider changes, STOP for review. Archived messages and already
committed proposals are not retroactively edited by bootstrap or rollback.

## 6. Audit and precise rollback

Private operation journal: operation/owner IDs, owner-origin confirmation evidence,
policy/normalizer version, snapshot/plan hashes, exact logical association refs,
original relation/property before-images, created/reused Concept keys, after-image
guards, revisions, Graph/projection state, retry/rollback outcomes. Preserve the
receipt and operation history after rollback. Application logs/aggregate pilot
metrics contain counts/status/error codes only, not raw memory text or credentials.
Owner preview is private; operators need explicit access, and population reports
are counts only. No payload goes to an LLM or another user's rationale.

Rollback is its own authenticated owner-confirmed (or separately explicitly
authorized operator) action bound to one operation and an unchanged after-image:

- Check owner revision, every touched edge and retirement journal. Later owner
  edits/in-flight work cause `rollback_conflict`, not a forced restore or partial
  guessed reverse migration.
- In one Graph transaction remove only this operation's new owner edges and
  restore each retired edge's **original polarity and complete properties**.
  Never convert missing/unknown AVOIDS into PREFERS or restore via ordinary
  MEMORY_DISABLED. Serialization must preserve Neo4j property types; unsupported
  types block before apply, not stringification/truncation.
- Neo4j physical relationship element IDs cannot be restored. Rollback restores
  logical association/multiplicity/properties; journal maps original and replacement
  physical IDs. Owner revision remains monotonic, not rewound. Test IDs are logical.
- Delete newly created v2 nodes only when this batch created them, their recorded
  state is unchanged and no other relation/consumer depends on them. If another
  owner adopted a node or a normal worker enriched it, leave it and explicitly
  report `owner_restored / retained_shared_or_enriched_concepts`. This is not a
  claim of byte-identical global Graph rollback. Preexisting/legacy nodes never
  get deleted. Later vector preparation is not rolled back by this bootstrap.
- Invalidate/rebuild the owner's projection again; no stale cache image is blindly
  restored. Retry is idempotent; a rolled-back operation cannot be applied again
  with its consumed receipt. No restoration of past chat/proposal decisions.

## 7. Synthetic isolated verification and limits

`bootstrap_contract_model.py` is an in-memory copy-on-write reference state machine.
It imports the **actual** deterministic Identity-v2 functions, but has no HTTP,
Graph/Mongo driver, credential loader or executable apply command. Its audit hashes
bind plans; they are not a production receipt-signing scheme.

Tests cover owner consent/cross-owner rejection, partial versus full set, explicit
empty negative list, atomic polarity/alias handling, 41/499/500/501 code points,
same40-prefix qualifiers, legacy collisions, complete inventory/overflow, unchanged
shared nodes/other owners/inactive/context, existing-v2 preservation, corrupt-v2
fail-closed, stale/expired/tampered confirmation, injected partial failures,
idempotency, projection failure readiness, rollback conflict and shared-node reuse.
A10-account synthetic rehearsal creates39 newly confirmed items with separate
receipts. Real `validate_memory_proposals` and unchanged `candidate_qualification`
are exercised with synthetic memory reads: legacy indeterminate conflict remains
blocked before retirement, verified exact evidence works after full-set, and a
verified hard conflict still rejects.

Tests use `scripts/run_offline_tests.py`; dotenv/provider/DB network is disabled,
and focused tests also block socket creation. They do **not** prove production
transactions, auth capabilities, all-writer fencing, cache integration, distributed
failure recovery, schema compatibility or independent human confirmation. Those
remain implementation/isolated integration gates. No production PASS is inferred.

Reproduce with an isolated Social-compatible test venv:

```bash
python scripts/run_offline_tests.py contracts -k bootstrap_contract
python scripts/run_offline_tests.py social -k 'identity_v2_parity or identity_v2_memory_boundaries or preference_off_parity or match_qualification'
python scripts/run_offline_tests.py matchmaker
bash -n start_all.sh
git diff --check
```

### Recorded isolated results (2026-09-25)

| Check | Result |
| --- | --- |
| New bootstrap contract scenarios | 47 passed |
| Existing Social identity-v2 parity, memory boundaries, OFF parity, qualification | 142 passed; 3 subtests |
| Existing full Matchmaker suite | 220 passed; 15 subtests |
| Full Contracts including these new scenarios | 489 passed; 232 subtests |
| compileall / shell syntax / whitespace | PASS |
| Production runtime diff | 0; only3 new files in this readiness directory |

The first test invocation identified two assertions in the new tests needing
correction (archive row-order assumption; actual result field is
`hard_conflict_keys`). The production behavior/gate and model decision rules were
not changed to obtain PASS. Generated logs remain ignored in `.runtime/`.

No real Neo4j/Mongo transaction, real owner confirmation, provider/embedding call
or live account was used. This is a design acceptance aid, not production bootstrap
completion or permission to proceed past the STOP below.

## 8. Stop / next approval

This phase ends at design + isolated verification. No commit/push, production
connection, account selection/contact, Graph write, apply capability, flag enable,
embedding_v2/index preparation, migration or P1-B is authorized/executed here.

Next approval, if this contract is accepted: implement the owner-authenticated
preview/confirmation and transaction/fence/projection/rollback adapters in an
isolated branch; repeat synthetic local-Graph and concurrency/fault-injection
tests. Only after that review collect5–10 real owner confirmations and separately
authorize those exact plans. Corpus preparation is later still.
