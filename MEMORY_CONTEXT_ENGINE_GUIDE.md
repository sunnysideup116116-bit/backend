# Memory／Context 指南入口

現行完整指南已統一維護於 [docs/MEMORY_CONTEXT_ENGINE_GUIDE.md](docs/MEMORY_CONTEXT_ENGINE_GUIDE.md)。
本檔保留原入口，避免根目錄與 docs 兩份內容再度漂移；不是另一個版本或 rollback 指南。

- Public Context：HTTP adapter 最多抓 32 筆作為 bounded sentinel；最終 Pi projection 最多保留 12 則／6,000 字元，常駐最多 8 筆帶方向偏好。
- Mongo preview 最多 12 筆；Neo4j durable PREFERS／AVOIDS 為長期關係讀取來源，CURRENTLY_WANTS 是有期限的近期意圖。
- Profile 的 memory.search_my_profile 可依 query 補查常駐集合以外的本人偏好；結果有 unavailable／truncated 限制，不代表全庫完整清單。
- 詳細 owner、CAS、compaction、restore、工具及降級契約一律以上述指南和目前程式為準。
- 2026-09-15：Summary v5 已部署受控全帳號試行；70／70 合成案例通過，46／46 現有 profile 通過資格判定。短聊天使用近期 Context，長聊天才排入 room-specific summary；品質 review 仍會保留舊摘要，不強制注入。最新英文簡報講稿見 [Context／Memory／Event speaker notes](docs/PROGRESS_REPORT_CONTEXT_MEMORY_EVENT_2026-09-15.md)。
