from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from app.config import load_config
from app.errors import FatalExternalError
from app.execution import (
    LLMClient,
    Scope,
    SlidingWindowLimiter,
    build_chunk_plans,
    classify_stage,
    estimate_messages,
    full_prompt,
    materialize_chunks,
    previous_context,
    render_messages,
    select_scope,
    stage_fingerprint,
)


ROOT = Path(__file__).parents[1]


def config() -> dict:
    return load_config(ROOT / "config" / "config.toml")


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
            content, request_id = await llm.chat(
                messages=render_messages("prompt", {"segments": []}),
                temperature=0.2,
                estimated_input_tokens=10,
            )
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()
    assert content == '{"type":"end"}'
    assert calls == 2
    assert len(list((tmp_path / "payloads").glob(f"{request_id}-A*.request.json"))) == 2
    assert (tmp_path / "attempts.jsonl").is_file()


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
