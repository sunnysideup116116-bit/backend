# Public Pi runtime interfaces

> 本文件是現行介面。舊 Planner／Scheduler／subagent 文件只供歷史查閱。

## Public HTTP boundary

`routers/public_chat.py` 驗證 owner 後建立 `PublicAgentRequestContext`，呼叫 `run_public_agent_turn()`。公開聊天的 JSON 與 NDJSON URL、choice fields、interaction blocks、sources 與主要 response shape 保持相容；新回覆的 `agent_mode`、`agent_version` 固定為 `pi`。

未驗證 owner 回 `401`；已驗證 owner 的資源權限錯誤回 `403`；Pi bridge 不可用回 `503`。這些失敗發生在保存 owner message 及任何工具執行之前。

## Pi loop

```python
run_pi_public_turn(
    ctx: PublicAgentRequestContext,
    *,
    on_progress=None,
    on_token=None,
    debug_enabled=False,
) -> AgentResult
```

Pi 每回合只看 `pi/registry.py` 的明確 allowlist。Python 保有 provider credentials、context、budgets、guards、confirmations 與 writes；Node bridge 只協調模型與工具呼叫訊息。

## Shared confirmation and writes

`shared/confirmation.py` 擁有 public/private surface、preview fingerprint、opaque choice、TTL、CAS 與 idempotent execution。新 public record 明確標記 `source_engine=pi`；private record 標記 `private_v2`。

Calendar command/preflight、contact selection、operation batch、write executor、public reply validation、debug trace 與短期狀態皆位於 `services/ayue_agent/shared/`。Pi 與 Private 不得匯入已退役的 runtime package。

## Background and voice boundaries

- Profile extraction 與 proactive care 是 owner-message 背景流程，不由 Pi 工具啟動。
- App voice 的 `ayue.public_query` 使用公開 Pi HTTP API；private query 繼續使用 Private V2。
- Registration voice 與 App voice 的 Gemini session 不改為 Pi。
- Domain writes 始終由既有 Calendar、Match、Relationship、Profile services 擁有。
