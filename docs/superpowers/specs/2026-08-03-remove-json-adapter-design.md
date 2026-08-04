# 設計: 移除 JSON Adapter，僅保留 Function Calling

## 背景

公開阿月 V2 的 Planner 目前有兩種模式：

1. **JSON adapter** (`plan_turn_v2`) — 用 `generate_chat_completion(json_output=True)` 讓模型輸出整個 `AgentDecision` JSON
2. **Function calling** (`plan_turn_v2_function_calling`) — 用 Ollama 原生 `tools` 參數

`runtime.py` 依 `get_planner_mode()` 結果二選一呼叫。目標是移除 JSON adapter，只保留 function calling。

## 差異分析

JSON adapter 經由模型輸出 JSON，能產生 function calling 目前未實作的 5 項欄位：

| 欄位 | JSON adapter 來源 | Function calling 現況 | 影響 |
|------|-------------------|----------------------|------|
| `opportunity_signal` | LLM JSON 欄位 | 預設 `"none"` | `_handle_match_opportunity` 的 social_opening 路徑無法觸發 |
| `opportunity_evidence_span` | LLM JSON 欄位 | 預設 `None` | 同上，且 Guard 驗證會失敗 |
| `recent_context_followup` | LLM JSON 欄位 | 預設 `"none"` | 「想做事但沒說活動」時不會主動追問 |
| `clarification_goal` + `missing_fields` | LLM JSON 欄位 | 預設 `None` / `[]` | 日曆新增缺欄位時不走結構化 draft 路徑 |
| `confidence` | LLM JSON 0-1 | 固定 0.85/0.7 | Guard `low_confidence` 闸門失去意義 |
| `evidence_span` | LLM 提供原文子字串 | 用整個 `ctx.message` | 寫入動作失去「模型指出授權段落」的安全檢查 |

## 補齊方案

### 方案: 模型回覆後追加結構化後設資料

讓 function calling 模式在模型未呼叫工具（FINAL 路徑）時，於 `content` 中以可解析格式附帶後設欄位。`plan_turn_v2_function_calling` 解析後填入 `AgentDecision`。

**格式**: 在回覆末尾附加一行 `\n[[meta]]{"opportunity_signal":"social_opening","opportunity_evidence_span":"...","confidence":0.9}`，程式解析後移除該行。

**理由**:
- 不破壞使用者可見回覆（程式移除 meta 行）
- 不需要第二個 LLM 呼叫
- 保留 Guard 所需的 evidence_span 精確驗證

### 各欄位補齊細節

1. **opportunity_signal + opportunity_evidence_span + opportunity_confidence**
   - 更新 `_fc_planner_prompt`：指示模型偵測 social opening 時，在 content 末尾附 meta 行
   - `plan_turn_v2_function_calling` 解析 meta 填入 decision
   - 保留現有 confidence >= 0.8 + evidence_span in message 驗證

2. **recent_context_followup**
   - 同上，meta 行可附 `"recent_context_followup":"ask_activity"`
   - 保留現有 intent==CHAT + 無 draft + evidence_span 驗證

3. **clarification_goal + missing_fields** (calendar_action 專用)
   - 當意圖是 calendar create 但缺欄位時，模型不呼叫工具，在 meta 行附 `{"clarification_goal":"calendar_action","missing_fields":["date","start_time"]}`
   - runtime 已有 `intent_name == "calendar_action"` + FINAL 路徑處理 `_save_action_draft`

4. **confidence**
   - 模型在 meta 行附 `"confidence":0.x`
   - 預設值維持 0.85/0.7，meta 有值時覆寫
   - Guard 的 low_confidence 闸門恢復作用

5. **evidence_span**
   - 模型在 meta 行附 `"evidence_span":"使用者原句子字串"`
   - 工具呼叫時也可附 evidence_span（寫入動作尤其需要）
   - 預設改為 `None`（不再用整個 message 代替），Guard 驗證保留

## 修改範圍

### 檔案清單

| 檔案 | 修改 |
|------|------|
| `services/ayue_agent/router.py` | 補齊 function calling meta 解析；移除 `plan_turn_v2`、`_planner_prompt` |
| `services/ayue_agent/runtime.py` | 移除 `plan_turn_v2`、`get_planner_mode` import；直接呼叫 `plan_turn_v2_function_calling` |
| `services/ai_service.py` | 移除 `_RUNTIME_PLANNER_MODE`、`get_planner_mode`；從 `set_runtime_model_override`、`get_runtime_model_override` 移除 planner_mode |
| `models.py` | 移除 `planner_mode` 欄位 |
| `routers/system.py` | 移除 `planner_mode` 傳遞 |
| `frontend.html` | 移除 JSON Adapter 選項與相關 UI/JS |
| `tests/test_ayue_agent_planner_sequences.py` | 將 `plan_turn_v2` patch 改為 `plan_turn_v2_function_calling` |
| `tests/test_ayue_agent_router.py` | 同上 |
| `tests/test_ayue_agent_trajectories.py` | 同上 |

### 不修改

- `contracts.py` 的 `AgentDecision` schema 保持不變（仍是 provider-neutral）
- `tool_registry.py` 不變
- `guard_v2_decision` 不變（仍驗證 evidence_span、confidence）
- trace allowlist 不變

## 測試策略

- 現有 trajectory/planner sequence 測試改 patch `plan_turn_v2_function_calling`
- 新增測試：驗證 meta 行解析正確（opportunity_signal、recent_context_followup、clarification_goal、confidence、evidence_span）
- 新增測試：驗證 meta 行從使用者可見回覆中移除
- 驗證 Python compile 與健康檢查

## 風險

- LOW: `plan_turn_v2` 無外部呼叫者（impact 分析 confirmed）
- 主要風險在測試 patch 目標變更，需逐一更新