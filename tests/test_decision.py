from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.decision import DecisionClient, DecisionQuestion, validate_decision_preset
from app.errors import ConfigError, ExternalError
from app.diagnostics import Diagnostics, DiagnosticsHub


@pytest.mark.parametrize("diagnostics_type", [Diagnostics, DiagnosticsHub])
def test_decision_diagnostics_retains_counts_after_detail_eviction(tmp_path, diagnostics_type):
    diagnostics = diagnostics_type(tmp_path / "app.log")
    with diagnostics.activate("project", "translation", task_id="TASK"):
        diagnostics.begin_request(request_id="LLM", model="generator", messages=[], max_attempts=1)
        diagnostics.request_finished(request_id="LLM", attempt=1, latency_seconds=.01, status=200, error=False)
        diagnostics.record_request_usage("LLM", {"input_tokens": 22, "output_tokens": 2, "total_tokens": 24}, complete=True)
        diagnostics.complete_request("LLM", content="translation", reasoning_content=None)
        for index in range(201):
            request_id = f"DEC-{index}"
            diagnostics.begin_request(request_id=request_id, model="judge", messages=[], max_attempts=1,
                                      request_kind="decision", segment_id="SEG", question_count=2)
            diagnostics.request_started(request_id)
            diagnostics.request_finished(request_id=request_id, attempt=1, latency_seconds=.01,
                                         status=200, error=False)
            diagnostics.record_request_usage(request_id, {"input_tokens": 10, "output_tokens": 0, "total_tokens": 10}, complete=True)
            diagnostics.complete_request(request_id, content="{}", reasoning_content=None)
        snapshot = diagnostics.snapshot()
        assert snapshot["metrics"]["total_requests"] == 1
        assert snapshot["decision"]["metrics"]["total_requests"] == 201
        assert snapshot["decision"]["activities"][0]["completed"] == 201
        assert snapshot["decision"]["activities"][0]["questions"] == 402
        assert len(snapshot["requests"]["items"]) == 201
        assert snapshot["request_usage"]["decision"]["input_tokens"] == 2010
        assert snapshot["request_usage"]["llm"] == {"input_tokens": 22, "output_tokens": 2, "total_tokens": 24, "available": True, "partial": False}
        assert diagnostics.request_detail("LLM")["response_content"] == "translation"
        with pytest.raises(ValueError):
            diagnostics.request_detail("DEC-0")


def test_decision_diagnostics_tracks_retry_and_preflight_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("DECISION_TEST_KEY", "secret")
    diagnostics = Diagnostics(tmp_path / "app.log")
    responses = iter([httpx.Response(429), httpx.Response(200, json={
        "model": "test-model", "answers": {"term": {"type": "refusal"}},
    })])
    async def run():
        with diagnostics.activate("project", "translation"):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: next(responses))) as http:
                value = preset()
                client = DecisionClient(value, http_client=http,
                                        retry={"http_max_attempts": 2, "base_delay_seconds": 0})
                await client.choose("evidence", [DecisionQuestion("term", "Question", {"a": "A", "b": "B"})], segment_id="SEG")
                value.update(context_window_tokens=513, context_safety_margin_tokens=512)
                with pytest.raises(ExternalError):
                    await client.choose("evidence", [DecisionQuestion("term", "Question", {"a": "A", "b": "B"})], segment_id="SEG")
            snapshot = diagnostics.snapshot()
            assert snapshot["metrics"]["total_requests"] == 0
            assert snapshot["decision"]["metrics"]["retry_count"] == 1
            assert snapshot["decision"]["activities"][0]["completed"] == 1
            assert snapshot["decision"]["activities"][0]["failed"] == 1
            items = snapshot["requests"]["items"]
            assert [item["attempt_count"] for item in items] == [2, 0]
            assert items[0]["segment_id"] == "SEG"
            import json
            assert json.loads(diagnostics.request_detail(items[0]["request_id"])["request_body"])["state"] == "evidence"
            assert "secret" not in json.dumps(diagnostics.request_detail(items[0]["request_id"]))
    asyncio.run(run())


def preset(protocol: str = "typesafe") -> dict:
    return dict(preset_id="local", protocol=protocol,
                url="http://localhost:9876/custom/path", model="test-model", proxy_url="",
                credential={"kind": "environment", "name": "DECISION_TEST_KEY"},
                context_window_tokens=32000, context_safety_margin_tokens=512, token_safety_factor=1.25,
                request_timeout_seconds=10, requests_per_minute=0, max_parallel=2)


@pytest.mark.parametrize("protocol", ["typesafe", "openai-decisions"])
@pytest.mark.parametrize("part", ["state", "instructions", "choices", "margin", "factor"])
def test_oversized_decision_fails_before_http(protocol, part, monkeypatch):
    monkeypatch.setenv("DECISION_TEST_KEY", "secret")
    value = preset(protocol)
    value.update(context_window_tokens=1000, context_safety_margin_tokens=100)
    if part == "margin":
        value.update(context_window_tokens=4000, context_safety_margin_tokens=3999)
    if part == "factor":
        value["token_safety_factor"] = 100
    state = "正文" * 1000 if part == "state" else "short"
    question = DecisionQuestion("term", "说明" * 1000 if part == "instructions" else "Question",
                                {"a": "选项" * 1000 if part == "choices" else "A", "b": "B"})
    async def run():
        def unexpected_request(request):
            pytest.fail("Oversized Decision must not send HTTP")
        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as http:
            client = DecisionClient(value, http_client=http)
            with pytest.raises(ExternalError, match="Decision.*Token"):
                await client.choose(state, [question], segment_id="SEG-1")
            assert client.pool.active == 0
    asyncio.run(run())


@pytest.mark.parametrize("protocol", ["typesafe", "openai-decisions"])
@pytest.mark.parametrize("recent,expected", [("near", ["near"]), ("近" * 1000, [])])
def test_decision_removes_oldest_context_until_request_fits(protocol, recent, expected, monkeypatch, tmp_path):
    import json
    monkeypatch.setenv("DECISION_TEST_KEY", "secret")
    value = preset(protocol)
    value.update(context_window_tokens=300, context_safety_margin_tokens=0)
    state = {"source": "current"}
    diagnostics = Diagnostics(tmp_path / "app.log")
    context = ["遠" * 1000, recent]
    question = DecisionQuestion("term", "Question", {"a": "A", "b": "B"})
    def respond(request):
        body = json.loads(request.content)
        evidence = body["state"] if protocol == "typesafe" else json.loads(body["input"])
        assert evidence["source"] == "current"
        assert evidence["reference_context"] == expected
        answers = {"term": {"type": "refusal"}} if protocol == "typesafe" else [{"name": "term", "type": "refusal"}]
        return httpx.Response(200, json={"model": "test-model", "answers": answers})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = DecisionClient(value, http_client=http)
            with diagnostics.activate("project", "translation"):
                await client.choose(state, [question], reference_context=context, segment_id="SEG")
            assert client.records[0]["context_segments_requested"] == 2
            assert client.records[0]["context_segments_used"] == len(expected)
    asyncio.run(run())
    assert state == {"source": "current"}
    assert context == ["遠" * 1000, recent]
    assert any("SEG" in log["message"] for log in diagnostics.snapshot()["logs"])


@pytest.mark.parametrize("window,margin", [(0, 0), (1000, -1), (1000, 1000), (1000, True)])
def test_preset_rejects_invalid_context_budget(window, margin):
    value = preset()
    value.update(context_window_tokens=window, context_safety_margin_tokens=margin)
    with pytest.raises(ConfigError):
        validate_decision_preset(value)


@pytest.mark.parametrize("factor", [0, -1, True, "1.25", float("inf"), float("nan")])
def test_preset_rejects_invalid_token_safety_factor(factor):
    value = preset()
    value["token_safety_factor"] = factor
    with pytest.raises(ConfigError, match="token_safety_factor"):
        validate_decision_preset(value)


@pytest.mark.parametrize("protocol", ["typesafe", "openai-decisions"])
def test_choice_request_uses_full_url_and_maps_answers(protocol, monkeypatch, tmp_path):
    monkeypatch.setenv("DECISION_TEST_KEY", "secret")
    question = DecisionQuestion("term", "Use this name?", {"required": "Yes", "ordinary": "No"})
    def respond(request):
        import json
        body = json.loads(request.content)
        assert str(request.url) == "http://localhost:9876/custom/path"
        assert request.headers["Authorization"] == "Bearer secret"
        answer = dict(type="choice", choice="required", confidence=0.9)
        if protocol == "typesafe":
            assert body["state"] == {"source": "text"}
            assert body["questions"]["term"]["criteria"] == question.choices
            answer["probabilities"] = {"required": 0.95, "ordinary": 0.05}
            answers = {"term": answer}
        else:
            assert body["questions"][0]["name"] == "term"
            answer.update(name="term", probabilities=[dict(value="required", probability=0.95), dict(value="ordinary", probability=0.05)])
            answers = [answer]
        return httpx.Response(200, json=dict(model="actual-model", answers=answers, usage=dict(input_tokens=10, output_tokens=85)))
    async def run():
        diagnostics = Diagnostics(tmp_path / "app.log")
        with diagnostics.activate("project", "translation"):
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
                client = DecisionClient(validate_decision_preset(preset(protocol)), http_client=http)
                result = await client.choose({"source": "text"}, [question])
                assert result["term"].choice == "required"
                assert result["term"].confidence == 0.9
                assert client.records[0]["model"] == "actual-model"
                assert client.records[0]["usage"]["input_tokens"] == 10
                assert client.usage_summary()["output_tokens"] == 0
                snapshot = diagnostics.snapshot()
                assert snapshot["requests"]["items"][0]["usage"] == {"input_tokens": 10, "output_tokens": 0, "total_tokens": 10, "available": True, "partial": False}
                assert snapshot["request_usage"]["decision"]["input_tokens"] == 10
                assert snapshot["request_usage"]["llm"]["input_tokens"] == 0
    asyncio.run(run())


def test_preset_rejects_credentials_in_url():
    value = preset()
    value["url"] = "https://secret@example.com/path"
    with pytest.raises(ConfigError):
        validate_decision_preset(value)


def test_invalid_answer_fails_without_fallback(monkeypatch):
    monkeypatch.setenv("DECISION_TEST_KEY", "secret")
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"answers": {}}))) as http:
            client = DecisionClient(validate_decision_preset(preset()), http_client=http)
            with pytest.raises(ExternalError):
                await client.choose("source", [DecisionQuestion("term", "Question", {"a": "A", "b": "B"})])
    asyncio.run(run())


@pytest.mark.parametrize("protocol,invalid", [
    ("typesafe", "empty"), ("typesafe", "json"),
    ("openai-decisions", "missing"), ("openai-decisions", "fields"),
])
@pytest.mark.parametrize("exhausted", [False, True])
def test_invalid_decision_response_uses_retry_budget(tmp_path, monkeypatch, protocol, invalid, exhausted):
    monkeypatch.setenv("DECISION_TEST_KEY", "secret")
    diagnostics = Diagnostics(tmp_path / "app.log")
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1 or exhausted:
            if invalid in {"empty", "json"}:
                return httpx.Response(200, content=b"" if invalid == "empty" else b"not JSON")
            answers = [] if invalid == "missing" else [{"name": "term", "type": "choice"}]
            return httpx.Response(200, json={"model": "test-model", "answers": answers, "usage": {"input_tokens": 10, "output_tokens": 99}})
        answers = {"term": {"type": "refusal"}} if protocol == "typesafe" else [{"name": "term", "type": "refusal"}]
        return httpx.Response(200, json={"model": "test-model", "answers": answers, "usage": {"input_tokens": 20, "output_tokens": 85}})

    async def run():
        with diagnostics.activate("project", "translation"):
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
                client = DecisionClient(preset(protocol), http_client=http, debug_directory=tmp_path / "debug",
                                        retry={"http_max_attempts": 2, "base_delay_seconds": 0})
                question = DecisionQuestion("term", "Question", {"a": "A", "b": "B"})
                if exhausted:
                    with pytest.raises(ExternalError, match="响应无效.*重试预算耗尽"):
                        await client.choose("source", [question])
                else:
                    assert (await client.choose("source", [question]))["term"].refused
                assert len(calls) == len(client.records) == 2
                assert client.pool.active == 0
            snapshot = diagnostics.snapshot()
            assert snapshot["decision"]["metrics"]["retry_count"] == 1
            item = snapshot["requests"]["items"][0]
            assert item["attempt_count"] == 2
            expected_input = (0 if invalid in {"empty", "json"} else 10) * (2 if exhausted else 1) + (0 if exhausted else 20)
            assert item["usage"]["input_tokens"] == expected_input
            assert item["usage"]["output_tokens"] == 0
            assert snapshot["request_usage"]["decision"] == item["usage"]
            assert client.usage_summary()["input_tokens"] == expected_input
            detail = diagnostics.request_detail(item["request_id"])
            assert detail["attempts"][0]["outcome"] == "response_parse_error"
            attempts = [json.loads(line) for line in (tmp_path / "debug" / "attempts.jsonl").read_text().splitlines()]
            assert [attempt["outcome"] for attempt in attempts] == [
                "response_parse_error", "response_parse_error" if exhausted else "succeeded",
            ]
    asyncio.run(run())


def test_retry_and_refusal_are_recorded(monkeypatch):
    monkeypatch.setenv("DECISION_TEST_KEY", "secret")
    responses = iter([httpx.Response(429), httpx.Response(200, json={
        "model": "test-model", "answers": {"term": {"type": "refusal"}},
    })])
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: next(responses))) as http:
            client = DecisionClient(preset(), http_client=http,
                                    retry={"http_max_attempts": 2, "base_delay_seconds": 0})
            result = await client.choose("source", [DecisionQuestion("term", "Question", {"a": "A", "b": "B"})])
            assert result["term"].refused
            assert [record["http_status"] for record in client.records] == [429, 200]
            assert client.pool.active == 0
    asyncio.run(run())


def test_cancellation_releases_request_lease(tmp_path, monkeypatch):
    monkeypatch.setenv("DECISION_TEST_KEY", "secret")
    diagnostics = Diagnostics(tmp_path / "app.log")
    async def run():
        entered = asyncio.Event()
        async def respond(request):
            entered.set()
            await asyncio.Event().wait()
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = DecisionClient(preset(), http_client=http)
            task = asyncio.create_task(client.choose("source", [DecisionQuestion("term", "Question", {"a": "A", "b": "B"})]))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert client.pool.active == 0
            assert client.records[0]["error"] == "cancelled"
            snapshot = diagnostics.snapshot()
            assert snapshot["decision"]["metrics"]["active_requests"] == 0
            assert snapshot["decision"]["metrics"]["http_errors"] == 0
            assert snapshot["decision"]["activities"][0]["interrupted"] == 1
    with diagnostics.activate("project", "translation"):
        asyncio.run(run())


def test_http_proxy_carries_decision_request(monkeypatch):
    monkeypatch.setenv("DECISION_TEST_KEY", "secret")
    async def run():
        requests = []
        async def respond(reader, writer):
            try:
                headers = await reader.readuntil(b"\r\n\r\n")
                requests.append(headers.decode())
                length = next(int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n") if line.lower().startswith(b"content-length:"))
                await reader.readexactly(length)
                body = b'{"model":"test-model","answers":{"term":{"type":"refusal"}}}'
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        async with await asyncio.start_server(respond, "127.0.0.1", 0) as server:
            value = preset()
            value.update(url="http://decision.invalid/v1/choose", proxy_url=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}")
            client = DecisionClient(validate_decision_preset(value))
            answers = await client.choose("source", [DecisionQuestion("term", "Question", {"a": "A", "b": "B"})])
            assert answers["term"].refused
            assert requests[0].startswith("POST http://decision.invalid/v1/choose HTTP/1.1\r\n")
    asyncio.run(run())

@pytest.mark.parametrize("protocol", ["typesafe", "openai-decisions"])
@pytest.mark.parametrize("debug_enabled", [False, True])
def test_decision_history_records_actual_input_and_refusal(tmp_path, monkeypatch, protocol, debug_enabled):
    import json
    from app.web_task_routes import _debug_request_index, _debug_request_detail
    monkeypatch.setenv("DECISION_TEST_KEY", "secret")
    answers = {"term": {"type": "refusal"}} if protocol == "typesafe" else [{"name": "term", "type": "refusal"}]
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"model": "judge", "answers": answers}))) as http:
            client = DecisionClient(preset(protocol), http_client=http, debug_directory=tmp_path if debug_enabled else None,
                                    project_id="PROJECT", run_id="RUN", stage="translation")
            await client.choose({"source": "Alice came", "translation": "她来了", "terms": {"term": {"source": "Alice"}}},
                                [DecisionQuestion("term", "Question", {"a": "A", "b": "B"})], segment_id="SEG")
    asyncio.run(run())
    if not debug_enabled:
        assert not (tmp_path / "payloads").exists()
        return
    index = _debug_request_index(tmp_path, "RUN")
    assert len(index["items"]) == 1
    detail = _debug_request_detail(tmp_path, "RUN", index["items"][0]["request_id"], full=True)
    attempt = detail["attempts"][0]
    overview = attempt["overview"]["value"]
    assert overview["request_kind"] == "decision"
    assert overview["input"]["source"] == "Alice came"
    assert overview["answers"]["term"]["refused"] is True
    assert "secret" not in json.dumps(detail)

@pytest.mark.parametrize("task_id", [None, "TASK"])
def test_decision_hub_publishes_answers_in_incremental_summary(tmp_path, task_id):
    diagnostics = DiagnosticsHub(tmp_path / "app.log")
    answer = {"term": {"choice": "a", "confidence": .9, "probabilities": {"a": .9, "b": .1}, "refused": False}}
    with diagnostics.activate("project", "translation", task_id=task_id):
        diagnostics.begin_request(request_id="DEC", model="judge", messages=[], max_attempts=1,
            request_kind="decision", overview={"request_kind": "decision", "input": {"source": "Alice"}, "answers": {}})
        diagnostics.complete_request("DEC", content="response", reasoning_content=None, overview_answers=answer)
        assert diagnostics.snapshot()["requests"]["items"][0]["overview"]["answers"] == answer


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("schema_enabled", [False, True])
@pytest.mark.parametrize("returned_model", [None, "untrusted-model"])
def test_llm_decision_uses_frozen_resources_and_retries_invalid_answers(tmp_path, monkeypatch, stream, returned_model, schema_enabled):
    from app.config import load_config
    from app.decision import load_decision_preset
    from app.decision_llm import resolve_llm_resources
    from app.sqlite_storage import atomic_write_json
    from tests.test_foundation import make_app_root

    root = make_app_root(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "secret")
    selected_path = root / "llm_presets" / "default.json"
    selected = json.loads(selected_path.read_text())
    selected.update(stream=stream, requests_per_minute=0, input_tokens_per_minute=0, json_schema_enabled=schema_enabled)
    selected_path.write_text(json.dumps(selected))
    value = dict(preset_id="judge", protocol="llm", llm_preset="default", prompt_language="en")
    validate_decision_preset(value)
    value = resolve_llm_resources(value, root)
    snapshot = tmp_path / "decision_presets" / "judge.json"
    atomic_write_json(snapshot, value)
    frozen = load_decision_preset(snapshot, snapshot=True)
    (root / "prompts" / "decision_llm.en.middle.txt").write_text("Changed prompt")
    attempts = []
    def respond(request):
        payload = json.loads(request.content)
        attempts.append(payload)
        if schema_enabled:
            schema = payload["response_format"]["json_schema"]["schema"]
            assert schema["required"] == ["answers"]
            answers_schema = schema["properties"]["answers"]
            assert answers_schema["required"] == ["term"]
            choice, refusal = answers_schema["properties"]["term"]["anyOf"]
            assert choice["properties"]["choice"]["enum"] == ["a", "b"]
            assert choice["properties"]["probabilities"]["required"] == ["a", "b"]
            assert refusal["properties"]["type"]["enum"] == ["refusal"]
        else:
            assert "response_format" not in payload
        assert "Changed prompt" not in json.dumps(payload)
        evidence = json.loads(payload["messages"][-1]["content"])
        assert set(evidence) == {"state", "questions"}
        assert evidence["state"] == {"text": "Evidence"}
        assert evidence["questions"]["term"]["criteria"] == {"a": "A", "b": "B"}
        probabilities = {"a": 0.9, "b": 0.1} if len(attempts) > 1 else {"a": 0.9}
        answer = {"answers": {"term": {
            "type": "choice", "choice": "a", "probabilities": probabilities, "confidence": 0.9}}}
        if returned_model is not None:
            answer["model"] = returned_model
        if stream:
            from tests.test_llm_streaming import stream_response
            return stream_response({"choices": [{"delta": {"content": json.dumps(answer)}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}, "[DONE]")
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
    async def run():
        diagnostics = Diagnostics(tmp_path / "llm.log")
        with diagnostics.activate("project", "translation"):
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
                retry = load_config(root / "config" / "config.toml")["retry"]
                retry["http_max_attempts"] = 2
                client = DecisionClient(frozen, http_client=http, retry=retry, debug_directory=tmp_path / "debug")
                result = await client.choose({"text": "Evidence"}, [DecisionQuestion("term", "Choose", {"a": "A", "b": "B"})])
                assert result["term"].choice == "a"
                assert client.usage_summary()["total_tokens"] == 30
                assert diagnostics.snapshot()["request_usage"]["decision"] == {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30, "available": True, "partial": False}
                assert diagnostics.snapshot()["request_usage"]["llm"]["input_tokens"] == 0
                assert len(client.records) == 2
                assert client.records[-1]["model"] == "example-model"
                assert diagnostics.snapshot()["decision"]["metrics"]["total_requests"] == 2
                assert diagnostics.snapshot()["metrics"]["total_requests"] == 0
                activity = diagnostics.snapshot()["decision"]["activities"][0]
                assert activity["completed"] == 1
                assert activity["failed"] == 1
    asyncio.run(run())
    assert len(attempts) == 2
    debug = tmp_path / "debug"
    records = [json.loads(line) for line in (debug / "attempts.jsonl").read_text().splitlines()]
    assert [r["status"] for r in records] == ["failed", "completed"]
    assert [r["outcome"] for r in records] == ["response_parse_error", "succeeded"]
    failed, completed = [json.loads((debug / "payloads" / f"{r['record_id']}.overview.json").read_text()) for r in records]
    assert failed["answers"] == {}
    assert failed["error"] == "response_parse_error"
    assert completed["answers"]["term"]["choice"] == "a"
    assert completed["error"] is None
    error = json.loads((debug / "payloads" / f"{records[0]['record_id']}.error.json").read_text())
    assert error["outcome"] == "response_parse_error"


def test_llm_decision_http_and_parse_errors_share_send_budget(tmp_path, monkeypatch):
    from app.config import load_config
    from app.decision_llm import resolve_llm_resources
    from tests.test_foundation import make_app_root

    root = make_app_root(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "secret")
    value = resolve_llm_resources(dict(preset_id="judge", protocol="llm", llm_preset="default", prompt_language="en"), root)
    attempts = []
    def respond(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            retry = load_config(root / "config" / "config.toml")["retry"]
            retry["http_max_attempts"] = 2
            client = DecisionClient(value, http_client=http, retry=retry)
            with pytest.raises(ExternalError, match="Decision 响应无效，重试预算耗尽"):
                await client.choose("Evidence", [DecisionQuestion("term", "Choose", {"a": "A", "b": "B"})])
    asyncio.run(run())
    assert len(attempts) == 2


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("content", ['{"answers":{"q":{"type":"refusal"}}}', '{}', ''])
def test_llm_decision_debug_records_final_outcome(tmp_path, monkeypatch, stream, content):
    from app.decision_llm import resolve_llm_resources
    from tests.test_foundation import make_app_root

    root = make_app_root(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "secret")
    preset = resolve_llm_resources(dict(preset_id="judge", protocol="llm", llm_preset="default", prompt_language="en"), root)
    preset["_resources"]["preset"]["stream"] = stream

    def respond(request):
        if stream:
            from tests.test_llm_streaming import stream_response
            return stream_response({"choices": [{"delta": {"content": content}, "finish_reason": "stop"}]}, "[DONE]")
        return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = DecisionClient(preset, http_client=http, debug_directory=tmp_path / "debug")
            question = DecisionQuestion("q", "Choose", {"a": "A", "b": "B"})
            if "refusal" in content:
                assert (await client.choose("Evidence", [question]))["q"].refused
            else:
                with pytest.raises(ExternalError):
                    await client.choose("Evidence", [question])

    asyncio.run(run())
    debug = tmp_path / "debug"
    records = [json.loads(line) for line in (debug / "attempts.jsonl").read_text().splitlines()]
    assert len(records) == 1
    record = records[0]
    overview = json.loads((debug / "payloads" / f"{record['record_id']}.overview.json").read_text())
    if "refusal" in content:
        assert record["status"] == "completed"
        assert record["outcome"] == "succeeded"
        assert overview["answers"]["q"]["refused"] is True
    else:
        error = "response_parse_error" if content else "empty_response_unknown"
        assert record["status"] == "failed"
        assert record["outcome"] == overview["error"] == error


def test_llm_decision_schema_is_included_in_context_preflight(tmp_path):
    from app.decision_llm import resolve_llm_resources
    from tests.test_foundation import make_app_root

    root = make_app_root(tmp_path)
    value = resolve_llm_resources(dict(preset_id='judge', protocol='llm', llm_preset='default', prompt_language='en'), root)
    questions = [DecisionQuestion(f'q_{i}', 'Choose', {'a':'A', 'b':'B'}) for i in range(10)]
    plain = DecisionClient(value)
    _, plain_estimate = plain.request_plan('Evidence', questions)
    value['_resources']['preset']['json_schema_enabled'] = True
    client = DecisionClient(value)
    _, schema_estimate = client.request_plan('Evidence', questions)
    assert schema_estimate > plain_estimate
    client.preset['context_window_tokens'] = plain_estimate + client.preset['context_safety_margin_tokens']
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail('Must fail before HTTP'))) as http:
            client.http_client = http
            with pytest.raises(ExternalError, match='超过可用上下文'):
                await client.choose('Evidence', questions)
    asyncio.run(run())
