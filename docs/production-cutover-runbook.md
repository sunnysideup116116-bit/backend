# 阿月 Backend Production Cutover 與相容回退 Runbook

**狀態：審查草案。本文的命令僅供另外授權後執行；本輪沒有切換服務。**

## 已核對的版本與環境

- GitHub `main`：`6a2a1372b3d4ea5ada4650da90f06f59642dec67`。
- Production 實際程序與 `.worktrees/active-release`：`1efa6c72f775309ce42ff3dcfd85f6479c3f91a9`。
- Main Release Candidate：`.worktrees/release-candidate-6a2a137-server`。
- 相容回退候選：由 Production commit `1efa6c72f775309ce42ff3dcfd85f6479c3f91a9` 建立的 `codex/rollback-compatibility-1efa6c7`；已審查的 code commit 是 `40597e962619bcccf2a91dce213bf2620e3f1883`。
- 為保持 review diff 僅含回退 patch，Draft PR 以 `codex/rollback-base-1efa6c7`（固定在 `1efa6c7`）為 base；它不是 main PR，也不得把整個舊分支合併回 main。
- 回退候選只修改舊版 weekly retry 的讀取、到期與重排邏輯，及只讀 guard；不含 PR #47/#48/#49 的其他程式變更。
- Production 於 2026-10-20 唯讀重查：四個 health endpoint 為 HTTP 200；`pending`、`retry_pending`、`resume_pending`、帶 `resume_at` 與帶 `checkpoint` 的 pending rows 均為 0；singleton weekly job 是 `completed`、`last_schedule_key=2026-W43`、沒有有效 lease。此狀態只代表檢查當下，切換前必須重查。
- `.worktrees/active-release` 是目前唯一 active symlink，GNOME 目前唯一 backend autostart 是 `~/.config/autostart/啟動DatingApp後端服務.desktop`，指向該 symlink 的 `start_all.sh`。同目錄的 `.desktop.bak-20261013` 不是 GNOME `.desktop` 入口。`ayue-backend.service` 尚未安裝。
- Production 的舊 venv 與 Pi `node_modules` 是跨 worktree symlink；此回退候選已另外建立三個獨立 Python venv 和 Pi `node_modules`，`pip check` 全數通過，Node bridge check 與 Pi tests 11 項通過。候選尚未複製任何 `.env`。
- User systemd manager 為 `Linger=yes`，`/run/user/1000` 為 owner `sunny:sunny` 的 mode `0700` 目錄；新 launcher 的全域鎖位於 `/run/user/$(id -u)/ayue-backend-start-all.lock`。切換時仍須確認當次 user manager 與 runtime directory 正常。
- Guardrail 共用的外部資產目前可讀：`/home/sunny/Applications/llama-b10333/llama-server` 與 `/home/sunny/下載/llama-guard-3-1b-q4_k_m (1).gguf`。部署及回退期間不可刪除或搬移。
- Production `.env` 的非機密 `AYUE_LLM_PROVIDER` 未設定，OpenRouter 與 Ollama key 都存在；已驗證 unit 會明確傳入 `ollama`，因此 systemd 啟動將選 Ollama。若部署目標是 OpenRouter，需先在隔離 runtime 驗證對應 unit/設定；不能僅因 key 存在就假定它會被使用。
- 新版 user unit 範本已在隔離環境通過 `systemd-analyze verify`，目前設定 `Restart=on-failure`、`RestartPreventExitStatus=75`、`KillMode=mixed`、`TimeoutStopSec=45s`、`AYUE_SHUTDOWN_TIMEOUT_SECONDS=30`，並設 `StartLimitBurst=5` / `StartLimitIntervalSec=300s`。它尚未安裝。
- 回退候選刻意不帶 PR #48 的 worker supervisor 或 global startup lock。它能相容 weekly retry 資料，但回退後的 worker liveness 與多入口啟動保護會退回 Production 目前舊版水準；回退期間只能由 systemd 單一入口啟動，GNOME/manual launcher 必須停用，且需人工監控 Event job progress。

## 強制 STOP 條件

1. 回退候選 PR 尚未審查合併前，不得把它當成正式可回退版本。
2. `main` 或 Production SHA 與上方版本不同時，停止並重新做版本與相容性審查。
3. 候選缺少任何必要 `.env`、依賴檢查失敗、Pi bridge check 失敗或 Guardrail binary/model 不可用時，不得切換。
4. 正式 worker 未停止、仍有 worker process、Mongo job lease 尚未到期、Mongo guard 讀取失敗或資料格式異常時，不得切換到回退版本。
5. Guard 通過只表示它讀取的 weekly retry／lease 欄位符合此相容候選；它不是全資料庫一致性證明，也不檢查 Neo4j。不得用它取代其餘唯讀檢查。

## 切換前準備

先固定並記錄來源 SHA；此處任一值改變就停止：

```bash
ROOT='/home/sunny/桌面/Graduate_Project'
ACTIVE="$ROOT/.worktrees/active-release"
NEW_RELEASE="$ROOT/.worktrees/release-candidate-6a2a137-server"
ROLLBACK_RELEASE="$ROOT/.worktrees/rollback-compatibility-1efa6c7-server"

git -C "$ROOT/Server" fetch legacy-origin main
test "$(git -C "$ROOT/Server" rev-parse legacy-origin/main)" = \
  '6a2a1372b3d4ea5ada4650da90f06f59642dec67' || exit 1
test "$(git -C "$(readlink -f "$ACTIVE")" rev-parse HEAD)" = \
  '1efa6c72f775309ce42ff3dcfd85f6479c3f91a9' || exit 1
git -C "$ROLLBACK_RELEASE" merge-base --is-ancestor '40597e962619bcccf2a91dce213bf2620e3f1883' HEAD || exit 1
git -C "$ROLLBACK_RELEASE" diff --quiet '40597e962619bcccf2a91dce213bf2620e3f1883' HEAD -- \
  . ':(exclude)docs/production-cutover-runbook.md' || exit 1
```

確認候選 worktree tracked files 乾淨、來源提交符合審查結果。Runtime 只能在各自 release 目錄中建置，不能 symlink 到其他 worktree：

```bash
for p in "$NEW_RELEASE/.local-venv/social" "$NEW_RELEASE/.local-venv/matchmaker" \
         "$NEW_RELEASE/venv" "$NEW_RELEASE/pi_agent/node_modules" \
         "$ROLLBACK_RELEASE/.local-venv/social" "$ROLLBACK_RELEASE/.local-venv/matchmaker" \
         "$ROLLBACK_RELEASE/venv" "$ROLLBACK_RELEASE/pi_agent/node_modules"; do
  test -d "$p" && test ! -L "$p" || exit 1
done
```

將現行 release 的必要設定檔以安全方式複製到新 release 與回退候選，保留 owner 與 `0600` 權限；不要在 terminal、PR 或 log 顯示內容。只比對變數名稱與所需變數是否存在。檢查 root、`social/`、`matchmaker_agent/` 的 `.env` 都在預期位置。不得把環境變數直接放入 shell arguments。

建立新 systemd unit 時安裝版本控制的範本。它設定 `Restart=on-failure`、`RestartPreventExitStatus=75`、`KillMode=mixed`、30 秒 launcher graceful shutdown 與 45 秒 systemd stop timeout：

```bash
UNIT="$HOME/.config/systemd/user/ayue-backend.service"
install -d -m 700 "$HOME/.config/systemd/user"
install -m 600 "$ROLLBACK_RELEASE/deploy/systemd/ayue-backend.service" "$UNIT"
systemd-analyze verify "$UNIT"
```

若需重建候選 runtime，先在 release 目錄中執行；不可對 active Production release 執行：

```bash
# 設定為剛建立且尚未含 runtime 的 NEW_RELEASE 或 ROLLBACK_RELEASE。
RELEASE="$NEW_RELEASE"
test "$RELEASE" != "$(readlink -f "$ACTIVE")" || exit 1
python3 -m venv "$RELEASE/.local-venv/social"
"$RELEASE/.local-venv/social/bin/python" -m pip install -r "$RELEASE/social/requirements.txt"
python3 -m venv "$RELEASE/.local-venv/matchmaker"
"$RELEASE/.local-venv/matchmaker/bin/python" -m pip install -r "$RELEASE/matchmaker_agent/requirements.txt"
python3 -m venv "$RELEASE/venv"
"$RELEASE/venv/bin/python" -m pip install -r "$RELEASE/risk_backend/requirements.txt"
(cd "$RELEASE/pi_agent" && npm ci --ignore-scripts)
```

接著對三個 Python 環境執行 `pip check`，執行 `node pi_agent/bridge.mjs --check`、`npm --prefix pi_agent test`、`bash -n "$RELEASE/start_all.sh"`。四個 runtime root 必須都是該 release 內的實體目錄，不能指向其他 worktree。

部署前確認 `systemd-analyze verify` 通過、`/run/user/1000` 存在且由 service user 擁有。Main 的 `start_all.sh` 使用跨入口 `flock`；正式啟動時應看到鎖被 launcher 持有。launcher 把 FD 9 以 `9>&-` 關閉後才啟動子服務，因此 child 不會延長 lock。Production 舊版 launcher 沒有這把鎖，不能讓舊、新 launcher 重疊。

## 受控接管順序

1. 安排在 2026-10-26 08:00 Asia/Taipei 的 W44 排程之前完成。不要改寫週次、schedule key、job、checkpoint 或 invitation；若維護跨過排程時間，先停止並重新確認 job 狀態。
2. 先將唯一 backend GNOME 入口移出 autostart 目錄，避免登入事件重新啟動舊 launcher；保留原檔以便 rollback：

   ```bash
   mkdir -m 700 -p "$HOME/.config/autostart.disabled"
   mv -- "$HOME/.config/autostart/啟動DatingApp後端服務.desktop" \
     "$HOME/.config/autostart.disabled/啟動DatingApp後端服務.desktop"
   ```

   只停用這個 `.desktop`；不要改 `fcitx5.desktop` 或將 `.desktop.bak-20261013` 改名成有效入口。

3. 從唯讀 process inventory 找到唯一舊 launcher PID，確認其 command/path 是 `$ACTIVE/start_all.sh` 後，只對 launcher PID 送 `SIGTERM`。不要執行舊 `start_all.sh` 當 stop command，也不要以 `fuser`／port cleanup 殺程序。舊 launcher 收到 TERM 後會透過 trap 對自己管理的子程序 graceful shutdown。
4. 等 launcher 結束，並確認 ports 8000、8001、9001、8081 都沒有 listener，沒有任何 process 的 cwd/argv 指向舊 release。若有殘留，STOP；不要啟動新 release 來清 port。
5. 在 worker 全停後執行回退 guard 的唯讀檢查；它需要候選的準確 code SHA、目前設定檔與 `--workers-stopped`：

   ```bash
   "$ROLLBACK_RELEASE/.local-venv/social/bin/python" \
     "$ROLLBACK_RELEASE/scripts/rollback_retry_guard.py" \
     --expected-target-sha '40597e962619bcccf2a91dce213bf2620e3f1883' \
     --current-release-dir "$ACTIVE" \
     --env-file "$ACTIVE/social/.env" \
     --workers-stopped
   ```

   Guard 也會計數未分類 pending rows、跨週次 rows、異常 `resume_at`/checkpoint，並拒絕 terminal job 留有 pending rows。Exit `0` 代表 weekly retry metadata 在讀取當下符合相容候選，仍須完成 Mongo/Neo4j 與 application health checks。Exit `75` 代表不確定或不安全，維持所有 worker 停止，不要啟動舊 Worker。Exit `64` 是參數錯誤。若 Mongo job 為 `running` 且 lease 未到期，等待自然到期後重新檢查；不手動清 lease。

6. 對新 release 執行唯讀版本與 runtime checks。原子切換 active symlink：

   ```bash
   ln -s "$NEW_RELEASE" "$ROOT/.worktrees/active-release.next"
   mv -Tf "$ROOT/.worktrees/active-release.next" "$ACTIVE"
   test "$(readlink -f "$ACTIVE")" = "$(readlink -f "$NEW_RELEASE")" || exit 1
   ```

7. 安裝已驗證 unit 到 `~/.config/systemd/user/ayue-backend.service`，執行 `systemd-analyze verify`、`systemctl --user daemon-reload`，再 `systemctl --user enable ayue-backend.service` 與 `systemctl --user start ayue-backend.service`。只有此 unit 可以管理 backend；GNOME backend autostart 必須仍在 disabled 目錄。
8. 確認 systemd unit 的 `MainPID` 是唯一 `start_all.sh` launcher、只存在一個 backend startup lock owner，且只有一個 Social Event worker/supervisor。若看到 `Another start_all.sh launcher is already active`、worker fatal、重複 launcher 或重啟限流，STOP 並開始 rollback 評估。

## 啟動後驗收

只使用低風險、唯讀或受控測試帳號；不可用真實使用者建立未授權配對或邀請。

- Health：Social `GET /api/health`、Risk `GET /health`、Matchmaker `GET /health`、Guardrail `GET /v1/models` 均須 200。
- systemd：`ActiveState=active`、`SubState=running`、MainPID 穩定；檢查 `RestartCount`/journal 沒有 liveness fatal 或重複啟動。
- Worker：log 出現 supervisor 與 worker started；只讀查詢確認 singleton lease/worker 狀態可用。正常 idle、Discovery 或 LLM 延遲不能當成死亡；只有 thread 實際退出才恢復。
- Mongo：用 majority read concern 檢查 singleton job、W43 completed、retry/checkpoint counters；不做 update、migration、backfill、schedule reset。
- Neo4j：使用設定中的 read-only credentials 執行 connectivity `RETURN 1` 與既有 bounded integrity check；不輸出 URI、個資或整個 node/property。
- Matching：於隔離或明確授權測試帳號驗證 Street Fighter 三個確定名稱與羽球片語、candidate-owned evidence、跨別名 AVOIDS、pair exclusion、qualification；無證據時確認 exploratory consent gate 未被跳過。
- APK：驗證 Pi 小寫 `street fighter` 能建立確認卡；確認推薦理由引用保存偏好但不推斷擅長、願意或已同意同行；確認沒有直接證據時先詢問探索性介紹。不得藉 APK 測試改動正式配對資料或扣 quota。
- Event：不要為 smoke test 手動 enqueue W43/W44。使用 mock/fixture 驗證 `retry_pending`、checkpoint 與 worker lease 行為；正式排程由既有 W44 schedule key 控制。

## 觸發條件與回退

觸發任一條件時進入 rollback 評估：主要 health 持續失敗、launcher 重複、worker supervisor 耗盡有界恢復、systemd 進入 start limit、Matching/AVOIDS 安全回歸或 Mongo/Neo4j 相容檢查異常。

1. 停止 systemd：`systemctl --user stop ayue-backend.service`。停用 unit 的 restart/boot 入口：`systemctl --user disable ayue-backend.service`；等待 MainPID、worker、所有 ports 完全停止。
2. 確認 GNOME backend `.desktop` 仍停用；不得讓 systemd 與 GNOME 同時啟動。
3. 用目前 active release 的 Mongo config 執行上述 rollback guard。若 guard 非 0、process scan 不完整、Mongo 讀取逾時、job lease 未到期、retry/checkpoint 欄位格式異常或狀態跨多個 run，**不要切換到任何不相容的舊 Worker**。維持服務停止並人工升級處理；此舊版 Social lifespan 會無條件啟動 Event worker，不能安全只停 Weekly/Event 後聲稱其他服務 rollback PASS。
4. Guard 通過後，切到已審查、已合併且自包含 runtime 的相容 rollback candidate（不是原始未修補的 `1efa6c7` 目錄）：

   ```bash
   ln -s "$ROLLBACK_RELEASE" "$ROOT/.worktrees/active-release.rollback-next"
   mv -Tf "$ROOT/.worktrees/active-release.rollback-next" "$ACTIVE"
   test "$(readlink -f "$ACTIVE")" = "$(readlink -f "$ROLLBACK_RELEASE")" || exit 1
   systemctl --user daemon-reload
   systemctl --user enable ayue-backend.service
   systemctl --user start ayue-backend.service
   ```

5. 重複 health、single launcher、lease、Mongo/Neo4j 只讀檢查。相容候選會等待每個 row 的 `resume_at` 到期、沿用 checkpoint、重用相同 job token；資料庫保持原樣。

回退候選本身沒有 global startup lock 和 Event thread supervisor。回退期間嚴禁手動執行 `start_all.sh` 或從 GNOME 啟動；若 Event worker thread 結束但 Social HTTP 仍健康，systemd `Restart=on-failure` 不會自行察覺，需由 read-only job progress/lease 監控發現後停止單一 unit並重新評估新版 release。不得為了恢復 Event 而在 systemd 外再啟第二個 launcher。

回退只改程式與 active symlink，**不回復 MongoDB/Neo4j snapshot**。本修正沒有 schema migration/backfill。不要刪除已完成週次、重置 `last_schedule_key`、清除 lease、重新建立 invitation 或人工重跑已完成的 W43。若未來發生不可逆資料寫入，另行評估資料恢復；不得用較舊 DB snapshot 覆蓋新增邀請或使用者資料。

## 本輪實際執行範圍

本輪只讀 Production process、active symlink、health、Mongo retry counters、autostart 與外部 Guardrail 資產；並在 mock Mongo 與新候選 worktree 建置/測試。沒有停用 GNOME、建立/安裝 systemd unit、切換 symlink、啟停 Production、改寫 DB 或觸發 matching。
