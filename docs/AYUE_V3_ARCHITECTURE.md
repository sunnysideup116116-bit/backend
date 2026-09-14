# 公開阿月 Pi：現行架構

此檔名為既有文件連結保留。公開阿月目前固定使用 Pi；舊 DAG runtime、Planner、Scheduler、Synthesizer、subagents、實驗 grant 與切換 API 均已退役。

完整現行說明請見 [Server/AYUE_V3_ARCHITECTURE.md](../AYUE_V3_ARCHITECTURE.md)。

關鍵規則：

- 公開 request 必須先驗證 owner，所有帳號一律進 Pi，失敗不降級。
- 寫入繼續使用 shared guard、preflight、preview-bound confirmation、CAS 與 idempotency。
- 主動關心、Private V2、App voice 與 registration voice 保持各自 runtime。
- 舊公開待辦透過 `scripts/retire_public_dag_state.py` 失效；Pi、Private 與完成紀錄保持不變。
- Server 仍只由 `start_all.sh` 啟動固定四個服務。
