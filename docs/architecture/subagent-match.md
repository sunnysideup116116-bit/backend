> **歷史文件（2026-09-14 以前）**：本文記錄已退役的公開 DAG 架構，不可作為現行操作指引。公開阿月目前固定使用 Pi；現行規格見 Server/AYUE_V3_ARCHITECTURE.md。\n\n# Sub-agent：match（配對搜尋與牽線收件匣）

> 對照後端 e06f9a1、前端 7b86365。現行 owner 是 `v3/match_runtime.py`，由 Scheduler 的 RuntimeRegistration dispatch；不是每次重新呼叫一個 LLM 來決定提案狀態。

## 1. 能力與邊界

- Match 查本人搜尋、牽線收件匣狀態與受限的單一對象摘要；收件匣可同時有一般、指定主題及 Event 卡。
- accepted contacts 的清單、數量、比較、活動同行推薦由 Relationship 擁有，不以「配對」關鍵字決定路由。
- 開始／取消搜尋先確認；接受、婉拒、撤回提案在「阿月牽線」卡片操作。聊天中的此類要求只引導到 Hub，不新建決策 confirmation。
- 本人發起且仍待本人決定的 draft，以及 queued/running 搜尋會阻擋新搜尋；等待對方或收到別人的邀請不會一律阻擋。
- 每個 job 最多產生一個人選，不等於帳號最多只有一張提案。一般和 Event 的名額政策分開；既有關係不因 Event 邀請重建。

## 2. 工具與語意契約

| 工具 | 風險 | 現行用途 |
| --- | --- | --- |
| `match.get_status` | READ | canonical 多卡片／搜尋 snapshot；不是 accepted contacts aggregate |
| `match.get_counterparty_summary` | READ | 受限公開摘要；不從聊天猜身份，不列整份聯絡人 |
| `match.start_search` | WRITE | 只準備搜尋確認，確認後建立 durable job |
| `match.cancel_search` | WRITE | 只取消本人 queued/running job，先確認，不撤回既有邀請 |

Registry 中的 `match.decide_active_proposal`／`match.decide_active_event_invitation` 是保留的相容 executor 定義，不是目前 Match 的聊天可見工具。不能據此恢復聊天決策路徑。

Planner 在 Match task 帶 `match_intent`。搜尋另帶 `match_search_request`：

| 欄位 | 意義 |
| --- | --- |
| `kind` | `general` 或 `activity`；「適合認識／合得來」是一般找人條件，不是活動 |
| `topic` | 本句具體活動原文；runtime 驗證，不從舊房間借用 |
| `invitation_evidence` | 只有本句明確要求找到就代送邀請才填原文，否則空 |

這不是 model 可提供的 IDs／revision 或直接寫入權限。一般與活動搜尋預設先看提案；只有活動搜尋的有效代送意圖才在可見確認中明示「開始找並送出邀請」。topic 存在不等於授權。語意未提供時 runtime 降為一般搜尋，activity 未通過原文驗證時先澄清。

## 3. 搜尋與呈現

1. Planner → Match runtime → Guard → `prepare_write_confirmation`；readiness 不足先追問。
2. 保存 room-scoped preview 與 choice，使用者確認後才呼叫 `_start_search`。
3. `match_search_jobs` 以 job id／lease／context revision 執行；狀態和入口回到原 room，Hub 是提案操作頁。
4. 向量檢索先取 100 筆（numCandidates=500），套用封鎖、群組、pair history 等排除後，最多 20 人進資格檢查，再有界分批交 9001 排序。這是較寬檢索窗，不放寬硬門檻，也不保證一定有結果。
5. 預設產生 draft 給發起者先看。明確授權的 invite_on_match 才由既有 job 經 canonical accept CAS 轉 pending，通知對方。
6. UI 顯示 viewer-bound 推薦理由與公開暱稱。Topic 文案保留公開性格說明或明示缺少共同活動依據，不把本人願望寫成對方共同興趣。
7. 技術失敗使用具體 failure code；真正無候選可回 no_candidates。Google 額度／Graph／模型失敗不可冒充成功配對。

## 4. 卡片決策與婉拒

Hub 發送 `POST /api/match/decision`，綁定 match ID、namespace、畫面上的 status/revision；9001 或模型不能自行選擇新 authority。

- lifecycle：draft → pending → accepted；declined／expired 為終態。
- 第一方接受是等待對方；雙方接受後才可使用授權聊天室。
- 「先不用」開啟原因視窗：先不拒絕不送 request；只婉拒傳空 reasons；記錄原因並婉拒只傳勾選的原始 options。
- 只有 opt-in 的 explicit_reasons 交既有 feedback／memory facade 形成本人 AVOIDS。沒有可用選項時仍可不記錄，不能捏造原因。
- stale 只同步狀態、不覆寫終態；effects 使用既有防重複機制。
- 歷史 choice projection 讀取 delivery_mode，避免「搜尋並邀請」重新進房後變成「開始搜尋」。

## 5. Opportunity 與 Event

`opportunity.social_opening` 是經原文 evidence／confidence 驗證的柔性提議，不自行開始搜尋或送出邀請。明確找新人的要求走 Match task。每週 Event discovery／掃描／離線投遞是獨立 background domain，詳見 [Event 指南](../EVENT_DRIVEN_MATCHMAKER_GUIDE.md)。

## 6. 驗證

`test_match_search_consent.py`、`test_match_vector_retrieval_window.py`、`test_match_invite_on_match.py`、`test_match_restart_flow.py`、`test_onboarding_decline_feedback.py`；Flutter `match_hub_decline_test.dart`、`match_hub_inbox_test.dart`。最新人工驗收和已知限制見 [修正紀錄](../MATCH_SEARCH_CONSENT_FIX_2026-09-08.md)。
