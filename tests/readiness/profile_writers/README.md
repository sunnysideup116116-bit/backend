# Profile writer concurrency rehearsal (synthetic local only)

No dotenv/production configuration, provider, Appwrite or Neo4j connection. The
network guard allows only127.0.0.1 at the verified owned disposable Mongo port.
Reuses `tests/readiness/preference_bootstrap/local_mongo.py`: fixed local baseline
mongo:8.2.5, loopback27029, isolated named network/volumes, replica set bootstrap_test.
This is not an assertion of production server version/compatibility.

```sh
python scripts/run_offline_tests.py social
python scripts/run_offline_tests.py contracts
python scripts/run_offline_tests.py matchmaker
python tests/readiness/preference_bootstrap/local_mongo.py up
python tests/readiness/profile_writers/run_concurrency.py
python tests/readiness/preference_bootstrap/local_mongo.py stop
bash -n start_all.sh
git diff --check
```

Use service-specific installed test dependencies (no production env links).
The fixture database is exactly `profile_writer_fixture`, collection profiles.
The test creates **local-only** UNIQUE(user_id), verifies image/resource ownership,
and only clears owners prefixed synthetic_. It refuses non-synthetic records or
an unknown pre-existing fixture database. All fixture profiles are removed after
PASS; stopped container/volumes remain for reproducibility. Volume deletion follows
the existing bootstrap harness's explicit validated-resource instructions.

Eight scenarios:

1.32 concurrent create-if-absent requests, exactly one profile.
2.Create vs guarded interest initialization; conditional miss remains update-only;
  conditional upsert is rejected before database access.
3.Actual registration mirror vs actual memory facade plus14 stub creators,
  12 rounds×16 workers. Mongo operations are real; Graph HTTP response and
  registration enqueue are explicit stubs, zero external side effects.
4.32 empty/default writers cannot change an existing populated profile.
5.Real server E11000 after a winning insert; loser reads winner and performs one
  exact-_id update-only recovery. Content and one notice retained.
6.Successful write followed by simulated lost response; helper never repeats it.
7.Real transaction E11000 propagates, without any read/retry inside aborted txn.
8.20 rounds×48 workers **from absent owner**, rich registration operator update
  vs stub defaults plus disjoint updates; one document and every field preserved.

Recorded run: all8 PASS, **1,248 parallel operations** (not DB command count),
median≈10.6ms / p95≈17.3ms for the local concurrent callback wall time.
These are observations, not a production benchmark/SLA. Every creation scenario
ended with count(user_id)=1 before exact synthetic cleanup.

Generated timing/result JSON resides in ignored `.runtime/profile-writer-concurrency.json`;
never commit DB volumes, logs, credentials or generated data. Runtime flags all OFF.
No new server is started; this is component/domain-function+real DB integration,
not a production HTTP deployment or a live LLM/Graph test.

## Regression record (2026-09-25)

- Social: 1824 passed, 63 subtests passed, 19 failures. Clean main d90d100 with
  the same PyMongo4.7.2/test environment:1790 passed and the exact same19 failures
  (existing Appwrite JWT/Pi fixtures). New helper cases34 passed; branch-only0.
- Contracts:525 passed,232 subtests passed (including two new wiring/schema guards).
- Matchmaker:220 passed,15 subtests passed.
- compileall, start_all.sh shell syntax, git diff/check and secret-pattern scan:PASS.
- GitNexus: shared updater CRITICAL,21 direct dependencies/20 affected flows;
  overall diff reports67 flows. Dynamic HTTP/index caps require manual review too.
  Caller changes preserve operators, predicates, domain effects and return values.

This is local validation, not remote CI or a production schema/rollout approval.
