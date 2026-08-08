# Google Maps API 遷移與功能優化計畫

> 建立日期:2026-08-04
> 適用範圍:`Dating-App/social_demotest` 下 Public Ayue V2 的 place 工具與前端 place card。
> 隱私與架構遵循 [`AGENTS.md`](../AGENTS.md) 第 6、9、10、10.1 條與 [`AYUE_V3_ARCHITECTURE.md`](../AYUE_V3_ARCHITECTURE.md)。

---

## 1. 背景與現況分析

### 1.1 現有 map 功能全貌

| 元件 | 檔案 | 功能 | provider |
|---|---|---|---|
| Geocoding | `services/ayue_agent/maps_client.py:132 nominatim_search` | 地名→經緯度 | OSM Nominatim |
| Nearby 搜尋 | `services/ayue_agent/maps_client.py:358 nearby_places` + `build_overpass_nearby` + `overpass_query` | 周邊餐廳/咖啡/景點 | OSM Overpass |
| Place resolve | `services/ayue_agent/maps_client.py:199 resolve_place` | 單一地名解析成卡片 | OSM |
| 直線距離 | `services/ayue_agent/maps_client.py:381 measure_distance` + `haversine_m` | haversine 直線距離 | OSM |
| Cache | `services/ayue_agent/maps_client.py:82 _cache_get/_cache_put` | memory + Mongo Atlas TTL | OSM only |
| Google Places(已存在) | `services/ayue_agent/google_places_client.py` | `search_nearby_places` + `resolve_place`(Text Search v1) | Google(預設 off) |
| Tool registry | `services/ayue_agent/tool_registry.py:117-137, 309-347` | `places.search_nearby` / `places.measure_distance` / `places.resolve_place`,output 已帶 `provider: "openstreetmap" \| "google"` | provider-neutral |
| Tool executor | `services/ayue_agent/tools.py:477-550` | Google 優先,失敗自動 fallback OSM | 雙 provider |
| Runtime 卡片驗證 | `services/ayue_agent/runtime.py:300-388` | place card 驗證 + OSM embed URL | 雙 provider |
| Router 可見性 | `services/ayue_agent/router.py:67` | `can_use_places=maps_enabled()`(只看 OSM 開關) | OSM only 判斷 |
| 前端 | `frontend.html:1300-1451` | Google Maps JS API loader + `<gmp-place-details-compact>` + OSM iframe fallback | 雙 provider |
| Client config | `routers/system.py:21 client_config` | 把 browser key 安全下發前端 | Google |
| 環境變數 | `config.py:20,29-31` + `.env.example:37-49` | `AYUE_MAPS_ENABLED` / `AYUE_GOOGLE_PLACE_CARDS_ENABLED` / 兩支 key | 雙開關 |
| 測試 | `tests/test_ayue_agent_maps_tools.py` + `tests/test_ayue_agent_place_cards.py` + `tests/test_ayue_agent_v2_policy.py:15` | 已覆蓋 Google 優先 / OSM fallback / card 安全驗證 | 雙 provider |

### 1.2 現況結論

**架構已經是 Google 優先、OSM fallback 的雙 provider 設計,只是 Google 預設關閉。** 主要缺口不是「換 API」,而是:

1. **功能層缺項** — 沒有真實路程距離(Routes API)、照片(Text Search `places.photos`)、Google Maps embed。
2. **可見性判斷不一致** — `router.py:67` 只看 `maps_enabled()`(OSM 開關);若 OSM 關閉但 Google 開啟,place 工具會整個消失。
3. **Google client 沒有 cache** — 每次都打 Places API,會浪費 quota。
4. **Runtime embed 只產 OSM** — `runtime.py:_osm_embed_url` 只為 OSM 產 iframe URL;Google 卡靠 Web Component,若 Web Component 載入失敗就沒有靜態預覽。
5. **照片缺項** — Text Search response 的 `places.photos`(Pro 等級)未被使用;原計畫的 Place Details API 補欄位已取消(見 §3.3 C 更新)。

---

## 2. 目標與範圍

### 2.1 目標

- 把 Google Maps 設為主要 provider,OSM 維持作為免費 fallback。
- 補齊 Google API 對應功能:Routes API(真實路程)、Text Search `places.photos`(照片)、Maps Embed API(卡片預覽)。
- 前端啟用 Google 卡片 + Maps embed 預覽 + 照片顯示(開關控制)。
- **明確不抓 Enterprise 等級欄位**(rating / userRatingCount / currentOpeningHours / Atmosphere),控制成本。
- 不破壞 AGENTS.md 第 10.1 條:「外部結果是不可信輸入,只能經 typed projection、數量上限與字元上限後交給 Planner」。

### 2.2 不在範圍

- 刪除 OSM 程式碼(保留 fallback)。
- Place Autocomplete 輸入框(日後可再追加)。
- Maps Static API / Street View Static API(決定不導入,只用 Maps Embed)。
- Place Details API(整段移除,2026-08)。
- Enterprise 欄位(評分/評論數/營業狀態)與 Atmosphere 特性。
- Place Details Photos 位元組載入(預設 OFF,日後再開)。
- 修改 Neo4j / 模型供應商 / 正式資料清理。
- Private runtime(悄悄話)的 map 功能。

---

## 3. 設計方案

### 3.1 整體架構(維持 V2 規則)

```
Planner → tool_registry(typed schema) → tools.py executor
                                          ├─ Google 優先
                                          │   ├─ search_nearby_places (Places v1 Text Search)
                                          │   ├─ resolve_place (Places v1 Text Search)
                                          │   └─ measure_distance (NEW: Routes Compute Routes)
                                          └─ OSM fallback
                                              ├─ nearby_places (Overpass)
                                              ├─ resolve_place (Nominatim)
                                              └─ measure_distance (haversine)
↓
runtime._public_place_cards (typed projection, provider-neutral, 最多 5 張)
↓
frontend (Maps Embed iframe + Google Web Component + 照片 + category icon fallback)
```

> 照片來源:Text Search response 的 `places.photos`(Pro 等級,隨 mask 免費取得),**沒有**獨立的 place_details 步驟(已移除)。

### 3.2 provider 選擇策略

新增統一的 provider policy:

```python
def places_provider_for_turn() -> Literal["google", "osm", "disabled"]:
    if google_place_cards_enabled():
        return "google"
    if maps_enabled():
        return "osm"
    return "disabled"
```

- **Router 可見性**:從「只看 `maps_enabled()`」改為「Google 開 或 OSM 開」。
- **Executor 優先序**:Google 開 → 用 Google;Google 失敗或沒開 → 用 OSM;都沒開 → `map_unavailable`。
- **不破壞現有測試**:OSM-only 測試仍會在 `google_place_cards_enabled()=False` 時走原路徑。

### 3.3 各功能優化設計

#### A. `places.search_nearby`(已存在,優化)

**現況**:Google Text Search + OSM Overpass fallback,Google 沒 cache。

**優化**:
- 在 `google_places_client.py` 加 memory cache(key 同 OSM 的方式,TTL 15 分鐘)。
- 維持現有 typed projection(name/address/distance/map_url/provider/place_id),外加 `photo_url`,不外漏 raw payload。
- Field mask 為 Text Search Pro 欄位 + `places.photos`(見 §3.7.2)。
- 支援 `cuisine` 自由文字(2026-08 新增,見 `docs/place-cuisine-support.md`)。

#### B. `places.resolve_place`(已存在,優化)

**現況**:Google Text Search 回傳 `place_id`,直接組成卡片。

**優化**:
- Resolve 後直接帶上 `photo_url`(從 Text Search response 的 `photos[0]` 建構)。
- 維持 fallback OSM;OSM 回傳時 `place_id=""`,前端走 OSM embed。
- **不再呼叫 Place Details**(2026-08 移除)。

#### C. ~~新增 Place Details~~(已取消,2026-08)

> 原計畫用 `GET /v1/places/{place_id}` 補 rating/opening_hours。官方文件核實後取消:
> - `rating` / `userRatingCount` / `currentOpeningHours` 屬 **Enterprise** SKU($20/1000,免費僅 1,000/月),不是 Pro;
> - `displayName` / `googleMapsUri` 屬 **Pro**($17/1000),不是 Essentials;
> - 照片欄位(`photos`)屬 **Essentials IDs Only(免費)**,但照片位元組載入計 Photos SKU($7/1000,免費僅 1,000/月)。
>
> 照片改用 Text Search response 的 `places.photos`(Pro 等級,隨既有 mask 免費取得),`get_place_details` 與相關 constants 已整段移除。

#### D. **`places.measure_distance` 改用 Routes API**(新功能)

**現況**:只用 haversine 直線距離。

**優化**:
- Google 啟用時,改用 Routes API `computeRoutes`(Compute Routes Essentials,$5/1000,免費 10,000)取真實行車距離與時間。
- Typed output schema 擴充:`distance_m`、`duration_text`(例如 "約 15 分鐘")、`distance_basis: "straight_line" | "driving"`。
- 失敗 fallback haversine,`distance_basis` 標明來源。
- `tool_registry.py:_PlacesDistanceOutput` 加 `duration_text: str = ""` 與 `distance_basis` 改 `Literal["straight_line", "driving"]`。

> ⚠️ 這是 schema 變更,依 AGENTS.md 第 9 條要保留 JSON contract 相容 — 只加 optional fields,不改既有欄位形狀。

#### E. **Maps Embed API**(取代 OSM iframe,主要預覽)

**現況**:OSM embed iframe;Google 卡靠 Web Component,載入失敗就沒預覽。

**優化**:
- `runtime.py` 新增 `_google_embed_url(place_id_or_latlon)` 產 `https://www.google.com/maps/embed/v1/place?key=...&q=...`。
- 在 `_public_place_cards` 對 Google card 補 `embed_url`(目前是空字串)。
- 前端 `renderCustomPlaceCard` 優先序:
  1. 有 `embed_url`(不限 provider)→ 用 iframe
  2. provider=google 且 Web Component 載入成功 → 用 `<gmp-place-details-compact>`(使用者互動後升級)
  3. fallback category icon
- 用 **browser key**(embed API 可 referrer-restricted,且無限免費)。
- **不導入 Maps Static API**(決定):Embed 無限免費,Web Component 載入失敗直接退到 category icon,不再加 Static Maps fallback 路徑。

### 3.4 前端優化

#### 啟用 Google(最小變動,起點)

- 設定環境變數即可,程式碼已就緒:
  ```env
  AYUE_GOOGLE_PLACE_CARDS_ENABLED=on
  GOOGLE_PLACES_SERVER_API_KEY=<server key, restrict to Places API>
  GOOGLE_MAPS_BROWSER_API_KEY=<browser key, restrict by HTTP referrer>
  ```
- `/api/client-config` 會自動把 browser key 下發前端。
- `upgradeGooglePlaceCard` 會自動把 OSM iframe 卡升級成 `<gmp-place-details-compact>`。

#### Google Maps embed 取代 OSM iframe

- `frontend.html:safePlaceUrl` 的 Google 分支加 `maps/embed/v1/place` 白名單。
- `renderCustomPlaceCard` 邏輯改為:
  1. 有 `embed_url` → 用 iframe(不限 provider)。
  2. 沒 `embed_url` 但 provider=google 且 Web Component 載入成功 → 用 `<gmp-place-details-compact>`。
  3. fallback category icon。

#### 照片顯示(Text Search `places.photos`)

- Place card body 的照片區塊由後端 `_public_place_cards` 帶出的 `photo_url` 顯示(前端 `safeGooglePhotoUrl` 白名單驗證後以 `<img>` 呈現,前端不直接呼叫 Google API,維持 AGENTS.md 第 6 條隱私邊界)。
- **照片區塊預設不顯示**(`AYUE_GOOGLE_PLACE_PHOTOS_ENABLED=off`)。
- **評分與營業狀態不顯示**(Enterprise 等級,基於成本控制不抓取,2026-08)。

### 3.5 Cache 策略

| 資料 | TTL | 儲存 | 為什麼這個 TTL |
|---|---|---|---|
| Geocoding(OSM) | 7 天 | memory + Mongo(可選) | 地名座標極少變 |
| Nearby(OSM) | 15 分 | memory + Mongo(可選) | 短期不變 |
| Google Text Search(含照片 URL) | 15 分 | memory | 同 OSM nearby 邏輯 |
| Google Routes Compute Routes | 1 小時 | memory | 路況會變,但同一對話內重複問 A→B 很常見 |

- 不把 Google response raw 存進 Mongo,只存 typed projection(符合第 10.1 條)。
- Place Details 24h cache 已隨 API 移除(2026-08)。

### 3.6 錯誤碼與 fail-closed

沿用既有 `MapClientError(code)` / `GooglePlacesError(code)`:

| code | 意義 | 行為 |
|---|---|---|
| `maps_disabled` | 兩個 provider 都沒開 | 工具不可見 |
| `google_places_disabled` | Google 開關 off | fallback OSM |
| `google_places_timeout` | Google 逾時 | fallback OSM |
| `google_places_rate_limited` | 429 | fallback OSM |
| `google_places_access_denied` | 401/403 | fallback OSM + 記 log |
| `map_unavailable` | 都失敗 | 回錯誤給 Planner |

> 維持 AGENTS.md 第 1 條:失敗 fail closed,不回 legacy;Google 失敗就退 OSM 不是退 legacy。

### 3.7 Google API 計費與成本控制

> 資料來源:[Google 地圖平台核心服務價格表](https://developers.google.com/maps/billing-and-pricing/pricing?hl=zh-tw)(上次更新 2026-07-28)。價格為美元,每 1000 個事件。

#### 3.7.1 本專案會用到的 API 與 SKU 對應

**Places API(New)v1 — 主要成本來源**

| 功能 | 本專案使用情境 | SKU 名稱 | SKU ID | 免費額度/月 | 超額單價(每 1000) |
|---|---|---|---|---|---|
| `places.search_nearby` | 使用者問「附近有什麼餐廳」、Planner 呼叫 `search_nearby_places` | **Places API Text Search Pro** | `4FDA-34B1-A910` | 5,000 | $32 → $25.60 → $19.20 … |
| `places.resolve_place` | 使用者明確問「XX店在哪」、Planner 呼叫 `resolve_place` | **Places API Text Search Pro** | `4FDA-34B1-A910` | 5,000(共用) | 同上 |
| 照片 URL 來源 | Text Search response 的 `places.photos` 欄位(隨 mask 免費) | 不計費 | — | — | $0 |
| *(選配)* 照片位元組載入 | Card 顯示店照片(預設 OFF) | **Places API Place Details Photos** | `DCD1-FE97-8C71` | 1,000 | $7 → $5.60 → $4.20 … |
| *(選配)* Autocomplete | location 欄位輸入建議(日後才會用) | **Autocomplete Requests** | `4EF4-B17C-B31A` | 10,000 | $2.83 → $2.27 … |

> ⚠️ **重要計費細節:**
> - **Text Search Pro 與 Nearby Search Pro 是不同 SKU**。目前 `google_places_client.py` 用 `places:searchText` endpoint → 算 **Text Search Pro**($32/1000,免費只有 5,000)。
> - **Text Search 的欄位等級(2026-08 官方文件核實)**：
>   - `places.id` / `places.name` → **Essentials ID Only(免費)**；
>   - `places.displayName` / `formattedAddress` / `location` / `types` / `googleMapsUri` / **`photos`** → **Pro**；
>   - **`rating` / `userRatingCount` / `currentOpeningHours` → Enterprise($35/1000,免費僅 1,000)**。
> - **本專案基於成本控制,一律不要求 Enterprise 欄位** — place card 不顯示評分、評論數或營業狀態。照片 URL 從 Text Search response 的 `places.photos` 直接取得(Pro 等級,不跳級),不需要額外的 Place Details request。
> - **Place Details API 已整段移除**(2026-08):它的 `rating` 等欄位屬 Enterprise($20/1000),`displayName`/`googleMapsUri` 屬 Pro 而非 Essentials,與先前文件假設不符;其唯一價值(照片)已由 Text Search 的 `places.photos` 取代。

**Maps 介面集 — 前端顯示**

| 功能 | 本專案使用情境 | SKU 名稱 | SKU ID | 免費額度/月 | 超額單價(每 1000) |
|---|---|---|---|---|---|
| Maps JavaScript API | 前端載入 `maps.googleapis.com/maps/api/js` 載入 Web Components(`<gmp-place-details-compact>`) | **Dynamic Maps** | `FAF4-3B2D-51B2` | 10,000 | $7 → $5.60 → $4.20 … |
| Maps Embed API | place card 用 `<iframe>` 內嵌地圖預覽(取代 OSM embed) | **Embed** | `9C10-8313-F21F` | **無限免費** | $0 |

> 💡 **Embed 無限免費** 是這裡最關鍵的省錢點:place card 預覽優先用 Maps Embed API,不要沒事就載入 Maps JavaScript API(會算 Dynamic Maps $7/1000)。

**Routes / Distance — 距離計算**

| 功能 | 本專案使用情境 | SKU 名稱 | SKU ID | 免費額度/月 | 超額單價(每 1000) |
|---|---|---|---|---|---|
| `places.measure_distance` | 使用者問「A 到 B 多遠」、改用真實路程 | **Routes: Compute Routes Essentials** | `9EFF-679A-9B16` | 10,000 | $5 → $4 → $3 … |

> ⚠️ 舊版 Distance Matrix API(C1B6-FF9D-7700)是「元素」計費(origins × destinations)。我們只一對一,1 次呼叫 = 1 element。建議用新版 **Routes API Compute Routes Essentials**(同等級免費額度,功能更現代)。

**Geocoding(選用,本專案保留 OSM)**

| 功能 | 本專案使用情境 | SKU 名稱 | SKU ID | 免費額度/月 | 超額單價(每 1000) |
|---|---|---|---|---|---|
| *(選用)* Geocoding API | 若日後要把 `nominatim_search` 也改 Google | **Geocoding** | `BAC8-4E68-E261` | 10,000 | $5 → $4 → $3 … |

> 目前規劃**保留 OSM Nominatim 做 geocoding**(免費),只在 Places 與距離用 Google。若日後想統一才改。

#### 3.7.2 觸發情境完整對應(使用者行為 → API 呼叫)

| 使用者行為(範例) | 觸發的 SKU | 預估次數/月(小規模 demo) |
|---|---|---|
| 「高雄鹽埕區附近有什麼餐廳」 | Text Search Pro ×1(照片隨 response 免費取得)+ Maps Embed ×1(每張 card) | ~300 / ~900 |
| 「信義威秀在哪」 | Text Search Pro ×1 + Maps Embed ×1 | ~200 / ~200 |
| 「駁二到科工館多遠」 | Routes Compute Routes Essentials ×1 | ~100 |
| 前端載入店照片(需 `AYUE_GOOGLE_PLACE_PHOTOS_ENABLED=on`) | Place Details Photos ×載入張數 | ~1,000 以內(免費額度) |
| 前端打開 place card 且 Web Component 載入失敗 | category icon fallback(不計費) | ~50 |
| 前端打開 place card 且 Web Component 載入成功 | Dynamic Maps ×1 | ~500 |

**小規模 demo 預估月費**:幾乎全部落在免費額度內,約 **$0**。
**正式產品預估**(萬次等級):Text Search Pro 約 $32×5 = $160/月;Dynamic Maps 約 $7×5 = $35/月。**月費約 $195–300 美元區間**(較舊估算減少 Place Details Pro 的 $85/月),可透過 cache 與 field mask 控制。

#### 3.7.3 Cache 成本控制

| 資料 | TTL | 為什麼這個 TTL |
|---|---|---|
| Google Text Search | 15 分鐘 | 同 OSM nearby;地點資訊短期不變 |
| Google Routes Compute Routes | 1 小時 | 路況會變,但同一趟對話內重複問 A→B 距離很常見 |
| Maps Embed URL | 不需 cache(URL 本身可重複用) | — |

> Place Details API 已移除(2026-08),故不再有 Place Details 24h cache。

#### 3.7.4 Field Mask 嚴格控制(對應 `google_places_client.py:_FIELD_MASK`)

**Text Search 現況 field mask**:
```
places.id,places.displayName,places.formattedAddress,places.location,
places.types,places.googleMapsUri,places.photos
```
→ 對應 **Text Search Pro**($32/1000)。`types` 與 `googleMapsUri` 屬 Pro 欄位;`places.photos` 也屬 Pro(照片名稱隨 response 免費取得);`places.id` 屬 Essentials ID Only(免費)。

**Text Search 欄位等級(官方 2026-07-28 核實)**:

- **Enterprise($35/1000,免費僅 1,000/月)— 一律不要求**:
  ```
  places.rating, places.userRatingCount, places.currentOpeningHours,
  places.regularOpeningHours, places.priceLevel, places.websiteUri ...
  ```
- **Enterprise + Atmosphere($40/1000)— 一律不要求**:`places.servesVegetarianFood`、`places.servesBreakfast` 等。

> **決策(2026-08)**:本專案**不抓評分、評論數、營業狀態**等 Enterprise 欄位。place card 只顯示名稱、地址、距離、地圖連結與照片。若日後需要料理特性查詢,優先使用 cuisine 自由文字進 textQuery(不影響 SKU 等級),不要改用結構化 Atmosphere 欄位。

#### 3.7.5 前端載入策略(對應 `frontend.html:1300 loadGooglePlacesUi`)

- **不要在頁面載入時就載入 Maps JavaScript API**。現況是 `loadGooglePlacesUi` 在需要時才載入(lazy),這是對的,要保留。
- **place card 預覽優先序**:
  1. Maps Embed iframe(無限免費)→ **主要預覽**
  2. Web Component `<gmp-place-details-compact>`(Dynamic Maps $7/1000)→ 使用者互動後才升級
  3. category icon → 最後 fallback
- **照片**:`photo_url` 由後端從 Text Search response 的 `places.photos` 直接建構(server key),前端 `safeGooglePhotoUrl` 白名單驗證後以 `<img>` 顯示。**載入照片位元組是獨立的 `GetPhotoMediaRequest` API 呼叫,計 Place Details Photos SKU($7/1000,免費 1,000/月)**,由 `AYUE_GOOGLE_PLACE_PHOTOS_ENABLED` 控制(預設 OFF);`off` 時 `_photo_url()` 完全不產生 URL,不會觸發任何 media 請求(2026-08-04 修正,避免旗標 OFF 仍打 media endpoint 導致 429)。
- **429 `GetPhotoMediaRequest` 配額**:目前該 metric 每日配額為 0(`quota_limit_value: "0"`),即使 `on` 也無法載圖。**決議(2026-08-04):不顯示照片**,理由與替代方案評估見 `docs/place-cuisine-support.md`「決策紀錄:為什麼不用 ON」;開啟條件:先申請提高 `GetPhotoMediaRequestPerDayPerProject` 或開付費方案,再設旗標 `on`。
- **不導入 Maps Static API**:決定只用 Embed,Web Component 載入失敗就退到 category icon,不再加 Static Maps fallback 路徑。
- **不內嵌 Street View**:避免 Static Street View $7/1000 累積。

#### 3.7.6 預算保護開關(環境變數)

```env
# 預設 place card 走 Embed(免費)+ Web Component
AYUE_GOOGLE_PLACE_CARDS_ENABLED=on
# 開啟照片 → Place Details Photos $7/1000,免費僅 1,000/月(照片欄位本身隨
# Text Search Pro 免費取得,此開關只控制 media 位元組載入)
# 預設 OFF(依使用者決定)
AYUE_GOOGLE_PLACE_PHOTOS_ENABLED=off
# 開啟真實路程距離 → Routes Compute Routes Essentials $5/1000,免費 10,000/月
AYUE_GOOGLE_DISTANCE_MATRIX_ENABLED=on
```

> 註:`AYUE_GOOGLE_PLACE_DETAILS_FULL` 已於 2026-08 移除(Place Details API 整段移除,評分/營業狀態屬 Enterprise 等級,基於成本控制不抓取)。

→ 預設開核心 place card + 距離,照片**決議不顯示**(2026-08-04,見 `docs/place-cuisine-support.md`「決策紀錄:為什麼不用 ON」),日後申請到配額再開。

#### 3.7.7 不會用到的 API(明確排除)

| API | 為什麼不用 |
|---|---|
| Places Nearby Search Pro($32/1000) | 已用 Text Search Pro,功能重複 |
| **Place Details Pro/Enterprise**($17–25/1000) | **已移除(2026-08)**;其唯一價值(照片)由 Text Search 的 `places.photos` 取代,`rating` 等屬 Enterprise 等級太貴 |
| Text Search / Place Details Enterprise 欄位($35–40/1000) | 評分、評論數、營業狀態、Atmosphere 特性一律不要求,免費額度僅 1,000/月 |
| Address Validation Pro($17/1000) | 沒有地址驗證需求 |
| Roads API / Route Optimization | 沒有路線追蹤/車隊最佳化 |
| Navigation SDK | 沒有導航 |
| Elevation / Air Quality / Pollen / Solar / Weather | 與約會無關 |
| Aerial View / Immersive Maps / 3D Tiles | 沒有 3D 地圖需求 |
| Time Zone API | 已用 Python `zoneinfo` 處理時區 |
| Geolocation API | 不做定位追蹤(隱私規則) |
| Map Tiles API | 不自己拼地圖 |
| Places Insights / Aggregate | 沒有地區統計需求 |
| Maps Static API / Street View Static API | 決定只用 Maps Embed(無限免費) |
| Distance Matrix API(舊版) | 改用 Routes API Compute Routes Essentials |

#### 3.7.8 月費試算範例

依 Google 官方計費範例格式。

**情境**:一個月內有 8,000 次 place 搜尋 + 3,000 次 Routes + 6,000 次 Dynamic Maps + 無限 Embed + 照片 OFF。

| SKU | 用量 | 免費額度 | 計費量 | 單價 | 費用 |
|---|---|---|---|---|---|
| Text Search Pro | 8,000 | 5,000 | 3,000 | $32/1000 | $96.00 |
| Place Details Photos | 0(OFF) | 1,000 | 0 | $7/1000 | $0.00 |
| Routes Compute Routes Essentials | 3,000 | 10,000 | 0 | $5/1000 | $0.00 |
| Dynamic Maps | 6,000 | 10,000 | 0 | $7/1000 | $0.00 |
| Maps Embed | 無限 | 無限 | 0 | $0 | $0.00 |
| **合計** | | | | | **$96.00/月** |

→ 在免費額度內 + cache 加持,實際會更低。主要可控成本是 **Text Search Pro**,建議用 15 分鐘 cache 與 `limit ≤ 10` 控制。

---

## 4. 實作步驟(分階段,可獨立交付)

### Phase 1:啟用 Google 並修可見性(最小變動,先看到效果)

1. **`.env`** 設定三個 Google 變數 + `AYUE_GOOGLE_PLACE_CARDS_ENABLED=on`。
2. **`router.py:tool_policy_for_turn`**:可見性改為 `can_use_places = maps_enabled() or google_place_cards_enabled()`。
3. **`google_places_client.py`**:加 memory cache 給 `search_nearby_places` 與 `resolve_place`。
4. **測試**:新增「Google 開、OSM 關時工具仍可見」測試;既有測試不動。
5. **驗收**:啟動服務,問阿月「高雄鹽埕區附近有什麼餐廳」,確認出現 Google 卡片。

### Phase 2:照片直接從 Text Search 取得(取代 Place Details)

> **2026-08 更新**:原「Place Details Pro 補充」已取消。官方文件核實 `rating`/`userRatingCount`/`currentOpeningHours` 屬 **Enterprise** SKU($35–40/1000,免費僅 1,000/月),不是 Pro。基於成本控制,本專案**不抓取任何 Enterprise 欄位**,並整段移除 `get_place_details`。照片改由 Text Search response 的 `places.photos`(Pro 等級,免費)直接取得。
>
> **2026-08-04 再更新**:照片顯示功能**已決議不使用**(`AYUE_GOOGLE_PLACE_PHOTOS_ENABLED` 維持 off,`_photo_url()` 在 off 時完全不產生 URL)。原因與替代方案評估見 `docs/place-cuisine-support.md`「決策紀錄:為什麼不用 ON」。以下 Phase 2 步驟的「保留照片區塊」均已完成(程式碼就緒),但預設不顯示。

1. **`google_places_client.py`**:移除 `get_place_details` 與 Place Details constants;Text Search `_FIELD_MASK` 加 `places.photos`;`search_nearby_places` 與 `resolve_place` 用 `_photo_url()` 從 response 建構 media URL(旗標 off 時回空字串)。
2. **`tools.py`**:移除 `_places_resolve` 的 details 補欄位呼叫。
3. **`tool_registry.py:_PlaceOutput`**:移除 `rating`/`user_rating_count`/`opening_hours_summary`,保留 `photo_url`。
4. **`runtime.py:_public_place_cards`**:卡片上限 3→5;移除 Enterprise 欄位投影,保留 photo_url 嚴格安全檢查。
5. **`frontend.html:renderCustomPlaceCard`**:移除 rating / 營業狀態區塊,保留照片區塊(off 時無 photo_url,不渲染)。
6. **`config.py`**:移除 `AYUE_GOOGLE_PLACE_DETAILS_FULL`(同步更新 `.env.example`、`AYUE_V3_ARCHITECTURE.md`、live smoke 測試)。
7. **測試**:新增「photos 從 Text Search response 直接取」「移除 Enterprise 欄位」「旗標 off 抑制 photo_url」測試。

### Phase 3:Maps Embed 取代 OSM iframe

1. **`runtime.py`**:新增 `_google_embed_url(place_id_or_latlon)`,在 `_public_place_cards` 對 google card 補 `embed_url`。
2. **`frontend.html:safePlaceUrl`**:加 `google.com/maps/embed/v1/place` 白名單。
3. **`frontend.html:renderCustomPlaceCard`**:embed_url 優先 > Web Component > category icon fallback。
4. **測試**:新增「google card 帶 embed_url」「OSM card 仍走 OSM embed」。

### Phase 4:Routes API 真實路程

1. **`config.py`**:新增 `AYUE_GOOGLE_DISTANCE_MATRIX_ENABLED=on`(預設)。
2. **`google_places_client.py`**:新增 `measure_distance_matrix(origin, destination)` 呼叫 Routes API `computeRoutes`。
3. **`tools.py:_places_distance`**:Google 啟用時優先呼叫,失敗 fallback haversine。
4. **`tool_registry.py:_PlacesDistanceOutput`**:加 `duration_text: str = ""`,`distance_basis` 改 `Literal["straight_line", "driving"]`。
5. **`runtime.py` / `frontend.html`**:距離顯示加 `duration_text`。
6. **測試**:新增「Routes 成功」「429 fallback haversine 並標 straight_line」測試。

### Phase 5(可選,日後):Place Autocomplete

留日後。會動 frontend 的 location 輸入框 + 一個新 API endpoint `/api/places/autocomplete`。

---

## 5. 檔案修改清單(預覽)

| 檔案 | Phase | 變更類型 |
|---|---|---|
| `.env` / `.env.example` | 1,2,4 | 補範例值與新開關 |
| `config.py` | 1,2,4 | 新增 4 個環境變數 |
| `services/ayue_agent/router.py` | 1 | 可見性判斷改 `maps_enabled() or google_place_cards_enabled()` |
| `services/ayue_agent/google_places_client.py` | 1,2,4 | 加 cache、`_photo_url`(Text Search photos)、`measure_distance_matrix`;移除 `get_place_details` |
| `services/ayue_agent/tools.py` | 2,4 | resolve 帶 photo_url、distance 用 Routes API;移除 details 補欄位 |
| `services/ayue_agent/tool_registry.py` | 2,4 | `_PlaceOutput` 移除 rating 等 Enterprise 欄位、保留 `photo_url`;`_PlacesDistanceOutput` 加 optional 欄位 |
| `services/ayue_agent/runtime.py` | 2,3 | `_public_place_cards` 上限 3→5、photo_url 投影;`_google_embed_url` |
| `services/ayue_agent/context.py` | 2 | `place_search_draft` limit 3→5 |
| `services/ayue_agent/config.py` | 2 | 移除 `AYUE_GOOGLE_PLACE_DETAILS_FULL` |
| `frontend.html` | 2,3,4 | `renderCustomPlaceCard` 移除 rating/營業狀態、保留照片;`safePlaceUrl` 加白名單 |
| `tests/test_ayue_agent_maps_tools.py` | 1-4 | 補各階段測試;移除 place_details 測試 |
| `tests/test_ayue_agent_place_cards.py` | 2,3 | 卡片 3→5、移除 rating 斷言、保留 photo 安全測試 |
| `tests/test_google_places_client.py` | 2 | photos 從 Text Search response 直接取 |
| `tests/test_google_maps_live_smoke.py` | 2 | 移除 `get_place_details` 測試,改測 photos;移除 FULL 旗標 |
| `AYUE_V3_ARCHITECTURE.md` | 1-4 | place tools 段落補 Google 為主、OSM 為輔說明與新欄位 |

> 不會動:`maps_client.py`(OSM 維持現狀)、Private V2 的 `private_calendar.py`、Neo4j、模型供應商設定。

---

## 6. 風險與緩解

| 風險 | 緩解 |
|---|---|
| Google quota 用完 | OSM fallback + memory cache + 限制 `limit ≤ 10` |
| Browser key 外洩 | referrer-restricted + 只下發 `google_maps_browser_api_key`,server key 不進前端 |
| 誤抓 Enterprise 欄位(評分/營業狀態)導致費用暴漲 | Text Search mask 不含 `rating`/`userRatingCount`/`currentOpeningHours`;Place Details API 已整段移除;測試斷言 card 永不帶這些欄位 |
| Photo API 用 browser key 會破壞 referrer 限制 | Photo URL 由後端產,用 server key,前端只拿到完整 URL(本計畫預設 OFF) |
| 旗標 OFF 仍打 media endpoint 導致 429 | `_photo_url()` 在 `AYUE_GOOGLE_PLACE_PHOTOS_ENABLED=off` 時完全不產生 URL;測試斷言 OFF 時 `photo_url=""` |
| 專案 `GetPhotoMediaRequest` 每日配額為 0 | 載入照片位元組是獨立 API 請求(計 Photos SKU);**決議不顯示照片(2026-08-04)**,維持 OFF;若要開啟需先在 Google Cloud Console 申請提高配額或開付費方案 |
| Schema 變更破壞 JSON contract | 只移除 optional 欄位(`rating` 等),既有必備欄位形狀不變(AGENTS.md 第 9 條) |
| Google 回傳 raw payload 進 trace | 沿用 `_persist_trace` allowlist,只存 tool name + ok + code,不存 arguments/result |
| 兩個 provider 都啟用會 double call | executor 邏輯是「Google 優先,成功就 return,不會再打 OSM」 |
| Maps Embed 被惡意網站盜用 | browser key 加 HTTP referrer 限制 |

---

## 7. 驗收標準

- `python -m compileall social_demotest/services/ayue_agent` 通過。
- `python -m pytest social_demotest/tests/test_ayue_agent_maps_tools.py` 全綠。
- `python -m pytest social_demotest/tests/test_ayue_agent_place_cards.py` 全綠。
- 啟動服務後 `GET /` 與 port 9001 health endpoint 200。
- `AYUE_GOOGLE_PLACE_CARDS_ENABLED=on` 時,問阿月「駁二附近餐廳」會出現 Google place card,最多五張,無 rating/營業狀態(Enterprise 欄位不抓取),無照片(2026-08-04 決議,見 `docs/place-cuisine-support.md`),預覽為 Maps Embed iframe。
- `AYUE_GOOGLE_PLACE_CARDS_ENABLED=off` 時,行為退回現狀(OSM iframe card)。
- `AYUE_MAPS_ENABLED=off` 但 Google on 時,工具仍可見(修了可見性 bug)。
- Text Search field mask 不含 `rating`/`userRatingCount`/`currentOpeningHours`(可用 live smoke 或 code review 驗證)。
- `AYUE_GOOGLE_PLACE_PHOTOS_ENABLED=off` 時不顯示照片區塊(2026-08-04 決議維持 OFF,不顯示照片;`on` 時 card 顯示照片,但需先申請 `GetPhotoMediaRequest` 配額)。
- Google Cloud Console 觀察實際用量落在免費額度內(見 §3.7.8)。

---

## 8. 同步要更新的文件

依 AGENTS.md 第 10 條與「修改 runtime contract、tool list、state machine、環境旗標時必須同步更新 AYUE_V3_ARCHITECTURE.md」:

- `AYUE_V3_ARCHITECTURE.md`:在 place tools 段落補 Google 為主、OSM 為輔的說明與新欄位、新環境開關。
- `.env.example`:補範例值與註解(含計費等級提示)。
- `MEMORY_CONTEXT_ENGINE_GUIDE.md`:不需動(map 與 memory engine 無關)。

---

## 9. 決策紀錄

| 決策點 | 選擇 | 理由 |
|---|---|---|
| OSM 處理 | 保留為 fallback | Google 失敗/quota 用完時 product 不會斷線;風險最低 |
| Google API 類型 | Places API v1 Text Search + Routes API + Maps JavaScript + Maps Embed(移除 Place Details) | 搜尋/距離/預覽;照片隨 Text Search 取得 |
| 前端範圍 | 啟用 Google + Maps Embed 取代 OSM iframe + 照片顯示(開關控制) | 最小變動看到效果 |
| 評分/評論數/營業狀態 | **不抓取(Enterprise 等級)** | `rating`/`userRatingCount`/`currentOpeningHours` 屬 Enterprise($35–40/1000,免費僅 1,000/月);Place Details API 整段移除 |
| 照片來源 | **Text Search response 的 `places.photos`** | Pro 等級欄位隨既有 mask 免費取得;不需額外 Place Details request |
| Place Details Photos | **預設 OFF,並決議不顯示照片(2026-08-04)** | ① 專案 `GetPhotoMediaRequest` 每日配額 = 0(429),開 ON 必爆;② 申請提高配額或開付費方案是帳戶層級操作,不保證通過;③ 免費額度僅 1,000 次/月(5 張卡/次查詢 = 200 次查詢/月);④ 評估過 Wikimedia Commons(餐廳覆蓋率低)、Yelp(台灣無資料)、Foursquare(需商業授權)皆不適用;⑤ 卡片左側已有 Maps Embed 地圖預覽(無限免費)作為主要視覺,照片為非必要點綴。詳見 `docs/place-cuisine-support.md`「決策紀錄:為什麼不用 ON」。若要開啟:先申請配額,再設 `AYUE_GOOGLE_PLACE_PHOTOS_ENABLED=on`(程式碼已就緒) |
| Maps Static API | **不導入** | Maps Embed 無限免費,Web Component 失敗退到 category icon 即可 |
| Street View | **不內嵌** | 避免 Static Street View $7/1000 累積 |
| Distance API | Routes API Compute Routes Essentials(新版) | 同等級免費額度,功能更現代 |
| Geocoding | 保留 OSM Nominatim | 免費,沒必要為了統一而增加 Google 成本 |
| 地點卡片上限 | 3 → **5** | 使用者需求;Text Search 按 request 計費不按筆數,無額外成本 |

---

## 10. 參考資料

- [Google 地圖平台核心服務價格表](https://developers.google.com/maps/billing-and-pricing/pricing?hl=zh-tw)(2026-07-28 更新)
- [Places API (New) 說明文件](https://developers.google.com/maps/documentation/places/web-service?hl=zh-tw)
- [Routes API 說明文件](https://developers.google.com/maps/documentation/routes?hl=zh-tw)
- [Maps Embed API 說明文件](https://developers.google.com/maps/documentation/embed?hl=zh-tw)
- [Maps JavaScript API Web Components](https://developers.google.com/maps/documentation/web-components?hl=zh-tw)
- [API 安全性最佳做法](https://developers.google.com/maps/api-security-best-practices?hl=zh-tw)
- 專案架構:[`AGENTS.md`](../AGENTS.md) §10.1 外部資訊工具
- 專案 V2 架構:[`AYUE_V3_ARCHITECTURE.md`](../AYUE_V3_ARCHITECTURE.md)
