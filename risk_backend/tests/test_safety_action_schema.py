"""Static contract checks for the additive safety-action migrations."""

import json
from pathlib import Path

from db_setup.safety_action_contract import (
    ACTION_OPTIONS_ATTRIBUTE,
    SAFETY_ACTION_UPDATES,
)
from db_setup.setup_kb_appwrite import SCHEMA as KB_SCHEMA


DB_SETUP = Path(__file__).resolve().parents[1] / "db_setup"


def test_appwrite_schema_has_eleven_collections_and_required_fields():
    schema = json.loads((DB_SETUP / "appwrite_schema_dump.json").read_text(encoding="utf-8"))
    collections = {item["id"]: item for item in schema["collections"]}
    assert len(collections) == 11
    assert {"user_blocks", "user_reports"} <= collections.keys()

    intervention_fields = {
        item["key"] for item in collections["intervention_logs"]["attributes"]
    }
    assert "receiver_report_text" in intervention_fields
    assert {item["key"] for item in collections["user_blocks"]["indexes"]} == {
        "idx_blocker", "idx_blocked", "idx_blocker_blocked",
    }
    assert {item["key"] for item in collections["user_reports"]["indexes"]} == {
        "idx_reported", "idx_status",
    }


def test_mysql_migration_is_repeatable_and_seeds_all_three_option_sets():
    sql = (DB_SETUP / "add_action_options.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS `action_options`" in sql
    assert "json_valid(`action_options`)" in sql
    assert "restrict_receiver_options" in sql
    assert "block_receiver_notice" in sql
    assert "receiver_state_notice" in sql
    assert '"action":"dismiss"' in sql


def test_appwrite_kb_schema_and_updates_include_action_contract():
    intervention_attributes = {
        item["key"]: item
        for item in KB_SCHEMA["kb_interventions"]["attributes"]
    }
    assert intervention_attributes["action_options"] == ACTION_OPTIONS_ATTRIBUTE
    assert set(SAFETY_ACTION_UPDATES) == {
        "restrict_receiver_options",
        "block_receiver_notice",
        "receiver_state_notice",
    }
