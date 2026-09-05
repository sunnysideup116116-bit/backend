"""Repair critical hard-block modes and backfill missing intervention logs.

The 2026-09-05 incident had two effects: direct-threat KB rows were seeded as
``flag`` despite the guardrail contract expecting ``block``, and an HTTP to
HTTPS Appwrite redirect caused chat-log writes to fail. This migration upserts
the verified critical KB rows and recreates only missing intervention records
from the canonical pair-message history in MongoDB. It never deletes documents.

Run without ``--apply`` for a dry run. Applying creates a JSON backup first.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any
import warnings

from appwrite.client import Client
from appwrite.exception import AppwriteException
from appwrite.id import ID
from appwrite.query import Query
from appwrite.services.databases import Databases
from pymongo import MongoClient
import urllib3


warnings.simplefilter("ignore", DeprecationWarning)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


RISK_ROOT = Path(__file__).resolve().parents[1]
if str(RISK_ROOT) not in sys.path:
    sys.path.insert(0, str(RISK_ROOT))

from app.core.appwrite_config import configure_appwrite_client  # noqa: E402


HARD_BLOCK_DOCUMENTS = {
    "8": ("殺死你", "violence_threat"),
    "9": ("強姦你", "sexual_violence_threat"),
    "10": ("傳裸照給我", "sexual_demand"),
    "11": ("拍裸照給我", "sexual_demand"),
    "12": ("殺了你", "violence_threat"),
    "13": ("弄死你", "violence_threat"),
    "14": ("宰了你", "violence_threat"),
    "15": ("砍死你", "violence_threat"),
    "16": ("性侵你", "sexual_violence_threat"),
    "17": ("裸照公開", "intimate_image_blackmail"),
    "18": ("裸照外流", "intimate_image_blackmail"),
    "19": ("私密照公開", "intimate_image_blackmail"),
    "20": ("私密照外流", "intimate_image_blackmail"),
    "21": ("私密照傳給", "intimate_image_blackmail"),
    "22": ("我知道你住哪", "stalking_threat"),
    "23": ("去你家堵你", "stalking_threat"),
    "24": ("跟蹤你", "stalking_threat"),
    "25": ("傷害你的家人", "violence_threat"),
    "26": ("沒有拒絕的權利", "coercion_threat"),
    "27": ("不准拒絕", "coercion_threat"),
    "28": ("把你弄死", "violence_threat"),
    "29": ("把你殺了", "violence_threat"),
    "30": ("殺掉你", "violence_threat"),
    "31": ("把你宰了", "violence_threat"),
    "32": ("把你砍死", "violence_threat"),
}
RISK_LEVELS = {"observation", "warning", "restricted", "blocked"}


def _document_data(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", None)
    if isinstance(data, dict):
        return data
    return value if isinstance(value, dict) else {}


def _risk_map(message: dict[str, Any]) -> dict[str, Any]:
    metadata = message.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            return {}
    risk = metadata.get("risk") if isinstance(metadata, dict) else None
    return risk if isinstance(risk, dict) else {}


def _iso_timestamp(value: Any) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    if isinstance(value, datetime):
        current = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).isoformat()
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    return datetime.now(timezone.utc).isoformat()


def intervention_from_message(
    message: dict[str, Any],
    state_record: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    risk = _risk_map(message)
    triggered = str(risk.get("triggered_by_msg_id") or "").strip()
    level = str(risk.get("level") or "").strip().lower()
    if not triggered or level not in RISK_LEVELS:
        return None

    sender_directive = risk.get("sender_directive") or {}
    receiver_directive = risk.get("receiver_directive") or {}
    if not isinstance(sender_directive, dict):
        sender_directive = {}
    if not isinstance(receiver_directive, dict):
        receiver_directive = {}
    sender_id = str(sender_directive.get("target_user_id") or "").strip()
    if not sender_id and message.get("sender_id") != "ai_assistant":
        sender_id = str(message.get("sender_id") or "").strip()
    receiver_id = str(receiver_directive.get("target_user_id") or "").strip()
    conversation_id = str(message.get("room_id") or "").strip()
    if not sender_id or not receiver_id or not conversation_id:
        return None

    sender_action = str(sender_directive.get("action") or "none")[:50]
    receiver_action = str(receiver_directive.get("action") or "none")[:50]
    content = receiver_directive.get("content") or sender_directive.get("content") or {}
    if not isinstance(content, dict):
        content = {}
    snapshot = (state_record or {}).get("risk_state") or "{}"
    if not isinstance(snapshot, str):
        snapshot = json.dumps(snapshot, ensure_ascii=False)

    return {
        "triggered_by_msg_id": triggered[:50],
        "conversation_id": conversation_id[:50],
        "user_id": sender_id[:50],
        "sender_id": sender_id[:50],
        "receiver_id": receiver_id[:50],
        "risk_level": level[:20],
        "action_taken": sender_action[:100],
        "sender_action": sender_action,
        "receiver_action": receiver_action,
        "decision_reason": "recovered_pair_history",
        "primary_risk_type": str(content.get("primary_risk_type") or "any")[:50],
        "timestamp": _iso_timestamp(message.get("timestamp")),
        "risk_state_snapshot": snapshot[:1000],
        "composite_score": 0.0,
        "max_score": 0.0,
        "spread_score": 0.0,
        "trend_score": 0.0,
        "cooldown_seconds": int(sender_directive.get("cooldown_seconds") or 0),
        "sender_feedback": None,
        "receiver_feedback": None,
    }


def _existing_trigger_ids(databases: Databases, database_id: str) -> set[str]:
    result: set[str] = set()
    offset = 0
    while True:
        response = databases.list_documents(
            database_id,
            "intervention_logs",
            queries=[Query.limit(100), Query.offset(offset)],
        )
        for document in response.documents:
            value = str(_document_data(document).get("triggered_by_msg_id") or "")
            if value:
                result.add(value)
        if len(response.documents) < 100:
            return result
        offset += len(response.documents)


def _candidates(messages, states, existing: set[str]) -> tuple[list[dict], int]:
    result: list[dict] = []
    ambiguous = 0
    for message in messages.find({}):
        risk = _risk_map(message)
        triggered = str(risk.get("triggered_by_msg_id") or "")
        if not triggered or triggered in existing:
            continue
        state = states.find_one({"triggered_by_msg_id": triggered})
        recovered = intervention_from_message(message, state)
        if recovered is None:
            ambiguous += 1
            continue
        result.append(recovered)
        existing.add(triggered)
    return result, ambiguous


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--backup-root",
        type=Path,
        default=RISK_ROOT / "db_setup" / "backups",
    )
    args = parser.parse_args()

    client = Client()
    config = configure_appwrite_client(client)
    databases = Databases(client)
    mongo_uri = os.getenv("MONGO_URI", "").strip()
    if not mongo_uri:
        parser.error("MONGO_URI is required")
    mongo = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
    mongo_db = mongo[os.getenv("MONGO_DB_NAME", "profiling_db").split()[0].strip('"')]

    existing = _existing_trigger_ids(databases, config.db_id)
    candidates, ambiguous = _candidates(
        mongo_db["messages"],
        mongo_db["risk_state_history"],
        existing,
    )
    kb_before = {}
    for document_id, (keyword, reason_label) in HARD_BLOCK_DOCUMENTS.items():
        try:
            document = databases.get_document(
                config.kb_db_id,
                "kb_hard_blocks",
                document_id,
            )
            data = _document_data(document)
            if data.get("keyword") != keyword:
                raise RuntimeError(f"kb_hard_blocks/{document_id} keyword mismatch")
        except AppwriteException as error:
            if getattr(error, "code", None) != 404:
                raise
            data = {}
        kb_before[document_id] = {
            "keyword": keyword,
            "reason_label": reason_label,
            "trigger_mode": data.get("trigger_mode"),
            "exists": bool(data),
        }

    print(f"Appwrite endpoint: {config.endpoint}")
    print(f"hard-block rows to verify/update: {len(kb_before)}")
    print(f"missing intervention logs to backfill: {len(candidates)}")
    print(f"ambiguous history rows skipped: {ambiguous}")
    if not args.apply:
        print("plan only; pass --apply to back up and repair")
        return 0

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_dir = args.backup_root / timestamp
    backup_dir.mkdir(parents=True, exist_ok=False)
    backup_path = backup_dir / "risk_repair_backup.json"
    backup_path.write_text(
        json.dumps(
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "kb_before": kb_before,
                "interventions_to_create": candidates,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(f"backup: {backup_path}")

    for document_id, (keyword, reason_label) in HARD_BLOCK_DOCUMENTS.items():
        data = {
            "id": int(document_id),
            "keyword": keyword,
            "reason_label": reason_label,
            "trigger_mode": "block",
            "enabled": True,
        }
        if kb_before[document_id]["exists"]:
            databases.update_document(
                config.kb_db_id,
                "kb_hard_blocks",
                document_id,
                data,
            )
        else:
            data["created_at"] = datetime.now(timezone.utc).isoformat()
            databases.create_document(
                config.kb_db_id,
                "kb_hard_blocks",
                document_id,
                data,
            )
    created = 0
    for data in candidates:
        databases.create_document(
            config.db_id,
            "intervention_logs",
            ID.unique(),
            data,
        )
        created += 1

    for document_id in HARD_BLOCK_DOCUMENTS:
        repaired = databases.get_document(
            config.kb_db_id,
            "kb_hard_blocks",
            document_id,
        )
        if _document_data(repaired).get("trigger_mode") != "block":
            raise RuntimeError(f"kb_hard_blocks/{document_id} verification failed")
    after = _existing_trigger_ids(databases, config.db_id)
    missing = [
        data["triggered_by_msg_id"]
        for data in candidates
        if data["triggered_by_msg_id"] not in after
    ]
    if missing:
        raise RuntimeError(f"{len(missing)} intervention backfills were not readable")
    print(f"created intervention logs: {created}")
    print("risk repair verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
