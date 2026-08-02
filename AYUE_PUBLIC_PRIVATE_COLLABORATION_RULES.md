# 公開阿月／阿月悄悄話協作與資料邊界規範

本文件適用於所有公開阿月、阿月悄悄話、Profile、Memory、Skill、Context 與共用 Domain Service 的修改。目標是讓兩組可以獨立開發，同時避免重複實作、隱私越界與共用模組互相破壞。

## 1. 核心分工

| 範圍 | 主要 owner | Source of truth |
| --- | --- | --- |
| 公開阿月的 Planner、Guard、Context、Tool、Composer、Trace | 公開阿月組 | `services/ayue_agent/runtime.py` 及其 public V2 模組 |
| 本人 Profile extraction 與 durable memory | 公開阿月組 | `profile_skills.py`、`memory_service.py` |
| 阿月悄悄話的 Planner、Context、Tool policy、Composer、Trace | 悄悄話組 | `services/ayue_agent/private_v2.py` 及 private-only 模組 |
| 雙人 shared history、semantic plan、關係建議記憶 | 悄悄話組 | pair／room-scoped service 與資料 namespace |
| Match、Calendar、Date coordination 等產品狀態 | 共用 Domain owner | 既有 canonical domain service |
| 共用 contracts、profile projection、privacy adapter | 雙方共同 review | typed interface，不是任一 runtime 的自由資料入口 |

分工依據是「資料代表什麼」，不是「哪個聊天畫面會使用」。

- 公開阿月負責：這位使用者本人是誰、本人明確表達過什麼、本人 durable memory。
- 阿月悄悄話負責：這段已接受關係的共同情境、雙人互動與 private advisory。
- 共用 Domain Service 負責：產品正式狀態及合法 transition。

## 2. 不可破壞的資料邊界

### 2.1 本人 durable memory

- 只能由已保存的 owner 原始訊息，經 `profile_skills.py` 的 typed extraction 與 evidence validation 產生。
- 寫入只能走 `memory_service.apply_profile_memory_proposals` 或其正式替代 contract。
- 公開阿月回覆、悄悄話、assistant message、tool result、對方資料與 pair memory 都不得直接寫成 owner durable memory。
- `HAS_PREFERENCE` 與 `profile_memory_preview` 不得成為悄悄話組的自由寫入區。

### 2.2 Pair／room relationship memory

- 必須以 accepted pair／room 為 scope，不得只用單一 user ID。
- 可以保存雙方共同確認的計畫、互動狀態與 bounded shared summary。
- 不得轉寫成任一方的個人偏好、人格事實或 durable profile。
- 關係失效、權限不足或 canonical relation 無法驗證時，讀寫都必須 fail closed。

### 2.3 Profile 讀取

阿月悄悄話可以讀 Profile，但只能經 typed projection：

- 本人：可讀完成且與當前建議相關的 bounded owner projection。
- 對方：只可讀 accepted relation 允許的 shareable projection。
- 對方未填的欄位必須維持空值，不得推測、拿本人的資料代替或從聊天反推。
- 禁止把 raw Mongo profile、完整 Neo4j memory、內部 ID、精確地址、私人行程或對方悄悄話放進 prompt、trace 或 skill input。

`public_relationship_projection.py` 目前同時被 public／private 使用；新增欄位必須採明確 opt-in，並分別測試兩個 runtime 的可見性。

## 3. Skill 規則

### Public skill

- 只註冊於 public tool／skill policy。
- 只能使用 public context 與允許的 domain service。
- 不得讀取 private advisory、private history 或 pair-private memory。

### Private skill

- 只註冊於 private registry／policy，不得加入 public `TOOL_REGISTRY`。
- 輸入只能來自 private context adapter，不得自行查完整 profile 或任意資料表。
- 若需要本人或對方 Profile 欄位，先擴充 typed private projection，再由 skill 使用。
- 寫入只能進 private／pair namespace，除非使用者另有明確同意流程且 durable-memory owner 接受該 typed proposal。

### Shared capability

- 真正共用的狀態或副作用應下沉到 canonical domain service，而不是複製兩份 prompt、router 或資料寫入。
- 共用層只提供 typed input/output、authorization、CAS、idempotency 與 privacy-safe projection。
- Public 與 Private 可以有不同 Planner 說明、工具可見性與回覆語氣，但不能各自定義同一份產品狀態。

## 4. 禁止的重複實作

- 禁止建立第二套 owner profile extractor 或自由文字 memory extractor。
- 禁止 Public／Private 各自直接寫 Match、Calendar、Date coordination 狀態。
- 禁止複製 `memory_service.py` 後形成第二個 owner-memory source of truth。
- 禁止讓 Public runtime 呼叫 Private runtime，或讓任一 runtime 把完整 prompt/history 傳給另一邊。
- 禁止用關鍵字 router 取代各自的 typed Planner／Guard。

## 5. 共用模組修改規則

修改下列範圍時，必須由雙方 review，並同時跑 public、private 與 privacy regression tests：

- `memory_service.py`、`profile_skills.py`、Profile／Memory contracts
- `public_relationship_projection.py`、`profile_location.py`
- Public／Private 共用的 contracts、context primitives、tool infrastructure
- Match、Calendar、Date coordination 等 domain service
- Mongo／Neo4j schema、collection、label、relationship type 或 migration

每次共用變更必須說明：

1. 讀取的是本人、對方還是 pair 資料。
2. 哪一個 canonical relation／owner check 授權該資料。
3. 是否會進 prompt、trace、回覆或持久化。
4. Public 與 Private 各自會看到哪些欄位。
5. 關係失效、資料缺失、stale revision 與 provider failure 時的行為。

## 6. 測試與交付門檻

涉及 Profile、Memory 或 Skill 的修改至少覆蓋：

- Public 原有能力與 tool visibility 不受 Private skill 影響。
- Private 原有能力不因 Public projection 擴充而自動取得新欄位。
- 本人 projection、對方 shareable projection 與 pair memory 的欄位差異。
- 非 accepted、錯誤 mention ID、關係失效及資料庫讀取失敗皆 fail closed。
- Private 訊息不能產生 owner durable preference；Public context 不能讀到 private／pair-private 內容。
- Trace、stream event 與錯誤訊息不包含 raw profile、memory、prompt、ID 或私人內容。
- 測試使用 stub config 或 local test database，禁止連線或修改正式 Atlas／Neo4j。

## 7. 判斷範例

### 允許

- Private skill 透過 adapter 讀取本人的性格摘要與對方公開興趣，產生不持久化的溝通建議。
- Private runtime 把雙方共同確認的約會方向保存到 pair-scoped semantic plan。
- Public 與 Private 呼叫同一個 Calendar domain service，但使用各自的 confirmation 與 privacy policy。

### 禁止

- 悄悄話根據對話推測「使用者喜歡浪漫的人」，直接寫入 `HAS_PREFERENCE`。
- Public 阿月讀取對方曾對私人阿月說過的內容。
- Private skill 直接讀完整 profile document，或因新增 schema 欄位而自動取得更多資料。
- 兩組各自實作一套取消約會、接受配對或記憶去重邏輯。

## 8. 最終原則

```text
Public Ayue：owner facts、owner durable memory、public interaction
Private Ayue：pair context、private advisory、relationship-scoped memory
Shared layer：typed contracts、privacy adapters、canonical domain state
```

如果一項功能同時需要 owner memory 與 pair memory，兩邊只能透過明確 typed projection 交換最小必要資料；不得合併兩個 source of truth。
