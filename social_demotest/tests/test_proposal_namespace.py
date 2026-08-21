import unittest

from services.proposal_namespace import (
    EVENT_INVITATION_NAMESPACE,
    RELATIONSHIP_MATCH_NAMESPACE,
    extend_match_status_validator,
    live_proposal_query,
    live_namespace_conflict_count,
    namespace_for_document,
    normalized_live_participants,
    participant_pair_key,
)


class ProposalNamespaceTests(unittest.TestCase):
    def test_legacy_event_source_maps_to_event_namespace(self):
        self.assertEqual(
            namespace_for_document({"proposal_source": "event_opportunity"}),
            EVENT_INVITATION_NAMESPACE,
        )

    def test_legacy_ordinary_proposal_maps_to_relationship_namespace(self):
        self.assertEqual(
            namespace_for_document({"reason_version": "v4_friend_intro"}),
            RELATIONSHIP_MATCH_NAMESPACE,
        )

    def test_live_queries_use_distinct_namespace_slots(self):
        relationship = live_proposal_query("owner", RELATIONSHIP_MATCH_NAMESPACE)
        event = live_proposal_query("owner", EVENT_INVITATION_NAMESPACE)
        self.assertNotEqual(relationship["$and"][1], event["$and"][1])
        self.assertIn("live_participants", str(relationship))
        self.assertIn("live_participants", str(event))

    def test_pair_key_is_unordered(self):
        self.assertEqual(
            participant_pair_key("owner", "other"),
            participant_pair_key("other", "owner"),
        )

    def test_migration_participants_are_rebuilt_from_canonical_roles(self):
        self.assertEqual(
            normalized_live_participants({
                "from_user": "owner", "to_user": "other",
                "live_participants": ["stale"],
            }),
            ["owner", "other"],
        )
        self.assertEqual(
            normalized_live_participants({"from_user": "owner", "to_user": "owner"}),
            [],
        )

    def test_migration_preflight_detects_same_namespace_live_conflict(self):
        documents = [
            {
                "status": "draft", "proposal_namespace": "event_invitation",
                "from_user": "owner", "to_user": "first",
            },
            {
                "status": "pending", "proposal_namespace": "event_invitation",
                "from_user": "owner", "to_user": "second",
            },
            {
                "status": "draft", "proposal_namespace": "relationship_match",
                "from_user": "owner", "to_user": "third",
            },
        ]
        self.assertEqual(live_namespace_conflict_count(documents), 1)

    def test_validator_migration_adds_expired_without_replacing_other_rules(self):
        original = {
            "$jsonSchema": {
                "bsonType": "object",
                "required": ["status"],
                "properties": {
                    "status": {"enum": ["draft", "pending", "accepted", "declined"]},
                    "from_user": {"bsonType": "string"},
                },
            }
        }
        updated, changed, found = extend_match_status_validator(original)
        self.assertTrue(found)
        self.assertTrue(changed)
        self.assertIn("expired", updated["$jsonSchema"]["properties"]["status"]["enum"])
        self.assertEqual(
            updated["$jsonSchema"]["properties"]["from_user"],
            {"bsonType": "string"},
        )
        self.assertNotIn("expired", original["$jsonSchema"]["properties"]["status"]["enum"])

    def test_validator_migration_is_idempotent(self):
        validator = {
            "$jsonSchema": {"properties": {"status": {
                "enum": ["draft", "pending", "accepted", "declined", "expired"],
            }}}
        }
        _updated, changed, found = extend_match_status_validator(validator)
        self.assertTrue(found)
        self.assertFalse(changed)


if __name__ == "__main__":
    unittest.main()
