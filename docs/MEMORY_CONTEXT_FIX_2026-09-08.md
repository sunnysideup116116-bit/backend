# Owner memory / Context 修正與配對理由複驗

本輪處理使用者指定的第 1、2 點，並測第 4 點；第 3 點分類 UI 未修改。保留前一輪未提交的 Event 變更。本輪同樣尚未 commit／push。

## 修正

- `owner_memory_projection.py` 純投影最多 8 筆帶方向的本人偏好；like/dislike/avoid/require 對應喜歡／不喜歡／避免／需要。排除 want、其他 owner、disabled、未帶 stance 的舊文字與不安全標籤。
- `context.py` 保持唯讀，透過共用 projector 建 relevant_memories，不再只取 label。
- `public_chat._complete_public_turn` 與 init 在進入 Context 前呼叫 memory service 做 lazy refresh；即使 preview 非空也會在 300 秒後重新讀 Graph，設定頁 force refresh。Graph 連線 timeout 1 秒、讀取 timeout 2 秒；失敗保留 cache，聊天路徑退避 30 秒。這不是所有使用者持續即時同步，尚未活躍的舊快取會在下次使用時刷新。
- Cache revision CAS 防止晚到的舊讀取覆蓋 disable/correct。成功記憶 mutation 使 cache revision 失效；disable/correct 先從 cache 移除對應 key，Graph read 失敗時不恢復該 key。
- `_sync_memory_projection` 遇 Graph read failure，保留其餘已知 cache 並合併新批次，不再以這一批資料替換整份記憶。
- 9001 `list_memories` 增加 optional durable_only，預設 false；Social 長期 cache 使用 true，只取 PREFERS／AVOIDS。預設讀取仍可包含未過期 CURRENTLY_WANTS；過期短期意圖不回傳。Graph schema 沒有變更。
- Public Planner 會直接產出 direct_chat 回覆，因此新增同一份 bounded user_preferences；只接受 projector 帶方向的封閉文字格式，未帶方向的 legacy string 仍不送 Planner。Profile／Relationship／Synthesizer 沿用同一份 wording。

## 驗證結果

- 完整 Social offline suite：1747 passed、4 個既有 opt-in 外部測試 skipped；另有 133 subtests。
- Matchmaker offline suite：76 passed（15 subtests）。
- 最後 response-shape tightening 後的記憶／Context／Planner／理由 focused suite：208 passed、4 skipped（33 subtests）。
- Flutter proposal_nickname、inline_match_proposal_state、match_hub_inbox：65 passed。
- Python compile、`bash -n start_all.sh`、`git diff --check` 通過。
- 新增測試覆蓋 stale 非空刷新、fresh 不重抓、成功空 Graph 清 cache、失敗保留、revision 競態不能恢復已停用記憶、新 batch 合併、owner 隔離與模型 prompt 傳遞。
- 舊同步測試只 mock update_one，新增 revision read 後需補 find_one 替身；保留原本空 Graph 清 cache 斷言。
- GitNexus 修改前 impact：Graph snapshot HIGH，牽涉記憶設定／寫入後投影；Context/Planner 呼叫鏈 LOW；HTTP decorators 為 UNKNOWN，已以路由與 caller 原始碼覆核。索引已更新；整體 diff 包含前輪 Event 與此次記憶修改，不能全部算成本輪。

## 正式驗證

透過原 start_all.sh 的 r 重啟完整四個服務，固定公開網址 /api/health 回 ok，Server 保持運行。

讀取兩個既有 seed 帳號的 Graph durable-only 與 cached durable keys：seed_user_01 為 8 筆、seed_user_04 為 3 筆，兩邊一致；當下有效 want 均為 0。這是 9/8 當下快照，不能回推 9/6 每個差異都是遺失長期偏好。

經正式 profile/memories API 正常刷新兩個 seed 的 Mongo read projection，再執行實際 Context builder（未呼叫模型、未保存聊天）：

| 帳號 | API 筆數 | Context 筆數 | 全部帶方向 | Planner／Synthesizer 與 Context 一致 |
| --- | --- | --- | --- | --- |
| seed_user_01 | 8 | 8 | 是 | 是 |
| seed_user_04 | 3 | 3 | 是 | 是 |

未修改 Graph 偏好、未新增測試偏好、未執行全庫 migration。快取刷新只更新投影與 freshness/revision metadata。

第 4 點：目前 10 張有效提案（7 張一般、3 張活動），20 個內部 viewer-bound reason 投影沒有空字串；配對理由／暱稱前端元件回歸也通過。本輪没有建立新配對或以真機 APK 驗收，不把非空理由證據等同所有文案品質都完美。

## 後续

- UI 的 interest／preference 為 Concept.kind；PREFERS／AVOIDS 是使用者關係。若第 3 點產品要顯示「偏好／避免」，應使用 stance/relation，而不是 kind；本輪依要求暫不改畫面。
- 真實自然語言回答是否每次善用偏好，仍需使用者聊天驗收；此次證明資料已正確進入模型輸入，不宣稱模型必然遵循每一筆記憶。
