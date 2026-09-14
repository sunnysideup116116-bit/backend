# 公開阿月 Pi：現行架構

> 檔名因既有連結暫時保留；本文描述目前的 Pi 正式架構。公開阿月的舊 DAG 已退役。

## 執行入口

- `POST /api/direct_chat` 與 `POST /api/direct_chat/stream` 保留原本 request/response contract。
- HTTP adapter 必須先驗證 Appwrite owner；驗證失敗時不保存訊息、不啟動模型、不執行工具。
- 所有已驗證帳號固定進入 `services/ayue_agent/pi/public_turn.py`，沒有帳號 grant、runtime preference 或 request-level fallback。
- Pi bridge、Node.js 22.19+ 與鎖定的 npm dependencies 是正式 Server 啟動必要條件。
- Pi 或 provider 無法服務時回明確錯誤，不改走其他推理引擎。

```text
Public HTTP / App voice delegation
  -> owner authentication
  -> bounded PublicAgentRequestContext
  -> Pi decision loop
  -> typed tool registry
  -> deterministic guard / preflight
  -> read result or preview-bound confirmation
  -> AgentResult / NDJSON final
```

## 責任邊界

| Owner | 責任 |
| --- | --- |
| `routers/public_chat.py` | 驗證 owner、保存訊息一次、公開串流與安全事件投影。 |
| `services/ayue_agent/pi/` | 模型回合、工具選擇、預算、Pi presentation 與結果組裝。 |
| `services/ayue_agent/shared/` | 確認、人選選擇、操作佇列、Calendar preflight、寫入、回覆驗證、短期狀態及診斷。 |
| `services/ayue_agent/tool_registry.py` | 模型可見 schema、risk、參數來源及 executor mapping。 |
| Domain services | Match、Calendar、Profile、Relationship 等正式資料與 CAS/idempotency。 |

模型不可提供 `user_id`、資料 ID、revision 或 expected status。所有寫入先由伺服器綁定正式目標並發布確認卡；owner 使用 opaque choice ID 確認後，ConfirmationManager 才以 CAS claim 並呼叫共用 executor。

## 共用與獨立功能

- 主動關心維持 Profile extraction → follow-up candidate → proactive scheduler → grounded message；它不使用公開 Agent 的推理 loop。
- App voice 與 registration voice 維持 Gemini runtime。App voice 轉交公開阿月時呼叫同一個 Pi HTTP contract；直接行事曆操作維持既有 typed API。
- Private Ayue 維持 `private_v2.py`，使用 private context 與搬至 shared 的 confirmation manager；不改成 Pi。
- Conversation compaction、owner memory、relationship memory、配對、活動與所有背景 worker 保持原本 domain ownership。

## Context、工具與串流

Public Context Builder 保留最近訊息、通過驗證的 conversation continuity、owner 記憶、正式 Match/Calendar/Relationship 投影與裝置位置權限。Pi prompt 只取得 bounded、安全投影；其他房間、未發布結果與 authority fields 不可進入模型。

Pi 的工具 allowlist 由 `pi/registry.py` 明確列出。讀取工具回傳 bounded observation；寫入工具只能建立 pending confirmation。多項操作以自然語言 request 保存於 operation batch，不保存模型推測的資料 identity。

NDJSON 公開事件為 `run_started`、`tool_started`、`tool_finished`、`token`、`final`、`error`。公開層不再映射 Planner、subagent 或 Synthesizer 階段。`agent_mode` 與 `agent_version` 對新公開回覆均為 `pi`。

## 舊狀態退役

現有 collection 名稱為相容資料名稱，暫不重新命名。部署前使用 `scripts/retire_public_dag_state.py`：

```bash
python scripts/retire_public_dag_state.py
python scripts/retire_public_dag_state.py --apply --backup-dir /secure/backup/path
python scripts/retire_public_dag_state.py --verify
```

工具只失效 `surface=public_ayue` 且非 Pi 的未完成確認、人選卡與操作佇列。Private、Pi、完成紀錄及 executing 寫入不修改；存在來源不明或 executing 紀錄時 apply 會 fail closed。

## 啟動與驗證

正式啟動入口只有 `start_all.sh`。它在清 ports 與建立 logs 前檢查 Node 版本、Pi dependency 與 bridge self-check，再啟動固定的 Social `8000`、Risk `8001`、Matchmaker `9001`、Guardrail `8081`。

主要驗證包括 Social 離線 suite、shared confirmation/write/Calendar 測試、Pi bridge 測試、registration/app voice、啟動 lifecycle、Flutter analyze/widget tests，以及 `scripts/build_web.sh`。提交前必須執行 GitNexus `detect-changes --scope all`。
