from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from app.config import load_global_config
from app.diagnostics import Diagnostics
from app.errors import ExternalError
from app.execution import LLMClient, SlidingWindowLimiter, render_messages
from app.llm_adapter import load_json_adapter

ROOT = Path(__file__).parents[1]


class ByteChunks(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


def event(value: object) -> str:
    if isinstance(value, str):
        data = value
    else:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return f"data: {data}\n\n"


def stream_response(*values: object, line_ending: str = "\n") -> httpx.Response:
    body = "".join(
        event(value).replace("\n", line_ending) for value in values
    ).encode("utf-8")
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream; charset=utf-8"},
        content=body,
    )


def streaming_config(tmp_path: Path) -> dict:
    current = load_global_config(ROOT)
    current["llm"]["stream"] = True
    current["llm"]["stream_endpoint"] = ""
    current["retry"]["http_max_attempts"] = 1
    current["retry"]["base_delay_seconds"] = 0
    current["retry"]["jitter_seconds"] = 0
    current["debug"]["enabled"] = False
    current["llm"]["request_timeout_seconds"] = 1
    return current


async def run_client(
    current: dict,
    tmp_path: Path,
    handler,
    *,
    diagnostics: Diagnostics | None = None,
) -> tuple[object, LLMClient, httpx.AsyncClient]:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    os.environ["LLM_API_KEY"] = "test-key"
    llm = LLMClient(
        current,
        SlidingWindowLimiter(0, 0),
        run_dir=tmp_path / "run",
        project_id="PRJ",
        run_id="RUN",
        stage="translation",
        client=client,
        on_usage=diagnostics.set_usage if diagnostics is not None else None,
    )
    context = diagnostics.activate("sample", "translation") if diagnostics else None
    if context is not None:
        context.__enter__()
    try:
        async with llm:
            result = await llm.chat(
                messages=render_messages(
                    "prompt", {"segments": [{"source": "source"}]}
                ),
                temperature=0.2,
                estimated_input_tokens=10,
            )
        return result[0], llm, client
    finally:
        if context is not None:
            context.__exit__(None, None, None)
        os.environ.pop("LLM_API_KEY", None)


@pytest.mark.asyncio
async def test_openai_stream_aggregates_split_utf8_reasoning_and_usage(
    tmp_path: Path,
) -> None:
    current = streaming_config(tmp_path)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = (
            event({"choices": [{"delta": {"reasoning_content": "思"}}]})
            + event({"choices": [{"delta": {"content": "你好"}}]})
            + event({"choices": [{"delta": {"content": "界"}}]})
            + event(
                {
                    "usage": {
                        "prompt_tokens": 7,
                        "completion_tokens": 3,
                        "total_tokens": 10,
                    }
                }
            )
            + event("[DONE]")
        ).encode("utf-8")
        marker = body.index("界".encode()) + 1
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ByteChunks([body[:marker], body[marker : marker + 1], body[marker + 1 :]]),
        )

    diagnostics = Diagnostics(tmp_path / "logs" / "app.log")
    response, llm, client = await run_client(
        current, tmp_path, handler, diagnostics=diagnostics
    )
    await client.aclose()

    assert response.content == "你好界"
    assert response.reasoning_content == "思"
    assert llm.usage_summary() == {
        "input_tokens": 7,
        "output_tokens": 3,
        "total_tokens": 10,
        "available": True,
    }
    assert len(calls) == 1
    payload = json.loads(calls[0].content)
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    request = diagnostics.snapshot()["requests"]["items"][0]
    detail = diagnostics.request_detail(request["request_id"])
    attempt = detail["attempts"][0]
    assert attempt["outcome"] == "succeeded"
    assert attempt["transport"] == "sse"
    assert attempt["stream_event_count"] == 5
    assert attempt["stream_received_bytes"] > 0


@pytest.mark.asyncio
async def test_stream_retry_discards_partial_output_and_saves_failed_events(
    tmp_path: Path,
) -> None:
    current = streaming_config(tmp_path)
    current["retry"]["http_max_attempts"] = 2
    current["debug"]["enabled"] = True
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return stream_response({"choices": [{"delta": {"content": "半成品"}}]})
        return stream_response(
            {"choices": [{"delta": {"content": "完成"}}]},
            {"usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            "[DONE]",
        )

    response, _, client = await run_client(current, tmp_path, handler)
    await client.aclose()

    assert response.content == "完成"
    assert "半成品" not in response.content
    assert calls == 2
    failed_responses = sorted((tmp_path / "run" / "payloads").glob("*-A001.response.json"))
    failed_errors = sorted((tmp_path / "run" / "payloads").glob("*-A001.error.json"))
    assert failed_responses and failed_errors
    assert "半成品" in failed_responses[0].read_text("utf-8")


@pytest.mark.asyncio
async def test_stream_retry_preserves_retry_after_headers(tmp_path: Path) -> None:
    current = streaming_config(tmp_path)
    current["retry"]["http_max_attempts"] = 2
    calls = 0
    delays: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "3"},
                text="slow down",
            )
        return stream_response(
            {"choices": [{"delta": {"content": "ok"}}]},
            "[DONE]",
        )

    current["retry"]["base_delay_seconds"] = 5

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    os.environ["LLM_API_KEY"] = "test-key"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path / "run",
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
            sleeper=sleeper,
        ) as llm:
            response, _ = await llm.chat(
                messages=render_messages("prompt", {"segments": []}),
                temperature=0.2,
                estimated_input_tokens=10,
            )
    finally:
        os.environ.pop("LLM_API_KEY", None)
        await client.aclose()

    assert response.content == "ok"
    assert calls == 2
    assert delays == [3.0]


@pytest.mark.asyncio
async def test_stream_exhaustion_is_failed_not_left_retrying(tmp_path: Path) -> None:
    current = streaming_config(tmp_path)
    current["retry"]["http_max_attempts"] = 2
    diagnostics = Diagnostics(tmp_path / "logs" / "app.log")

    def handler(_: httpx.Request) -> httpx.Response:
        return stream_response({"choices": [{"delta": {"content": "partial"}}]})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    os.environ["LLM_API_KEY"] = "test-key"
    try:
        with diagnostics.activate("sample", "translation"):
            async with LLMClient(
                current,
                SlidingWindowLimiter(0, 0),
                run_dir=tmp_path / "run",
                project_id="PRJ",
                run_id="RUN",
                stage="translation",
                client=client,
            ) as llm:
                with pytest.raises(ExternalError, match="流式请求重试耗尽"):
                    await llm.chat(
                        messages=render_messages("prompt", {"segments": []}),
                        temperature=0.2,
                        estimated_input_tokens=10,
                    )
    finally:
        os.environ.pop("LLM_API_KEY", None)
        await client.aclose()

    item = diagnostics.snapshot()["requests"]["items"][0]
    assert item["status"] == "failed"
    detail = diagnostics.request_detail(item["request_id"])
    assert detail["response_content"] is None
    assert [attempt["outcome"] for attempt in detail["attempts"]] == [
        "stream_error",
        "stream_error",
    ]


@pytest.mark.asyncio
async def test_malformed_stream_fails_immediately_with_http_status(
    tmp_path: Path,
) -> None:
    current = streaming_config(tmp_path)
    current["retry"]["http_max_attempts"] = 3
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return stream_response("not-json")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    os.environ["LLM_API_KEY"] = "test-key"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path / "run",
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            with pytest.raises(ExternalError, match="不是合法 JSON"):
                await llm.chat(
                    messages=render_messages("prompt", {"segments": []}),
                    temperature=0.2,
                    estimated_input_tokens=10,
                )
    finally:
        os.environ.pop("LLM_API_KEY", None)
        await client.aclose()

    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_name", "events", "expected", "usage"),
    [
        (
            "openai-responses",
            [
                {"type": "response.output_text.delta", "delta": "hello"},
                {
                    "type": "response.completed",
                    "response": {
                        "usage": {
                            "input_tokens": 2,
                            "output_tokens": 1,
                            "total_tokens": 3,
                        }
                    },
                },
            ],
            "hello",
            {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
        ),
        (
            "anthropic",
            [
                {"type": "message_start", "message": {"usage": {"input_tokens": 4}}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "你好"}},
                {"type": "message_delta", "usage": {"output_tokens": 2}},
                {"type": "message_stop"},
            ],
            "你好",
            {"input_tokens": 4, "output_tokens": 2, "total_tokens": 0},
        ),
        (
            "google-gemini",
            [
                {"candidates": [{"content": {"parts": [{"text": "gem"}]}}]},
                {
                    "candidates": [{"finishReason": "STOP"}],
                    "usageMetadata": {
                        "promptTokenCount": 3,
                        "candidatesTokenCount": 2,
                        "totalTokenCount": 5,
                    },
                },
            ],
            "gem",
            {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        ),
    ],
)
async def test_builtin_provider_stream_protocols(
    tmp_path: Path,
    adapter_name: str,
    events: list[dict],
    expected: str,
    usage: dict[str, int],
) -> None:
    current = streaming_config(tmp_path)
    current["_llm_adapter"] = load_json_adapter(
        ROOT / "llm_adapters" / f"{adapter_name}.json"
    )
    if adapter_name == "google-gemini":
        current["llm"]["stream_endpoint"] = "/models/${model}:streamGenerateContent?alt=sse"

    def handler(request: httpx.Request) -> httpx.Response:
        if adapter_name == "google-gemini":
            assert ":streamGenerateContent?alt=sse" in str(request.url)
        return stream_response(*events, line_ending="\r\n")

    response, llm, client = await run_client(current, tmp_path, handler)
    await client.aclose()

    assert response.content == expected
    assert llm.usage_summary() == {**usage, "available": True}
