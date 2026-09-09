# Codex connection reuse and streaming — 2026-09-09

Implemented bounded process reuse (default two workers), exclusive leases,
fresh ephemeral threads per request, short-lived metadata cache, bounded worker
recycling, and Social/exit cleanup. Ordinary text can stream final-answer deltas;
JSON and tool proposals remain buffered and schema-validated. Model remains
`gpt-5.6-luna`, service tier Fast (`priority`), default reasoning (`medium`).

Same synthetic short-reply and Calendar proposal workloads as the previous
comparison, three requests per workload/provider. Pre-change GPT was copied
before edits and tested first (six calls). A two-call optimized smoke test was
followed by the twelve-call final alternating Ollama/GPT comparison in a fresh
process. Times include client overhead; the first optimized GPT call is cold,
later requests can reuse its worker. This is not a full HTTP/app latency test.

| Metric (median seconds) | GPT before | GPT after | Ollama current |
| --- | --- | --- | --- |
| Complete short reply | 7.3389 | 7.8397 | 2.5659 |
| First visible text callback | 7.3386 | 7.0246 | 2.4099 |
| Complete validated tool proposal | 5.5767 | 5.2925 | 1.6458 |

Tool proposal median improved about 5%; short-reply completion did not improve.
Optimized GPT first text arrived 0.55–0.82 seconds before completion. The sample
is small, provider/network fluctuations are large, and these results do not
establish a statistically significant model-speed improvement. Reusing Codex
does remove repeated startup and metadata work, but this experiment does not
support the earlier expectation of a large latency reduction. Ollama remains
faster for these current workloads.

All six before and twelve final calls succeeded. Tool proposals matched the
requested date and passed JSON schema checks; no tools were executed. Ordinary
text was checked only for nonempty content, not comprehensive answer quality.
No application user messages were read, and no Server services were restarted.

GPT after full reply seconds: 7.7277, 8.0475, 7.8397.
GPT after first callback seconds: 6.9782, 7.4960, 7.0246.
GPT after proposal seconds: 4.6595, 8.5379, 5.2925.
GPT before full reply seconds: 6.3611, 7.3389, 8.5130.
GPT before proposal seconds: 5.5767, 5.8976, 4.9817.

Inputs/prompts are identical at the application boundary. Plain GPT replies now
use ordinary text rather than a JSON envelope, as required for streaming.
Codex adds runtime instructions; input sizes/tokenizers and sampling controls
are not equivalent across Ollama and GPT. Reasoning strength was not lowered.

Detailed data: `/tmp/ayue-speed-before-pool.json`,
`/tmp/ayue-speed-after-pool.json`; harness `/tmp/ayue_provider_speed.py`,
baseline wrapper `/tmp/ayue_speed_before_pool.py`.
