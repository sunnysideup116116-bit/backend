"""Apply the additive Appwrite migration twice against an in-memory API fake."""

from types import SimpleNamespace

from db_setup.add_block_report_collections import migration_schema
from db_setup.apply_20260831_appwrite import (
    KB_ACTION_SCHEMA,
    apply_kb_action_updates,
    verify_kb_action_updates,
)
from db_setup.migrate_appwrite_schema import ensure_schema, verify_schema
from db_setup.safety_action_contract import SAFETY_ACTION_UPDATES


class FakeClient:
    def __init__(self, databases):
        self.databases = databases
        self.mutations = 0

    def call(self, method, path, headers, params):
        del method, headers
        parts = path.strip("/").split("/")
        if path.endswith("/collections"):
            collection_id = params["collectionId"]
            self.databases.collections.add(collection_id)
            self.databases.attributes.setdefault(collection_id, set())
            self.databases.indexes.setdefault(collection_id, set())
        elif "/attributes/" in path:
            collection_id = parts[3]
            self.databases.attributes[collection_id].add(params["key"])
        elif path.endswith("/indexes"):
            collection_id = parts[3]
            self.databases.indexes[collection_id].add(params["key"])
        else:
            raise AssertionError(f"unexpected path: {path}")
        self.mutations += 1


class FakeDatabases:
    def __init__(self):
        self.collections = {"intervention_logs"}
        self.attributes = {"intervention_logs": set()}
        self.indexes = {"intervention_logs": set()}
        self.documents = {}
        self.document_updates = 0
        self.client = FakeClient(self)

    def list_collections(self, database_id, queries=None):
        del database_id, queries
        return SimpleNamespace(
            collections=[SimpleNamespace(id=value) for value in self.collections]
        )

    def list_attributes(self, database_id, collection_id, queries=None):
        del database_id, queries
        return SimpleNamespace(
            attributes=[
                SimpleNamespace(key=value, status="available")
                for value in self.attributes.get(collection_id, set())
            ]
        )

    def list_indexes(self, database_id, collection_id, queries=None):
        del database_id, queries
        return SimpleNamespace(
            indexes=[
                SimpleNamespace(key=value)
                for value in self.indexes.get(collection_id, set())
            ]
        )

    def get_document(self, database_id, collection_id, document_id):
        del database_id, collection_id
        return self.documents[document_id]

    def update_document(
        self,
        database_id,
        collection_id,
        document_id,
        data,
    ):
        del database_id, collection_id
        self.documents[document_id].data.update(data)
        self.document_updates += 1
        return self.documents[document_id]


def test_appwrite_additive_migration_is_repeatable():
    databases = FakeDatabases()
    schema = migration_schema()

    ensure_schema(databases, "risk-db", schema)
    first_mutation_count = databases.client.mutations
    ensure_schema(databases, "risk-db", schema)

    assert databases.client.mutations == first_mutation_count
    assert verify_schema(databases, "risk-db", schema) == []
    assert {item["id"] for item in schema["collections"]} == {
        "intervention_logs",
        "user_blocks",
        "user_reports",
    }
    assert "receiver_report_text" in databases.attributes["intervention_logs"]


def test_appwrite_kb_action_migration_is_repeatable():
    databases = FakeDatabases()
    databases.collections = {"kb_interventions"}
    databases.attributes = {"kb_interventions": {"template_id"}}
    databases.indexes = {"kb_interventions": {"idx_risk_level"}}
    databases.documents = {
        template_id: SimpleNamespace(
            id=template_id,
            data={"template_id": template_id},
        )
        for template_id in SAFETY_ACTION_UPDATES
    }

    ensure_schema(databases, "kb", KB_ACTION_SCHEMA)
    first = apply_kb_action_updates(databases, "kb")
    first_update_count = databases.document_updates
    second = apply_kb_action_updates(databases, "kb")

    assert first == {"updated": 3, "unchanged": 0, "missing": 0}
    assert second == {"updated": 0, "unchanged": 3, "missing": 0}
    assert databases.document_updates == first_update_count
    assert verify_kb_action_updates(databases, "kb") == []
    assert "action_options" in databases.attributes["kb_interventions"]
