from __future__ import annotations

import asyncio

import httpx
import pytest

from app.decision import DecisionClient, DecisionQuestion, validate_decision_preset
from app.errors import ConfigError, ExternalError


def preset(protocol: str = "typesafe") -> dict:
    return dict(preset_id="local", protocol=protocol,
                url="http://localhost:9876/custom/path", model="test-model",
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
