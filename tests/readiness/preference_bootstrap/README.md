# Preference bootstrap tests

Runtime contract: `docs/PREFERENCE_BOOTSTRAP_RUNTIME.md`.
All fixtures are synthetic; no account, credential, raw message or Graph data from
production. The historical model tests remain design evidence, not integration.

## Offline

Use service-specific test environments and `scripts/run_offline_tests.py`:

```sh
python scripts/run_offline_tests.py contracts
python scripts/run_offline_tests.py social
python scripts/run_offline_tests.py matchmaker
bash -n start_all.sh
git diff --check
```

The runner disables dotenv and external sockets. New runtime contract tests cover
HMAC owner/path/body/expiry, strict separate consent, atomic bounded admission,
full Unicode source, source epoch, shared revision/pending fence, default OFF,
one known-rolled-back deadlock retry with shared deadline, no ambiguous retry.

## Opt-in disposable database rehearsal

Requires Docker local socket, Python Social/test dependencies plus neo4j driver.
No production `.env` is loaded. Use a separate checkout and synthetic generated
Neo4j secret only in ignored local artifacts.

```sh
python tests/readiness/neo4j/run_readiness.py up
python tests/readiness/preference_bootstrap/local_mongo.py up
python tests/readiness/preference_bootstrap/run_runtime_integration.py
python tests/readiness/preference_bootstrap/local_mongo.py stop
python tests/readiness/neo4j/run_readiness.py stop
```

Neo4j local baseline `neo4j:2026.08.1` (existing harness); Mongo local baseline
`mongo:8.2.5` replica set. Neither asserts production versions. Mongo binds only
127.0.0.1:27029, named isolated volumes/network, no host data mount, no auth because
synthetic-only/loopback. Graph uses the existing loopback-only harness ports.
Ownership/image/mount/network/volume checks precede resets. Harness network access
is restricted to those two local DB ports. It refuses foreign Graph nodes/owners.

The rehearsal uses real Neo4j/Mongo transactions and actual HMAC internal HTTP
boundary with in-process FastAPI clients. Appwrite authentication is an explicit
synthetic principal override; Matchmaker model construction is stubbed to avoid
cross-service SDK differences. Provider calls=0, all semantic flags OFF. This is
not a full Server/start_all.sh deployment or a real Appwrite authentication test.

21 scenarios: read-only preview; explicit owner/polarity; atomic full set/idempotency;
late duplicate preparation cannot reopen completed projection;
precise rollback/consumed receipt; add-only legacy preservation; stale no-write;
Graph mid-transaction abort; Graph committed/Mongo unavailable; Mongo mid-tx abort;
lost ack; lost successful commit response; pre-Graph failure/late-delivery tombstone;
two competing operations; concurrent duplicate; bootstrap vs ordinary write;
rollback after later edit; old Graph and Mongo sources rejected; ordinary
disable/restore/correction pending fence; another owner retains shared Concept.

Generated `.runtime/bootstrap-*.json`, logs, local state and Neo4j `artifacts/`
are ignored. Never commit generated reports, passwords or DB volumes.

Stop keeps synthetic data for reproducibility. For explicit volume deletion, first
inspect names/labels from `.runtime/bootstrap-mongo.json`, confirm `io.ayue.readiness`
and checkout match; stop then `docker rm <verified-container>`,
`docker network rm <verified-network>`, and
`docker volume rm <verified-name>-data <verified-name>-config`.
Never use glob/production names. Neo4j cleanup/volume-delete is documented in its
existing harness README. No cleanup command in this directory targets other stacks.

## Recorded validation (2026-09-25)

Base: clean backend `99098067f7d462e3f641bbdbc5d31b3436858975`.

- Matchmaker: 220 passed, 15 subtests passed.
- Contracts: 523 passed, 232 subtests passed, including 47 historical design
  cases and 34 new runtime contract cases.
- Social: 1790 passed, 63 subtests passed; 19 failures. Same interpreter and
  isolated runner on clean base: identical 19 auth/Pi fixture failures, same pass
  count. No branch-only failure. Not claiming the whole suite is green.
- Real disposable DB integration: all 21 scenarios PASS, zero provider calls.
- compileall, start_all.sh shell syntax, diff whitespace and credential-pattern
  scan: PASS. No generated artifacts/secrets in intended diff.
- GitNexus: CRITICAL shared owner-memory/projection boundary, 45 reported flows;
  index process caps/dynamic HTTP calls mean the index is not exhaustive. Manual
  writer inventory and real transaction/fault/race tests supplement it. No
  automatic low-risk claim from unresolved symbols. Local index buffer had to be
  raised to 1 GiB; generated index/AGENTS summaries are not part of the PR.

No production service start, production Graph connection, bootstrap, migration,
embedding_v2, semantic enablement or deployment was performed.

## Complete-set capacity regression

`test_complete_set_capacity.py` checks combined totals at both HTTP schemas and
the shared normalizer: 5, 7, 8 and 10 accepted for complete-set; 11 (including
split-polarity totals) rejected before normalization. Add-only and ordinary
memory limits remain unchanged under configured limits 1/3/5/6/8.

The disposable runtime rehearsal additionally commits 5/7/8/10-item full sets
with real Graph/Mongo transactions, including single-polarity 10-item sets. Each
preview inventories all 12 synthetic legacy associations, retires exactly those
associations, preserves the second owner, and projects every new item. Overflow
and add-only-overflow requests prove zero mutation; the ordinary 7-item write is
still rejected under the unchanged default cap of 6. No real account text is used.

## Confirmed legacy-compound compatibility

`test_legacy_compound.py` covers a single lossless v2 identity from an exact,
same-owner fresh legacy locator; both polarities; absent/other-owner/inactive/
duplicate/v2 sources; polarity changes; tampered proof/retirement; unchanged
atomic behavior; ordinary Social memory rejection; add-only rejection; and
required complete-set consent. Client schemas reject a supplied snapshot override.

The real disposable rehearsal adds seven scenarios: literal compound preview /
commit / synced Mongo projection / audit / duplicate commit / precise rollback;
add-only, new-text and polarity rejection; changed-association stale rejection;
signed retirement-locator fault injection rejected with zero mutation; ordinary
memory façade cannot reuse the exemption. Full-text key is present; sub-fragment
keys are absent. Another owner referencing the old Concept is unchanged.

Compatibility gate against clean `main@976b06e`:

- Contracts: 631 passed, 232 subtests.
- Matchmaker: 236 passed, 15 subtests.
- Social: 1852 passed /34 failures, 63 subtests; clean main has exactly the same
  34 failing testcase names and first-line signatures. Branch-only failures: 0.
- Actual disposable Graph + Mongo replica set: all **41 scenarios PASS**.
- compileall, `bash -n start_all.sh`, diff check: PASS.

The existing 9001 ordinary atomic decomposition remains unchanged. The exemption
is not passed to that path; the owner-facing Social write façade still rejects
compound items. No general atomicity, cap, relation policy or semantic flag change.
