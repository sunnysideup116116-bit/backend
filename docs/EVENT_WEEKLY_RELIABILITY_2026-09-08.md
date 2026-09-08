# Event 每週流程可靠性修正與驗證（2026-09-08）

> 這是分階段執行紀錄。現行程式已隨後端 e06f9a1 推到工作分支；文中的早期 commit／測試數量／run 16 是當時快照，不代表目前未提交。main 尚未合併。

基線：Server 3efae74；本輪變更尚未 commit／push。前端及其他記憶 Bug 未修改。

## 已實作

- 正式 Event worker 將 job token 作為持久 cycle id，event_weekly_runs 保存 discovery、cleanup、result checkpoint，event_weekly_users 保存逐人進度。接手同一租約工作可從 checkpoint 繼續。
- 三張改為每批建立上限，每批最多讀 30 位待處理者，跨批次處理快照內所有 profile 使用者；是否能建立仍由原 Event create facade 驗證。未曾取得活動提案者優先，同順位穩定排序。
- 以 cycle id/requester 找回已插入但來不及保存逐人結果的 match，避免再次選人。逐人暫時失敗最多三次，明確記為 partial；整週 stage 失敗最多再試三次，有退避。
- 每週一台灣時間 08:00 排程；錯過當時窗口，同週恢復後補跑。ISO week key 仍防止同週重複入列。
- 正式 weekly job 使用增量 discovery、Event reconciliation 與 lifecycle 過期清理，不先整庫 reset。保留仍有效活動與已決定歷史。這不是兩份 Graph inventory 的原子切換，搜尋服務既有每類數量上限仍會收斂 inventory。舊無 run id Python 呼叫與手動 Demo 的 reset 行為保留相容，不是正式週期入口。
- Event delivery worker 由 Social lifecycle 啟停，每 10 秒領取最多 10 張 canonical live Event proposals。draft 只送第一方，pending 才送第二方。卡片沿用既有投影與固定 message key；確認保存後才 ack 对應 inbox，失敗退避重試。
- discover/status 增加 weekly_progress 統計，不公開帳號或偏好。升級前没有明細的週期為 not_recorded，不誤回零人成功。

## 驗證

`Server/venv/bin/python Server/scripts/run_offline_tests.py social -k 'event or match_hub or match_quota or service_health'`

結果：202 passed、25 subtests passed。包含 65 人跨批次、租約失效、已插入 match 的重啟恢復、錯過週一補跑、有界失敗重試、離線保存、實際 card adapter 的雙向理由、固定 key 去重、先同意才送第二方、保存失敗不 ack、終態不再投遞及統計隱私。

Python compile、`bash -n Server/start_all.sh`、`git diff --check` 通過。

環境問題：Server/venv 缺少 requirements-test.txt 已指定的 mongomock==4.3.0，已安裝。sandbox 內 Starlette/AnyIO TestClient 初始化停住，改用相同禁止外部網路的 offline runner 在 sandbox 外測試通過；沒有放開測試的正式 DB／外部服務連線。

GitNexus 修改前完成 impact：Event create 為 HIGH；週期 worker 為 LOW；FastAPI startup/shutdown 為 UNKNOWN，已核對 decorator 動態接線。索引刷新遇 sandbox git EPERM，外部重建成功。tracked diff 的 detect-changes 回報 10 files、24 symbols、38 affected processes、critical；它不涵蓋未暫存新增檔案，不能當成全部修改的完整圖譜簽核。新 service／測試另以原始碼及上述回歸檢查；本輪沒有 commit。

## 正式執行驗證

重啟前確認原 supervisor 為 start_all.sh，Event singleton state=completed、last_schedule_key=2026-W37、run_number=16。正常停止 supervisor 後，確認固定 ports 已釋放，再以原 start_all.sh 啟動；四個服務健康，固定公開網址 /api/health 回應 ok。

新 worker 已自動補送 9/7 的三張提案：

| match | state | 保存卡片數 | 第一方 inbox 剩餘 |
| --- | --- | --- | --- |
| 6a9e085b889327c3aeb93b95 | draft | 1 | 0 |
| 6a9e0861889327c3aeb93b98 | draft | 1 | 0 |
| 6a9e0869889327c3aeb93b9b | draft | 1 | 0 |

三筆 event_delivery.initiator=true、attempts=1。驗證是正式 Mongo 持久卡片與收據，不是手機推播到達／已讀的證據。沒有替使用者按接受，也沒有重跑本週探索；singleton 仍第 16 輪。Server 保持運行。

## 後續

### 9/8 19:00 一次性加跑

依使用者要求，新增 enqueue 的 optional `not_before`（未提供仍立即可領取），沿用既有 retry_at 領取閘門。
已排入 run_number=17、source=scheduled_once、job_kind=weekly_cycle，scheduled_for/retry_at=1788865200
（2026-09-08 19:00 Asia/Taipei）。last_schedule_key 保持 2026-W37，不改週一 08:00 排程。
16 個排程／worker 隔離測試通過，包含未到時間不可 claim、不得覆寫已排入工作、非法時間拒絕。
這是完整增量活動刷新→過期清理→向量 readiness→分批配對工作；19:00 是可開始時間，不是完成時間。
當時 Server 必須在線；離線時佇列會保留，恢復後才領取。本次最終結果已於 9/8 晚間確認：run 17 在 20:40:43 完成，outcome=partial（活動類別覆蓋不足）；active events=28、readiness ready／pending=0。全人口快照 66 人：54 no_match、9 already_active、3 created、0 failed，新提案已保存 Hub 卡片。19:00 是排程可領取時間；首次可見 claim 約 19:40，延遲原因未單獨定位，不能宣稱準點完成或每人必定配到。

- 下週真實活動搜尋與全人口掃描尚未發生；目前該流程經隔離回歸驗證，不宣稱所有使用者必定配到。
- 新增服務可補送既有 canonical Event proposal；後續使用者已回報 Hub 卡片、雙方接受與活動開場正常。這不等於手機推播到達／已讀或未來排程已驗證。
- 另觀察到既有文字正規化把「看表演」變成「看錶演」；記為後續獨立文字 Bug，本輪未修改語言服務。
- 後續記憶分類／同步與配對收尾已記於 [Memory 紀錄](MEMORY_CONTEXT_FIX_2026-09-08.md)及 [配對紀錄](MATCH_SEARCH_CONSENT_FIX_2026-09-08.md)；未修改的文字正規化問題仍獨立追蹤。
