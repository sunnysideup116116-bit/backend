"""Scheduled public-event discovery through the bounded Tavily adapter."""

from __future__ import annotations

import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
import time
import hashlib
import json
import unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlsplit

import requests

from database import db
from services.ayue_agent.web_tools import extract_web, is_safe_public_url, search_web
from services.event_opportunity_service import request_event_opportunity_scan
from services.event_relevance_service import project_event_relevance
from services.skill_loader import load_skill


AGENT_EVENT_INGEST_URL = "http://127.0.0.1:9001/api/events/ingest"
AGENT_ACTIVE_EVENTS_URL = "http://127.0.0.1:9001/api/events/active"
AGENT_EVENT_RECONCILE_URL = "http://127.0.0.1:9001/api/events/reconcile"
DEFAULT_REGION = "高雄"
DEFAULT_WINDOW_DAYS = 30
SUPPORTED_CATEGORIES = ("市集", "音樂", "運動", "節慶", "美食")
MAX_DISCOVERY_RESULTS = 50
MAX_RESULTS_PER_CATEGORY = 10
MAX_RESULTS_PER_QUERY = 5
MAX_INGEST_SOURCES_PER_BATCH = 3
MAX_SOURCE_CLUSTER_SIZE = 3
MAX_EXTRACT_RESULTS = 30
MAX_EXTRACT_RESULTS_PER_CATEGORY = 10
MAX_SUPPLEMENTAL_RESULTS_PER_CATEGORY = 8
MAX_SUPPLEMENTAL_QUERIES_PER_CATEGORY = 3
DEFAULT_MIN_EVENTS_PER_CATEGORY = 4
DEFAULT_TARGET_EVENTS_PER_CATEGORY = 6
MAX_ACTIVE_EVENTS_PER_CATEGORY = 6
CATEGORY_INGEST_BATCH_LIMITS = {"音樂": 3, "市集": 3, "美食": 3, "運動": 3, "節慶": 3}
DEFAULT_MAX_SUPPLEMENTAL_CATEGORIES = 6
DEFAULT_SUPPLEMENTAL_INGEST_TIMEOUT_SECONDS = 180
KAOHSIUNG_MARKET_SOURCE = "https://www.twmarket.tw/category/%E5%B8%82%E9%9B%86%E7%B8%A3%E5%B8%82%E5%88%86%E5%8D%80/kaohsiung-market/"
TAIPEI = timezone(timedelta(hours=8), name="Asia/Taipei")
# pass through the same typed LLM extraction and deterministic validation as search.
REGION_CURATED_SOURCES: dict[str, dict[str, tuple[dict[str, str], ...]]] = {
    "高雄": {
        "展覽": (
            {
                "title": "高雄市立美術館展覽資訊",
                "snippet": "官方展覽列表；抽取日期範圍內仍在展出的高雄展覽。",
                "source_url": "https://ud.kmfa.gov.tw/Exhibition/C000003",
            },
            {
                "title": "高雄會展網活動行事曆",
                "snippet": "高雄會展活動入口；只抽取日期與場地明確的公開展覽。",
                "source_url": "https://www.khmice.org.tw/",
            },
        ),
        "市集": (
            {
                "title": "高雄市集分類",
                "snippet": "指定市集來源；只抽取未來日期且地點在高雄的實際市集。",
                "source_url": KAOHSIUNG_MARKET_SOURCE,
            },
        ),
        "音樂": (
            {
                "title": "高雄近期音樂演出",
                "snippet": "高雄演出列表；只抽取日期、演出名稱與場地明確的音樂活動。",
                "source_url": "https://www.artists.tw/gigs/city/kaohsiung",
            },
            {
                "title": "高雄旅遊網年度活動行事曆",
                "snippet": "官方年度活動列表；只抽取未來日期明確的高雄音樂活動。",
                "source_url": "https://khh.travel/zh-tw/event/calendar/{year}/",
            },
        ),
        "運動": (
            {
                "title": "高雄運動賽事與活動",
                "snippet": "公開運動活動列表；只抽取高雄且日期明確的賽事或體驗。",
                "source_url": "https://www.ctrun.com.tw/",
            },
        ),
        "節慶": (
            {
                "title": "高雄旅遊網年度活動行事曆",
                "snippet": "官方年度活動列表；只抽取未來日期明確的高雄節慶與文化祭。",
                "source_url": "https://khh.travel/zh-tw/event/calendar/{year}/",
            },
            {
                "title": "高雄市政府活動訊息",
                "snippet": "市府活動列表；只抽取日期範圍內屬於節慶或文化祭的活動。",
                "source_url": "https://kcginfo.kcg.gov.tw/pda/active.aspx",
            },
        ),
        "美食": (
            {
                "title": "高雄旅遊網年度活動行事曆",
                "snippet": "官方年度活動列表；只抽取未來日期明確的高雄美食節、飲食主題活動。",
                "source_url": "https://khh.travel/zh-tw/event/calendar/{year}/",
            },
            {
                "title": "高雄茶、咖啡暨食品展",
                "snippet": "主辦單位活動頁；抽取日期與場地明確的食品及品飲活動。",
                "source_url": "https://www.chanchao.com.tw/KTCFS/",
            },
            {
                "title": "高雄國際酒展",
                "snippet": "主辦單位活動頁；抽取日期與場地明確的高雄品飲活動。",
                "source_url": "https://www.chanchao.com.tw/twsf/kaohsiung/visitor.asp",
            },
        ),
    },
}

CATEGORY_SPECS: dict[str, dict[str, Any]] = {
    "展覽": {
        "skill": "event-exhibition-discovery",
        "official_query": (
            "site:ud.kmfa.gov.tw/Exhibition/C000003/ OR site:pier2.org "
            "展覽 展期 地點"
        ),
        "broad_query": "展覽 特展 藝術裝置 博物館 官方",
        "extraction_focus": "展覽名稱、開展與結束日期、開放時間、高雄展覽場地",
        "recovery_terms": "展期 開幕 特展 博物館 藝文中心",
    },
    "市集": {
        "skill": "event-market-discovery",
        "official_query": (
            "site:twmarket.tw OR site:kcginfo.kcg.gov.tw OR site:khh.travel "
            "OR site:kpmc.com.tw OR site:pier2.org 市集 活動日期"
        ),
        "broad_query": "文創市集 假日市集 週末市集 生活節 ACCUPASS KKTIX",
        "extraction_focus": "市集名稱、每個獨立場次日期、營業時間、高雄場地",
        "recovery_terms": "週末市集 文創市集 招商 攤位 活動時間",
        "trusted_domains": ("twmarket.tw", "kcginfo.kcg.gov.tw", "khh.travel", "accupass.com", "kktix.com"),
    },
    "音樂": {
        "skill": "event-music-discovery",
        "official_query": (
            "site:kpmc.com.tw OR site:npac-weiwuying.org OR site:pier2.org OR site:khh.travel "
            "音樂會 演唱會 LIVE 節目表"
        ),
        "broad_query": (
            "site:kktix.com OR site:indievox.com OR site:opentix.life OR site:tixcraft.com "
            "高雄 演唱會 OR 音樂會 OR live OR 專場"
        ),
        "extraction_focus": "音樂活動名稱、演出日期與時間、高雄演出場地、演出類型",
        "recovery_terms": "演唱會 音樂會 音樂祭 LIVE 售票 專場 樂團 高流 衛武營",
        "trusted_domains": ("kpmc.com.tw", "npac-weiwuying.org", "pier2.org", "khh.travel", "kktix.com", "tixcraft.com", "indievox.com", "opentix.life", "artists.tw"),
    },
    "運動": {
        "skill": "event-sports-discovery",
        "official_query": "site:kcg.gov.tw OR site:khh.travel 運動 賽事 高雄 活動日期",
        "broad_query": "路跑 球賽 戶外運動 水域運動 體驗 報名平台 主辦",
        "extraction_focus": "運動活動名稱、比賽或體驗日期時間、高雄場地、運動項目",
        "recovery_terms": "報名 賽程 路跑 球賽 運動體驗",
        "trusted_domains": ("kcg.gov.tw", "khh.travel", "ctrun.com.tw", "bao-ming.com"),
    },
    "節慶": {
        "skill": "event-festival-discovery",
        "official_query": (
            "site:khh.travel OR site:kcg.gov.tw OR site:kcginfo.kcg.gov.tw OR site:accupass.com "
            "高雄 節慶 OR 文化祭 OR 嘉年華 OR 生活節 OR 主題日 OR 風箏節 OR 祭典"
        ),
        "broad_query": "高雄 節慶 OR 嘉年華 OR 文化節 OR 風箏節 OR 奶茶節 OR 啤酒節 OR 海洋派對 OR 生活節 2026",
        "extraction_focus": "節慶名稱、活動開始與結束日期、高雄場域、主要公開活動",
        "recovery_terms": "節慶 文化祭 嘉年華 生活節 風箏節 奶茶節 啤酒節 海洋派對 祭典",
        "trusted_domains": ("khh.travel", "kcg.gov.tw", "kcginfo.kcg.gov.tw", "accupass.com", "kktix.com", "twmarket.tw"),
    },
    "美食": {
        "skill": "event-food-discovery",
        "official_query": (
            "site:accupass.com OR site:kktix.com OR site:twmarket.tw OR site:supertaste.tvbs.com.tw OR site:khh.travel "
            "高雄 美食節 OR 咖啡節 OR 調酒 OR 甜點 OR 餐飲市集 OR 品飲 OR 烤肉"
        ),
        "broad_query": "高雄 美食節 OR 咖啡節 OR 甜點節 OR 美食市集 OR 啤酒節 OR 快閃餐車 OR 烘焙展 OR 烤肉 2026",
        "extraction_focus": "美食活動名稱、開始與結束日期、高雄場地、主要飲食主題",
        "recovery_terms": "美食節 咖啡節 甜點 烤肉 調酒 餐酒 美食市集 快閃餐車 啤酒節",
        "trusted_domains": ("khh.travel", "kcginfo.kcg.gov.tw", "chanchao.com.tw", "accupass.com", "kktix.com", "supertaste.tvbs.com.tw", "twmarket.tw"),
    },
}

_worker_thread: threading.Thread | None = None
_stop_event = threading.Event()
_run_lock = threading.Lock()
_last_run_date = ""
_extraction_cache = db["event_extraction_cache"]
_EXTRACTION_CACHE_VERSION = "event-extraction-v3"


class _EventIngestError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = re.sub(r"[^a-z0-9_]+", "_", str(code or "ingest_error").lower())[:80]
        super().__init__(self.code)


def _ingest_error_code(payload: Any, fallback: str) -> str:
    if not isinstance(payload, dict):
        return fallback
    value = payload.get("error_code")
    detail = payload.get("detail")
    if not value and isinstance(detail, dict):
        value = detail.get("error_code") or detail.get("code")
        nested = detail.get("detail")
        if not value and isinstance(nested, dict):
            value = nested.get("error_code") or nested.get("code")
    return str(value or fallback)


def _post_event_ingest(
    payload: dict[str, Any], *, timeout_seconds: int, category: str,
) -> dict[str, Any]:
    attempts = max(1, min(int(os.getenv("EVENT_INGEST_MAX_ATTEMPTS", "2") or 2), 3))
    last_error = "ingest_unavailable"
    for attempt in range(1, attempts + 1):
        try:
            attempt_payload = {
                **payload,
                # A local HTTP timeout does not cancel work already executing
                # on port 9001. Give that service a server-owned write fence so
                # a late provider response cannot mutate Event inventory after
                # this attempt has been reported as timed out.
                "write_deadline": time.time() + max(1, timeout_seconds - 1),
            }
            response = requests.post(
                AGENT_EVENT_INGEST_URL,
                json=attempt_payload,
                timeout=(3, timeout_seconds),
            )
            response.raise_for_status()
            try:
                result = response.json()
            except ValueError as exc:
                raise _EventIngestError("ingest_invalid_json") from exc
            if not isinstance(result, dict) or result.get("status") != "success":
                raise _EventIngestError(_ingest_error_code(result, "ingest_agent_error"))
            return result
        except requests.Timeout:
            last_error = "ingest_timeout"
        except requests.HTTPError as exc:
            status = int(getattr(exc.response, "status_code", 0) or 0)
            try:
                error_payload = exc.response.json() if exc.response is not None else {}
            except ValueError:
                error_payload = {}
            last_error = _ingest_error_code(
                error_payload, f"ingest_http_{status or 'error'}",
            )
            if status and status < 500 and status not in {408, 429}:
                raise _EventIngestError(last_error) from exc
        except requests.RequestException as exc:
            last_error = f"ingest_{type(exc).__name__.lower()}"
        except _EventIngestError as exc:
            last_error = exc.code
        print(
            f"[event-discovery] ingest_retry category={category} "
            f"attempt={attempt}/{attempts} error={last_error}"
        )
        if attempt < attempts:
            time.sleep(min(2.0 * attempt, 5.0))
    raise _EventIngestError(last_error)


def ensure_event_discovery_cache_indexes() -> None:
    try:
        _extraction_cache.create_index("updated_at", name="event_extraction_cache_updated")
    except Exception:
        pass


def _extraction_cache_key(category: str, batch: list[dict[str, str]]) -> str:
    projection = [{
        "title": str(item.get("title") or ""),
        "snippet": str(item.get("snippet") or ""),
        "source_url": str(item.get("source_url") or ""),
        "skill_name": str(item.get("skill_name") or ""),
        "skill_version": str(item.get("skill_version") or ""),
    } for item in batch]
    raw = json.dumps(
        {"version": _EXTRACTION_CACHE_VERSION, "category": category, "sources": projection},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cached_extraction(
    category: str, batch: list[dict[str, str]], active_event_ids: set[str],
) -> dict[str, Any] | None:
    if os.getenv("AYUE_TEST_MODE", "").strip().lower() in {"1", "true", "on"}:
        return None
    try:
        document = _extraction_cache.find_one(
            {"_id": _extraction_cache_key(category, batch)}, {"payload": 1},
        ) or {}
    except Exception:
        return None
    payload = document.get("payload")
    if not isinstance(payload, dict):
        return None
    events = [item for item in list(payload.get("events") or []) if isinstance(item, dict)]
    event_ids = {str(item.get("event_id") or "") for item in events}
    if not events or "" in event_ids or not event_ids.issubset(active_event_ids):
        return None
    return payload


def _store_cached_extraction(
    category: str, batch: list[dict[str, str]], payload: dict[str, Any],
) -> None:
    if os.getenv("AYUE_TEST_MODE", "").strip().lower() in {"1", "true", "on"}:
        return
    events = [item for item in list(payload.get("events") or []) if isinstance(item, dict)]
    if not events:
        return
    bounded = {
        "status": "success",
        "ingested_count": min(
            int(payload.get("ingested_count", len(events)) or 0),
            MAX_ACTIVE_EVENTS_PER_CATEGORY,
        ),
        "validation_counts": dict(payload.get("validation_counts") or {}),
        "events": events[:MAX_ACTIVE_EVENTS_PER_CATEGORY],
    }
    try:
        _extraction_cache.update_one(
            {"_id": _extraction_cache_key(category, batch)},
            {"$set": {"payload": bounded, "updated_at": time.time(),
                      "contract_version": _EXTRACTION_CACHE_VERSION}},
            upsert=True,
        )
    except Exception:
        pass


def _wait_for_interactive_chat() -> float:
    """Yield between Event batches while a Public Ayue turn owns priority."""
    if os.getenv("AYUE_TEST_MODE", "").strip().lower() in {"1", "true", "on"}:
        return 0.0
    started = time.monotonic()
    max_wait = max(0, min(float(os.getenv(
        "EVENT_CHAT_PRIORITY_MAX_WAIT_SECONDS", "600",
    ) or 600), 3600.0))
    while interactive_chat_active() and time.monotonic() - started < max_wait:
        time.sleep(0.5)
    return time.monotonic() - started


def _month_query_terms(now: datetime, window_days: int) -> str:
    end = now + timedelta(days=window_days)
    cursor = now.replace(day=1)
    months: list[str] = []
    while cursor <= end:
        months.append(f"{cursor.year}年{cursor.month}月")
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return " ".join(months)


def _queries(region: str, now: datetime, window_days: int) -> list[tuple[str, str]]:
    month_terms = _month_query_terms(now, window_days)
    queries: list[tuple[str, str]] = []
    for category in SUPPORTED_CATEGORIES:
        spec = CATEGORY_SPECS[category]
        queries.extend([
            (category, f"{region} {month_terms} {spec['official_query']}"),
            (category, f"{region} {month_terms} {spec['broad_query']}"),
        ])
    return queries


def _metadata_rejection_reason(item: dict[str, Any], now: datetime) -> str:
    title = str(item.get("title", ""))
    snippet = str(item.get("snippet", ""))
    text = f"{title} {snippet}"
    title_years = [int(value) for value in re.findall(r"(?<!\d)(20\d{2})(?!\d)", title)]
    if title_years and max(title_years) < now.year:
        return "stale_year"
    years = [int(value) for value in re.findall(r"(?<!\d)(20\d{2})(?!\d)", text)]
    if years and max(years) < now.year:
        if not any(f"{m}月" in text for m in range(1, 13)):
            return "stale_year"
    return ""


def _source_priority(item: dict[str, str], region: str) -> tuple[int, int]:
    category = str(item.get("discovery_category") or "")
    url = str(item.get("source_url") or "")
    curated_urls = {
        str(source.get("source_url") or "")
        for source in REGION_CURATED_SOURCES.get(region, {}).get(category, ())
    }
    if url in curated_urls:
        return (0, 0)
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    trusted = tuple(CATEGORY_SPECS.get(category, {}).get("trusted_domains") or ())
    if any(host == domain or host.endswith(f".{domain}") for domain in trusted):
        return (1, 0)
    return (2, 0)


def _url_healthcheck_enabled() -> bool:
    return os.getenv("EVENT_URL_HEALTHCHECK_ENABLED", "on").strip().lower() in {
        "1", "true", "on",
    }


def _source_url_alive(url: str) -> bool:
    if not is_safe_public_url(url):
        return False
    headers = {"User-Agent": "AyueEventVerifier/1.0", "Accept": "text/html,*/*;q=0.8"}
    try:
        response = requests.head(url, headers=headers, allow_redirects=True, timeout=(2, 4))
        if response.status_code in {403, 405}:
            response = requests.get(
                url, headers={**headers, "Range": "bytes=0-2047"},
                allow_redirects=True, stream=True, timeout=(2, 4),
            )
        return 200 <= response.status_code < 400
    except requests.RequestException:
        return False


def _normalized_source_title(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"20\d{2}", "", text)
    return "".join(
        character for character in text
        if unicodedata.category(character)[0] in {"L", "N"}
    )


def _source_batches(
    items: list[dict[str, str]], category: str = "",
) -> list[list[dict[str, str]]]:
    """Keep highly similar source titles together, then pair unrelated singletons."""
    batch_limit = max(1, min(
        int(CATEGORY_INGEST_BATCH_LIMITS.get(category, MAX_INGEST_SOURCES_PER_BATCH)),
        MAX_INGEST_SOURCES_PER_BATCH,
    ))
    clusters: list[list[dict[str, str]]] = []
    for item in items:
        title = _normalized_source_title(item.get("title", ""))
        match: list[dict[str, str]] | None = None
        if len(title) >= 6:
            for cluster in clusters:
                other = _normalized_source_title(cluster[0].get("title", ""))
                if other and SequenceMatcher(None, title, other).ratio() >= 0.9:
                    match = cluster
                    break
        if match is None or len(match) >= min(MAX_SOURCE_CLUSTER_SIZE, batch_limit):
            clusters.append([item])
        else:
            match.append(item)
    batches = [cluster for cluster in clusters if len(cluster) > 1]
    singletons = [cluster[0] for cluster in clusters if len(cluster) == 1]
    batches.extend(
        singletons[offset:offset + batch_limit]
        for offset in range(0, len(singletons), batch_limit)
    )
    return batches


def _reconcile_event_inventory(
    categories: tuple[str, ...], max_per_category: int,
) -> dict[str, Any]:
    try:
        response = requests.post(
            AGENT_EVENT_RECONCILE_URL,
            json={
                "categories": list(categories),
                "max_per_category": max_per_category,
            },
            timeout=(3, 120),
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {
            "status": "error", "error_code": "invalid_reconcile_response",
        }
    except requests.Timeout:
        return {"status": "error", "error_code": "reconcile_timeout"}
    except requests.RequestException:
        return {"status": "error", "error_code": "reconcile_http_error"}
    except ValueError:
        return {"status": "error", "error_code": "invalid_reconcile_json"}


def _skill_metadata(category: str) -> dict[str, str]:
    skill = load_skill(str(CATEGORY_SPECS[category]["skill"]))
    return {"skill_name": skill["name"], "skill_version": skill["version"]}


def _bounded_search_results(
    region: str, window_days: int, categories: tuple[str, ...] = SUPPORTED_CATEGORIES,
) -> tuple[list[dict[str, str]], list[str]]:
    now = datetime.now(TAIPEI)
    by_category_url: dict[tuple[str, str], dict[str, str]] = {}
    category_result_counts: dict[str, int] = {}
    for category in categories:
        for source in REGION_CURATED_SOURCES.get(region, {}).get(category, ()):
            url = str(source.get("source_url") or "").format(year=now.year)[:500]
            if not url or category_result_counts.get(category, 0) >= MAX_RESULTS_PER_CATEGORY:
                continue
            by_category_url[(category, url)] = {
                "title": str(source.get("title") or "")[:180],
                "snippet": str(source.get("snippet") or "")[:2500],
                "source_url": url,
                "discovery_category": category,
                **_skill_metadata(category),
            }
            category_result_counts[category] = category_result_counts.get(category, 0) + 1
    errors: list[str] = []
    for category, query in _queries(region, now, window_days):
        if category not in categories:
            continue
        if len(by_category_url) >= MAX_DISCOVERY_RESULTS:
            break
        if category_result_counts.get(category, 0) >= MAX_RESULTS_PER_CATEGORY:
            continue
        payload, error = search_web(query, location=region)
        if error:
            errors.append(f"{category}:{error}")
            continue
        added_for_query = 0
        for item in (payload or {}).get("results", []):
            url = str(item.get("url") or "")
            key = (category, url)
            if not url:
                continue
            # 跨類別 URL 去重：如果該 URL 已經被前面類別加入過，不重複納入其他類別
            existing_cat_with_url = next((c for (c, u) in by_category_url if u == url), None)
            if existing_cat_with_url and existing_cat_with_url != category:
                continue
            if key in by_category_url:
                found = by_category_url[key]
                snippet = str(item.get("snippet") or "")[:2500]
                if snippet and snippet not in found["snippet"]:
                    found["snippet"] = f"{found['snippet']} {snippet}".strip()[:2500]
                continue
            candidate = {
                "title": str(item.get("title") or "")[:180],
                "snippet": str(item.get("snippet") or "")[:2500],
                "source_url": url[:500],
                "discovery_category": category,
                **_skill_metadata(category),
            }
            if _metadata_rejection_reason(candidate, now):
                continue
            by_category_url[key] = candidate
            category_result_counts[category] = category_result_counts.get(category, 0) + 1
            added_for_query += 1
            if (
                added_for_query >= MAX_RESULTS_PER_QUERY
                or category_result_counts[category] >= MAX_RESULTS_PER_CATEGORY
                or len(by_category_url) >= MAX_DISCOVERY_RESULTS
            ):
                break
    all_results = list(by_category_url.values())
    results: list[dict[str, str]] = []
    for category in categories:
        ranked = sorted(
            (
                item for item in all_results
                if item.get("discovery_category") == category
            ),
            key=lambda item: _source_priority(item, region),
        )
        results.extend(ranked[:MAX_EXTRACT_RESULTS_PER_CATEGORY])
    enriched_by_category_url: dict[tuple[str, str], str] = {}
    range_end = now + timedelta(days=window_days)
    extracted_count = 0
    urls_by_category = {
        category: [
            item["source_url"] for item in results
            if item.get("discovery_category") == category
        ][:MAX_EXTRACT_RESULTS_PER_CATEGORY]
        for category in categories
    }
    # Round-robin prevents early categories from exhausting the global page
    # budget before festival/food sources receive any detailed extraction.
    for offset in range(0, MAX_EXTRACT_RESULTS_PER_CATEGORY, 2):
        for category in categories:
            if extracted_count >= MAX_EXTRACT_RESULTS:
                break
            batch_urls = urls_by_category[category][offset:offset + 2]
            if not batch_urls:
                continue
            batch_urls = batch_urls[:MAX_EXTRACT_RESULTS - extracted_count]
            extracted_count += len(batch_urls)
            extraction_query = (
                f"{region} {now:%Y/%m/%d} 到 {range_end:%Y/%m/%d} "
                f"{CATEGORY_SPECS[category]['extraction_focus']}"
            )
            extracted, error = extract_web(batch_urls, query=extraction_query)
            if error:
                errors.append(f"{category}:{error}")
                continue
            for page in (extracted or {}).get("pages", []):
                url = str(page.get("url") or "")
                content = " ".join(str(page.get("content") or "").split())[:2500]
                # Fast Pre-filter: 忽略 404 / 無效頁面 / 過短垃圾內容
                if url and content and len(content) >= 10:
                    lower_content = content.lower()
                    if not any(noise in lower_content for noise in ["404 not found", "頁面不存在", "找不到該頁面", "找不到網頁", "error 404"]):
                        enriched_by_category_url[(category, url)] = content
    for item in results:
        detail = enriched_by_category_url.get(
            (str(item.get("discovery_category") or ""), item["source_url"]), "",
        )
        if detail:
            item["snippet"] = f"{item['snippet']} {detail}".strip()[:2500]
    if _url_healthcheck_enabled() and results:
        healthy_results: list[dict[str, str]] = []
        pending_check = []
        for item in results:
            key = (str(item.get("discovery_category") or ""), item["source_url"])
            if key in enriched_by_category_url:
                healthy_results.append(item)
            else:
                pending_check.append(item)
        if pending_check:
            with ThreadPoolExecutor(max_workers=min(12, len(pending_check))) as pool:
                checks = list(pool.map(lambda it: (_source_url_alive(it["source_url"]), it), pending_check))
                for is_alive, item in checks:
                    if is_alive:
                        healthy_results.append(item)
                    else:
                        errors.append(f"{item.get('discovery_category', '其他')}:dead_source_url")
        results = healthy_results
    return results, list(dict.fromkeys(errors))


def _supplemental_search_results(
    region: str, window_days: int, category: str, *,
    excluded_urls: set[str], validation_counts: dict[str, int] | None = None,
    time_gaps: tuple[str, ...] = (),
) -> tuple[list[dict[str, str]], list[str]]:
    """Run bounded failure- and time-gap-aware recovery for one category."""
    now = datetime.now(TAIPEI)
    end = now + timedelta(days=window_days)
    counts = validation_counts or {}
    if int(counts.get("outside_window", 0) or 0) > 0:
        recovery_hint = "以下日期範圍內 即將舉辦 完整日期"
    elif int(counts.get("missing_or_invalid_date", 0) or 0) > 0:
        recovery_hint = "完整活動日期 開始時間 結束時間 場地"
    elif int(counts.get("model_returned_empty", 0) or 0) > 0:
        recovery_hint = "近期公開活動 活動日期 地點 報名 官方 主辦"
    else:
        recovery_hint = "近期活動 日期 地點 官方 主辦"
    spec = CATEGORY_SPECS[category]
    month_terms = _month_query_terms(now, window_days)
    gap_terms = {
        "near": "近期 本週",
        "middle": "本月中旬",
        "late": "本月下旬 下個月上旬",
    }
    time_hint = " ".join(gap_terms[value] for value in time_gaps if value in gap_terms)
    metadata = _skill_metadata(category)
    selected: list[dict[str, str]] = []
    seen = set(excluded_urls)
    errors: list[str] = []
    query_suffixes = (
        f"{spec['recovery_terms']} {recovery_hint}",
        f"{spec['recovery_terms']} 活動行事曆 場館節目 售票 報名 最新公告",
        f"{spec['recovery_terms']} {time_hint or '不同週次'} 主辦 官方",
    )
    for query_index, suffix in enumerate(query_suffixes[:MAX_SUPPLEMENTAL_QUERIES_PER_CATEGORY], 1):
        query = f"{region} {month_terms} {category} {suffix}"
        payload, error = search_web(query, location=region)
        if error:
            errors.append(f"{category}:supplemental_{query_index}_{error}")
            continue
        added_for_query = 0
        for item in (payload or {}).get("results", []):
            url = str(item.get("url") or "")[:500]
            if not url or url in seen:
                continue
            seen.add(url)
            candidate = {
                "title": str(item.get("title") or "")[:180],
                "snippet": str(item.get("snippet") or "")[:2500],
                "source_url": url,
                "discovery_category": category,
                **metadata,
            }
            if _metadata_rejection_reason(candidate, now):
                continue
            selected.append(candidate)
            added_for_query += 1
            if (
                added_for_query >= MAX_RESULTS_PER_QUERY
                or len(selected) >= MAX_SUPPLEMENTAL_RESULTS_PER_CATEGORY
            ):
                break
        if len(selected) >= MAX_SUPPLEMENTAL_RESULTS_PER_CATEGORY:
            break
    if not selected:
        return [], list(dict.fromkeys(errors))

    urls = [item["source_url"] for item in selected]
    extraction_query = (
        f"{region} {now:%Y/%m/%d} 到 {end:%Y/%m/%d} "
        f"{spec['extraction_focus']}"
    )
    enriched: dict[str, str] = {}
    for offset in range(0, len(urls), 2):
        extracted, extract_error = extract_web(urls[offset:offset + 2], query=extraction_query)
        if extract_error:
            errors.append(f"{category}:supplemental_extract_{extract_error}")
            continue
        for page in (extracted or {}).get("pages", []):
            page_url = str(page.get("url") or "")
            content = " ".join(str(page.get("content") or "").split())[:2500]
            if page_url and content:
                enriched[page_url] = content
    for item in selected:
        detail = enriched.get(item["source_url"], "")
        if detail:
            item["snippet"] = f"{item['snippet']} {detail}".strip()[:2500]
    if _url_healthcheck_enabled() and selected:
        checked: list[dict[str, str]] = []
        pending_supp_check = []
        for item in selected:
            if item["source_url"] in enriched:
                checked.append(item)
            else:
                pending_supp_check.append(item)
        if pending_supp_check:
            with ThreadPoolExecutor(max_workers=min(10, len(pending_supp_check))) as pool:
                supp_checks = list(pool.map(lambda it: (_source_url_alive(it["source_url"]), it), pending_supp_check))
                for is_alive, item in supp_checks:
                    if is_alive:
                        checked.append(item)
                    else:
                        errors.append(f"{category}:supplemental_dead_source_url")
        selected = checked
    return selected, list(dict.fromkeys(errors))


def _event_time_bucket(event: dict[str, Any], now_timestamp: float) -> str:
    candidates: list[float] = []
    for raw in list(event.get("session_starts") or []):
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value >= now_timestamp:
            candidates.append(value)
    if not candidates:
        try:
            starts_at = float(event.get("starts_at") or 0)
            ends_at = float(event.get("ends_at") or starts_at)
        except (TypeError, ValueError):
            return ""
        if starts_at <= now_timestamp <= ends_at:
            return "near"
        if starts_at >= now_timestamp:
            candidates.append(starts_at)
    if not candidates:
        return ""
    days_ahead = (min(candidates) - now_timestamp) / 86400
    if days_ahead <= 7:
        return "near"
    if days_ahead <= 20:
        return "middle"
    return "late"


def _active_event_inventory(categories: tuple[str, ...]) -> dict[str, Any]:
    """Read active category supply and its near/middle/late date distribution."""
    empty_counts = {category: 0 for category in categories}
    empty_buckets = {
        category: {"near": 0, "middle": 0, "late": 0}
        for category in categories
    }
    try:
        response = requests.get(AGENT_ACTIVE_EVENTS_URL, params={"limit": 100}, timeout=(3, 10))
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success":
            raise ValueError("active_event_inventory_failed")
    except (requests.RequestException, ValueError):
        return {
            "status": "unavailable",
            "category_counts": empty_counts,
            "time_bucket_counts": empty_buckets,
            "time_gaps": {},
            "temporal_status": "unavailable",
        }
    counts = dict(empty_counts)
    bucket_counts = {category: dict(values) for category, values in empty_buckets.items()}
    active_event_ids: set[str] = set()
    timestamped_event_count = 0
    now_timestamp = datetime.now(TAIPEI).timestamp()
    for event in list(payload.get("events") or [])[:100]:
        category = str(event.get("category") or "")
        if category in counts:
            event_id = str(event.get("event_id") or "")
            if event_id:
                active_event_ids.add(event_id)
            counts[category] += 1
            bucket = _event_time_bucket(event, now_timestamp)
            if bucket:
                bucket_counts[category][bucket] += 1
                timestamped_event_count += 1
    temporal_available = timestamped_event_count > 0
    time_gaps = {
        category: [bucket for bucket, count in values.items() if count == 0]
        for category, values in bucket_counts.items()
    } if temporal_available else {}
    return {
        "status": "success",
        "event_count": sum(counts.values()),
        "user_count": int(payload.get("user_count", 0) or 0),
        "category_counts": counts,
        "time_bucket_counts": bucket_counts,
        "time_gaps": time_gaps,
        "temporal_status": "available" if temporal_available else "unavailable",
        "event_ids": active_event_ids,
    }


def _event_identity(event: dict[str, Any]) -> str:
    internal = str(event.get("dedupe_key") or event.get("event_id") or "").strip()
    if internal:
        return internal
    return "|".join((
        "".join(str(event.get("title") or "").lower().split()),
        "".join(str(event.get("venue") or "").lower().split()),
        str(int(float(event.get("starts_at") or 0)) // 86400),
    ))


def discover_and_ingest_events(
    *, region: str = DEFAULT_REGION, window_days: int = DEFAULT_WINDOW_DAYS,
    categories: list[str] | tuple[str, ...] | None = None,
    request_invitation_scan: bool = True,
) -> dict[str, Any]:
    """Search bounded public snippets, then let port 9001 validate and store Events."""
    safe_region = " ".join(str(region or DEFAULT_REGION).split())[:40] or DEFAULT_REGION
    safe_window = max(1, min(int(window_days or DEFAULT_WINDOW_DAYS), 60))
    requested_categories = tuple(dict.fromkeys(
        str(item).strip() for item in (categories or SUPPORTED_CATEGORIES)
        if str(item).strip() in SUPPORTED_CATEGORIES
    )) or SUPPORTED_CATEGORIES
    if not _run_lock.acquire(blocking=False):
        return {"status": "already_running", "region": safe_region, "window_days": safe_window}
    try:
        baseline_inventory = _active_event_inventory(requested_categories)
        active_event_ids = set(baseline_inventory.get("event_ids") or [])
        target_per_category = max(1, min(int(os.getenv(
            "EVENT_DISCOVERY_TARGET_PER_CATEGORY", str(DEFAULT_TARGET_EVENTS_PER_CATEGORY),
        ) or DEFAULT_TARGET_EVENTS_PER_CATEGORY), MAX_ACTIVE_EVENTS_PER_CATEGORY))
        minimum_per_category = max(1, min(int(os.getenv(
            "EVENT_DISCOVERY_MIN_PER_CATEGORY", str(DEFAULT_MIN_EVENTS_PER_CATEGORY),
        ) or DEFAULT_MIN_EVENTS_PER_CATEGORY), target_per_category))
        baseline_counts = dict(baseline_inventory.get("category_counts") or {})
        results, search_errors = _bounded_search_results(
            safe_region, safe_window, requested_categories,
        )
        if not results:
            return {
                "status": "empty" if not search_errors else "search_failed",
                "region": safe_region,
                "window_days": safe_window,
                "categories": list(requested_categories),
                "searched_results": 0,
                "ingested_count": 0,
                "error_codes": search_errors,
                "events": [],
            }
        grouped: dict[str, list[dict[str, str]]] = {}
        for item in results:
            category = str(item.get("discovery_category") or "其他")[:30]
            grouped.setdefault(category, []).append(item)

        def ingest_batch(
            category: str, batch: list[dict[str, str]], *, max_events: int,
            timeout_seconds: int | None = None,
        ) -> tuple[str, dict[str, Any]]:
            cached = _cached_extraction(category, batch, active_event_ids)
            if cached is not None:
                cached_events = list(cached.get("events") or [])[:max_events]
                payload = {
                    **cached, "events": cached_events,
                    "ingested_count": len(cached_events), "cache_hit": True,
                }
                print(
                    f"[event-discovery] extraction_cache_hit category={category} "
                    f"events={len(cached_events)}"
                )
                return category, payload
            ingest_timeout = max(30, min(
                int(
                    timeout_seconds if timeout_seconds is not None
                    else os.getenv("EVENT_INGEST_TIMEOUT_SECONDS", "600") or 600
                ),
                900,
            ))
            payload = _post_event_ingest(
                {
                    "region": safe_region,
                    "window_days": safe_window,
                    "max_events": max(
                        1,
                        min(int(max_events or 1), MAX_ACTIVE_EVENTS_PER_CATEGORY),
                    ),
                    "search_results": batch,
                },
                timeout_seconds=ingest_timeout,
                category=category,
            )
            for event in list(payload.get("events") or []):
                event_id = str(event.get("event_id") or "") if isinstance(event, dict) else ""
                if event_id:
                    active_event_ids.add(event_id)
            _store_cached_extraction(category, batch, payload)
            return category, payload

        batch_results: list[tuple[str, dict[str, Any]]] = []
        ingest_errors: list[str] = []
        for category in requested_categories:
            accepted_keys: set[str] = set()
            remaining = target_per_category - int(baseline_counts.get(category, 0) or 0)
            if remaining <= 0:
                continue
            batch = grouped.get(category, [])
            if not batch:
                continue
            for source_batch in _source_batches(batch, category):
                try:
                    batch_result = ingest_batch(
                        category, source_batch, max_events=remaining,
                    )
                    batch_results.append(batch_result)
                    accepted_keys.update(
                        _event_identity(event)
                        for event in list(batch_result[1].get("events") or [])
                        if isinstance(event, dict)
                    )
                    remaining = max(
                        0,
                        target_per_category
                        - int(baseline_counts.get(category, 0) or 0)
                        - len(accepted_keys),
                    )
                    if remaining <= 0:
                        break
                except _EventIngestError as exc:
                    ingest_errors.append(f"{category}:{exc.code}")
                    break

        def aggregate_payloads() -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, dict[str, int]]]:
            events_by_category = {category: {} for category in requested_categories}
            validations = {category: {} for category in requested_categories}
            for category, payload in batch_results:
                for reason, value in dict(payload.get("validation_counts") or {}).items():
                    try:
                        count = int(value or 0)
                    except (TypeError, ValueError):
                        continue
                    validations[category][str(reason)[:60]] = (
                        validations[category].get(str(reason)[:60], 0) + count
                    )
                for event in list(payload.get("events") or []):
                    if isinstance(event, dict):
                        events_by_category[category][_event_identity(event)] = event
            return events_by_category, validations

        events_by_category, validation_counts = aggregate_payloads()
        primary_event_ids = {
            category: set(events_by_category[category]) for category in requested_categories
        }
        initial_inventory = _active_event_inventory(requested_categories)
        supply_counts = (
            dict(initial_inventory["category_counts"])
            if initial_inventory.get("status") == "success"
            else {
                category: len(events_by_category[category]) for category in requested_categories
            }
        )
        time_gaps_by_category = dict(initial_inventory.get("time_gaps") or {})
        supplemental_categories: list[str] = []
        supplemental_searched_results = 0
        supplemental_ingested_count = 0
        adaptive_enabled = os.getenv(
            "EVENT_ADAPTIVE_SUPPLEMENTAL_SEARCH", "on",
        ).strip().lower() in {"1", "true", "on"}
        max_supplemental_categories = 0
        supplemental_timeout = 0
        if adaptive_enabled:
            known_urls = {
                category: {
                    str(item.get("source_url") or "") for item in results
                    if item.get("discovery_category") == category
                }
                for category in requested_categories
            }
            max_supplemental_categories = max(1, min(int(os.getenv(
                "EVENT_ADAPTIVE_SUPPLEMENTAL_MAX_CATEGORIES",
                str(DEFAULT_MAX_SUPPLEMENTAL_CATEGORIES),
            ) or DEFAULT_MAX_SUPPLEMENTAL_CATEGORIES), len(requested_categories)))
            supplemental_timeout = max(30, min(int(os.getenv(
                "EVENT_SUPPLEMENTAL_INGEST_TIMEOUT_SECONDS",
                str(DEFAULT_SUPPLEMENTAL_INGEST_TIMEOUT_SECONDS),
            ) or DEFAULT_SUPPLEMENTAL_INGEST_TIMEOUT_SECONDS), 600))
            # A primary extraction timeout must not permanently disqualify the
            # category. Recovery uses fresh, smaller source batches and can still
            # fill the inventory without retrying the exact failed request.
            eligible_categories = [
                category for category in requested_categories
                if supply_counts[category] < target_per_category
            ]
            eligible_categories.sort(key=lambda category: (
                supply_counts[category],
                -len(time_gaps_by_category.get(category) or []),
                -sum(int(value or 0) for value in validation_counts.get(category, {}).values()),
                requested_categories.index(category),
            ))
            for category in eligible_categories[:max_supplemental_categories]:
                supplemental_categories.append(category)
                supplemental, supplemental_errors = _supplemental_search_results(
                    safe_region, safe_window, category,
                    excluded_urls=known_urls[category],
                    validation_counts=validation_counts.get(category),
                    time_gaps=tuple(time_gaps_by_category.get(category) or ()),
                )
                search_errors.extend(supplemental_errors)
                supplemental_searched_results += len(supplemental)
                if not supplemental:
                    continue
                for source_batch in _source_batches(supplemental, category):
                    try:
                        batch_results.append(ingest_batch(
                            category,
                            source_batch,
                            max_events=target_per_category - supply_counts[category],
                            timeout_seconds=supplemental_timeout,
                        ))
                    except _EventIngestError as exc:
                        ingest_errors.append(f"{category}:supplemental_{exc.code}")
                        break
            events_by_category, validation_counts = aggregate_payloads()

        category_counts = {
            category: len(events_by_category[category]) for category in requested_categories
        }
        ingested_events = [
            event for category in requested_categories
            for event in events_by_category[category].values()
        ]
        primary_unique_count = sum(len(values) for values in primary_event_ids.values())
        supplemental_ingested_count = max(0, len(ingested_events) - primary_unique_count)
        reconciliation = _reconcile_event_inventory(
            requested_categories, target_per_category,
        )
        if reconciliation.get("status") != "success":
            ingest_errors.append(str(
                reconciliation.get("error_code") or "inventory_reconcile_failed"
            )[:80])
        final_inventory = _active_event_inventory(requested_categories)
        baseline_user_count = baseline_inventory.get("user_count")
        final_user_count = final_inventory.get("user_count")
        user_projection_preserved = (
            baseline_inventory.get("status") != "success"
            or final_inventory.get("status") != "success"
            or baseline_user_count == final_user_count
        )
        if not user_projection_preserved:
            ingest_errors.append("graph_user_count_changed")
        active_category_counts = (
            dict(final_inventory["category_counts"])
            if final_inventory.get("status") == "success"
            else dict(category_counts)
        )
        minimum_deficits = {
            category: minimum_per_category - count
            for category, count in active_category_counts.items()
            if count < minimum_per_category
        }
        target_deficits = {
            category: target_per_category - count
            for category, count in active_category_counts.items()
            if count < target_per_category
        }
        final_time_bucket_counts = dict(final_inventory.get("time_bucket_counts") or {})
        final_time_gaps = dict(final_inventory.get("time_gaps") or {})
        relevance_result = (
            project_event_relevance(ingested_events)
            if ingested_events
            else {"status": "empty", "event_count": 0, "link_count": 0}
        )
        public_events = [
            {
                "title": item.get("title", ""),
                "summary": item.get("summary", ""),
                "venue": item.get("venue", ""),
                "starts_at": item.get("starts_at"),
                "ends_at": item.get("ends_at"),
                "time_precision": item.get("time_precision", "date"),
                "sessions": [
                    {
                        "starts_at": start,
                        "ends_at": (
                            list(item.get("session_ends") or [])[index]
                            if index < len(list(item.get("session_ends") or [])) else start
                        ),
                        "time_precision": (
                            list(item.get("session_precisions") or [])[index]
                            if index < len(list(item.get("session_precisions") or [])) else "date"
                        ),
                    }
                    for index, start in enumerate(list(item.get("session_starts") or [])[:8])
                ],
                "category": item.get("category", ""),
                "source_url": item.get("source_url", ""),
                "source_tier": item.get("source_tier", "curated"),
                "tags": list(item.get("tags") or []),
                "vibes": list(item.get("vibes") or []),
            }
            for item in ingested_events[:MAX_DISCOVERY_RESULTS]
        ]
        ingested_count = sum(category_counts.values())
        if ingested_count and request_invitation_scan:
            request_event_opportunity_scan()
        relevance_count = int(relevance_result.get("relevance_count", 0) or 0)
        avoidance_count = int(relevance_result.get("avoidance_count", 0) or 0)
        return {
            "status": "partial" if search_errors or ingest_errors else "success",
            "region": safe_region,
            "window_days": safe_window,
            "categories": list(requested_categories),
            "searched_results": len(results) + supplemental_searched_results,
            "ingested_count": ingested_count,
            "category_counts": category_counts,
            "active_category_counts": active_category_counts,
            "validation_counts": validation_counts,
            "reconciliation": reconciliation,
            "graph_integrity": {
                "user_projection_preserved": user_projection_preserved,
                "users_before": baseline_user_count,
                "users_after": final_user_count,
            },
            "coverage": {
                "status": "complete" if not minimum_deficits else "underfilled",
                "source": "graph" if final_inventory.get("status") == "success" else "current_batch",
                "minimum_per_category": minimum_per_category,
                "target_per_category": target_per_category,
                "max_per_category": MAX_ACTIVE_EVENTS_PER_CATEGORY,
                "deficits": minimum_deficits,
                "target_deficits": target_deficits,
                "temporal_status": final_inventory.get("temporal_status", "unavailable"),
                "time_bucket_counts": final_time_bucket_counts,
                "time_gaps": final_time_gaps,
            },
            "supplemental": {
                "enabled": adaptive_enabled,
                "target_per_category": target_per_category,
                "max_categories": max_supplemental_categories,
                "ingest_timeout_seconds": supplemental_timeout,
                "triggered_categories": supplemental_categories,
                "time_gaps": {
                    category: time_gaps_by_category.get(category, [])
                    for category in supplemental_categories
                    if time_gaps_by_category.get(category)
                },
                "searched_results": supplemental_searched_results,
                "additional_ingested_count": supplemental_ingested_count,
            },
            "relevance": {
                "status": relevance_result.get("status", "error"),
                "event_count": int(relevance_result.get("event_count", 0) or 0),
                "relevance_count": relevance_count,
                "avoidance_count": avoidance_count,
                "link_count": int(
                    relevance_result.get("link_count", relevance_count + avoidance_count) or 0
                ),
                "error_code": relevance_result.get("error_code"),
            },
            "error_codes": search_errors + ingest_errors,
            "events": public_events,
        }
    finally:
        _run_lock.release()


def _worker_loop() -> None:
    global _last_run_date
    while not _stop_event.wait(30.0):
        now = datetime.now(TAIPEI)
        run_hour = max(0, min(int(os.getenv("EVENT_DISCOVERY_HOUR", "8") or 8), 23))
        run_weekday = max(0, min(int(os.getenv("EVENT_DISCOVERY_WEEKDAY", "0") or 0), 6))
        run_key = now.strftime("%G-W%V")
        if now.weekday() != run_weekday or now.hour != run_hour or _last_run_date == run_key:
            continue
        result = discover_and_ingest_events(
            region=os.getenv("EVENT_DISCOVERY_REGION", DEFAULT_REGION),
            window_days=int(os.getenv("EVENT_DISCOVERY_WINDOW_DAYS", str(DEFAULT_WINDOW_DAYS)) or DEFAULT_WINDOW_DAYS),
            categories=[
                item.strip() for item in os.getenv(
                    "EVENT_DISCOVERY_CATEGORIES", ",".join(SUPPORTED_CATEGORIES)
                ).split(",") if item.strip()
            ],
        )
        _last_run_date = run_key
        print(f"[event-discovery] status={result.get('status')} ingested={result.get('ingested_count', 0)}")


def start_event_discovery_scheduler() -> None:
    """Deprecated in-process entrypoint; the dedicated Event worker owns schedules."""
    global _worker_thread
    print(
        "[event-discovery] in-process scheduler is disabled; "
        "run event_worker.py and use EVENT_WEEKLY_CYCLE_ENABLED"
    )


def stop_event_discovery_scheduler() -> None:
    global _worker_thread
    _stop_event.set()
    if _worker_thread and _worker_thread.is_alive():
        _worker_thread.join(timeout=2.0)
    _worker_thread = None
