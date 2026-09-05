"""
Knowledge Base 服務 - Appwrite 版本

將原本存放於 MySQL/SQLite 的知識庫 (kb_configs / kb_features / kb_hard_blocks
/ kb_interventions / kb_prompts / kb_rules / kb_scenario_rules) 改為從 Appwrite
的 KB database 讀取，統一後端儲存層。對外 method 簽章與回傳結構與舊版一致。
"""
import json
import os
import threading
import time

import requests
from dotenv import load_dotenv
from appwrite.query import Query
from app.core.appwrite_config import get_appwrite_config

load_dotenv()

# 知識庫是設定資料；避免每次 /detect 都以同步 HTTP 重抓相同內容。
# 設為 0 可停用快取，更新 KB 後可呼叫 KBService.clear_cache() 立即失效。
_CACHE_TTL = float(os.getenv("KB_CACHE_TTL_SECONDS", "300"))
_cache: dict[str, tuple[float, list]] = {}
_cache_lock = threading.Lock()


class KBService:
    _endpoint = None
    _project_id = None
    _api_key = None
    _kb_db_id = None

    @classmethod
    def _ensure_config(cls):
        if cls._endpoint is not None:
            return
        config = get_appwrite_config()
        cls._endpoint = config.endpoint
        cls._project_id = config.project_id
        cls._api_key = config.api_key
        cls._kb_db_id = config.kb_db_id

    @classmethod
    def _headers(cls):
        cls._ensure_config()
        return {
            "X-Appwrite-Project": cls._project_id,
            "X-Appwrite-Key": cls._api_key,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _list(collection_id, queries=None, limit=100):
        """從 Appwrite KB database 列出 documents（含 TTL 快取）。"""
        if _CACHE_TTL <= 0:
            return KBService._fetch(collection_id, queries, limit)

        key = f"{collection_id}|{json.dumps(queries, sort_keys=True, default=str)}|{limit}"
        now = time.monotonic()
        with _cache_lock:
            hit = _cache.get(key)
            if hit and now - hit[0] < _CACHE_TTL:
                return hit[1]

        result = KBService._fetch(collection_id, queries, limit)

        # _fetch 失敗時會回空 list；不快取失敗，讓下次請求可以立即重試。
        if result:
            with _cache_lock:
                _cache[key] = (now, result)
        return result

    @classmethod
    def clear_cache(cls):
        """清除所有 KB 快取，讓下一次查詢重新讀取 Appwrite。"""
        with _cache_lock:
            _cache.clear()

    @staticmethod
    def _fetch(collection_id, queries=None, limit=100):
        """從 Appwrite KB database 列出 documents，自動分頁，回傳 list[dict]。

        直接走 REST API（與 setup_appwrite.py 一致），避免 SDK 對 legacy server
        將 attribute key 小寫化的行為差異。
        """
        KBService._ensure_config()
        ep = KBService._endpoint
        kb_db_id = KBService._kb_db_id
        headers = KBService._headers()

        results = []
        seen_ids = set()
        offset = 0
        while True:
            serialized_queries = [
                query if isinstance(query, str) else json.dumps(query)
                for query in (queries or [])
            ]
            # Appwrite 1.9 ignores top-level limit/offset on the legacy
            # documents route. Pagination must be encoded as Query values;
            # otherwise only the first 25 KB rows are ever visible.
            serialized_queries.extend([
                json.dumps({"method": "limit", "values": [limit]}),
                json.dumps({"method": "offset", "values": [offset]}),
            ])
            params = [("queries[]", query) for query in serialized_queries]
            verify_ssl = False if ("127.0.0.1" in ep or "localhost" in ep) else True
            r = requests.get(
                f"{ep}/databases/{kb_db_id}/collections/{collection_id}/documents",
                headers=headers,
                params=params,
                verify=verify_ssl,
            )
            if r.status_code != 200:
                print(f"[KB] list {collection_id} -> {r.status_code} {r.text[:200]}")
                return results
            data = r.json()
            docs = data.get("documents", [])
            for doc in docs:
                document_id = doc.get("$id")
                if document_id and document_id in seen_ids:
                    continue
                if document_id:
                    seen_ids.add(document_id)
                clean = {k: v for k, v in doc.items() if not k.startswith("$")}
                results.append(clean)
            total = data.get("total", 0)
            offset += len(docs)
            if not docs or offset >= total:
                break

        # 防禦性客戶端過濾：若 Appwrite REST endpoint 未正確套用 queries 參數，在此二次過濾
        if queries:
            filtered = []
            for doc in results:
                matched = True
                for q in queries:
                    if isinstance(q, dict) and q.get("method") == "equal":
                        attr = q.get("attribute")
                        vals = q.get("values", [])
                        if doc.get(attr) not in vals:
                            matched = False
                            break
                if matched:
                    filtered.append(doc)
            results = filtered

        return results

    @staticmethod
    def _parse_json_fields(record, fields):
        """若欄位是字串 JSON 則解析；None 則保留 None。"""
        for f in fields:
            v = record.get(f)
            if isinstance(v, str) and v.strip():
                try:
                    record[f] = json.loads(v)
                except (ValueError, TypeError):
                    pass
        return record

    @staticmethod
    def get_features():
        """讀取所有啟用的特徵清單"""
        try:
            docs = KBService._list("kb_features", queries=[{"method": "equal", "attribute": "enabled", "values": [True]}])
            for f in docs:
                KBService._parse_json_fields(f, ["logic_config"])
            return docs
        except Exception as e:
            print(f"[KB] get_features failed: {e}")
            return []

    @staticmethod
    def get_scenario_rules():
        """讀取啟用的二階複合規則"""
        try:
            docs = KBService._list("kb_scenario_rules", queries=[{"method": "equal", "attribute": "enabled", "values": [True]}])
            for r in docs:
                KBService._parse_json_fields(r, ["condition_logic", "bonus_actions"])
            return docs
        except Exception as e:
            print(f"[KB] get_scenario_rules failed: {e}")
            return []

    @staticmethod
    def get_rules():
        """讀取行為規則 (用於 RuleBasedEngine)，依 priority 降冪"""
        try:
            docs = KBService._list(
                "kb_rules",
                queries=[
                    {"method": "equal", "attribute": "enabled", "values": [True]},
                    {"method": "orderDesc", "attribute": "priority"},
                ],
            )
            for r in docs:
                KBService._parse_json_fields(r, ["conditions", "actions"])
            return docs
        except Exception as e:
            print(f"[KB] get_rules failed: {e}")
            return []

    @staticmethod
    def get_prompt(prompt_id="risk_analysis_v2"):
        """抓取特定的 Prompt 模板"""
        try:
            docs = KBService._list(
                "kb_prompts",
                queries=[
                    {"method": "equal", "attribute": "prompt_id", "values": [prompt_id]},
                    {"method": "equal", "attribute": "enabled", "values": [True]},
                ],
                limit=1,
            )
            return docs[0] if docs else None
        except Exception as e:
            print(f"[KB] get_prompt failed: {e}")
            return None

    @staticmethod
    def get_prompt_by_id(prompt_id: str):
        """抓取特定的 Prompt 模板"""
        return KBService.get_prompt(prompt_id)

    @staticmethod
    def get_fusion_config(config_id="threshold_v1"):
        """讀取融合/決策設定"""
        try:
            docs = KBService._list(
                "kb_configs",
                queries=[
                    {"method": "equal", "attribute": "config_id", "values": [config_id]},
                    {"method": "equal", "attribute": "enabled", "values": [True]},
                ],
                limit=1,
            )
            if not docs:
                return None
            config = docs[0]
            KBService._parse_json_fields(config, ["thresholds", "weights"])
            return config
        except Exception as e:
            print(f"[KB] get_fusion_config failed: {e}")
            return None

    @staticmethod
    def get_interventions_by_level(risk_level: str):
        """讀取特定等級的所有介入模板"""
        try:
            docs = KBService._list(
                "kb_interventions",
                queries=[{"method": "equal", "attribute": "risk_level", "values": [risk_level]}],
            )
            for t in docs:
                KBService._parse_json_fields(t, ["message_template", "ui_behavior"])
            return docs
        except Exception as e:
            print(f"[KB] get_interventions_by_level failed: {e}")
            return []

    @staticmethod
    def get_hard_block_records():
        """Get all hard-block records with trigger_mode."""
        fallback = [
            {"keyword": "炸彈", "reason_label": "violence", "trigger_mode": "flag"},
            {"keyword": "槍枝", "reason_label": "violence", "trigger_mode": "flag"},
            {"keyword": "自殺", "reason_label": "self_harm", "trigger_mode": "flag"},
            {"keyword": "毒品", "reason_label": "illegal_drugs", "trigger_mode": "flag"},
            {"keyword": "殺人", "reason_label": "violence", "trigger_mode": "flag"},
            {"keyword": "強姦", "reason_label": "sexual_violence", "trigger_mode": "flag"},
            {"keyword": "裸照", "reason_label": "sexual_content", "trigger_mode": "flag"},
            {"keyword": "殺死你", "reason_label": "violence_threat", "trigger_mode": "block"},
            {"keyword": "強姦你", "reason_label": "sexual_violence_threat", "trigger_mode": "block"},
            {"keyword": "傳裸照給我", "reason_label": "sexual_demand", "trigger_mode": "block"},
            {"keyword": "拍裸照給我", "reason_label": "sexual_demand", "trigger_mode": "block"},
        ]

        try:
            docs = KBService._list("kb_hard_blocks", queries=[{"method": "equal", "attribute": "enabled", "values": [True]}])
            records = [{"keyword": d.get("keyword"), "reason_label": d.get("reason_label"), "trigger_mode": d.get("trigger_mode")} for d in docs]
            return records if records else fallback
        except Exception as e:
            print(f"[KB] get_hard_block_records failed: {e}")
            return fallback

    @staticmethod
    def get_hard_block_keywords():
        """Backward compat: returns just keyword strings."""
        records = KBService.get_hard_block_records()
        return [r["keyword"] for r in records]
