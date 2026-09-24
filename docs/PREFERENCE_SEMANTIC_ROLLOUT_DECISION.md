# Preference Semantic Retrieval — research closeout decision

Decision date: 2026-09-24. Owner decision: stop the current P1-A production
qualification effort; preserve the implementation and frozen research evidence.

```yaml
P1-A implementation: complete
local Neo4j readiness: PASS
embedding quality investigation: complete
directional relation design: viable
production qualification: NO-GO
semantic runtime mode: "OFF"
disposition: deferred until a validator backend can satisfy frozen quality/reliability requirements
```

`viable` describes the directional relation-only design plus deterministic acceptance
mapping, not a qualified validator backend or production approval. Local readiness
PASS concerns a disposable synthetic Neo4j baseline, not production compatibility.
This is deferral, not permanent abandonment, and does not remove the P1-A code.

## Evidence for NO-GO

| Finding | Existing evidence |
| --- | --- |
| A single ANN threshold cannot provide sufficient precision while retaining recall | [R2.5 quality gate](../tests/readiness/neo4j/R25_SEMANTIC_QUALITY_GATE.md): ANN .95 precision 86.7% / recall 97.5%; .98 precision 100% / recall 22.5% on the labeled exploratory sample |
| Downstream Matchmaker cannot reliably correct related-but-not-satisfying candidates | [R2.5 downstream replay](../tests/readiness/neo4j/R25_SEMANTIC_QUALITY_GATE.md): three of four negative pairs still selected; this was not a full production E2E |
| Directional validator semantic correctness is promising, but insufficient for release | [R3.3](../tests/readiness/neo4j/R33_RESULTS.md): acceptance precision 100%, recall 99.3056%; final FAIL because first-pass validity 98.9583% missed its frozen gate |
| DeepSeek execution reliability did not pass; later final consistency also failed | [R3.4](../tests/readiness/neo4j/R34_FINAL_RESULTS.md): first-pass validity 98.75%, binary consistency 98.75%; final FAIL remains permanent |
| Gemini structured backends did not pass availability/semantic gates | [Backend selection](../tests/readiness/neo4j/VALIDATOR_BACKEND_SELECTION_RESULTS.md): 2.5 quota failures plus valid ambiguity false accepts (precision 95.24%); 3.1/3.5 unavailable/incompletely qualified, not semantic FAIL claims |
| Local Qwen did not pass semantic gates | [Backend selection](../tests/readiness/neo4j/VALIDATOR_BACKEND_SELECTION_RESULTS.md): schema validity 100%, precision 33.59%, broad/role/constraint false accepts |
| No candidate backend passed all frozen development requirements | [Prospective protocol](../tests/readiness/neo4j/VALIDATOR_BACKEND_SELECTION_PROTOCOL.md) and [results](../tests/readiness/neo4j/VALIDATOR_BACKEND_SELECTION_RESULTS.md); selection is empty |

All results are synthetic sample evidence, not population guarantees. R3.3/R3.4
labels lacked independent human review; neither final FAIL may be retroactively
changed, reused for configuration/model selection, or turned into a new final PASS.

## Operational disposition

- Keep `MATCH_PREFERENCE_SEMANTIC_MODE=off` and
  `MATCH_PREFERENCE_SEMANTIC_EMBEDDING_SPACE_CONFIRMED=off`.
- Keep runtime threshold `.82` unchanged; no new prompt/model/backend sweep,
  fresh holdout, retry experiment or production qualification run is authorized.
- Existing exact matching and safety/lifecycle behavior are unchanged. No P1-B,
  Event pipeline change, validator runtime integration, deployment, migration,
  production Graph access or backfill is part of this closeout.
- Retain logical evidence commits. Generated JSON reports, provider outputs,
  vectors, local secrets and environments stay ignored, not in this PR.
- Any future resumption needs a separate approval and a backend satisfying the
  frozen quality/reliability requirements, not a relaxed gate or another attempt
  against the two retired final holdouts. Independent blind labels by another
  teammate and pre-score human adjudication remain required under the
  [review process](../tests/readiness/neo4j/INDEPENDENT_LABEL_REVIEW_PROCESS.md).
- Production historical embedding fingerprint remains `unknown`; production
  Neo4j version and index coverage/integrity are not established by local tests.
  Those remain separate blockers even if a future validator qualifies.

This decision record supersedes prospective "next experiment" suggestions in
the archived research documents. Historical protocols/tooling are evidence and
reusable offline components, not authorization to resume experiments automatically.
