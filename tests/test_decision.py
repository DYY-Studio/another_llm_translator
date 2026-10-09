from __future__ import annotations

import asyncio

import httpx
import pytest

from app.decision import DecisionClient, DecisionQuestion, validate_decision_preset
from app.errors import ConfigError, ExternalError


def preset(protocol: str = "typesafe") -> dict:
    return dict(preset_id="local", protocol=protocol,
                url="http://localhost:9876/custom/path", model="test-model", proxy_url="",
                credential={"kind": "environment", "name": "DECISION_TEST_KEY"},
                request_timeout_seconds=10, requests_per_minute=0, max_parallel=2)


@pytest.mark.parametrize("protocol", ["typesafe", "openai-decisions"])
def test_choice_request_uses_full_url_and_maps_answers(protocol, monkeypatch):
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
        return httpx.Response(200, json=dict(model="actual-model", answers=answers, usage=dict(input_tokens=10, output_tokens=0)))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = DecisionClient(validate_decision_preset(preset(protocol)), http_client=http)
            result = await client.choose({"source": "text"}, [question])
            assert result["term"].choice == "required"
            assert result["term"].confidence == 0.9
            assert client.records[0]["model"] == "actual-model"
            assert client.records[0]["usage"]["input_tokens"] == 10
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


def test_cancellation_releases_request_lease(monkeypatch):
    monkeypatch.setenv("DECISION_TEST_KEY", "secret")
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
