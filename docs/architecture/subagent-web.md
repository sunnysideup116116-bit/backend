# Web Sub-agent

> 現行 owner：`services/ayue_agent/v3/sub_agents/web_agent.py` 保存 Web decision contract；`v3/web_runtime.py` 保存 bounded research loop；`v3/guarded_execution.py` 提供最小 Guard→URL binding→executor arguments→`execute_tool` adapter。`web_tools.py` 只負責 Tavily adapter，不負責研究策略。

## 1. 責任邊界

Web sub-agent 只處理需要外部或近期公開資訊的問題，例如活動、公告、新聞、文章、公開社群內容與使用者提供的 URL。它可使用：

- `web.search`：取得 bounded title／URL／snippet／published date。
- `web.extract`：只擷取本回合搜尋結果或使用者提供的安全公開 URL。

附近地點、距離與地圖卡屬於 Places sub-agent。Web 不直接呼叫地點工具，也不能產生 place ID、map URL 或卡片。

## 2. Planner 契約

Web task 的 `task_brief` 必須保留使用者真正要找的 proposition、地區／時間與證據需求；不能只寫成泛用的「查相關資訊」。`evidence_policy` 只有兩種：

| policy | 適用情況 | 完成標準 |
| --- | --- | --- |
| `casual_discovery` | 一般活動、餐廳、旅遊、優惠、賽事、商店探索 | 相關且可追溯的公開結果即可；不強制每次 extract |
| `strict_verification` | 明確要求官方／確認，或醫療、法律、金融、安全風險問題 | 必須有直接證據；不足時明確回 partial／insufficient |

省略 policy 時 typed contract 預設為 `casual_discovery`。這是 Planner 的語意判斷，不是 keyword router。

## 3. Bounded observation loop

Scheduler 只 dispatch `agent="web"` 的 registered runtime，並收集一個
`TaskRunnerResult` 的 completed projection。round、observation、search/extract counters、finish
decision 與 `web_research.v1` assembly 全部由 `web_runtime.py` 管理；因此
Scheduler 不需要知道 Web 是 loop-based 還是 single-shot。

```text
Web Agent decision
→ Web Runtime 驗證 action／budget
→ guarded execution adapter 驗證 Guard／URL／executor arguments
→ web.search 或 web.extract
→ typed observation 回到 Web Agent
→ finish 產生 web_research.v1
```

### Research quality policy

Tavily search uses `search_depth="advanced"`; `include_answer` and
`include_raw_content` remain disabled. Extracted content is bounded to 8,000
characters per page and remains under the existing research context cap.

Search is source discovery. Comparisons, recommendations, reviews, nuanced
facts, and context-sensitive requests should prefer one or two relevant,
authoritative extracts. Simple explicit lookups may finish from direct search
evidence; extraction is not mechanically required for every query.

The research/tool phase allows at most three tool-producing calls. A separate
bounded finish-only phase gets one final decision whenever observations exist;
decision/model failures do not consume Web tool budget or remove that phase.
Existing search/extract budgets and initial parallel search remain unchanged;
finish-only exposes no search/extract capability.

Action legality only follows runtime-owned state：

- `phase="research"`：Web Agent 必須從 `available_actions` 恰選一個 action。
- `phase="finish"`：只能呼叫 `web_finish_decision`。
- `round_index` 只供診斷與上下文使用，不是 workflow authority；即使 finish 的 index 大於 3，仍依 `phase` 正常解析。
- `assessment` 只屬於 finish decision；search/extract decision 不先填 assessment。

硬上限由 `web_research.py` 定義：最多 3 次 tool-producing call、3 次總工具呼叫、3 次 search（首輪最多 2 個 query、refine 最多 1 個 query）、1 次 extract（最多 2 個 URL），以及獨立 1 次 finish-only decision。模型輸出格式錯誤時只重試一次 typed decision，不會增加工具額度；研究階段失敗後仍保留 finish-only 機會。

每次 search query 都由 server 綁回 Planner 的 `answer_target`。若 task 正在驗證 Places 候選，還會綁定 server-owned `place_candidate_*` reference；模型不能靠名稱猜測候選身分。

`project_web_observations()` 對 search rows、per-page extract 與總 research context 分別設限。達到 `MAX_WEB_PROMPT_SEARCH_RESULTS` 只停止加入更多 search rows，不會停止掃描後續 observations；因此 `search -> refined search -> extract -> finish` 的 late extract 仍會進 finalizer。不得以提高 search cap 取代此行為。

## 4. 輸出契約

Web task 最終只輸出 `web_research.v1`：

- `status`：`answered | partial | insufficient_evidence`
- `execution_status`：`completed | degraded | unavailable`
- `coverage`：`direct_sufficient | direct_partial | adjacent_only | none`
- `findings[]`：每項 claim 必須綁本回合觀察到的 source URL
- `primary_activity`：活動標題、日期、時間、場地、行政區與來源
- `sources[]`、`limitations[]`、`stop_reason`

搜尋結果會先轉成 `web_source_01` 形式的本回合 reference；finish 時由 server 還原成已觀察 URL。未觀察、危險或不符合 subject binding 的 URL 會被丟棄。相鄰背景資料不能被升級成直接回答。

## 5. 與 Places／行程的 DAG

- 單純查近期資訊：`web → synthesizer`
- 地點候選需要外部條件驗證：`places → web → synthesizer`
- 找一個新活動並排一日行程：`web → places → web → synthesizer`，且 `Plan.presentation_mode="itinerary"`

最後一種流程中，第一個 Web task 提供 typed `primary_activity`，Places 以活動場地為 anchor 找餐廳／咖啡／景點，第二個 Web task只驗證 bounded 候選。沒有指定日期時仍可輸出「未指定日期的建議版」，不得假裝知道即時營業資訊。

## 6. 失敗與隱私

- 沒有 `TAVILY_API_KEY` 時 Web 工具不可見，結果為 unavailable，不得假裝已搜尋。
- timeout、rate limit、provider failure 與 evidence 不足使用不同 typed code。
- 已有安全 observation 但後續模型失敗時保留來源與已驗證 finding，不改寫成空白成功。
- Raw HTML、第三方 instruction、完整 provider payload、query arguments 與 tool result 不進 trace 或 public stream。
- Web 只使用本人手動保存的粗略地點或當回合明確地點，不推測即時位置或地址。

## 7. 測試

`test_v3_web_research.py` 覆蓋 phase/available-actions、finish-only、late extract projection、完整 search→refine→extract→finish trajectory、query anchoring、evidence grading、來源綁定、budget、partial／unavailable fallback，以及 Web／Places／itinerary trajectory。`test_ayue_agent_web_tools.py` 覆蓋 Tavily adapter、URL 安全與 bounded projection。
