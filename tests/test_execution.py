from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from app.config import load_global_config
from app.errors import ExternalError, FatalExternalError
from app.execution import (
    LLMClient,
    Scope,
    SlidingWindowLimiter,
    build_chunk_plans,
    classify_stage,
    estimate_messages,
    finalize_run,
    full_prompt,
    materialize_chunks,
    previous_context,
    render_messages,
    select_scope,
    stage_fingerprint,
)
from app.llm_adapter import load_json_adapter


ROOT = Path(__file__).parents[1]


def config() -> dict:
    return load_global_config(ROOT)


def segments() -> list[dict]:
    return [
        {
            "segment_id": f"F0001-S{index + 1:06d}",
            "file_id": "F0001",
            "line_index": index,
            "source": source,
            "is_empty": source == "",
        }
        for index, source in enumerate(["one", "", "three", "four", "five"])
    ]


def test_stage_fingerprint_ignores_chunk_but_tracks_scheduling() -> None:
    first = config()
    prompt = full_prompt(
        "translation",
        (ROOT / "prompts" / "translation.middle.txt").read_text(encoding="utf-8"),
    )
    original = stage_fingerprint(first, "translation", prompt, terms_revision=1)
    first["chunking"]["target_chunk_input_tokens"] = 100
    assert stage_fingerprint(first, "translation", prompt, terms_revision=1) == original
    first["execution"]["scheduling_mode"] = (
        "ordered_by_file"
        if first["execution"]["scheduling_mode"] == "parallel"
        else "parallel"
    )
    assert stage_fingerprint(first, "translation", prompt, terms_revision=1) != original


@pytest.mark.parametrize("stage", ["proofreading", "polishing"])
def test_review_prompt_uses_conditional_fields(stage: str) -> None:
    prompt = full_prompt(stage, "Review carefully.")
    assert (
        '{"type":"segment","id":"F0001-S000001","status":"accepted"}'
        in prompt
    )
    assert (
        '{"type":"segment","id":"F0001-S000001","status":"suggested",'
        '"suggested_text":"完整建议","reason":"原因"}'
        in prompt
    )
    assert "即使附带 suggested_text 或 reason 也不会采用" in prompt


def test_scope_and_result_selection_preserve_old_success() -> None:
    source = segments()
    files = [{"file_id": "F0001", "file_order": 1}]
    selected = select_scope(source, files, Scope())
    history = [
        {
            "segment_id": "F0001-S000001",
            "status": "completed",
            "stage_fingerprint": "old",
        },
        {
            "segment_id": "F0001-S000001",
            "status": "failed",
            "stage_fingerprint": "new",
        },
    ]
    result = classify_stage(selected, history, force=False)
    assert [item["segment_id"] for item in result.reusable] == ["F0001-S000001"]
    assert result.last_attempt_failed == ("F0001-S000001",)
    assert "F0001-S000001" not in {
        item["segment_id"] for item in result.work
    }
    forced = classify_stage(selected, history, force=True)
    assert len(forced.work) == 4


def test_chunk_builder_crosses_empty_gaps_and_materializes_run_ids() -> None:
    current = config()
    prompt = full_prompt("translation", "Translate faithfully.")
    work = [segments()[0], segments()[2], segments()[3]]

    def payload_builder(items: list[dict]) -> dict:
        return {
            "segments": [
                {"id": item["segment_id"], "source": item["source"]} for item in items
            ]
        }

    plans = build_chunk_plans(
        work,
        all_segments=segments(),
        config=current,
        prompt=prompt,
        payload_builder=payload_builder,
    )
    assert [[item["line_index"] for item in plan.segments] for plan in plans] == [
        [0, 2, 3],
    ]
    chunks = materialize_chunks("RUN-X", "translation", plans)
    assert all(chunk.chunk_id and "RUN-X" in chunk.chunk_id for chunk in chunks)


def test_chunk_builder_only_crosses_gaps_made_entirely_of_empty_segments() -> None:
    source = [
        {
            "segment_id": f"F0001-S{index + 1:06d}",
            "file_id": "F0001",
            "line_index": index,
            "source": value,
            "is_empty": value == "" or value.isspace(),
        }
        for index, value in enumerate(["one", "\u3000", " \t", "four", "five"])
    ]
    source.append(
        {
            "segment_id": "F0002-S000001",
            "file_id": "F0002",
            "line_index": 0,
            "source": "other",
            "is_empty": False,
        }
    )
    work = [source[0], source[3], source[5]]
    plans = build_chunk_plans(
        work,
        all_segments=source,
        config=config(),
        prompt=full_prompt("translation", "Translate."),
        payload_builder=lambda items: {
            "segments": [
                {"id": item["segment_id"], "source": item["source"]}
                for item in items
            ]
        },
    )
    assert [[item["segment_id"] for item in plan.segments] for plan in plans] == [
        ["F0001-S000001", "F0001-S000004"],
        ["F0002-S000001"],
    ]

    plans = build_chunk_plans(
        [source[0], source[4]],
        all_segments=source,
        config=config(),
        prompt=full_prompt("translation", "Translate."),
        payload_builder=lambda items: {
            "segments": [
                {"id": item["segment_id"], "source": item["source"]}
                for item in items
            ]
        },
    )
    assert [len(plan.segments) for plan in plans] == [1, 1]


def test_chunk_builder_packs_alternating_empty_lines_near_soft_target() -> None:
    source: list[dict] = []
    for index in range(80):
        for value in (f"source text number {index:03d}", ""):
            line_index = len(source)
            source.append(
                {
                    "segment_id": f"F0001-S{line_index + 1:06d}",
                    "file_id": "F0001",
                    "line_index": line_index,
                    "source": value,
                    "is_empty": value == "",
                }
            )
    current = config()
    current["chunking"]["target_chunk_input_tokens"] = 600
    work = [item for item in source if not item["is_empty"]]
    plans = build_chunk_plans(
        work,
        all_segments=source,
        config=current,
        prompt=full_prompt("translation", "Translate."),
        payload_builder=lambda items: {
            "segments": [
                {"id": item["segment_id"], "source": item["source"]}
                for item in items
            ]
        },
    )
    assert len(plans) < 10
    assert any(plan.estimated_input_tokens >= 480 for plan in plans)
    assert all(plan.estimated_input_tokens <= 600 for plan in plans)
    assert plans[-1].estimated_input_tokens <= 600


def test_single_segment_may_exceed_soft_target_but_not_input_limit() -> None:
    current = config()
    current["chunking"]["target_chunk_input_tokens"] = 50
    source = [segments()[0]]
    plans = build_chunk_plans(
        source,
        all_segments=source,
        config=current,
        prompt=full_prompt("translation", "Translate."),
        payload_builder=lambda items: {
            "segments": [
                {"id": item["segment_id"], "source": item["source"]}
                for item in items
            ]
        },
    )
    assert len(plans) == 1
    assert plans[0].estimated_input_tokens > 50


def test_chunk_builder_splits_without_duplicating_segments() -> None:
    current = config()
    current["chunking"]["target_chunk_input_tokens"] = 75
    prompt = full_prompt("translation", "Translate.")
    work = [segments()[2], segments()[3], segments()[4]]

    def payload_builder(items: list[dict]) -> dict:
        return {
            "segments": [
                {
                    "id": item["segment_id"],
                    "source": str(item["source"]) * 30,
                }
                for item in items
            ]
        }

    plans = build_chunk_plans(
        work,
        all_segments=segments(),
        config=current,
        prompt=prompt,
        payload_builder=payload_builder,
    )
    ids = [
        item["segment_id"]
        for plan in plans
        for item in plan.segments
    ]
    assert ids == [item["segment_id"] for item in work]


def test_chunk_builder_ignores_disabled_itpm() -> None:
    current = config()
    current["execution"]["input_tokens_per_minute"] = 0
    prompt = full_prompt("translation", "Translate.")
    plans = build_chunk_plans(
        [segments()[0]],
        all_segments=segments(),
        config=current,
        prompt=prompt,
        payload_builder=lambda items: {
            "segments": [
                {"id": item["segment_id"], "source": item["source"]}
                for item in items
            ]
        },
    )
    assert len(plans) == 1
    assert plans[0].estimated_input_tokens > 0


def test_context_is_same_file_and_optional_target() -> None:
    source = segments()
    source.append(
        {
            "segment_id": "F0002-S000001",
            "file_id": "F0002",
            "line_index": 0,
            "source": "other",
            "is_empty": False,
        }
    )
    context = previous_context(
        source,
        source[4],
        2,
        target_resolver=lambda segment_id: f"translated:{segment_id}",
    )
    assert [item["id"] for item in context] == [
        "F0001-S000003",
        "F0001-S000004",
    ]
    assert all("translation" in item for item in context)


@pytest.mark.asyncio
async def test_llm_client_retries_429_and_saves_debug(tmp_path: Path) -> None:
    current = config()
    current["debug"]["enabled"] = True
    current["retry"]["base_delay_seconds"] = 0
    current["retry"]["jitter_seconds"] = 0
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="slow")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"type":"end"}'}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    limiter = SlidingWindowLimiter(100, 100000)
    os.environ["LLM_API_KEY"] = "test"
    try:
        async with LLMClient(
            current,
            limiter,
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            response, request_id = await llm.chat(
                messages=render_messages("prompt", {"segments": []}),
                temperature=0.2,
                estimated_input_tokens=10,
            )
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()
    assert response.content == '{"type":"end"}'
    assert response.reasoning_content is None
    assert calls == 2
    assert len(list((tmp_path / "payloads").glob(f"{request_id}-A*.request.json"))) == 2
    assert (tmp_path / "attempts.jsonl").is_file()


@pytest.mark.asyncio
async def test_llm_client_extracts_embedded_reasoning(tmp_path: Path) -> None:
    current = config()
    current["debug"]["enabled"] = True

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                "\ufeff \r\n<thought>reasoning</thought>\r\n"
                                '{"type":"end"}'
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "test"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            response, _ = await llm.chat(
                messages=render_messages("prompt", {"segments": []}),
                temperature=0.2,
                estimated_input_tokens=10,
            )
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()

    assert response.content == '{"type":"end"}'
    assert response.reasoning_content == "reasoning"
    saved_response = next((tmp_path / "payloads").glob("*.response.json"))
    assert "<thought>reasoning</thought>" in saved_response.read_text("utf-8")
    assert not list(tmp_path.rglob("*reasoning*"))


@pytest.mark.asyncio
async def test_llm_client_rejects_structured_and_embedded_reasoning_together(
    tmp_path: Path,
) -> None:
    current = config()
    definition = dict(current["_llm_adapter"].definition)
    definition["response_reasoning_content_pointer"] = (
        "/choices/0/message/reasoning_content"
    )
    adapter_file = tmp_path / "adapter.json"
    adapter_file.write_text(json.dumps(definition), encoding="utf-8")
    current["_llm_adapter"] = load_json_adapter(adapter_file)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                "<think>embedded</think>\n"
                                '{"type":"end"}'
                            ),
                            "reasoning_content": "structured",
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "test"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            with pytest.raises(ExternalError, match="同时包含结构化"):
                await llm.chat(
                    messages=render_messages("prompt", {"segments": []}),
                    temperature=0.2,
                    estimated_input_tokens=10,
                )
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()


@pytest.mark.asyncio
async def test_llm_client_sends_preset_extra_body(tmp_path: Path) -> None:
    current = config()
    current["_llm_extra_body"] = {
        "provider": {
            "order": ["anthropic", "google"],
            "allow_fallbacks": False,
        }
    }
    sent_payload: dict | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent_payload
        sent_payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"type":"end"}'}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "test"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            await llm.chat(
                messages=render_messages("prompt", {"segments": []}),
                temperature=0.2,
                estimated_input_tokens=10,
            )
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()

    assert sent_payload is not None
    assert sent_payload["provider"] == current["_llm_extra_body"]["provider"]


@pytest.mark.asyncio
async def test_llm_client_stops_on_auth_and_normal_mode_has_no_payloads(
    tmp_path: Path,
) -> None:
    current = config()
    current["debug"]["enabled"] = False
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, text="unauthorized")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "test"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(100, 100000),
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            with pytest.raises(FatalExternalError, match="鉴权失败"):
                await llm.chat(
                    messages=render_messages("prompt", {"segments": []}),
                    temperature=0.2,
                    estimated_input_tokens=10,
                )
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()
    assert calls == 1
    assert not (tmp_path / "payloads").exists()
    assert not (tmp_path / "attempts.jsonl").exists()


@pytest.mark.asyncio
async def test_llm_client_clamps_max_tokens_to_remaining_context(
    tmp_path: Path,
) -> None:
    current = config()
    current["llm"]["context_window_tokens"] = 1000
    current["llm"]["context_safety_margin_tokens"] = 100
    current["llm"]["max_output_tokens"] = 5000
    current["debug"]["enabled"] = False
    sent_payload: dict | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent_payload
        sent_payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"type":"end"}'}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "test"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            await llm.chat(
                messages=render_messages("prompt", {"segments": []}),
                temperature=0.2,
                estimated_input_tokens=250,
            )
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()
    assert sent_payload is not None
    assert sent_payload["max_tokens"] == 650
    assert len(llm.warnings) == 1
    assert "5000" in llm.warnings[0]
    assert "650" in llm.warnings[0]


@pytest.mark.asyncio
async def test_rate_limits_can_be_disabled_independently() -> None:
    now = [0.0]
    waits: list[float] = []

    async def sleeper(delay: float) -> None:
        waits.append(delay)
        now[0] += delay

    no_limits = SlidingWindowLimiter(
        0,
        0,
        clock=lambda: now[0],
        sleeper=sleeper,
    )
    await no_limits.acquire(1000000)
    await no_limits.acquire(1000000)
    assert not no_limits.records
    assert waits == []

    itpm_only = SlidingWindowLimiter(
        0,
        5,
        clock=lambda: now[0],
        sleeper=sleeper,
    )
    await itpm_only.acquire(3)
    await itpm_only.acquire(3)
    assert waits == [60.0]

    waits.clear()
    rpm_only = SlidingWindowLimiter(
        1,
        0,
        clock=lambda: now[0],
        sleeper=sleeper,
    )
    await rpm_only.acquire(1000000)
    await rpm_only.acquire(1000000)
    assert waits == [60.0]


@pytest.mark.asyncio
async def test_concurrent_requests_reserve_one_shared_rate_window() -> None:
    now = [0.0]
    waits: list[float] = []

    async def sleeper(delay: float) -> None:
        waits.append(delay)
        now[0] += delay

    limiter = SlidingWindowLimiter(
        1,
        0,
        clock=lambda: now[0],
        sleeper=sleeper,
    )
    await asyncio.gather(limiter.acquire(10), limiter.acquire(10))
    assert waits == [60.0]
    assert len(limiter.records) == 1


def test_token_safety_factor_below_one_scales_estimate() -> None:
    messages = render_messages("prompt", {"segments": [{"source": "one"}]})
    assert estimate_messages(messages, 0.5) < estimate_messages(messages, 1.0)


@pytest.mark.asyncio
async def test_llm_client_accumulates_usage_across_requests(tmp_path: Path) -> None:
    current = config()
    responses = [
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
    ]
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        usage = responses[min(calls - 1, len(responses) - 1)]
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"type":"end"}'}}],
                "usage": usage,
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "test"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            for _ in range(3):
                await llm.chat(
                    messages=render_messages("prompt", {"segments": []}),
                    temperature=0.2,
                    estimated_input_tokens=10,
                )
            assert llm.usage_summary() == {
                "input_tokens": 14,
                "output_tokens": 11,
                "total_tokens": 25,
                "available": True,
            }
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()


@pytest.mark.asyncio
async def test_llm_client_marks_usage_unavailable_when_omitted(
    tmp_path: Path,
) -> None:
    current = config()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"type":"end"}'}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "test"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            await llm.chat(
                messages=render_messages("prompt", {"segments": []}),
                temperature=0.2,
                estimated_input_tokens=10,
            )
            summary = llm.usage_summary()
            assert summary is not None
            assert summary["available"] is False
            assert summary["input_tokens"] == 0
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()


@pytest.mark.parametrize(
    "second_usage",
    [
        None,
        {"prompt_tokens": "invalid", "completion_tokens": 3, "total_tokens": 5},
    ],
)
@pytest.mark.asyncio
async def test_llm_client_marks_mixed_usage_unavailable(
    tmp_path: Path,
    second_usage: object,
) -> None:
    current = config()
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload: dict[str, object] = {
            "choices": [{"message": {"content": '{"type":"end"}'}}],
        }
        usage = (
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
            if calls == 1
            else second_usage
        )
        if usage is not None:
            payload["usage"] = usage
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "test"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            for _ in range(2):
                await llm.chat(
                    messages=render_messages("prompt", {"segments": []}),
                    temperature=0.2,
                    estimated_input_tokens=10,
                )
            assert llm.usage_summary() == {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "available": False,
            }
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()


@pytest.mark.asyncio
async def test_llm_client_without_usage_mapping_has_no_summary(
    tmp_path: Path,
) -> None:
    current = config()
    definition = dict(current["_llm_adapter"].definition)
    definition.pop("usage")
    adapter_file = tmp_path / "adapter.json"
    adapter_file.write_text(json.dumps(definition), encoding="utf-8")
    current["_llm_adapter"] = load_json_adapter(adapter_file)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"type":"end"}'}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "test"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            await llm.chat(
                messages=render_messages("prompt", {"segments": []}),
                temperature=0.2,
                estimated_input_tokens=10,
            )
            assert llm.usage_summary() is None
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()


def test_finalize_run_records_usage_in_manifest(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    def write_manifest() -> None:
        (run_dir / "manifest.json").write_text(
            json.dumps({"schema_version": 1, "status": "running"}),
            encoding="utf-8",
        )

    usage = {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "available": True,
    }
    write_manifest()
    finalize_run(run_dir, status="completed", completed=2, failed=0, usage=usage)
    manifest = json.loads((run_dir / "manifest.json").read_text("utf-8"))
    assert manifest["usage"] == usage
    assert manifest["status"] == "completed"

    write_manifest()
    finalize_run(run_dir, status="failed", completed=0, failed=1)
    manifest = json.loads((run_dir / "manifest.json").read_text("utf-8"))
    assert "usage" not in manifest


def _use_adapter(config: dict, name: str) -> None:
    config["_llm_adapter"] = load_json_adapter(
        ROOT / "llm_adapters" / f"{name}.json"
    )
    config["_llm_adapter_hash"] = config["_llm_adapter"].digest


@pytest.mark.asyncio
async def test_llm_client_sends_anthropic_format_request(tmp_path: Path) -> None:
    current = config()
    _use_adapter(current, "anthropic")
    sent: dict | None = None
    sent_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent, sent_headers
        sent = json.loads(request.content)
        sent_headers = dict(request.headers)
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": '{"type":"end"}'}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "test"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            response, _ = await llm.chat(
                messages=render_messages("prompt", {"segments": []}),
                temperature=0.2,
                estimated_input_tokens=10,
            )
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()

    assert response.content == '{"type":"end"}'
    assert response.reasoning_content is None
    assert sent is not None
    assert sent_headers["x-api-key"] == "test"
    assert sent_headers["anthropic-version"] == "2023-06-01"
    assert sent["system"] == "prompt"
    assert sent["messages"] == [
        {"role": "user", "content": '{"segments":[]}'}
    ]
    assert sent["max_tokens"] == current["llm"]["max_output_tokens"]
    assert sent["stream"] is False


@pytest.mark.asyncio
async def test_llm_client_sends_gemini_format_request(tmp_path: Path) -> None:
    current = config()
    _use_adapter(current, "google-gemini")
    current["llm"]["model"] = "gemini-2.5-flash"
    current["llm"]["base_url"] = "https://example.com"
    current["llm"]["endpoint"] = "/v1beta/models/${model}:generateContent"
    sent: dict | None = None
    sent_url = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent, sent_url
        sent_url = str(request.url)
        sent = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {"text": "thought", "thought": True},
                                {"text": '{"type":"end"}'},
                            ],
                        },
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "test"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            response, _ = await llm.chat(
                messages=render_messages("prompt", {"segments": []}),
                temperature=0.2,
                estimated_input_tokens=10,
            )
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()

    assert response.content == '{"type":"end"}'
    assert response.reasoning_content is None
    assert sent_url == (
        "https://example.com/v1beta/models/gemini-2.5-flash:generateContent"
    )
    assert sent is not None
    assert sent["system_instruction"] == {"parts": [{"text": "prompt"}]}
    assert sent["contents"] == [
        {"role": "user", "parts": [{"text": '{"segments":[]}'}]}
    ]
    assert sent["generationConfig"] == {
        "temperature": 0.2,
        "maxOutputTokens": current["llm"]["max_output_tokens"],
    }
    assert "api_key" not in sent_url


@pytest.mark.asyncio
async def test_llm_client_sends_openai_responses_format_request(
    tmp_path: Path,
) -> None:
    current = config()
    _use_adapter(current, "openai-responses")
    current["llm"]["endpoint"] = "/v1/responses"
    sent: dict | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "reasoning",
                        "id": "rs_123",
                        "summary": [],
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"type":"end"}',
                                "annotations": [],
                            }
                        ],
                    },
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "test"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            response, _ = await llm.chat(
                messages=render_messages("prompt", {"segments": []}),
                temperature=0.2,
                estimated_input_tokens=10,
            )
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()

    assert response.content == '{"type":"end"}'
    assert sent is not None
    assert sent["input"] == render_messages("prompt", {"segments": []})
    assert sent["store"] is False
    assert sent["stream"] is False
