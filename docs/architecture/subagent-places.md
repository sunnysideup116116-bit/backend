# Sub-agent：places（地點子代理）

> 本文說明使用者與阿月談「附近餐廳／景點／距離」時，背後怎麼運作：places sub-agent 能做什麼、呼叫哪些 function、provider（OSM/Google）如何選擇、地點卡如何顯示。

## 1. 角色與能做什麼

**系統角色**（`v3/sub_agents/places_agent.py` 的 `_SYSTEM`）：

> 你是公開阿月的地點子代理：負責搜尋附近地點、餐廳與測量距離，可輔以網路搜尋。

能力：

- **搜尋附近地點**：餐廳、咖啡廳（含手搖飲／甜點）、小酌、景點、公園，可帶料理類型（cuisine）篩選。
- **測量距離**：兩地直線距離（或 Google Routes 車程距離）。
- **解析地點卡**：把使用者明確提到的店名解析成公開地點卡（不能捏造店名）。
- **輔以網路搜尋**：`web.search`／`web.extract`（活動、店家、新聞等最新公開資訊）。

**不負責**：不推測精確住址或即時位置（只用本人手動保存的城市／行政區或當回合明確地點）；不讀對方行事曆。

## 2. 可呼叫的工具（5 個）

| 工具 | risk | 用途 |
| --- | --- | --- |
| `places.search_nearby` | READ | 搜尋附近地點（categories + cuisine 篩選） |
| `places.measure_distance` | READ | 測量兩地距離（同 sub-task 內可重用結果） |
| `places.resolve_place` | READ | 把明確店名解析成地點卡 |
| `web.search` | READ | 最新公開資訊搜尋（Tavily） |
| `web.extract` | READ | 讀取受信任網址內容細節（URL 必須是本回合搜尋結果或使用者提供） |

### 2.1 `places.search_nearby`（READ）

Planner 參數（`_PlacesNearbyArguments`）：

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `anchor` | str（≤160） | 明確地點（如「三民區」）；或配合 `use_saved_location` |
| `categories` | list[enum]，1–3 個 | `restaurant` / `cafe` / `bar` / `attraction` / `park` |
| `cuisine` | str（≤30） | 料理類型提示（炸雞、牛排、火鍋、珍奶…），與 categories 搭配 |
| `radius_m` | int（300–5000, 預設 1500） | 搜尋半徑 |
| `limit` | int（1–10, 預設 8） | 結果數量 |
| `use_saved_location` | bool | 使用本人手動保存的城市／行政區 |

**agent 規則**（`_SYSTEM`）：珍奶／手搖飲 → `categories=["cafe"]` + `cuisine=「珍奶」`；炸雞 → `["restaurant"]` + `cuisine=「炸雞」`。模型常輸出自由文字類別（drink、bubble_tea…），`base.py:_repair_categories` 會 deterministic 修正成 allowlist 後才驗證。

回傳（`_PlacesNearbyOutput`）：`anchor_label`、`origin_kind`（explicit/saved_profile）、`distance_basis`（straight_line）、attribution、`places[]`（name/category/distance_m/address_summary/map_url/provider/place_id/photo_url）。

範例（「三民區附近有沒有好吃的牛排？」）：

```json
{
  "tool_name": "places.search_nearby",
  "arguments": {"anchor": "三民區", "categories": ["restaurant"], "cuisine": "牛排", "limit": 5}
}
```

### 2.2 `places.measure_distance`（READ）

Planner 參數（`_PlacesDistanceArguments`）：

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `origin` | str（≤160） | 起點；或 `use_saved_origin=true` 用保存地區 |
| `destination` | str（2–160, 必填） | 終點 |
| `use_saved_origin` | bool | 以保存地區為起點 |

回傳（`_PlacesDistanceOutput`）：`origin_label`、`destination_label`、`origin_kind`、`distance_m`、`distance_basis`（straight_line/driving）、`duration_text`、attribution。標註 `reuse_success_within_turn=True`：同 sub-task 內相同請求直接重用成功 observation。

### 2.3 `places.resolve_place`（READ）

Planner 參數（`_PlacesResolveArguments`）：`query`（2–160，使用者明確提到的店名）。

回傳（`_PlacesResolveOutput`）：`found`、`place`、`attribution`、`attribution_url`。找不到或捏造店名 → `found: false`。

### 2.4 `web.search` / `web.extract`（READ）

- `web.search`：`query`(2–300)、`recency`(none/day/week/month/year)、`use_saved_location` → `results[]`（title/url/snippet/published_date）。
- `web.extract`：`urls`(1–2)、`query` → `pages[]`（url/content/truncated）。**URL 安全**：只能提取「本回合 web.search 結果或使用者提供」的 URL（`scheduler._web_extract_urls_allowed` 驗證），且經 `web_tools.is_safe_public_url` 檢查（擋 localhost／private IP／帶帳密的 URL）。
- 沒有 `TAVILY_API_KEY` 時（`web_tools.web_enabled()` 為 False）執行回 `web_not_configured` 失敗碼，observation 不進 Synthesizer 的成功集——不能假裝已查詢。

## 3. Provider 選擇（OSM vs Google）

`tools.py:_places_nearby` / `_places_distance` / `_places_resolve` 的邏輯：

```text
if google_place_cards_enabled():          # AYUE_GOOGLE_PLACE_CARDS_ENABLED=on + keys
    try Google Places / Routes → 成功即用
    except → fallback 到 OSM
else:
    OpenStreetMap（Nominatim 定位 + Overpass 查詢, TTL cache）
```

- 預設使用 OpenStreetMap／Overpass，不需 Google key。
- Google 是選用的 presentation 增強；Google 失敗**不能**拿走既有 OSM 能力。
- 外部回應是**不可信輸入**：只經 typed projection、數量上限與字元上限後進 observation；timeout／失敗時自然說明查不到並可追問，不能轉成寫入操作。

## 4. 呼叫流程（背後怎麼運作）

```text
使用者: 「這星期天想吃附近的牛排，再找牛排店附近有沒有甜點店」
  │
  ▼ Planner（依「牛排」與「甜點」兩段需求）
  ├─ t1 places: 搜尋牛排 (depends_on: [])
  ├─ t2 places: 搜尋牛排店附近的甜點 (depends_on: [t1])   ← 依賴前一任務結果
  └─ t3 synthesizer (depends_on: [t1, t2])
  │
  ▼ slice_for_agent("places"): message + recent_messages + user_location + clock + prior_observations
  ▼ places_agent.run → LLM function calling
     t1: 一次回覆輸出兩個 calls（模型可並列多個提案）：
        {"tool_name":"places.search_nearby","arguments":{"anchor":"三民區","categories":["restaurant"],"cuisine":"牛排"}}
        {"tool_name":"places.search_nearby","arguments":{"anchor":"三民區","categories":["cafe"],"cuisine":"甜點"}}
  ▼ Guard：registered / schema（_repair_categories 後）/ duplicate / 步數上限(3)
  ▼ execute_tool → _places_nearby → google_place_cards_enabled()? Google : OSM
      → output_model 驗證（_PlacesNearbyOutput, extra=forbid）
  ▼ t2 以 t1 的 prior_observations 再查（reuse 判斷：不同 arguments 不重用）
  ▼ Synthesizer：
      - observation 經 _strip_place_internals（移除 map_url/place_id/photo_url 才進 prompt）
      - 候選卡片存在時，模型以 decide_place_cards 選 show_all/select/none
      - 回覆 + place_cards（server-side projection 含 map_url 等, 供 UI 渲染卡片）
```

## 5. 地點卡（place cards）

- 候選卡片由 `scheduler._public_place_cards` 從成功的 places observations 組出（name/category/distance_label/map_url/attribution…）。
- Synthesizer 只看到摘要（name/category/distance），卡片內部欄位（address_summary/map_url/provider/place_id/photo_url）留在 server-side projection 回傳給 UI。
- `decide_place_cards` 是 typed tool call：`{mode: show_all|select|none, indices: []}`，不確定時 `show_all`。

## 6. 端到端範例

**使用者**：「週五晚上想跟小安約在左營吃火鍋，順便問兩家店哪個比較近。」

1. Planner：t1 places（火鍋推薦）、t2 places（距離比較, depends_on t1）、t3 synthesizer。
2. t1：`places.search_nearby {anchor: "左營", categories: ["restaurant"], cuisine: "火鍋"}` → OSM/Google 回 2 家店（含距離）。
3. t2：`places.measure_distance {origin: "左營", destination: "第一家店"}` → 距離結果；第二筆若同 request 則重用或另一 call。
4. Synthesizer：推薦＋距離比較，`decide_place_cards: show_all` → 回覆附 2 張地點卡。
5. UI 顯示卡片（含地圖連結）；progress bubble 在 `tool_started`／`tool_finished` 期間顯示「我找一下附近的地點…」。
