# 阿月 Backend Production Cutover 與相容回退 Runbook

**狀態：受控切換草案；本文命令只供另外授權的維護窗口使用。本輪沒有切換服務。**

## 凍結版本與適用範圍

- GitHub `main`：`6a2a1372b3d4ea5ada4650da90f06f59642dec67`。
- Production release：`1efa6c72f775309ce42ff3dcfd85f6479c3f91a9`。
- 新版 Release Candidate：`.worktrees/release-candidate-6a2a137-server`，目標 SHA 同上 main。
- Rollback PR base：`codex/rollback-base-1efa6c7`，固定於 `1efa6c72f775309ce42ff3dcfd85f6479c3f91a9`。
- Rollback 分支：`codex/rollback-compatibility-1efa6c7`。程式碼 SHA：`c55450c644fcad13b705c4baa529c301351f7882`；manifest 的 `rollback_code_commit` 必須與此值一致，這是 guard 的 `--expected-target-sha`。
- **PR review head 與 Production rollback checkout 分開固定：** PR #50 的 `56f15c14729895b8135cbff4cef18c6af4bf1625` 是完整 review/CI 記錄，包含對 `test_durable_weekly_scans_population_with_semantic_pending` 的測試契約修正；它不是 Production rollback checkout。正式 rollback worktree 固定使用 `2d70ceb507f5edbd19a0d0a9eae5ae8aa7956e7e`，該 commit 是 `c55450c644fcad13b705c4baa529c301351f7882` 的 docs-only descendant，程式碼不變；PR head 相對 code pin 還含有 `social/tests/test_event_v2_contract.py`，guard 必須拒絕直接以 PR head 作為 runtime checkout，不得放寬 guard allowlist。操作時使用 PR #50 最新審查過的 Runbook，runtime source checkout 則固定為 `2d70ceb`；後續文件更新不改變此 runtime SHA。
- Rollback branch 是舊版專用相容回退版本。PR base 必須是 rollback base；不得合併至新版 `main`。
- Production 最近一次已知健康驗證由 2026-10-21 使用者回報：四個 health endpoint 為 HTTP 200、無重複 launcher、MongoDB／Neo4j 可用、W43 completed；Event lifecycle、delivery、weekly scheduling 暫時關閉。本文件沒有重查 Production。
- 相容回退只增加 retry/checkpoint 恢復及唯讀 guard；不做 schema migration/backfill，也不回復 MongoDB／Neo4j snapshot。

## 回退 Runtime Artifact 與設定

- Runtime manifest：`docs/rollback-runtime-manifest.json`。它固定 rollback code SHA、三個 Python runtime 的 pip freeze、Python／Node 版本、依賴檔與安裝目錄 digest，以及外部 Guardrail 資產 digest。
- Runtime archive 僅包含 `.local-venv/social`、`.local-venv/matchmaker`、`venv`、`pi_agent/node_modules`；不含 `.env`、正式資料或 Guardrail 大型模型。Archive 的 SHA-256 記錄在 manifest。
- Archive 對應目前 Linux 主機與 manifest 的 `archive.extraction_release_path`：`/home/sunny/桌面/Graduate_Project/.worktrees/rollback-compatibility-1efa6c7-server`。正式使用前，該路徑必須是乾淨的 `2d70ceb` rollback checkout（其程式碼 pin 為 `c55450c`）；不可將 PR review head `56f15c1` 留在該路徑後直接執行 guard。Python venv 的 Python 執行檔連結到主機 `/usr/bin/python3`；不可把 venv 搬到另一個 host 或不同路徑後直接使用。若 worktree 被清理，先從 GitHub checkout 固定的 `2d70ceb` 至 manifest 指定原路徑，再以 digest 驗證並還原 runtime archive。
- Artifact 目前放在隔離交付目錄 `/tmp/ayue-rollback-artifacts/rollback-runtime-c55450c644fcad13b705c4baa529c301351f7882.tar.gz`；檔案 SHA-256 由 manifest 固定。它不依賴 worktree 存活，但 `/tmp` 不是長期保存位置；正式維護前須將 archive 與 manifest 複製到受控、備份且權限限制的 release storage，並重新驗證 digest。
- Guardrail binary/model 是外部相依：manifest 列出絕對路徑、大小、mode 與 SHA-256。部署前逐一比對，檔案須可由服務使用者讀取，binary 須可執行；不可刪除或搬移。
- `.env` 不進 Git、不進 runtime archive、不輸出至 terminal/log。維護窗口從目前 active release 安全複製到新 release 和 rollback release，檔案 owner 為服務使用者、mode `0600`。Social 設定必須明確包含 `MONGO_DB_NAME=profiling_fordemo`；guard 會拒絕缺漏、不同 DB、缺少必要集合或 singleton。
- systemd unit 的 `ExecStart` 明確使用 `ollama`；保留 Production 目前的 semantic rollout flags，不切換 OpenRouter，也不擴大 canary。不要將秘密放進 unit、命令列或 PR。

## 強制 STOP 條件

1. main、Production、rollback base 或 rollback code SHA 與本文件／manifest 不同；GitHub remote 狀態無法核對；rollback branch 未經 review 核准；或候選程式與 manifest digest 不符。
2. 新版／回退 runtime 缺失、`pip check`/Pi bridge check 失敗、runtime digest 不符、Guardrail 資產不完整，或 `.env` 權限／必要設定不符。
3. GNOME、user/system systemd 啟動入口仍 active/enabled、任何 backend/Event writer/launcher process 存在，或 process table / systemd / autostart 無法完整讀取。
4. rollback guard exit code 非 0、Mongo DB 名稱不是明確的 `profiling_fordemo`、必要 collection 或 singleton/run record 缺失，Mongo timeout/授權/部分讀取，job schema/checkpoint/lease 不明，或有不同 run 的 pending rows。
5. Mongo singleton 為 queued/running 且工作狀態無法由 pinned rollback worker 接手；lease 尚未到期；未知 pending、異常 checkpoint、無 checkpoint 的 `resume_pending`，或 retry 次數超過已支援範圍。
6. Stage 1 前唯讀發現 queued/running Event job、有效 lease 或 pending rows。`EVENT_WEEKLY_CYCLE_ENABLED=off` 只阻止新週次入列，**不會阻止 Event worker claim 已存在的 queued job**。
7. W44 週次已到但尚未通過 Stage 2、隊列狀態不清楚、已存在 W44 job/checkpoint/retry，或未取得本次啟用的明確核准。
8. 任何期間都不得用手動 enqueue、重設 `last_schedule_key`、清 lease、刪除 checkpoint、重跑 W43、覆蓋使用者資料或以舊 DB snapshot 做「程式 rollback」。

## 部署前固定檢查

在另行核准的維護窗口，先將以下路徑設為實際值，並確認任何命令輸出都不含 `.env` 內容：

```bash
ROOT='/home/sunny/桌面/Graduate_Project'
ACTIVE="$ROOT/.worktrees/active-release"
NEW_RELEASE="$ROOT/.worktrees/release-candidate-6a2a137-server"
ROLLBACK_RELEASE="$ROOT/.worktrees/rollback-compatibility-1efa6c7-server"
ROLLBACK_CODE_SHA='c55450c644fcad13b705c4baa529c301351f7882'
RUNTIME_ARCHIVE='/tmp/ayue-rollback-artifacts/rollback-runtime-c55450c644fcad13b705c4baa529c301351f7882.tar.gz'
```

1. 重新 fetch GitHub，確認 `legacy-origin/main`、active release、new release、rollback base、rollback branch 與 manifest 的 SHA 完全一致；確認 rollback PR base 是 rollback base。不得把 rollback branch 推到 `main`。

   ```bash
   git -C "$ROOT/Server" fetch legacy-origin
   test "$(git -C "$ROOT/Server" rev-parse legacy-origin/main)" = \
     '6a2a1372b3d4ea5ada4650da90f06f59642dec67' || exit 1
   test "$(git -C "$ROOT/Server" rev-parse legacy-origin/codex/rollback-base-1efa6c7)" = \
     '1efa6c72f775309ce42ff3dcfd85f6479c3f91a9' || exit 1
   git -C "$ROLLBACK_RELEASE" merge-base --is-ancestor "$ROLLBACK_CODE_SHA" HEAD || exit 1
   git -C "$ROLLBACK_RELEASE" diff --quiet "$ROLLBACK_CODE_SHA" HEAD -- . \
     ':(exclude)docs/production-cutover-runbook.md' \
     ':(exclude)docs/rollback-runtime-manifest.json' \
     ':(exclude)docs/rollback-runtime-artifact.sha256' || exit 1
   ```

2. 對 runtime archive、Python runtime roots、`pi_agent/node_modules`、requirements、`package-lock.json`、Guardrail binary/model 比對 manifest SHA-256。目錄必須為 release 自己的實體目錄；不可連到其他 worktree。先驗證交付 archive 的 checksum：

   ```bash
   (cd "$(dirname "$RUNTIME_ARCHIVE")" && \
     sha256sum -c "$(basename "$RUNTIME_ARCHIVE").sha256") || exit 1
   ```

   目前 archive 位於 `/tmp`；正式維護前需先複製 archive 與 sidecar 到受控 release storage，再以同一方式核對。
3. 對兩個候選各自執行三個 Python runtime 的 `pip check`、`node pi_agent/bridge.mjs --check`、`npm --prefix pi_agent test` 與 `bash -n start_all.sh`。任何相依檢查失敗即停止。
4. 將 `.env` 複製到兩個候選 release，保留 `0600` owner/mode；只檢查必要變數是否存在及 non-secret flags，不列印值。Social `MONGO_DB_NAME` 必須是 guard 固定的 `profiling_fordemo`。不要改 Production `.env`。
5. 新版首次啟動前，於新版 `social/.env` 明確設成下列值；其他 Production semantic flags 保持原樣：

   ```dotenv
   EVENT_LIFECYCLE_WORKER_ENABLED=off
   EVENT_DELIVERY_WORKER_ENABLED=off
   EVENT_WEEKLY_CYCLE_ENABLED=off
   EVENT_SEMANTIC_CIRCUIT_RECOVERY=off
   ```

   這些設定在 Stage 1/2 保持 OFF；不代表停用了 Event worker claim loop。

## 受控 systemd 接管順序

本機先前確認的入口是 GNOME backend autostart 與手動 terminal；沒有已安裝的專用 `ayue-backend.service`。維護窗口仍須重新盤點使用者與 systemd 入口。依此順序執行，不能讓舊/新 launcher 重疊：

1. 驗證來源 SHA、release runtime、`.env` 與 Guardrail 資產；確認 `/run/user/<uid>` 存在、屬於 service user 且權限為 `0700`。新版 global `flock` 位於該 runtime directory；只有 launcher 持有 lock，child 不得繼承 lock FD。
2. 先盤點所有 GNOME/system-wide autostart `.desktop` 與 user/system systemd unit；只處理實際 backend launcher。以下是已確認 user entry 的範例，若檔案清單不同就停止並重新識別，不要改其他桌面入口：

   ```bash
   AUTOSTART_ENTRY="$HOME/.config/autostart/啟動DatingApp後端服務.desktop"
   DISABLED_DIR="$HOME/.config/autostart.disabled"
   test -f "$AUTOSTART_ENTRY" || exit 1
   install -d -m 700 "$DISABLED_DIR"
   mv -- "$AUTOSTART_ENTRY" "$DISABLED_DIR/啟動DatingApp後端服務.desktop"
   ```

3. 唯讀列出 listener 與 launcher，核對舊 launcher 的 PID、使用者、cwd/path：

   ```bash
   ss -lntp | rg ':(8000|8001|9001|8081)\b' || true
   ps -eo pid,ppid,user,comm,args | rg 'start_all\.sh|uvicorn|event_worker' || true
   ```

   只向已核實為舊 active release `start_all.sh` 的 launcher PID 送 `SIGTERM`：

   ```bash
   OLD_LAUNCHER_PID='人工從上述唯讀 inventory 核對後填入'
   kill -TERM "$OLD_LAUNCHER_PID"
   ```

   不要再執行舊 `start_all.sh` 當 stop command；不要用 `fuser`、`pkill` 或 port cleanup 清除未知 process。

4. 等舊 launcher 與子程序完全結束；再次確認 ports 8000、8001、9001、8081 無 listener，無 process cwd/argv 指向任何 backend release，也沒有 Event writer。若有殘留或來源不明，STOP。
5. 執行 Stage 1 的 read-only Mongo snapshot 前置檢查：singleton 必須是有效 terminal state、lease 已清空、weekly pending/retry/resume rows 為 0、沒有 queued job。若有任一 queued/running job，worker 即使 weekly flag OFF 仍可能 claim，故不得啟動。
6. 原子切換 `active-release` symlink 至新版 candidate；驗證 target SHA/path：

   ```bash
   test -d "$NEW_RELEASE" && test ! -L "$NEW_RELEASE" || exit 1
   ln -s "$NEW_RELEASE" "$ROOT/.worktrees/active-release.next"
   mv -Tf "$ROOT/.worktrees/active-release.next" "$ACTIVE"
   test "$(readlink -f "$ACTIVE")" = "$(readlink -f "$NEW_RELEASE")" || exit 1
   ```

7. 從已 review 的 `deploy/systemd/ayue-backend.service` 範本安裝唯一 user unit。它使用 `start_all.sh ollama`、`Restart=on-failure`、`RestartPreventExitStatus=75`、`KillMode=mixed`、launcher graceful shutdown 30 秒、systemd stop timeout 45 秒與有限 start rate。執行 `systemd-analyze verify`、`systemctl --user daemon-reload`，確認 user/system 同名 unit 狀態後，才 enable/start user unit。

   ```bash
   UNIT="$HOME/.config/systemd/user/ayue-backend.service"
   install -d -m 700 "$HOME/.config/systemd/user"
   install -m 600 "$ROLLBACK_RELEASE/deploy/systemd/ayue-backend.service" "$UNIT"
   systemd-analyze verify "$UNIT"
   systemctl --user daemon-reload
   systemctl --user enable ayue-backend.service
   systemctl --user start ayue-backend.service
   ```

8. 確認 GNOME backend autostart 持續停用；systemd `MainPID` 是唯一 launcher；global lock 只有一個 owner；四個服務各自只有一個 listener；Social Event supervisor/worker 只有一份。若有第二個啟動者、port 清理、restart limit 或 worker fatal，STOP。

## Event Worker 分階段驗收與啟用

### Stage 1 — Core Backend Smoke

- 四個 Event flags 全 OFF，Provider 固定 Ollama，semantic rollout flags 沿用 Production。
- 開始前確認沒有 queued/running Event job、pending user rows、未到期/active lease 或 retry/checkpoint。這一步必要，因 weekly flag 不會阻止 worker claim 已排入的 job。
- 啟動後驗證 Social/Risk/Matchmaker/Guardrail health、Pi confirmation、受控測試帳號 API/Matching 流程；不建立真實 invitation、不扣 quota、不手動 enqueue。
- Read-only 檢查 singleton、W43/W44 key、event weekly users、proposal/delivery counts；確認沒有新 Event job 或邀請。
- Lifecycle、delivery、weekly scheduling、semantic circuit recovery 保持 OFF。Stage 1 全部通過後仍不自動開啟背景功能。

### Stage 2 — Worker Readiness

- 確認 systemd `ActiveState=active`、MainPID 穩定、RestartCount 沒有異常；global startup lock 唯一且在 launcher 結束時釋放。
- 確認 Event supervisor/worker started、沒有 exhausted recovery/fatal；正常 idle、慢 Discovery 或 LLM 延遲不能當作死亡。
- 以 majority read concern 唯讀檢查 Mongo singleton lease、retry/resume、checkpoint 與 run record；Neo4j 只做 connectivity/integrity read-only query。不得 reset/backfill。
- Lifecycle 與 delivery 各自審核：確認 expiry/graph cleanup 邊界、delivery retry/outbox、既有 proposal state 都可接受後，**逐項另行核准**再設定 ON。不得將全部 Worker 一次開回。
- Semantic circuit recovery 保持 OFF，直到已確認 circuit file/state、provider health 與持續監控，再另行核准。

### Stage 3 — Weekly Scheduling（W44：2026-10-26 08:00 Asia/Taipei）

- W44 前只有在 Stage 1/2 通過、DB read-only check 顯示無舊 queued/running work、worker/supervisor/lock 穩定、delivery/lifecycle 已逐項決定且使用者明確核准後，才可把 `EVENT_WEEKLY_CYCLE_ENABLED` 設為 ON。
- 若部署早於 W44 但上述驗收尚未完成，保持 OFF。系統不應手動建立 W44 job；準備好後由 singleton schedule key 的原生 idempotency 入列一次。
- 若維護晚於 2026-10-26 08:00，保持 weekly OFF。現行 due 邏輯在排程時間過後仍會把本週視為 due，開啟後可能晚到補入 W44；不可把這當成自動追趕許可。只有再次取得使用者對 W44 late run 的明確批准，才評估恢復。
- 若 read-only 查到 `last_schedule_key=2026-W44`、W44 queued/running/completed job、run record、checkpoint 或 retry，禁止重設 key、重建 job 或刪除資料。先比對 token、lease、run 與 pending rows；未完成/未知狀態需單獨批准處置。
- 若 W44 尚未存在且已錯過原排程，保持 OFF 並等待新的明確排程決策；不補跑 W43、不自行改 weekday/hour、不觸發 Event。

## 啟動後驗收

- Health：Social `GET /api/health`、Risk `GET /health`、Matchmaker `GET /health`、Guardrail `GET /v1/models` 均須 HTTP 200。
- systemd：`ActiveState=active`、`SubState=running`、MainPID 穩定；journal 無重複 launcher、supervisor fatal 或 restart throttling。
- Process/lock：每個固定 port 只有一個預期 listener；一個 `start_all.sh`；一個 startup lock owner；一個 Social Event supervisor/worker。
- Mongo：majority read-only 查 singleton、週次 key、run/progress/retry/checkpoint、proposal/invitation counts；禁止 write、migration、backfill、schedule reset。
- Neo4j：使用 read-only credentials 執行 `RETURN 1` 與 bounded integrity query；不輸出 URI、使用者資料或完整 property。
- Matching/Pi：只用隔離或明確授權的測試帳號驗證 Activity Evidence、跨 alias AVOIDS、pair exclusion、qualification、Pi `street fighter` confirmation 與主要 recommendation reason；不可產生真實邀請/扣 quota。
- Event：不手動 enqueue W43/W44。Stage 1 必須看不到新 Event job 或 invite；Stage 2/3 僅依獨立核准啟用。

## Rollback Guard 與操作程序

新版故障後，先停止所有 writer，再執行唯讀 guard；guard 必須在預定 release 路徑中運行。`--workers-stopped` 是人工聲明，**單獨使用不會通過**：程式另檢查 GNOME autostart、user/system `ayue-backend.service` inactive/disabled、所有 backend/Event writer process，並在 Mongo 讀取前後各掃描一次。

```bash
ROOT='/home/sunny/桌面/Graduate_Project'
ACTIVE="$ROOT/.worktrees/active-release"
ROLLBACK_RELEASE="$ROOT/.worktrees/rollback-compatibility-1efa6c7-server"
EXPECTED_ROLLBACK_CODE_SHA='c55450c644fcad13b705c4baa529c301351f7882'

"$ROLLBACK_RELEASE/.local-venv/social/bin/python" \
  "$ROLLBACK_RELEASE/scripts/rollback_retry_guard.py" \
  --expected-target-sha "$EXPECTED_ROLLBACK_CODE_SHA" \
  --current-release-dir "$ACTIVE" \
  --env-file "$ACTIVE/social/.env" \
  --workers-stopped
```

- Exit `0` 只證明讀取當下的 singleton/weekly retry state 與此固定相容候選相符；仍需人工 review、健康檢查與 Mongo/Neo4j 只讀檢查。Guard 不做 DB 寫入，也不是全資料庫一致性證明。
- Exit `75` 代表任何來源無法確認或狀態不相容；保持所有服務與 writer 停止，不要切 symlink 或啟動舊 Worker。包括錯誤/空 DB、缺 collection/singleton/run、Mongo timeout/授權/部分讀取、active lease、未知 state/pending、checkpoint schema 不支援、不同 run、啟動入口 enabled、process scan 不完整。
- 未到期 `retry_pending`/`resume_pending` 只有在 guard 驗證正確 DB、run token、有效 `resume_at`、checkpoint v1 schema、無 active lease/其他 run，且 rollback code SHA 完全匹配 manifest 的相容版本時才可 PASS；該 worker 會保留 due time/checkpoint/token。沒有精確 SHA 或任一欄位不明即 BLOCKED。
- Guard 通過後才可原子切到已 review 的 rollback release，安裝/啟用唯一 user systemd unit，再做 health/lease/read-only 驗收：

   ```bash
   ln -s "$ROLLBACK_RELEASE" "$ROOT/.worktrees/active-release.rollback-next"
   mv -Tf "$ROOT/.worktrees/active-release.rollback-next" "$ACTIVE"
   test "$(readlink -f "$ACTIVE")" = "$(readlink -f "$ROLLBACK_RELEASE")" || exit 1
   systemctl --user daemon-reload
   systemctl --user enable ayue-backend.service
   systemctl --user start ayue-backend.service
   ```

   rollback branch 沒有新版 PR #48 的 worker supervisor/global startup lock；GNOME/manual launcher 必須繼續停用，只能由單一 systemd unit 管理，並人工監控 Event progress。

若 guard 非 0，**不允許**以直接切回原始 `1efa6c7`、跳過檢查、清 lease、重設 schedule、還原舊 DB snapshot 或 force-start 代替。程式回退與資料回退是兩回事：此候選保留資料、不回滾 invitations，也不復原 Neo4j/MongoDB。若資料本身已不相容，維持停機並另行制定資料修復方案。

## 本輪範圍

本輪只修改 rollback guard、mock regression、此 runbook 與 runtime manifest，並在隔離環境封裝 runtime artifact。沒有停止/重啟 Production、改 active-release、改 GNOME/systemd、寫入 MongoDB/Neo4j、啟用 Event Worker 或觸發 Matching/Event job。
