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
