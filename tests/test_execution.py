from __future__ import annotations

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
    first["execution"]["scheduling_mode"] = "parallel"
    assert stage_fingerprint(first, "translation", prompt, terms_revision=1) != original


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


def test_chunk_builder_respects_gaps_and_materializes_run_ids() -> None:
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
        work, config=current, prompt=prompt, payload_builder=payload_builder
    )
    assert [[item["line_index"] for item in plan.segments] for plan in plans] == [
        [0],
        [2, 3],
    ]
    chunks = materialize_chunks("RUN-X", "translation", plans)
    assert all(chunk.chunk_id and "RUN-X" in chunk.chunk_id for chunk in chunks)


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
        work, config=current, prompt=prompt, payload_builder=payload_builder
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
            json={"choices": [{"message": {"content": '{"segments":[]}'}}]},
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
    assert json.loads(content) == {"segments": []}
    assert calls == 2
    assert len(list((tmp_path / "payloads").glob(f"{request_id}-A*.request.json"))) == 2
    assert (tmp_path / "attempts.jsonl").is_file()


@pytest.mark.asyncio
async def test_llm_client_stops_on_auth_and_normal_mode_has_no_payloads(
    tmp_path: Path,
) -> None:
    current = config()
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
