# R3.2 offline directional contract v2 — human-confirmed development refinement

Production STOP. No P1-A runtime integration, threshold change, Graph access, deploy,
backfill or P1-B. R3/R3.1 evidence is frozen before this document; old prompt/contract
and all 96 human labels remain byte-for-byte unchanged. These cases are development
data, not a final generalization holdout.

## Human decisions confirmed before writing the new prompt

The operator explicitly confirmed in this task:

1. Q=Origami, C=Folding Paper into Shapes is **YES** by ordinary everyday equivalence,
   preserving its original human label. Do not demand the exact conventional hobby
   name or invent cultural/historical/technical restrictions not expressed by Q.
2. Q=Observing Planets, C=Mercury is **ABSTAIN**. Judge ambiguity first and do not use
   the other side of the pair to select an unconfirmed sense of a bare label.
3. For diagnostics after meanings are clear: lexical sense/domain mismatch, explicit
   constraint conflict, role mismatch, equivalent, directed broad/narrow, siblings,
   unrelated. Ambiguity/unknown comes before all these. Secondary never overrides primary.

These decisions are human contract choices, not relabeling to follow model outputs.
The new prompt expresses general rules rather than including fixture IDs or answer
keys. All development labels remain the original frozen annotations, including any
remaining debatable semantic judgments.

## Primary decision

Question: Is the candidate's confirmed preference C sufficient evidence of satisfying
the query preference Q? This is not whether the people might have a good conversation.

- **YES**: ordinary equivalent/paraphrase/translation, or clearly specific C supports
  a broader Q. A longer description is not automatically a narrower semantic category.
- **NO**: clear, insufficient evidence (broad C does not establish specific Q), a
  sibling/different role, an explicit conflicting qualifier, clear homonym-domain
  mismatch, or unrelated meaning. No inferred extra owner preference is permitted.
- **ABSTAIN**: Q or C cannot be understood unambiguously on its own description.
  Missing explicit qualifiers in an otherwise clear C are a NO; ambiguity about the
  meaning itself is ABSTAIN. Do not convert the latter to an overconfident NO.

Composite Q requires every material qualifier/conjunct. C may be more specific while
satisfying the broader Q; reversing a pair is an independent decision. Ordinary named
hobbies and their defining descriptions do not require literal wording or unstated
specializations to count as equivalent.

## Secondary diagnostics and error semantics

The enum set is unchanged: equivalent, candidate_specific_satisfies_broader_query,
candidate_broader_insufficient, sibling_related, role_mismatch, constraint_conflict,
lexical_ambiguity, unrelated, unknown. Choose the most explanatory type after primary.
Lexical ambiguity here means two clear but different senses, not unresolved ambiguity;
the latter is unknown/ABSTAIN. Different domains without a shared ambiguous meaning
are unrelated, not automatically lexical_ambiguity.

For a syntactically valid response with a legal primary and legal secondary,
semantic disagreement between those fields is diagnostic-only. It does not rewrite
primary or turn it into ERROR. Genuine schema/API errors still yield whole-batch
ERROR/fail-closed; an invalid enum is a protocol error, not a semantic NO.

Proposed acceptance rule (offline evaluation only, not a production change):

```text
valid primary YES → eligible for a future bounded Concept expansion
valid primary NO → reject semantic Concept
valid primary ABSTAIN → reject/fail closed
schema/API/internal ERROR → reject/fail closed
```

No relation result creates aliases, merges Concepts, edits PREFERS/AVOIDS, promotes
shared_persistent_preferences, or expands public preference disclosure.

## Two independent experiment axes

1. **Generation reliability:** original frozen v1 prompt and labels; batch 2/4 ×
   current request config versus an actually verified reduced-reasoning control.
   Temperature 0, max_tokens 4096, timeout 60s stay constant so only effort/batch vary.
   Two fixed-shuffle passes (96 cases per condition per pass). Controls must pass a
   live synthetic compatibility probe, not just return HTTP 200.
2. **Semantic refinement:** new versioned v2 prompt, unchanged labels, batch 2/4 and
   verified reduced-reasoning config. Three fixed-shuffle passes for stability.
   Do not tune the prompt after seeing these scores in this run.

Maximum one identical retry for truncation, malformed/empty output or transient
transport/provider failure; never retry valid NO/ABSTAIN to obtain YES. All results
retain first-attempt and eventual metrics, errors and case coverage separately.

The existing provider rejects/does not formally support schema-constrained Cloud
outputs; do not silently substitute local/native endpoints. A stop string that can
cut JSON is not added. Client timeout does not prove server-side cancellation/billing.

## Proposed engineering exit gate, not a production SLA

For each tested final v2 condition, report exactly (without rounding up): first-pass
valid >=99%; eventual valid >=99.9% or this sample 100%; no role/constraint false YES;
primary all-repeat consistency >=99%; development YES precision >=99%. Keep expected
YES/ERROR cases in recall denominators and report recall even though no minimum has
yet been specified. No predicted YES makes precision undefined, not a pass.

Passing only authorizes proposing **R3.3 fresh frozen holdout**, never feature enable.
Failure keeps STOP. Secondary accuracy/stability is reported separately, never used
to override primary decisions. Production fingerprint, version and index readiness
remain unknown, and both semantic runtime flags stay OFF.
