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
    usage = adapter.extract_usage({"usage": {"input_tokens": 50, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 100000, "output_tokens": 20, "output_tokens_details": {"thinking_tokens": 10}}})
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (100050, 20, 100070)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["openai-responses", "openai-compatible"])
async def test_stream_refusal_rejects_partial_body_without_retry(tmp_path, name):
    from tests.test_llm_streaming import streaming_config, run_client, stream_response
    current = streaming_config(tmp_path)
    current["_llm_adapter"] = load_json_adapter(ROOT / "llm_adapters" / f"{name}.json")
    calls = []
    events = ([{"type": "response.output_text.delta", "delta": "partial"}, {"type": "response.refusal.delta", "delta": "no"}, {"type": "error", "message": "error after refusal"}]
              if name == "openai-responses" else [{"choices": [{"delta": {"content": "partial"}}]}, {"choices": [{"delta": {"refusal": "no"}}]}, {"choices": [{"delta": {}, "finish_reason": "stop"}]}, "[DONE]"])
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


@pytest.mark.parametrize("name", ["anthropic", "google-gemini"])
def test_model_discovery_fetches_all_pages_and_filters_generation(tmp_path, monkeypatch, name):
    import json
    import httpx
    from fastapi.testclient import TestClient
    from app.web import create_app
    from tests.test_web import make_project
    root, _ = make_project(tmp_path)
    client = TestClient(create_app(projects_root=root))
    preset = json.loads((ROOT / "llm_presets/default.json").read_text())
    preset.update(adapter_id=name, base_url="https://models.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "test")
    calls = []
    def handler(request):
        calls.append(request)
        if name == "anthropic":
            return httpx.Response(200, json={"data": [{"id": "second" if request.url.params.get("after_id") else "first"}], "has_more": not bool(request.url.params.get("after_id")), "last_id": "cursor"})
        if request.url.params.get("pageToken"):
            return httpx.Response(200, json={"models": [{"name": "models/second", "supportedGenerationMethods": ["generateContent"]}]})
        return httpx.Response(200, json={"models": [{"name": "models/embedding", "supportedGenerationMethods": ["embedContent"]}, {"name": "models/first", "supportedGenerationMethods": ["generateContent"]}], "nextPageToken": "cursor"})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)))
    response = client.post("/api/v1/global/presets/default/models", json=preset)
    assert response.status_code == 200
    assert [model["id"] for model in response.json()["models"]] == ["first", "second"]
    assert len(calls) == 2


@pytest.mark.parametrize("failure", ["http", "cursor"])
def test_discovery_does_not_return_a_partial_list(tmp_path, monkeypatch, failure):
    import json
    import httpx
    from fastapi.testclient import TestClient
    from app.web import create_app
    from tests.test_web import make_project
    root, _ = make_project(tmp_path)
    client = TestClient(create_app(projects_root=root))
    preset = json.loads((ROOT / "llm_presets/default.json").read_text())
    preset.update(adapter_id="anthropic", base_url="https://models.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "test")
    def handler(request):
        if failure == "http" and request.url.params.get("after_id"):
            return httpx.Response(503)
        return httpx.Response(200, json={"data": [{"id": "first"}], "has_more": True, "last_id": "cursor"})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)))
    response = client.post("/api/v1/global/presets/default/models", json=preset)
    assert response.status_code == 400
    assert "models" not in response.json()
    assert ("HTTP 503" if failure == "http" else "重复分页游标") in response.json()["error"]


def test_unknown_responses_incomplete_fails_even_with_body():
    adapter = load_json_adapter(ROOT / "llm_adapters/openai-responses.json")
    response = {"status": "incomplete", "incomplete_details": {"reason": "unknown"}, "output": [{"type": "message", "content": [{"type": "output_text", "text": "partial"}]}]}
    with pytest.raises(ExternalError, match="未完成.*unknown"):
        adapter.parse_response(response)
    assert adapter.stream_error_details({"type": "response.incomplete", "response": response}).message.endswith("unknown")


def test_gemini_usage_includes_thinking_output():
    adapter = load_json_adapter(ROOT / "llm_adapters/google-gemini.json")
    payload = {"usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2, "thoughtsTokenCount": 5, "totalTokenCount": 10}}
    usage = adapter.extract_usage(payload)
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (3, 7, 10)
    assert adapter.extract_stream_usage(payload)["output_tokens"] == 7


@pytest.mark.parametrize(("name", "payload"), [
    ("openai-compatible", {"usage": {"prompt_tokens": -1}}),
    ("openai-responses", {"response": {"usage": {"output_tokens": "2"}}}),
    ("anthropic", {"type": "message_start", "message": {"usage": {"input_tokens": 2, "cache_read_input_tokens": -1}}}),
    ("google-gemini", {"usageMetadata": {"thoughtsTokenCount": -1}}),
])
def test_native_stream_rejects_invalid_token_counts(name, payload):
    adapter = load_json_adapter(ROOT / "llm_adapters" / f"{name}.json")
    with pytest.raises(ExternalError, match="Token"):
        adapter.extract_stream_usage(payload)


@pytest.mark.asyncio
async def test_anthropic_stream_keeps_final_thinking_usage(tmp_path):
    from tests.test_llm_streaming import streaming_config, run_client, stream_response
    current = streaming_config(tmp_path)
    current["_llm_adapter"] = load_json_adapter(ROOT / "llm_adapters/anthropic.json")
    def handler(request):
        return stream_response(
            {"type": "message_start", "message": {"usage": {"input_tokens": 2, "cache_read_input_tokens": 3, "output_tokens": 1}}},
            {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "thinking"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "ok"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7, "output_tokens_details": {"thinking_tokens": 5}}},
            {"type": "message_stop"},
        )
    response, llm, client = await run_client(current, tmp_path, handler)
    await client.aclose()
    assert response.content == "ok"
    usage = llm.usage_summary()
    assert (usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]) == (5, 7, 12)


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled,declares_schema,reject_schema', [(True, True, False), (True, False, False), (False, True, False), (True, True, True)])
async def test_generic_client_only_sends_schema_when_preset_and_task_enable_it(tmp_path, monkeypatch, enabled, declares_schema, reject_schema):
    import json
    import httpx
    from app.diagnostics import DiagnosticsHub
    from app.config import load_global_config
    from app.llm_client import LLMClient, SlidingWindowLimiter

    monkeypatch.setenv('LLM_API_KEY', 'synthetic-key')
    config = load_global_config(ROOT)
    config['llm']['json_schema_enabled'] = enabled
    config['retry']['http_max_attempts'] = 1
    schema = {'type':'object', 'properties':{'result':{'type':'string'}}, 'required':['result'], 'additionalProperties':False}
    def respond(request):
        body = json.loads(request.content)
        if enabled and declares_schema:
            assert body['response_format']['json_schema']['schema'] == schema
        else:
            assert 'response_format' not in body
        if reject_schema:
            return httpx.Response(400, json={'error':{'message':'JSON Schema unsupported'}})
        return httpx.Response(200, json={'choices':[{'message':{'content':'{"result":"ok"}'}, 'finish_reason':'stop'}]})
    diagnostics = DiagnosticsHub(tmp_path / 'app.log')
    with diagnostics.activate('P', 'translation', task_id='TASK'):
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            async with LLMClient(config, SlidingWindowLimiter(0, 0), run_dir=tmp_path,
                                 project_id='P', run_id='R', stage='translation', client=http) as client:
                if reject_schema:
                    with pytest.raises(ExternalError, match='HTTP 400'):
                        await client.chat(messages=[{'role':'user','content':'Return JSON'}], temperature=0,
                                          estimated_input_tokens=100, json_schema=schema)
                    assert client.send_count == 1
                    return
                response, request_id = await client.chat(messages=[{'role':'user', 'content':'Return JSON'}],
                    temperature=0, estimated_input_tokens=100, json_schema=schema if declares_schema else None)
                assert json.loads(response.content) == {'result':'ok'}
            detail = diagnostics.request_detail(request_id)
            if enabled and declares_schema:
                assert json.loads(detail['request_body'])['response_format']['json_schema']['schema'] == schema
                assert 'synthetic-key' not in detail['request_body']
            else:
                assert detail['request_body'] is None
