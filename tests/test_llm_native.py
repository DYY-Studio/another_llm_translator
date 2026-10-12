from pathlib import Path

import pytest

from app.errors import ExternalError
from app.llm_adapter import load_json_adapter

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize(("name", "payload"), [
    ("openai-responses", {"output": [{"type": "message", "content": [{"type": "output_text", "text": "first"}, {"type": "output_text", "text": "second"}]}]}),
    ("anthropic", {"content": [{"type": "thinking", "thinking": "ignore"}, {"type": "text", "text": "first"}, {"type": "text", "text": "second"}], "stop_reason": "end_turn"}),
    ("google-gemini", {"candidates": [{"content": {"parts": [{"text": "ignore", "thought": True}, {"text": "first"}, {"text": "second"}]}, "finishReason": "STOP"}]}),
])
def test_native_response_aggregates_all_body_text(name, payload):
    adapter = load_json_adapter(ROOT / "llm_adapters" / f"{name}.json")
    assert adapter.parse_response(payload).content == "firstsecond"


def test_native_errors_and_refusals_keep_provider_context():
    responses = load_json_adapter(ROOT / "llm_adapters/openai-responses.json")
    event = {"type": "response.failed", "response": {"error": {"message": "provider disconnected"}}}
    assert responses.stream_error_details(event).message == "provider disconnected"
    refusal = {"type": "response.refusal.delta", "delta": "no"}
    assert responses.stream_finish_reason(refusal) == "refusal"
    gemini = load_json_adapter(ROOT / "llm_adapters/google-gemini.json")
    assert gemini.parse_response({"promptFeedback": {"blockReason": "SAFETY"}}).finish_reason == "SAFETY"
    compatible = load_json_adapter(ROOT / "llm_adapters/openai-compatible.json")
    event = {"error": {"code": "server_error", "message": "disconnected"}, "choices": [{"finish_reason": "error", "delta": {"content": ""}}]}
    assert compatible.stream_error_details(event).message == "disconnected"


def test_anthropic_usage_includes_cached_input_and_total():
    adapter = load_json_adapter(ROOT / "llm_adapters/anthropic.json")
    usage = adapter.extract_usage({"usage": {"input_tokens": 50, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 100000, "output_tokens": 20}})
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (100050, 20, 100070)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["openai-responses", "openai-compatible"])
async def test_stream_refusal_rejects_partial_body_without_retry(tmp_path, name):
    from tests.test_llm_streaming import streaming_config, run_client, stream_response
    current = streaming_config(tmp_path)
    current["_llm_adapter"] = load_json_adapter(ROOT / "llm_adapters" / f"{name}.json")
    calls = []
    events = ([{"type": "response.output_text.delta", "delta": "partial"}, {"type": "response.refusal.delta", "delta": "no"}, {"type": "response.completed"}]
              if name == "openai-responses" else [{"choices": [{"delta": {"content": "partial"}}]}, {"choices": [{"delta": {"refusal": "no"}, "finish_reason": "stop"}]}, "[DONE]"])
    def handler(request):
        calls.append(request)
        return stream_response(*events)
    with pytest.raises(ExternalError, match="拒绝"):
        await run_client(current, tmp_path, handler)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_gemini_stream_collects_all_parts_and_usage_after_finish(tmp_path):
    from tests.test_llm_streaming import streaming_config, run_client, stream_response
    current = streaming_config(tmp_path)
    current["_llm_adapter"] = load_json_adapter(ROOT / "llm_adapters/google-gemini.json")
    current["llm"]["stream_endpoint"] = ""
    def handler(request):
        return stream_response(
            {"candidates": [{"content": {"parts": [{"text": "ignore", "thought": True}, {"text": "first"}, {"text": "second"}]}, "finishReason": "STOP"}]},
            {"usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2, "totalTokenCount": 5}},
        )
    response, llm, client = await run_client(current, tmp_path, handler)
    await client.aclose()
    assert response.content == "firstsecond"
    assert llm.usage_summary()["total_tokens"] == 5
