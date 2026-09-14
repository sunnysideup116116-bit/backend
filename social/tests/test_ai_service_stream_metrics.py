"""Provider-free regressions for stream fidelity and observable timings."""

from unittest.mock import patch

import pytest

from services import ai_service


def test_stream_preserves_provider_fragments_and_counts_full_response_time():
    now = [10.0]
    fragments = ["Hello ", "world", "\n", "- 第一間 ", "café"]

    def provider(**_kwargs):
        now[0] += 0.02

        def stream():
            for fragment in fragments:
                now[0] += 0.2
                yield {"message": {"content": fragment}}
            now[0] += 0.5
            yield {"message": {}, "prompt_eval_count": 30, "eval_count": 20}

        return stream()

    emitted = []
    with patch.object(ai_service, "OLLAMA_API_KEY", "mock"), \
         patch.object(ai_service.time, "perf_counter", side_effect=lambda: now[0]), \
         patch.object(ai_service.ollama_client, "chat", side_effect=provider):
        result = ai_service.generate_chat_completion_with_tools("mock", [], on_token=emitted.append)

    assert emitted == fragments
    assert result.content == "Hello world\n- 第一間 café"
    assert result.duration_ms == 1520
    assert result.ttft_ms == 220
    assert result.tps == pytest.approx(20 / 1.3, abs=0.001)
    assert (result.input_tokens, result.output_tokens) == (30, 20)


@pytest.mark.parametrize("eval_duration, expected_tps", [(None, 0.0), (500_000_000, 40.0)])
def test_nonstream_does_not_invent_first_token_or_decoding_time(eval_duration, expected_tps):
    response = {
        "message": {"content": "Hello world"},
        "prompt_eval_count": 30, "eval_count": 20,
        "eval_duration": eval_duration,
    }
    with patch.object(ai_service, "OLLAMA_API_KEY", "mock"), \
         patch.object(ai_service.time, "perf_counter", side_effect=[10.0, 11.0]), \
         patch.object(ai_service.ollama_client, "chat", return_value=response):
        result = ai_service.generate_chat_completion_with_tools("mock", [])

    assert result.duration_ms == 1000
    assert result.ttft_ms == 0
    assert result.tps == expected_tps


def test_stream_without_content_has_no_fabricated_first_token_or_throughput():
    with patch.object(ai_service, "OLLAMA_API_KEY", "mock"), \
         patch.object(ai_service.time, "perf_counter", side_effect=[10.0, 11.0]), \
         patch.object(ai_service.ollama_client, "chat", return_value=iter([
             {"message": {}, "eval_count": 20},
         ])):
        result = ai_service.generate_chat_completion_with_tools("mock", [], on_token=lambda _: None)

    assert (result.duration_ms, result.ttft_ms, result.tps) == (1000, 0, 0.0)


def test_stream_uses_provider_decode_duration_for_tool_call_only_response():
    call = {"function": {"name": "example", "arguments": {"name": "coffee"}}}
    with patch.object(ai_service, "OLLAMA_API_KEY", "mock"), \
         patch.object(ai_service.time, "perf_counter", side_effect=[10.0, 10.4, 11.0]), \
         patch.object(ai_service.ollama_client, "chat", return_value=iter([
             {"message": {"tool_calls": [call]}, "eval_count": 20, "eval_duration": 500_000_000},
         ])):
        result = ai_service.generate_chat_completion_with_tools("mock", [], on_token=lambda _: None)

    assert result.tool_calls == [{"name": "example", "arguments": {"name": "coffee"}}]
    assert (result.duration_ms, result.ttft_ms, result.tps) == (1000, 400, 40.0)
