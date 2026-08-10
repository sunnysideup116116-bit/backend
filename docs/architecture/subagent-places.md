# Sub-agent：places（地點子代理）

> 現行 owner：`services/ayue_agent/v3/sub_agents/places_agent.py`。Places 只負責結構化地點候選、距離與地圖卡；即時／公開條件由獨立 Web task 處理。

## 1. 責任邊界

Places 可以：

- 依明確地點或本人保存的城市／行政區搜尋附近餐廳、咖啡廳、小酌、景點與公園。
- 依 cuisine（例如牛排、火鍋、珍奶）縮小結構化候選。
- 測量兩個公開地點的距離。
- 將使用者明確提到或本回合已驗證的地點解析成 place card。

Places 不可以：

- 推測精確住址、GPS 或即時位置。
- 查對方位置或行事曆。
- 聲稱候選符合營業時間、活動日期、菜單、優惠、評價或其他 current/public criterion。
- 呼叫 `web.search`／`web.extract`。Web 是獨立 registered runtime。

## 2. 可呼叫的工具（3 個）

| 工具 | 用途 |
| --- | --- |
| `places.search_nearby` | 依 anchor/category/cuisine/radius 取得 bounded candidate pool |
| `places.measure_distance` | 測量明確兩地，或保存地區到目的地的距離 |
| `places.resolve_place` | 將明確店名／景點解析成安全 place card |

### 2.1 `places.search_nearby`

Planner arguments：

| 欄位 | 規則 |
| --- | --- |
| `anchor` | 明確地點，最多 160 字；沒有明確地點才可搭配 saved location |
| `categories` | `restaurant|cafe|bar|attraction|park`，1–3 類 |
| `cuisine` | 具體料理／飲品提示，最多 30 字 |
| `radius_m` | 300–5000；是 server-side hard bound |
| `limit` | 1–10；agent 一般最多要求 8 |
| `use_saved_location` | 只使用本人手動保存的粗粒度城市／行政區 |
| `ordering` | `distance|balanced`；跨類別 itinerary 才使用 balanced |

回傳 `_PlacesNearbyOutput`：anchor、origin kind、distance basis、attribution 與 bounded `places[]`。Provider ID、map URL、photo URL 保留在 server-side card projection，不直接進 Synthesizer prompt。

### 2.2 `places.measure_distance`

Arguments：`origin`、`destination`、`use_saved_origin`。Google Routes 可用時可回 driving distance/time；否則回 straight-line distance，必須依 `distance_basis` 如實描述。

此工具標記 `reuse_success_within_turn=True`；相同端點的成功 observation 在同一 turn 內重用，不重跑 provider。

### 2.3 `places.resolve_place`

Arguments：`query`。Query 必須是使用者明確提到或已驗證 observation 中的名稱；找不到時回 `found=false`，不能發明地點或 map URL。

## 3. Provider 與 radius enforcement

```text
AYUE_GOOGLE_PLACE_CARDS_ENABLED=on 且 keys 可用
  -> Google Places / Routes
  -> 失敗時 fallback OSM
否則
  -> Nominatim + Overpass / OSM
```

- Google location bias 不是距離保證；server 仍會移除超出 `radius_m` 的候選。
- OSM/Google raw payload 都是不可信輸入，只能經 typed projection、數量與字元上限後進 observation。
- Google presentation 增強失敗不得移除既有 OSM 能力。

## 4. Places 與 Web collaboration interface

需要 current/public criterion 時，Planner 建立獨立 DAG：

```text
places -> web -> synthesizer
```

1. Places 只建立符合地點／類別／radius 的候選池，不聲稱已符合 current criterion。
2. Scheduler 把最多五個 privacy-safe candidate summaries 投影成 `place_candidate_*` refs。
3. Web 只能查證這些 refs；findings 必須保留 subject binding，不能靠名稱相似度換成別家店。
4. Synthesizer 只對 direct-supported candidate 作 verified recommendation；證據不足時明確標示 unconfirmed candidates。

「找一個新活動並排整天」使用：

```text
web(activity) -> places(activity venue) -> web(candidate verification) -> synthesizer
```

Places 從 upstream typed `primary_activity.venue` 取 anchor，不從自由文字 claim 猜地點，也不改寫活動日期／地區。

## 5. Observation 與 place-card interface

- Retrieval 與 display 分開：可取最多八個候選；一般 grounded recommendation 顯示 2–3 張卡，除非使用者明確要求更多，否則最多 4 張。
- `_strip_place_internals` 在 observation 進 Synthesizer 前移除 map/provider/photo internals。
- `decide_place_cards` 只選 server 已有 candidate indices；模型不能製造卡片。
- `presentation_blocks` 將 Markdown fragment 綁定至最多一張 place card；卡片擁有 map link，模型文字不提供可點擊 URL。
- Normal Places/Places+Web grounded success uses Synthesizer `compose_public_reply`; `requested_limit` describes the comparison pool, while `selected_candidate_refs` controls visible cards. Explicit final counts use `card_intent=explicit_set`; non-selected candidates remain internal unless the user asks to see all.
- Deterministic Places/Web formatters are reserved for provider, compose, grounding, or presentation validation degradation and must not replace normal LLM composition.
- Ordinary recommendation composition does not accept model-authored `blocks` or require `card_mode`. The model returns the natural-language `messages`, `card_intent`, and server-owned candidate refs; the Synthesizer derives `card_mode` and emits card-only presentation projections for the validated selected refs. Legacy ordinary `blocks` may be ignored for compatibility, while itinerary remains the block-based exception.
- Web-only `web_research.v1` results are LLM-first even when their typed status is partial, insufficient, degraded, or unavailable. The natural reply must preserve the typed limitation; deterministic Web formatting is only a post-composition degradation fallback.
- Places 的常見 user-facing failure 可帶 bounded `failure` observation：`location_not_found`、`location_required`、map timeout/unavailable 等 code 使用 server-owned 固定 message；可公開的 `subject` 只取自已驗證的 executor argument。未知 failure 維持 error code，不傳 raw exception、provider detail 或 internal ID。

## 6. 測試重點

- Saved location 只能是本人粗粒度地區；明確 anchor 優先。
- `radius_m` 對 Google 與 OSM 都是 hard bound。
- Places Agent 看不到 `WEB_TOOLS`。
- Places→Web subject refs 不可被模型改綁或新增。
- Provider failure、超界 candidate、無法 resolve 與缺乏 Web evidence 都有 deterministic bounded fallback。

## 7. Optional Google enrichment

Places uses an empty `enrichments` list by default. The model may request only
`rating`, `hours`, or `walking` when the Places task needs that evidence; the
executor deduplicates the list and the schema rejects unsupported values.
`rating` fetches Google `rating` plus `userRatingCount`. `hours` fetches
`currentOpeningHours` and exposes only `open_now`, next open/close timestamps,
and bounded weekday descriptions. Ordinary recommendations must not request
Enterprise-tier fields merely to make cards richer.

Google current-open/current-opening-hours facts may be Places-owned. Temporary
closures, special announcements, events, social-media updates, menus,
promotions, and other public claims requiring external verification remain
Web-owned. `walking` uses one bounded Routes matrix for the candidate pool;
individual matrix-element failures remove walking fields only from their own
candidate.
