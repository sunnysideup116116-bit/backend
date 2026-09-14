> **歷史文件（2026-09-14 以前）**：本文記錄已退役的公開 DAG 架構，不可作為現行操作指引。公開阿月目前固定使用 Pi；現行規格見 Server/AYUE_V3_ARCHITECTURE.md。\n\n# Candy 的 Pi Agent 實驗

這是使用者明確要求的帳號限定試驗。預設公開阿月仍走 V3 DAG；具備後端 grant 的帳號可在 App 設定的「Pi Agent 測試模式」切換，下一則一般公開阿月訊息生效。Candy 可試用完整公開能力；Private、獨立語音 agent 與既有確認按鈕仍由原 owner 處理。

## 權限與切換

最新的 15 秒逾時、衝浪主題配對、對人稱呼及確認卡刷新修正，見 [Candy feedback 修正紀錄](PI_FEEDBACK_FIXES_2026-09-14.md)。

- `agent_experiments` 使用 Appwrite user ID 作 `_id`，`pi_agent_allowed` 是 operator-only grant，`runtime` 是 `dag|pi`。不從暱稱、email、可修改的 profile 或 request body 判斷權限。
- `GET/PUT /api/settings/agent-runtime` 以 Appwrite Bearer JWT 確認 owner。PUT 只接受 `runtime`；不接受 user ID、grant、模型名稱或其他欄位。
- 公開聊天在保存訊息前讀取帳號引擎設定；選擇 Pi 卻缺少有效 Appwrite owner proof 時回 401，不可默默執行 DAG。Pi 不可用時回 503；server-only `agent_runtime` 綁定本次已驗證的選擇，交給中立 `public_runtime` 分流。一般 Pi turn 不載入 Scheduler。
- 預設為 DAG；撤銷 grant 立即使後續 turn 回 DAG。Pi 不可用時不能開啟，但仍能切回 DAG。
- 此設定只新增 Pi 實驗權限，不授予管理員、其他帳號或資料庫權限。

Operator 指令：

```sh
.local-venv/social/bin/python scripts/provision_pi_experiment.py --user-id ACCOUNT_ID --expected-email EXACT_EMAIL --grant
# 同樣參數搭配 --show 查閱；--revoke 撤銷並切回 DAG。
```

## Runtime 與資料邊界

- Pi 官方 `@earendil-works/pi-agent-core` / `pi-ai` 鎖定 `0.85.1`，Node >=22.19。依賴位於 `pi_agent`，使用 `npm ci --ignore-scripts` 安裝。
- 唯一啟動入口仍是 `start_all.sh`。啟動時非生成式檢查 Pi；Pi 缺少依賴不阻止四個原服務。Pi 為 Social 每回合建立的 stdin/stdout 子程序，不新增 port 或公開服務網址。
- Pi 的原生 loop 接收既有 Context Builder 產生的同房間有限公開對話、訊息時間、已發布來源 metadata 與各領域安全 projection。不同 owner/room 不共用 Node session，也不保存 Pi transcript。
- Node child 不接收環境中的 API key、DB URI、Appwrite JWT 或原始 profile。模型呼叫經既有 `ai_service` 與獨立 `pi` model owner，未設定時沿用全域模型；Ollama 接收原生 user/assistant/tool messages並保留 tool call ID 與結果關聯。
- 開放 Calendar、Relationship、Product Info、Profile/Memory、Places/Web、Match、Assessment 與 2–4 項自然語言 operation queue。Match 的接受、婉拒與撤回仍在「阿月牽線」Hub 操作。
- Pi 不輸出 DAG。`services/ayue_agent/pi/public_turn.py` 管理 Pi 公開 lifecycle；`tool_runtime.py` 只保留共同 dispatch／guard／讀取／確認邊界。生產 Pi 路徑不呼叫 Scheduler、Planner、Synthesizer 或舊推理 subagent。
- 工具 schema 由 `pi/registry.py` 明確列出 31 個已授權工具並送入 Node；每回合固定可見，不依「查一下／附近／然後」等字詞改變。`tools.enable_domain` 已從正常 Pi 工具集合移除；DAG registry 新增工具不會自動暴露給 Pi。
- 各領域實作與提示已分到 Calendar、Relationship、Product、Profile/Memory、Places、Web、Match、Assessment、Workflow 與 System 模組；這些是提示與工具，不是動態 SKILL loader，也沒有各自的通用推理 LLM。
- 一般任務最多 6 次 Pi 模型呼叫、8 次 tool dispatch、90 秒；只有實際呼叫 Web 或建立 workflow queue 後，才升級一次為 10／16／120 秒，不再由句中關鍵字預判。沿用每 domain 3 reads、全回合 9 reads 和一筆確認限制。
- 明確的 2–4 項需求由 `workflow.queue_operations` 保存自然語言目標；每次最多一個互動元件，確認後先輸出 executor 收據再續接下一項。佇列保存來源引擎，切換引擎時暫停而不跨引擎推理。
- Pi 自己產生查詢回覆與澄清，不呼叫 Synthesizer LLM。一般回答由同一個 loop 決定何時結束，不固定追加整理呼叫。確認卡準備完成後鎖住所有業務工具，最多使用總預算內的一次 Pi presentation 呼叫安排 `[[confirmation]]` 前後文；純程式驗證失敗時使用後端 preview。確認／拒絕後只使用 executor 收據，不再呼叫模型。DAG 繼續使用原 Synthesizer。
- 一般 Pi 每回合最多 5 次決策模型呼叫，複合／研究最多 9 次；若有確認卡最多再 1 次 presentation。trace 分開記錄 `pi_decision_llm_calls`、`pi_presentation_llm_calls`、預算類型與工具上限。
- Node schema 驗證或 Python typed validation 的同一工具格式錯誤只允許一次修復；第二次以 `pi_tool_schema_invalid` 結束。trace 只保留工具名稱、欄位路徑、階段、錯誤代碼、次數與耗時，不保存參數值或 transcript。
- Pi error 不自動重跑 DAG。timeout 與 provider error 保留各自代碼及準確錯誤文案；不得把模型／格式錯誤說成使用者缺少時間或不是聯絡人。DAG Planner 的 failure_code 也必須傳到既有錯誤回覆 boundary。
- `agent_mode=pi_experiment` 可在 API final 確認引擎；durable trace 記 `agent_runtime=pi`、`execution_mode=pi`、reply owner 與 call count，不保存 prompt/arguments/result。一般聊天恢復既有 MessageUse 記憶擷取；確認、行事曆與 assessment 仍排除。

## 歷史互動與真實 UI 元件

- DAG 保留既有歷史文字投影。Pi 在 provider context 中移除 assistant 歷史的 `[確認卡｜…]／操作／內容／確認後` 仿卡區塊，另收到 `historical_interactions` 的無權限摘要。
- 歷史 `confirmed` 只代表舊卡狀態，不等同 executor 成功，也不能授權本回合操作。
- Pi 模型只決定 `[[confirmation]]` 的文案位置；真正 `choice_prompt`、按鈕、狀態、owner/room/surface、期限與 fingerprint 都由後端紀錄產生。
- 無有效後端紀錄時，仿卡標題、marker 與「卡片已準備」宣稱會被拒絕。同一個 Pi `Agent.followUp()` 最多修復一次，修復回合工具集合為空；再次失敗安全結束。
- Pi 確認卡與選人卡皆採 publication handshake：prepared → 綁定正規化 final/blocks → assistant 訊息成功保存 → pending。刷新或點擊只讀後端 projection，Flutter 不從普通文字生成按鈕。
- 若保存或 prepared→pending 啟用失敗，當次 API response 會移除確認／選人元件；prepared 紀錄不可執行。
- 新 Pi 紀錄保存 `source_engine=pi` 與 `tool_protocol_version`；舊紀錄缺欄位時保留原相容行為。

## 前後文、地點與來源

- Pi 以本房間使用者實際看見的 user／assistant 訊息理解「第二間／剛才那個活動／他／改成五點」；超出有界 context 或候選不唯一時澄清。
- 不新增 Pi 專用對話 reference store、地點 snapshot、序號解析器、隱藏選中狀態或第二套 Calendar 草稿。DAG 仍在用的 reference 模組與既有資料保留。
- assistant 訊息實際發布的來源名稱與安全 URL 沿用既有 message metadata；後續模型同時看到可見文案與該則來源，不收到 opaque token。
- 「第二間」由模型依可見文案順序解成具體名稱，再由 Places／Calendar 依名稱、地區、日期與時間核對。正式 ID、revision、confirmation ID 和 idempotency 只在後端。

## 驗收

先用同帳號、不同乾淨聊天室比較原模式／Pi：

1. 「明天下午兩點到五點看牙醫」→「幫我加到行事曆」，確認卡必須保留時間和活動。
2. 指定已建立聯絡人的名字邀約，模糊名字應先出選人卡，空白邀約不需要追問日期。
3. 缺欄位後补充、改日期／時間、取消、切回 DAG；不應遺失已提供資訊。
4. 確認卡出現前零 domain writes；切換引擎後，既有已呈現確認卡仍可由原確認流程處理。

`social/tests/test_pi_agent_experiment.py` 使用真實 Pi Node loop、假模型和隔離 collection；`pi_agent/bridge.test.mjs` 驗證 loop 回饋、schema 和同 batch 停止。Flutter 控件／HTTP 測試為 `DatingApp/test/pi_agent_settings_test.dart`。自動測試不觸及正式資料。

## Runtime ownership 與退役清單

| 分類 | 現況 | 第一階段退役條件 |
| --- | --- | --- |
| Pi 專用 | `services/ayue_agent/pi/`：public lifecycle、Agent loop、封閉 registry、領域工具／提示、reply/presentation | 正式保留；Candy 試用期間持續調整 |
| 共用 | `services/ayue_agent/shared/`：runtime collections、產品檢索、Calendar mutation、確認排版、write-actions 具名橋接 | 不隨 DAG 刪除；任何搬移都先保留相容 import |
| 混合待拆 | `v3/calendar_commands.py`、confirmation/contact selection/operation batch 與 deterministic write executor | 不可整檔刪除；兩引擎共用安全契約 |
| 相容薄層 | `v3/pi_runtime.py`、`v3/pi_reply.py` | 全域文字搜尋與 GitNexus impact 都證明無外部引用後才能刪除 |
| DAG 專用（保留） | Planner、Synthesizer、各領域 semantic subagent 與 DAG orchestration | Candy 可手動切回；本次沒有刪除或退役日期 |

資料層共用既有聊天、確認與正式行事曆；Pi 寫入的行程可被 DAG 讀到。新 Pi Calendar confirmation payload 由後端加入 `source_engine=pi` 與 `tool_protocol_version=pi_calendar.v1`，舊卡缺少欄位時仍依既有 `calendar_plan_version=1` 執行，不按目前切換狀態重新推理。

## Pi API 適用點與訊息時間處理

已核對本機鎖定的 `@earendil-works/pi-agent-core@0.85.1` 與[官方 API 說明](https://github.com/earendil-works/pi/tree/main/packages/agent)。本輪不升級依賴。

- **本輪採用 `convertToLlm`**：Python 將來源時間放在歷史訊息的 `messageTime` metadata，Node 只在模型輸入投影時附加時間標記。原始 AgentMessage 的文字不因此改變，模型仍能依原訊息時間理解「下週四」。
- **公開文字過濾**：Pi 一般回覆、確認卡前後文與收據統一移除 `[訊息時間：…；時區：…]`；只剩標記或不完整標記時走現有 fallback。正常行程日期、時段及時區說明不刪除，也不修改已保存的歷史資料。舊 assistant 回覆中的標記在回送模型前去除，避免反覆抄入。
- **已採用 `Agent` 生命周期**：`subscribe()` 的 listener 會被依序 await；`beforeToolCall` 阻擋未啟用／超額／確認後工具，`afterToolCall` 統一終止狀態，`shouldStopAfterTurn` 控制 bounded loop。
- **已採用 lifecycle 與長上下文接口**：`prepareNextTurnWithContext` 在純文案修復階段清空工具，`transformContext` 保留頭尾並限制 transcript，`followUp()` 只做一次回覆修復，`abort()`／`waitForIdle()` 用於 child 關閉與完整收斂。

時間過濾回歸涵蓋純文字、字面 `\\n`、重複／不完整標記、卡片前後文、執行收據與實際 Pi loop 的 token 回放；模型輸入保留時間、使用者輸出不含內部標记，且不增加模型呼叫。
