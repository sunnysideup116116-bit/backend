# Memory／Context 指南入口

現行完整指南已統一維護於 [docs/MEMORY_CONTEXT_ENGINE_GUIDE.md](docs/MEMORY_CONTEXT_ENGINE_GUIDE.md)。
本檔保留原入口，避免根目錄與 docs 兩份內容再度漂移；不是另一個版本或 rollback 指南。

- Public Context：最多 32 則／8,000 字元原文，常駐最多 8 筆帶方向偏好。
- Mongo preview 最多 12 筆；Neo4j durable PREFERS／AVOIDS 為長期關係讀取來源，CURRENTLY_WANTS 是有期限的近期意圖。
- Profile 的 memory.search_my_profile 可依 query 補查常駐集合以外的本人偏好；結果有 unavailable／truncated 限制，不代表全庫完整清單。
- 詳細 owner、CAS、compaction、restore、工具及降級契約一律以上述指南和目前程式為準。
