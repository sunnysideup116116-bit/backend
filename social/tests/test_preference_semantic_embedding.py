from unittest.mock import Mock
import pytest

from services import ai_service


def test_semantic_query_embedding_has_bounded_provider_timeout(monkeypatch):
    embed = Mock(return_value={"embedding": [[1.0] + [0.0] * 767]})
    monkeypatch.setattr(ai_service.genai, "embed_content", embed)
    monkeypatch.setattr(
        ai_service.google_key_pool, "execute", lambda operation: operation("key"),
    )
    monkeypatch.setattr(ai_service, "GOOGLE_EMBEDDING_MODEL", "models/gemini-embedding-2")

    result = ai_service.get_embeddings(
        ["K-pop"], task_type="semantic_similarity",
        output_dimensionality=768, request_timeout_seconds=999,
    )

    assert len(result[0]) == 768
    sent = embed.call_args.kwargs
    assert sent["content"] == ["task: sentence similarity | query: K-pop"]
    assert sent["output_dimensionality"] == 768
    assert 0 < sent["request_options"]["timeout"] <= 10.0
    assert sent["request_options"]["retry"] is None


def test_existing_embedding_call_has_no_new_timeout_by_default(monkeypatch):
    embed = Mock(return_value={"embedding": [[1.0] + [0.0] * 767]})
    monkeypatch.setattr(ai_service.genai, "embed_content", embed)
    monkeypatch.setattr(
        ai_service.google_key_pool, "execute", lambda operation: operation("key"),
    )
    monkeypatch.setattr(ai_service, "GOOGLE_EMBEDDING_MODEL", "models/gemini-embedding-2")

    ai_service.get_embeddings(
        ["活動"], task_type="semantic_similarity", output_dimensionality=768,
    )

    assert "request_options" not in embed.call_args.kwargs


def test_key_failover_shares_deadline_and_hides_provider_exception(monkeypatch, capsys):
    clock = [0.0]
    monkeypatch.setattr(ai_service.time, "monotonic", lambda: clock[0])
    requests = []
    def embed(**payload):
        requests.append(payload)
        clock[0] += 2.5
        raise RuntimeError("quota: raw private provider request")
    def two_keys(operation):
        try:
            operation("first")
        except RuntimeError:
            return operation("second")
    monkeypatch.setattr(ai_service.genai, "embed_content", embed)
    monkeypatch.setattr(ai_service.google_key_pool, "execute", two_keys)
    with pytest.raises(ai_service.HTTPException) as caught:
        ai_service.get_embeddings(["K-pop"], request_timeout_seconds=3.0)
    assert [r["request_options"]["timeout"] for r in requests] == [3.0, .5]
    assert "raw private" not in str(caught.value.detail) + capsys.readouterr().out
