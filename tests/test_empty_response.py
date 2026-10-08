from pathlib import Path
import json

import httpx
import pytest

from app.config import load_config, load_global_config
from app.errors import ExternalError
from app.execution import Scope
from app.llm_client import LLMClient, SlidingWindowLimiter
from app.stage_translation import run_translation
from tests.test_terminology_translation import create_project
from tests.test_llm_streaming import stream_response

ROOT = Path(__file__).parents[1]


def test_empty_response_defaults_for_missing_config_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            line
            for line in (ROOT / "config/config.toml").read_text().splitlines()
            if not line.startswith("empty_")
        )
    )
    retry = load_config(path)["retry"]
    assert retry["empty_truncated_mode"] == "split"
    assert retry["empty_truncated_max_attempts"] == 2
    assert retry["empty_unknown_mode"] == "retry"
    assert retry["empty_unknown_max_attempts"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reason", ["stop", "length"])
async def test_empty_original_retry_preserves_request_and_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stream: bool, reason: str
) -> None:
    monkeypatch.setenv("LLM_API_KEY", "test")
    config = load_global_config(ROOT)
    config["llm"]["stream"] = stream
    config["retry"]["empty_truncated_mode"] = "retry"
    config["retry"]["empty_truncated_max_attempts"] = 1
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        content = "" if len(bodies) == 1 else "answer"
        if stream:
            return stream_response(
                {
                    "choices": [
                        {
                            "delta": {
                                "content": content,
                                "reasoning_content": "thinking",
                            },
                            "finish_reason": reason,
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 20,
                        "total_tokens": 30,
                    },
                },
                "[DONE]",
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": content,
                            "reasoning_content": "thinking",
                        },
                        "finish_reason": reason,
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with LLMClient(
            config,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path / "run",
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            response, _ = await llm.chat(
                messages=[{"role": "user", "content": "source"}],
                temperature=0,
                estimated_input_tokens=10,
            )
            assert response.content == "answer"
            assert llm.usage_summary()["total_tokens"] == 60
    assert len(bodies) == 2 and bodies[0] == bodies[1]


@pytest.mark.asyncio
async def test_empty_split_limit_is_inherited_and_never_formats(tmp_path: Path) -> None:
    project = await create_project(tmp_path, "a\nb\nc\nd\ne\nf\ng\nh")
    payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        payloads.append(payload)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "", "reasoning_content": "thinking"},
                        "finish_reason": "length",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await run_translation(project, Scope(), http_client=client)
    assert [len(payload["segments"]) for payload in payloads] == [8, 4, 2, 2, 4, 2, 2]
    assert result["failed"] == 8
    assert all("format_correction" not in payload for payload in payloads)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,count", [("stop", 1), ("length", 2), ("stop", 0)])
async def test_empty_retry_exhaustion_and_disable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str, count: int
) -> None:
    monkeypatch.setenv("LLM_API_KEY", "test")
    config = load_global_config(ROOT)
    kind = "truncated" if reason == "length" else "unknown"
    config["retry"][f"empty_{kind}_mode"] = "retry"
    config["retry"][f"empty_{kind}_max_attempts"] = count
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": " "}, "finish_reason": reason}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with LLMClient(
            config,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path / "run",
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            with pytest.raises(ExternalError, match="预算已耗尽"):
                await llm.chat(
                    messages=[{"role": "user", "content": "source"}],
                    temperature=0,
                    estimated_input_tokens=10,
                )
    assert calls == count + 1


@pytest.mark.asyncio
async def test_unknown_empty_draft_does_not_reenter_format_correction(
    tmp_path: Path,
) -> None:
    from app.stage_terminology import run_terminology

    project = await create_project(tmp_path, "a\nb")
    payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(
            json.loads(json.loads(request.content)["messages"][1]["content"])
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await run_terminology(
            project, Scope(), http_client=client, include_draft_translation=True
        )
    assert len(payloads) == 2
    assert result["failed"] == 2
    assert all("format_correction" not in payload for payload in payloads)


@pytest.mark.asyncio
async def test_responses_stream_incomplete_retains_explicit_length_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.llm_adapter import load_json_adapter
    from app.errors import EmptyResponseSplitError

    monkeypatch.setenv("LLM_API_KEY", "test")
    config = load_global_config(ROOT)
    config["llm"]["stream"] = True
    config["_llm_adapter"] = load_json_adapter(
        ROOT / "llm_adapters/openai-responses.json"
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return stream_response(
            {
                "type": "response.incomplete",
                "response": {
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 20,
                        "total_tokens": 30,
                    },
                },
            }
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with LLMClient(
            config,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path / "run",
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            with pytest.raises(EmptyResponseSplitError):
                await llm.chat(
                    messages=[{"role": "user", "content": "source"}],
                    temperature=0,
                    estimated_input_tokens=10,
                )
            assert llm.usage_summary()["total_tokens"] == 30


def test_responses_missing_content_at_length_limit_is_not_parse_error() -> None:
    from app.llm_adapter import load_json_adapter

    adapter = load_json_adapter(ROOT / "llm_adapters/openai-responses.json")
    response = adapter.parse_response(
        {
            "output": [{"type": "reasoning"}],
            "incomplete_details": {"reason": "max_output_tokens"},
        }
    )
    assert response.content == "" and response.finish_reason == "max_output_tokens"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["translation", "terminology", "proofreading"])
async def test_singleton_empty_split_fails_with_correct_reason(
    tmp_path: Path, stage: str
) -> None:
    from app.stage_runtime import load_stage_history

    project = await create_project(tmp_path, "a")
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": None}, "finish_reason": "length"}]
            },
        )

    if stage == "proofreading":
        from tests.test_terminology_translation import llm_jsonl

        def translate(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": llm_jsonl(
                                    [
                                        {
                                            "type": "segment",
                                            "id": "1",
                                            "translation": "啊",
                                        }
                                    ]
                                )
                            }
                        }
                    ]
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(translate)
        ) as client:
            await run_translation(project, Scope(), http_client=client)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        if stage == "terminology":
            from app.stage_terminology import run_terminology

            result = await run_terminology(project, Scope(), http_client=client)
        elif stage == "proofreading":
            from app.stage_review import run_review

            result = await run_review(project, stage, Scope(), http_client=client)
        else:
            result = await run_translation(project, Scope(), http_client=client)
    assert result["failed"] == calls == 1
    if stage == "terminology":
        from app.sqlite_storage import read_jsonl

        record = read_jsonl(project, project / "terminology/scans.jsonl")[-1]
    else:
        record = load_stage_history(project, stage)[-1]
    if stage != "proofreading":
        assert record["error_class"] == "empty_response"
    assert "长度限制截断" in record["error_message"]


@pytest.mark.asyncio
async def test_explicit_blocked_reason_is_not_empty_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_API_KEY", "test")
    config = load_global_config(ROOT)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": None}, "finish_reason": "content_filter"}
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with LLMClient(
            config,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path / "run",
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            with pytest.raises(ExternalError, match="拒绝生成正文"):
                await llm.chat(
                    messages=[{"role": "user", "content": "source"}],
                    temperature=0,
                    estimated_input_tokens=10,
                )
    assert calls == 1
