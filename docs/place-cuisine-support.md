# 餐廳搜尋支援料理種類（cuisine）與計費等級修正變動紀錄

> 本文紀錄「指定食物種類（火鍋、日式、素食…）查詢餐廳」功能的設計與實作變動。
> 狀態：**已實作並通過測試**。
> 附錄（2026-08-04）：本文件一併記錄 Google Places 計費等級修正（移除 Enterprise 欄位、照片改從 Text Search 取得、卡片 3→5 筆）。

## 背景：原本為什麼會出問題

`places.search_nearby` 的 `categories` 欄位是封閉 enum，只允許 5 個粗類別：

```python
categories: list[Literal["restaurant", "cafe", "bar", "attraction", "park"]]
```

當使用者說「我想吃火鍋」「找日式料理」時，Planner 無法把料理語意放進 schema：

1. 傳非法值（如 `categories=["火鍋"]`）→ Pydantic `extra="forbid"` 拒絕 → tool call 靜默失敗。
2. 退回 `categories=["restaurant"]` → 查到無關餐廳，忽略使用者意圖。
3. 想追問料理種類 → 被 prompt 規則「不可再追問料理種類」禁止。

結果：**料理語意被丟掉，指定食物種類就出問題**。

## 設計決策

- **不動 5 類 enum**：它控制 OSM tag 對應、卡片分類與 cache key，是封閉協議邊界（AGENTS.md §2）。
- **新增 optional `cuisine` 自由文字欄位**（限長 30 字）：料理語意由 LLM 抽取（§2 語意判斷），程式只做 bounded 驗證（§3 輸入安全）。
- **cuisine 只流向 Google Places textQuery 的 JSON body**（無注入面）；**絕不進入 Overpass QL**（OSM 路徑退回固定 tag，避免注入面）。
- **prompt 規則放寬**：完全沒有料理線索且尚未問過時，最多問一次（附具體選項）；「隨意／你挑」仍沿用 draft 不追問。
- **為何用 Text Search 而非 Nearby Search**：Nearby Search 只能吃結構化 `includedTypes`（英文 Table A enum，如 `hotpot_restaurant`），無法表達「家鄉味」「巷口那家」等自由描述，且需要中文→英文類型對照表（接近 AGENTS.md §2 禁止的 regex 映射）；Text Search 的 `textQuery` 自由文字可直接放「火鍋」「日式」，價格相同（皆 Pro $32/1000），且 `places.id` 在 Text Search 屬免費 Essentials。

## 計費等級修正（2026-08）

### 官方欄位分級（2026-07-28 定價頁核實）

**Text Search / Nearby Search / Place Details 三者的欄位等級不同**：

| 欄位 | Text Search | Place Details |
|---|---|---|
| `places.id` / `places.name` | Essentials ID Only（免費） | Essentials ID Only（免費） |
| `displayName` / `formattedAddress` / `location` / `types` / `googleMapsUri` / **`photos`** | **Pro**（$32/1000，免費 5,000） | **Pro**（$17/1000，免費 5,000） |
| **`rating` / `userRatingCount` / `currentOpeningHours`** | **Enterprise**（$35/1000，免費僅 1,000） | **Enterprise**（$20/1000，免費僅 1,000） |
| Atmosphere 欄位（`servesVegetarianFood` 等） | Enterprise + Atmosphere（$40/1000） | Enterprise + Atmosphere（$25/1000） |

**關鍵錯誤修正**：migration plan 原本假設 Place Details 的 `rating` 等屬 Pro（$17/1000），實際是 **Enterprise**（$20/1000、免費僅 1,000/月）——比 Pro 貴且額度只有 1/5。

### 修正行動

1. **整段移除 `get_place_details`**（Place Details API 不再呼叫）：它的唯一價值（照片）可由 Text Search 免費取得，`rating` 等屬 Enterprise 太貴。
2. **Text Search mask 加 `places.photos`**（Pro 等級，不跳級）：`search_nearby_places` 與 `resolve_place` 直接從 response 的 `photos[0].name` 建構 media URL（`_photo_url` helper，server key）。
3. **照片位元組載入計 Photos SKU**（`GetPhotoMediaRequest`，$7/1000、免費 1,000/月）：由 `AYUE_GOOGLE_PLACE_PHOTOS_ENABLED` 控制（預設 OFF）。
4. **移除 `AYUE_GOOGLE_PLACE_DETAILS_FULL` 旗標**（config.py、.env.example、AYUE_V3_ARCHITECTURE.md、live smoke 同步）。
5. **卡片與 draft 上限 3→5 筆**：`_public_place_cards` 上限、`_place_search_arguments` draft limit、`_save_place_search_draft`、`_safe_place_search_draft` 投影。**零額外成本**：Text Search 按 request 計費不按筆數。

### 照片配額 429 與開關修正（2026-08-04 追加）

**問題**：`GetPhotoMediaRequest`（載入照片位元組）是獨立於 Text Search 的 API 請求，計 `Place Details Photos` SKU。錯誤訊息 `"quota_limit_value": "0"` 代表該 Google 專案的此 metric **每日配額為 0**（未開付費或未申請配額），任何圖片載入都回 429。

**實作 bug 修正**：`_photo_url()` 初版沒檢查 `AYUE_GOOGLE_PLACE_PHOTOS_ENABLED`，旗標 `off` 時仍會產生 media URL → 前端每張卡都嘗試載圖 → 即使預設 OFF 也觸發 `GetPhotoMediaRequest` 並爆配額。已修正為：

- `AYUE_GOOGLE_PLACE_PHOTOS_ENABLED=off`（預設）→ **完全不會產生 `photo_url`**，前端無圖可載，零 `GetPhotoMediaRequest`。
- `on` → 才產生 media URL，且需要專案配額 > 0（需在 Google Cloud Console 申請提高 `GetPhotoMediaRequestPerDayPerProject` 配額或開付費方案）。
- live smoke 測試只驗證 URL 建構，**永不實際載入 media 位元組**，因此不消耗該配額。

### 決策紀錄：為什麼不用 ON（2026-08-04）

**結論：place card 不顯示照片，`AYUE_GOOGLE_PLACE_PHOTOS_ENABLED` 維持 `off`（預設）。**

考量與評估過的替代方案：

| 方案 | 結果 | 理由 |
|---|---|---|
| **Google `GetPhotoMediaRequest`（ON）** | ❌ 不使用 | ① 目前專案該 metric 每日配額 = 0（`"quota_limit_value": "0"`），開 ON 必 429；② 需去 Google Cloud Console 申請提高配額或開付費方案（帳戶層級操作，不保證通過）；③ 免費額度僅 1,000 次/月，5 張卡/次查詢 = 200 次查詢/月就用完，對正式產品太少 |
| Wikimedia Commons（OSM `wikimedia_commons=*` tag） | ❌ 不採用 | 免費無配額，但餐廳/小吃店覆蓋率極低（主要覆蓋景點/地標）；為少數卡片加一個低覆蓋率的資料來源，複雜度與收益不成比例 |
| Yelp Fusion `image_url` | ❌ 不採用 | 台灣幾乎無資料（美/歐為主），不實際 |
| Foursquare Places | ❌ 不採用 | 需申請審核與商業授權，流程長，不適合 demo |
| **不顯示照片（現狀）** | ✅ **採用** | 卡片左側已有 **Maps Embed 地圖預覽 iframe（無限免費）** 作為主要視覺；照片只是額外點綴，拿掉不影響功能或體驗完整性；零成本、零配額風險 |

**未來若想開啟照片**（程式碼已就緒，只需兩步）：
1. Google Cloud Console → 申請提高 `GetPhotoMediaRequestPerDayPerProject` 每日配額（或開付費方案）；
2. `.env` 設 `AYUE_GOOGLE_PLACE_PHOTOS_ENABLED=on`。

**注意**：不要為了「想顯示照片」而改用 Text Search 的 Enterprise 欄位（`places.photos` 屬 Pro 免費，但**載入位元組**仍計 Photos SKU）——欄位等級與載入計費是兩回事，任何 Google 照片顯示都繞不開 `GetPhotoMediaRequest`。

## 變動檔案

| 檔案 | 變動 |
|------|------|
| `services/ayue_agent/tool_registry.py` | `_PlacesNearbyArguments` 新增 `cuisine`（限長 30）；`_PlaceOutput` 移除 `rating`/`user_rating_count`/`opening_hours_summary`，保留 `photo_url` |
| `services/ayue_agent/google_places_client.py` | `search_nearby_places` 新增 `cuisine` 參數並摺進 textQuery 與 cache key；Text Search mask 加 `places.photos`；新增 `_photo_url`；**移除 `get_place_details` 與 Place Details constants** |
| `services/ayue_agent/tools.py` | `_places_nearby` 傳 cuisine 給 Google 路徑；**移除 `_places_resolve` 的 details 補欄位呼叫** |
| `services/ayue_agent/runtime.py` | `_place_search_arguments` 合併 cuisine；`_save_place_search_draft` 持久化 cuisine；`_public_place_cards` 卡片上限 3→5、移除 Enterprise 欄位投影（保留 photo_url 嚴格安全檢查）；draft limit 3→5 |
| `services/ayue_agent/context.py` | `_safe_place_search_draft` 白名單加 cuisine（限長 30、只接受 str）；limit 3→5 |
| `services/ayue_agent/router.py` | prompt 規則：明確料理 → 放入 cuisine；無線索且未問過 → 最多問一次 |
| `services/ayue_agent/config.py` | **移除 `AYUE_GOOGLE_PLACE_DETAILS_FULL`** |
| `frontend.html` | **移除 rating / 營業狀態顯示區塊**，保留照片區塊 |
| `social_demotest/.env.example` | 移除 FULL 旗標、更新計費註解 |
| `AYUE_V3_ARCHITECTURE.md` | place tools 段落更新（5 張卡、photo_url、Enterprise 排除、旗標移除） |
| `docs/google-maps-migration-plan.md` | 計費表修正（rating 屬 Enterprise）、Place Details 移除紀錄、決策紀錄更新 |

## 測試

新增／修改的測試：

| 測試檔 | 覆蓋 |
|--------|------|
| `tests/test_google_places_client.py`（新增） | cuisine 摺進 textQuery、空值省略、bounded 清理；**photos 從 Text Search response 直接取、無 photos 時回空、resolve 帶 photo** |
| `tests/test_ayue_agent_context.py`（新增） | context 白名單 projection 的 cuisine 傳遞與清理 |
| `tests/test_ayue_agent_maps_tools.py` | cuisine 傳給 Google 路徑、OSM 路徑忽略 cuisine；**移除 place_details 測試、photo_url 不額外呼叫** |
| `tests/test_ayue_agent_planner_sequences.py` | draft 合併／保存 cuisine；draft limit 3→5 |
| `tests/test_ayue_agent_place_cards.py` | **卡片上限 3→5、移除 rating/評論數/營業狀態斷言、保留 photo 安全測試** |
| `tests/test_google_maps_live_smoke.py` | **移除 `get_place_details` 測試（改測 photos）、移除 FULL 旗標** |

執行：

```bash
cd social_demotest
python -m unittest tests.test_ayue_agent_maps_tools tests.test_google_places_client tests.test_ayue_agent_context tests.test_ayue_agent_planner_sequences tests.test_ayue_agent_v2_policy tests.test_ayue_agent_place_cards
```

結果：96 tests OK。

## 安全邊界（AGENTS.md 對照）

- §2：cuisine 由 LLM 抽取，程式只做 bounded 驗證（限長 30、去控制字元）。
- §3：cuisine 只進 Google textQuery JSON body；不進 Overpass QL；cache key 含 cuisine 避免跨料理串資料。
- §6：`_safe_place_search_draft` 白名單同步加 cuisine，非 str 一律丟棄。
- §10.1：OSM 路徑不支援料理細分，退回固定 tag（可接受的免費後援限制）。
- **計費**：Enterprise 欄位（rating/評論數/營業狀態）在 schema、mask、投影、前端四層全部移除；測試斷言 card 永不帶這些欄位。

## 未解決問題

- OSM／Overpass 路徑無法細分料理（Google 關閉時，料理語意仍會丟失）。若未來需要，可擴充 `_CATEGORY_TAGS` 支援 `cuisine=*` tag，但需先評估 OSM cuisine tag 的資料品質與注入面。
- 照片**已決定不顯示**（見「決策紀錄：為什麼不用 ON」）。若要開啟，需先申請 Google `GetPhotoMediaRequest` 配額再設 `AYUE_GOOGLE_PLACE_PHOTOS_ENABLED=on`；程式碼已就緒。
- 既有測試套件有 28 個失敗與 3 個錯誤，皆為 function-calling 遷移（`plan_turn_v2` → `plan_turn_v2_function_calling`）的既有未提交變更造成，與本次變動無關（已用 git stash 驗證）。
