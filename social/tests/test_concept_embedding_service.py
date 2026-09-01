import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException

from services import concept_embedding_service as service


class ConceptEmbeddingServiceTests(unittest.TestCase):
    @patch.object(service, "_mongo_kind_overrides", return_value={"hiking": "activity"})
    @patch.object(service, "get_embeddings", return_value=[[3.0] + [4.0] + [0.0] * 766])
    @patch.object(service.requests, "post")
    @patch.object(service.requests, "get")
    def test_one_batch_is_normalized_and_projected(
        self, read, write, embed, _mongo_kinds,
    ):
        read_response = Mock()
        read_response.raise_for_status.return_value = None
        read_response.json.return_value = {
            "status": "success",
            "concepts": [{"key": "hiking", "label": "爬山", "suggested_kind": "unknown"}],
        }
        read.return_value = read_response
        write_response = Mock()
        write_response.raise_for_status.return_value = None
        write_response.json.return_value = {
            "status": "success", "embedded_count": 1, "pending_count": 0,
        }
        write.return_value = write_response

        result = service.process_pending_concept_embeddings(batch_size=50)

        self.assertEqual(result["embedded_count"], 1)
        embed.assert_called_once_with(
            ["爬山"], task_type="semantic_similarity", output_dimensionality=768,
        )
        sent = write.call_args.kwargs["json"]["concepts"][0]
        self.assertEqual(sent["kind"], "activity")
        self.assertAlmostEqual(sent["embedding"][0], 0.6)
        self.assertAlmostEqual(sent["embedding"][1], 0.8)
        self.assertEqual(read.call_args.kwargs["params"]["limit"], 20)

    @patch.object(service, "_mongo_kind_overrides", return_value={})
    @patch.object(service, "get_embeddings")
    @patch.object(service.requests, "get")
    def test_embedding_quota_returns_retry_delay(self, read, embed, _mongo_kinds):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "status": "success",
            "concepts": [{"key": "hiking", "label": "爬山", "suggested_kind": "activity"}],
        }
        read.return_value = response
        embed.side_effect = HTTPException(
            status_code=500, detail="429 quota exceeded. Please retry in 45.5s",
        )

        result = service.process_pending_concept_embeddings()

        self.assertEqual(result["status"], "rate_limited")
        self.assertGreaterEqual(result["retry_after"], 46.0)


if __name__ == "__main__":
    unittest.main()
