# 阿月 Backend Production Cutover 與相容回退 Runbook

**狀態：受控切換草案；本文命令只供另外授權的維護窗口使用。本輪沒有切換服務。**

## 凍結版本與適用範圍

- GitHub `main` 的本輪檢查基準：`d545b5816a72d32f22eb4dd71935449db85f2495`。CI timeout 修復分支為 `codex/event-v2-selection-clock-fix`，commit `4cfbd82263dcdb1946b54567d3e8158c49ef8250`；PR 建立受 GitHub API 403 阻擋，尚未合併。該 commit 只修改測試，不得在 PR 建立、CI failure-name parity 和 merge 之前宣稱它已進入 main。
- 目前 pre-merge Release Candidate：`.worktrees/release-candidate-6a2a137-server`，實際 checkout `4cfbd82263dcdb1946b54567d3e8158c49ef8250`；名字沿用舊目錄，不能按目錄名稱推斷 SHA。合併 P0 修復後，仍須用 GitHub 最新 main SHA 更新並再次核對。
- Production active-release symlink 指向 `1efa6c72f775309ce42ff3dcfd85f6479c3f91a9`；但 2026-10-27 唯讀 Host 檢查發現現有 GNOME Terminal launcher 和服務的 cwd 在 dirty `Server/`（HEAD `68dd674f88c07aabea9a1329d9db35e079865c1b`，16 個 dirty entries），所以實際載入版本不能表示成單一 Git SHA。不得改動這個 dirty `Server/`。
- Rollback PR base：`codex/rollback-base-1efa6c7`，固定於 `1efa6c72f775309ce42ff3dcfd85f6479c3f91a9`。Rollback 分支：`codex/rollback-compatibility-1efa6c7`；PR #50 必須留在這個舊版 base，不得合併到最新 `main`。
- Rollback code pin：`aff995c8a6740e1b9e0b9e430075bf4c04992636`，包含 Atlas 相容的 collection filter、安全測試及 OpenRouter systemd provider。Manifest 的 `rollback_code_commit` 必須一致；這是 guard 的 `--expected-target-sha`。PR head 僅可在此 pin 之後增加 runbook、manifest 等允許的文件變更；Guard 不得接受額外程式或測試差異。
- Rollback runtime worktree 固定路徑：`/home/sunny/桌面/Graduate_Project/.worktrees/rollback-compatibility-1efa6c7-server`。最後交付的精確 runtime checkout SHA 以本機 artifact receipt 記錄為準；其 HEAD 必須是上述 code pin 的後代，且相對 pin 只能有 Runbook、Manifest 和 archive sidecar 文件差異。
- PR #50 是獨立舊版相容回退交付。它保留現有 Mongo/Neo4j 資料，不做 migration/backfill，也不還原舊 DB snapshot。
- 2026-10-27 唯讀 Production 檢查：四個 health endpoint 均 HTTP 200；一個 GNOME Terminal launcher、沒有專用 user/system `ayue-backend.service`；Mongo/W44 讀取成功，W44 `2026-W44`, run 35, `completed/partial`, 47/47 terminal，無 pending/retry/resume、無 active lease。Global Invitation Delivery 狀態仍 UNKNOWN，Production 沒有對應索引。
- 同一檢查發現目前 dirty `Server/start_all.sh` 沒有全域 flock，`/run/user/1000/ayue-backend-start-all.lock` 不存在，launcher 沒持有 lock FD；目前只有一個 launcher，但 GNOME backend autostart 仍 active，可能另起指向 `active-release` 的 launcher。現有 process ancestry 是 GNOME Terminal；不得在維護時段前另起 launcher。PR #48 候選的 global lock 已由隔離啟動競態測試驗證，但 Production 尚未使用該版本。
- 目前 live checkout 的 Event Worker 沒有 supervisor，Production heartbeat endpoint 最近回報 404；DB 顯示 W44 已完成且沒有待 claim 工作，但 worker liveness 仍無法由目前服務監管。
- 實際 Production provider 為 OpenRouter。Rollback/New Release systemd unit 必須明確使用 `start_all.sh openrouter`；不得因舊 unit 範本預設 Ollama 而切換 provider。
- 現有來源 `Server/social/.env` 明確設 lifecycle/weekly 為 `on`；程式預設 delivery 與 semantic circuit recovery 也為 `on`。Production Process memory 不能由本次 read-only shell 直接確認，維護前按「可能為 ON」處理；新版候選 `.env` 的 Stage 1 則已明確將四項全部設為 `off`。

## 回退 Runtime Artifact 與設定

- Runtime manifest：`docs/rollback-runtime-manifest.json`。它固定 rollback code SHA、三個 Python runtime 的 pip freeze、Python／Node 版本、依賴檔與安裝目錄 digest，以及外部 Guardrail 資產 digest。
- Runtime archive 僅包含 `.local-venv/social`、`.local-venv/matchmaker`、`venv`、`pi_agent/node_modules`；不含 `.env`、正式資料或 Guardrail 大型模型。Archive 的 SHA-256 記錄在 manifest。
- Archive 已還原至 Manifest 指定的固定路徑 `/home/sunny/桌面/Graduate_Project/.worktrees/rollback-compatibility-1efa6c7-server`；檢查過 Python 3.12.3 三組 `pip freeze` 完全符合 manifest、`pip check` 無 broken requirements、Pi bridge check 和 11 項 Node bridge tests 通過。Python interpreter symlink 指向主機 `/usr/bin/python3.12`；不得將 runtime roots 搬到其他 host 或不同路徑。
- Archive 已複製至受控目錄 `/home/sunny/桌面/Graduate_Project/artifacts/rollback-runtime-20261027/`，目錄權限 `0700`、archive/sidecar/manifest/runbook 權限 `0600`。原始 `/tmp` archive 保留。複製前後 SHA-256 均為 `b0db1fca0bd471bc9ab6d45405b6c744a2a175f4fe9be73231d1c8450e65dd92`；檔案有 42,993 個 member，沒有 `.env`、路徑跳脫或正式資料。Manifest 固定的副本路徑必須在維護窗口再次核對。
- Guardrail binary/model 是外部相依：manifest 列出絕對路徑、大小、mode 與 SHA-256。部署前逐一比對，檔案須可由服務使用者讀取，binary 須可執行；不可刪除或搬移。
- `.env` 不進 Git、不進 runtime archive、不輸出至 terminal/log。維護窗口前，已在兩個非 Production 候選 checkout 中以 mode `0600` 複製根目錄 `.env`、`social/.env`、`matchmaker_agent/.env`；只驗證必要 key 存在，未輸出秘密。Social `MONGO_DB_NAME` 為 `profiling_fordemo`。新 Release / rollback 候選的 `social/.env` 四個 Event Stage 1 flags 已各自設為 `off`；這不會修改 Production `.env`。
- systemd unit `ExecStart` 明確傳入 `openrouter`；根目錄 `.env` 的 OpenRouter key 在候選副本存在且非空，秘密不在 unit、命令列、terminal output 或 Git。安裝前仍須在維護窗口重新核對執行中 provider 與候選設定；若當時有效 provider 不同，立即 STOP。

## 強制 STOP 條件

1. main、Production、rollback base 或 rollback code SHA 與本文件／manifest 不同；GitHub remote 狀態無法核對；rollback branch 未經 review 核准；或候選程式與 manifest digest 不符。
2. 新版／回退 runtime 缺失、`pip check`/Pi bridge check 失敗、runtime digest 不符、Guardrail 資產不完整，或 `.env` 權限／必要設定不符。
3. GNOME、user/system systemd 啟動入口仍 active/enabled、任何 backend/Event writer/launcher process 存在，或 process table / systemd / autostart 無法完整讀取。2026-10-27 的 GNOME backend entry 仍 active，現有 launcher cwd 是 dirty `Server/`；維護時必須重新識別目前實際 PID/source，不能假設 active-release symlink 就是執行來源。
4. rollback guard exit code 非 0、Mongo DB 名稱不是明確的 `profiling_fordemo`、必要 collection 或 singleton/run record 缺失，Mongo timeout/授權/部分讀取，job schema/checkpoint/lease 不明，或有不同 run 的 pending rows。
5. Mongo singleton 為 queued/running 且工作狀態無法由 pinned rollback worker 接手；lease 尚未到期；未知 pending、異常 checkpoint、無 checkpoint 的 `resume_pending`，或 retry 次數超過已支援範圍。
6. Stage 1 前唯讀發現 queued/running Event job、有效 lease 或 pending rows。`EVENT_WEEKLY_CYCLE_ENABLED=off` 只阻止新週次入列，**不會阻止 Event worker claim 已存在的 queued job**。
7. W44 已自然完成；若 W44/W45 job、checkpoint、retry、lease 或 Global Invitation Delivery 狀態不明，禁止啟用 Weekly/Delivery。下一個週次 W45 必須另行授權。
8. 任何期間都不得用手動 enqueue、重設 `last_schedule_key`、清 lease、刪除 checkpoint、重跑 W43、覆蓋使用者資料或以舊 DB snapshot 做「程式 rollback」。

## 部署前固定檢查

在另行核准的維護窗口，先將以下路徑設為實際值，並確認任何命令輸出都不含 `.env` 內容：

```bash
ROOT='/home/sunny/桌面/Graduate_Project'
ACTIVE="$ROOT/.worktrees/active-release"
NEW_RELEASE="$ROOT/.worktrees/release-candidate-6a2a137-server"
ROLLBACK_RELEASE="$ROOT/.worktrees/rollback-compatibility-1efa6c7-server"
NEW_RELEASE_SHA='4cfbd82263dcdb1946b54567d3e8158c49ef8250' # pre-merge only; STOP until P0 PR is merged and main SHA is frozen
ROLLBACK_CODE_SHA='aff995c8a6740e1b9e0b9e430075bf4c04992636'
RUNTIME_ARCHIVE="$ROOT/artifacts/rollback-runtime-20261027/rollback-runtime-c55450c644fcad13b705c4baa529c301351f7882.tar.gz"
```

1. 重新 fetch GitHub，確認 `legacy-origin/main`、active release、new release、rollback base、rollback branch 與 manifest 的 SHA 完全一致；確認 rollback PR base 是 rollback base。不得把 rollback branch 推到 `main`。

   ```bash
   git -C "$ROOT/Server" fetch legacy-origin
   test "$(git -C "$ROOT/Server" rev-parse legacy-origin/main)" = \
     'd545b5816a72d32f22eb4dd71935449db85f2495' || exit 1
   test "$(git -C "$NEW_RELEASE" rev-parse HEAD)" = "$NEW_RELEASE_SHA" || exit 1
   # STOP if the CI-fix PR is not merged: 4cfbd82 is a pre-merge branch commit, not main.
   git -C "$ROOT/Server" merge-base --is-ancestor "$NEW_RELEASE_SHA" legacy-origin/main || exit 1
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

   Archive 已位於受控副本目錄；每次維護仍須重新驗證 archive 與 sidecar 的 SHA-256。
3. 對兩個候選各自執行三個 Python runtime 的 `pip check`、`node pi_agent/bridge.mjs --check`、`npm --prefix pi_agent test` 與 `bash -n start_all.sh`。任何相依檢查失敗即停止。
4. 將根目錄 `.env`、`social/.env`、`matchmaker_agent/.env` 複製到兩個候選 release，保留 owner 與 `0600` mode；根目錄 `.env` 提供 OpenRouter key，Matchmaker `.env` 提供 Neo4j 設定。只檢查必要變數是否存在及 non-secret flags，不列印值。Social `MONGO_DB_NAME` 必須是 `profiling_fordemo`。不要改 Production `.env`。
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

   只向重新核實為目前 Backend `start_all.sh` 的 launcher PID 送 `SIGTERM`。最近檢查時該 launcher cwd 是 dirty `Server/`，而 active-release symlink 指向另一個 `1efa6c7` worktree；不可用 symlink target 代替 PID/cwd 識別：

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

7. 從已 review 的 `deploy/systemd/ayue-backend.service` 範本安裝唯一 user unit。它使用 `start_all.sh openrouter`、`Restart=on-failure`、`RestartPreventExitStatus=75`、`KillMode=mixed`、launcher graceful shutdown 30 秒、systemd stop timeout 45 秒與有限 start rate。執行 `systemd-analyze verify`、`systemctl --user daemon-reload`，確認 user/system 同名 unit 狀態後，才 enable/start user unit。

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

- 四個 Event flags 全 OFF，Provider 固定 OpenRouter，semantic rollout flags 沿用 Production。
- 開始前確認沒有 queued/running Event job、pending user rows、未到期/active lease 或 retry/checkpoint。這一步必要，因 weekly flag 不會阻止 worker claim 已排入的 job。
- 啟動後驗證 Social/Risk/Matchmaker/Guardrail health、Pi confirmation、受控測試帳號 API/Matching 流程；不建立真實 invitation、不扣 quota、不手動 enqueue。
- Read-only 檢查 singleton、W43/W44 key、event weekly users；因缺少 Global Invitation Delivery 索引，不對 `matches` 做全表 delivery count，維持 delivery status UNKNOWN 並保持 worker OFF。
- Lifecycle、delivery、weekly scheduling、semantic circuit recovery 保持 OFF。Stage 1 全部通過後仍不自動開啟背景功能。

### Stage 2 — Worker Readiness

- 確認 systemd `ActiveState=active`、MainPID 穩定、RestartCount 沒有異常；global startup lock 唯一且在 launcher 結束時釋放。
- 確認 Event supervisor/worker started、沒有 exhausted recovery/fatal；正常 idle、慢 Discovery 或 LLM 延遲不能當作死亡。
- 以 majority read concern 唯讀檢查 Mongo singleton lease、retry/resume、checkpoint 與 run record；Neo4j 只做 connectivity/integrity read-only query。不得 reset/backfill。
- Lifecycle 與 delivery 各自審核：確認 expiry/graph cleanup 邊界、delivery retry/outbox、既有 proposal state 都可接受後，**逐項另行核准**再設定 ON。不得將全部 Worker 一次開回。
- Semantic circuit recovery 保持 OFF，直到已確認 circuit file/state、provider health 與持續監控，再另行核准。

### Stage 3 — Weekly Scheduling（W44 已於 2026-10-26 自然完成）

- 2026-10-27 Host read-only 驗證：`last_schedule_key=2026-W44`, run 35, `completed/partial`, 47/47 terminal；pending/retry/resume 為 0，active lease=false，沒有 queued/running discovery job。W44 已完成，不得重新排入或補跑 W43/W44。
- Global Invitation Delivery index 仍缺失，delivery 狀態 UNKNOWN；不能據此宣稱所有邀請已交付或不存在待交付項目。Stage 1 保持 delivery OFF，直到維護窗口有安全的索引/狀態驗證與另外授權。
- 下一個原生週次為 W45（2026-11-02 08:00 Asia/Taipei）。即使 Stage 1/2 通過也保持 weekly OFF；只有逐項驗收並取得明確核准後，才允許 Weekly Scheduling。不得手動建立 W45 job 或把錯過的 W44 當成自動補跑許可。
- 重新啟動任何 worker 前先用 read-only 檢查 singleton、W44/W45 key、queued/running、pending/retry/checkpoint/lease。任何未知或未完成狀態都 STOP；不得重設 key、重建 job、清 lease 或刪 checkpoint。

## 啟動後驗收

- Health：Social `GET /api/health`、Risk `GET /health`、Matchmaker `GET /health`、Guardrail `GET /v1/models` 均須 HTTP 200。
- systemd：`ActiveState=active`、`SubState=running`、MainPID 穩定；journal 無重複 launcher、supervisor fatal 或 restart throttling。
- Process/lock：每個固定 port 只有一個預期 listener；一個 `start_all.sh`；一個 startup lock owner；一個 Social Event supervisor/worker。
- Mongo：majority read-only 查 singleton、週次 key、run/progress/retry/checkpoint、proposal/invitation counts；禁止 write、migration、backfill、schedule reset。
- Neo4j：使用 read-only credentials 執行 `RETURN 1` 與 bounded integrity query；不輸出 URI、使用者資料或完整 property。
- Matching/Pi：只用隔離或明確授權的測試帳號驗證 Activity Evidence、跨 alias AVOIDS、pair exclusion、qualification、Pi `street fighter` confirmation 與主要 recommendation reason；不可產生真實邀請/扣 quota。
- Event：不手動 enqueue W43/W44。Stage 1 必須看不到新 Event job 或 invite；Stage 2/3 僅依獨立核准啟用。

## Rollback Guard 與操作程序

新版故障後，先停止所有 writer，再執行唯讀 guard；guard 必須在預定 release 路徑中運行。Guard 會用 anchored BSON regex 查核三個必要 Mongo collection；這已在 Production Atlas 上以 read-only probe 驗證，原本 `$in` listCollections filter 會回 Atlas error `8000`，修正後的 filter 可辨識三個 collection。`--workers-stopped` 是人工聲明，**單獨使用不會通過**：程式另檢查 GNOME autostart、user/system `ayue-backend.service` inactive/disabled、所有 backend/Event writer process，並在 Mongo 讀取前後各掃描一次。

```bash
ROOT='/home/sunny/桌面/Graduate_Project'
ACTIVE="$ROOT/.worktrees/active-release"
ROLLBACK_RELEASE="$ROOT/.worktrees/rollback-compatibility-1efa6c7-server"
EXPECTED_ROLLBACK_CODE_SHA='aff995c8a6740e1b9e0b9e430075bf4c04992636'

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

本輪修改 rollback guard、mock regression、OpenRouter systemd unit 範本、此 runbook 與 runtime manifest；修正 Atlas collection metadata filter，並將 verified runtime archive 保存到受控 artifact 目錄。Release Candidate 與 rollback runtime 的 `.env` 是非 Production 副本，mode `0600`；四個 Stage 1 Event flags 在候選設定中皆 OFF。沒有停止/重啟 Production、改 active-release、改 Production `.env`、修改 GNOME autostart、安裝/啟用 Production systemd、寫入 MongoDB/Neo4j、啟用 Event Worker 或觸發 Matching/Event job。Production 的 Global Invitation Delivery index 尚缺，狀態維持 UNKNOWN。
