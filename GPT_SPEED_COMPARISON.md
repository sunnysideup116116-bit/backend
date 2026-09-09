# Ollama / GPT Luna Fast speed comparison — 2026-09-09

Tested the actual shared Social completion entrypoints with synthetic input.
No Server services were restarted, no user conversations read, and no domain
tools executed. These are provider-call wall times, not full app/agent latency.

Configuration: Ollama `deepseek-v4-flash:cloud`; GPT `gpt-5.6-luna`, Fast
(`serviceTier=priority`, validated against the live catalog and thread response),
default reasoning (catalog default: medium). GPT settings loaded from Server/.env.

Each workload ran three times per backend, sequentially, alternating backend
order between repeats. A six-call Ollama preliminary run and two-call GPT smoke
test preceded the final twelve-call comparison; they are excluded below.

| Workload | Ollama median (range), seconds | Luna Fast median (range), seconds |
| --- | --- | --- |
| Short Traditional Chinese reply | 4.338 (1.211–11.406) | 6.899 (5.961–10.292) |
| One validated Calendar read proposal | 1.653 (1.017–1.818) | 5.858 (5.658–6.462) |

All twelve final calls returned usable results. All six tool calls produced
the exact requested date and passed the same JSON schema. Short replies were
checked for nonempty text only; this was not a comprehensive quality evaluation.
One final Ollama reply was shorter than the requested 40–60 characters.

Raw wall times in order by repeat:

- Ollama reply: 4.3377, 11.4064, 1.2114.
- Luna reply: 5.9606, 10.2921, 6.8990.
- Ollama proposal: 1.6527, 1.0171, 1.8179.
- Luna proposal: 5.8579, 6.4623, 5.6582.

The currently integrated Ollama path was faster by median for these workloads.
The sample is small and network/provider variability is visible. This does not
establish an intrinsic model-speed ranking. The GPT adapter starts a new Codex
process, checks auth/catalog, creates an ephemeral thread and validates output
on every call. Codex also adds runtime instructions: observed input tokens were
3493/3607 for GPT versus 56/341 for Ollama (tokenizers differ). GPT buffers its
answer; its first callback measures validated completion rather than true TTFT.
No temperature/token-cap/reasoning equivalence is claimed between providers.

Test harness: `/tmp/ayue_provider_speed.py`.
Final detailed results: `/tmp/ayue-provider-speed-final.json`.
Re-running the harness makes billable/quota-consuming requests.
