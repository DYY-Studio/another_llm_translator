from __future__ import annotations

import asyncio
import logging
import os
from contextlib import ExitStack
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.diagnostics import Diagnostics, DiagnosticsHub
from app.errors import ExternalError
from app.llm_client import LLMClient, SlidingWindowLimiter
from app.execution import render_messages
from app.logging_utils import get_logger
from app.web import create_app
from tests.test_execution import config


def test_diagnostics_keeps_bounded_global_logs_and_filters(tmp_path: Path) -> None:
    diagnostics = Diagnostics(tmp_path / "logs" / "app.log")
    logger = get_logger("translation")

    with diagnostics.activate("first", "translation"):
        logger.info("first message")
    logger = get_logger("proofreading")
    with diagnostics.activate("second", "proofreading"):
        logger.warning("second message")

    snapshot = diagnostics.snapshot(
        level="warning", project="second", stage="proofreading", query="SECOND"
    )
    assert [item["message"] for item in snapshot["logs"]] == ["second message"]
    assert {item["project"] for item in diagnostics.snapshot()["logs"]} == {
        "first",
        "second",
    }
    assert snapshot["filters"]["projects"] == ["first", "second"]
    assert (tmp_path / "logs" / "app.log").is_file()


@pytest.mark.asyncio
async def test_diagnostics_hub_keeps_concurrent_task_sessions_separate(
    tmp_path: Path,
) -> None:
    hub = DiagnosticsHub(tmp_path / "logs" / "app.log")
    entered = asyncio.Event()
    release_first = asyncio.Event()
    release_second = asyncio.Event()

    async def run_task(task_id: str, project: str, release: asyncio.Event) -> None:
        with hub.activate(project, "translation", task_id=task_id):
            hub.begin_request(
                request_id=f"REQ-{task_id}",
                model="model",
                messages=[],
                max_attempts=1,
            )
            hub.set_usage(
                {
                    "input_tokens": 10 if task_id == "T1" else 20,
                    "output_tokens": 1 if task_id == "T1" else 2,
                    "total_tokens": 11 if task_id == "T1" else 22,
                    "available": True,
                    "partial": False,
                }
            )
            entered.set()
            await release.wait()

    first = asyncio.create_task(run_task("T1", "first", release_first))
    second = asyncio.create_task(run_task("T2", "second", release_second))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert entered.is_set()
    active = hub.snapshot()
    assert active["metrics"]["total_requests"] == 2
    assert {
        item["task_id"] for item in active["requests"]["items"]
    } == {"T1", "T2"}
    assert active["metrics"]["input_tokens"] == 30
    assert active["metrics"]["output_tokens"] == 3
    assert active["metrics"]["usage_available"] is True

    release_first.set()
    await first
    remaining = hub.snapshot()
    assert remaining["metrics"]["total_requests"] == 1
    assert remaining["metrics"]["project"] == "second"
    assert hub.request_detail("REQ-T1")["status"] == "interrupted"
    assert hub.request_detail("REQ-T2")["status"] == "running"
    assert hub.snapshot(project="second")["requests"]["total"] == 1
    release_second.set()
    await second


def test_diagnostics_hub_filters_metrics_and_requests_by_project(
    tmp_path: Path,
) -> None:
    hub = DiagnosticsHub(tmp_path / "logs" / "app.log")
    with hub.activate("first", "translation", task_id="T1"):
        hub.begin_request(
            request_id="REQ-FIRST",
            model="model",
            messages=[],
            max_attempts=1,
        )
        hub.request_finished(
            request_id="REQ-FIRST",
            attempt=1,
            latency_seconds=0.1,
            status=200,
            error=False,
        )
        hub.complete_request("REQ-FIRST", content="ok", reasoning_content=None)
    with hub.activate("second", "translation", task_id="T2"):
        hub.begin_request(
            request_id="REQ-SECOND",
            model="model",
            messages=[],
            max_attempts=1,
        )
        hub.request_finished(
            request_id="REQ-SECOND",
            attempt=1,
            latency_seconds=0.3,
            status=500,
            error=True,
        )

    filtered = hub.snapshot(project="second")
    assert filtered["metrics"]["total_requests"] == 0
    assert filtered["requests"]["total"] == 1
    assert filtered["requests"]["items"][0]["task_id"] == "T2"


def test_diagnostics_hub_distinguishes_unavailable_and_partial_usage(
    tmp_path: Path,
) -> None:
    hub = DiagnosticsHub(tmp_path / "logs" / "app.log")
    with hub.activate("first", "translation", task_id="T1"):
        hub.set_usage(
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "available": False,
                "partial": False,
            }
        )
        unavailable = hub.snapshot()["metrics"]
        assert unavailable["usage_available"] is False
        assert unavailable["usage_partial"] is False

        with hub.activate("second", "translation", task_id="T2"):
            hub.set_usage(
                {
                    "input_tokens": 12,
                    "output_tokens": 3,
                    "total_tokens": 15,
                    "available": True,
                    "partial": False,
                }
            )
            partial = hub.snapshot()["metrics"]
            assert partial["usage_available"] is False
            assert partial["usage_partial"] is True
            assert partial["input_tokens"] == 12
            assert partial["output_tokens"] == 3


def test_diagnostics_hub_filters_active_metrics_and_merges_latency_samples(
    tmp_path: Path,
) -> None:
    hub = DiagnosticsHub(tmp_path / "logs" / "app.log")
    with ExitStack() as stack:
        stack.enter_context(hub.activate("first", "translation", task_id="T1"))
        stack.enter_context(hub.activate("second", "proofreading", task_id="T2"))
        first = hub.sessions["T1"]
        second = hub.sessions["T2"]
        for session, request_id, latency in (
            (first, "REQ-FIRST", 0.1),
            (second, "REQ-SECOND", 0.3),
        ):
            session.begin_request(
                request_id=request_id,
                model="model",
                messages=[],
                max_attempts=1,
            )
            session.request_finished(
                request_id=request_id,
                attempt=1,
                latency_seconds=latency,
                status=200,
                error=False,
            )

        aggregate = hub.snapshot()
        assert aggregate["metrics"]["total_requests"] == 2
        assert aggregate["metrics"]["average_latency_ms"] == 200.0
        assert aggregate["metrics"]["p95_latency_ms"] == 300.0
        assert {item["task_id"] for item in aggregate["requests"]["items"]} == {
            "T1",
            "T2",
        }

        filtered = hub.snapshot(project="first", stage="translation")
        assert filtered["metrics"]["total_requests"] == 1
        assert filtered["metrics"]["average_latency_ms"] == 100.0
        assert filtered["metrics"]["p95_latency_ms"] == 100.0
        assert filtered["requests"]["total"] == 1
        assert filtered["requests"]["items"][0]["task_id"] == "T1"


def test_diagnostics_hub_keeps_global_terminal_detail_window_across_sessions(
    tmp_path: Path,
) -> None:
    hub = DiagnosticsHub(tmp_path / "logs" / "app.log")
    for index in range(201):
        task_id = f"T-{index}"
        request_id = f"REQ-{index}"
        with hub.activate("project", "translation", task_id=task_id):
            hub.begin_request(
                request_id=request_id,
                model="model",
                messages=[{"role": "user", "content": request_id}],
                max_attempts=1,
            )
            hub.complete_request(request_id, content="response", reasoning_content=None)

    items = hub.snapshot()["requests"]["items"]
    assert len(items) == 201
    assert sum(item["detail_available"] for item in items) == 200
    with pytest.raises(ValueError, match="已从内存释放.*REQ-0"):
        hub.request_detail("REQ-0")
    assert hub.request_detail("REQ-200")["request_id"] == "REQ-200"


def test_diagnostics_hub_preserves_details_for_more_than_200_active_requests(
    tmp_path: Path,
) -> None:
    hub = DiagnosticsHub(tmp_path / "logs" / "app.log")
    with ExitStack() as stack:
        for index in range(201):
            task_id = f"ACTIVE-{index}"
            stack.enter_context(hub.activate("project", "translation", task_id=task_id))
            hub.begin_request(
                request_id=f"REQ-ACTIVE-{index}",
                model="model",
                messages=[],
                max_attempts=1,
            )

        active = hub.snapshot()
        assert active["metrics"]["total_requests"] == 201
        assert sum(
            item["detail_available"] for item in active["requests"]["items"]
        ) == 201
        assert hub.request_detail("REQ-ACTIVE-0")["status"] == "running"


def test_request_exchange_and_exact_usage_are_session_only(tmp_path: Path) -> None:
    diagnostics = Diagnostics(tmp_path / "logs" / "app.log")
    logger = get_logger("diagnostics-test")

    with diagnostics.activate("sample", "translation"):
        diagnostics.begin_request(
            request_id="REQ-1",
            model="test-model",
            messages=[{"role": "user", "content": "private source"}],
            max_attempts=2,
            segment_id_map={"1": "F0001-S000001"},
        )
        diagnostics.request_started("REQ-1")
        diagnostics.request_finished(
            request_id="REQ-1",
            attempt=1,
            latency_seconds=0.25,
            status=429,
            error=True,
        )
        diagnostics.retried()
        diagnostics.rate_limit_wait_started()
        assert diagnostics.snapshot()["metrics"]["rate_limit_waiting_requests"] == 1
        diagnostics.rate_limit_wait_finished()
        diagnostics.request_started("REQ-1")
        diagnostics.request_finished(
            request_id="REQ-1",
            attempt=2,
            latency_seconds=0.1,
            status=200,
            error=False,
        )
        diagnostics.complete_request(
            "REQ-1", content="private response", reasoning_content="private chain"
        )
        diagnostics.set_usage(
            {
                "input_tokens": 12,
                "output_tokens": 3,
                "total_tokens": 15,
                "available": True,
                "partial": False,
            }
        )
        logger.info("safe summary")

    snapshot = diagnostics.snapshot()
    metrics = snapshot["metrics"]
    assert metrics["active_requests"] == 0
    assert metrics["total_requests"] == 1
    assert metrics["http_errors"] == 1
    assert metrics["retry_count"] == 1
    assert metrics["rate_limit_waiting_requests"] == 0
    assert metrics["average_latency_ms"] == 175.0
    assert metrics["p95_latency_ms"] == 250.0
    assert metrics["usage_available"] is True
    assert metrics["usage_partial"] is False
    assert metrics["input_tokens"] == 12
    assert metrics["output_tokens"] == 3
    assert metrics["throughput_input_tokens_per_second"] is not None
    assert metrics["throughput_output_tokens_per_second"] is not None
    assert metrics["throughput_tokens_per_second"] is not None
    assert metrics["throughput_tokens_per_second"] == pytest.approx(
        metrics["throughput_input_tokens_per_second"]
        + metrics["throughput_output_tokens_per_second"],
        abs=0.02,
    )
    assert "reasoning" not in snapshot
    assert snapshot["requests"]["reset"] is True
    assert snapshot["requests"]["total"] == 1
    assert snapshot["requests"]["items"] == [
        {
            "timestamp": snapshot["requests"]["items"][0]["timestamp"],
            "finished_at": snapshot["requests"]["items"][0]["finished_at"],
            "project": "sample",
            "stage": "translation",
            "request_id": "REQ-1",
            "model": "test-model",
            "transport": "non_streaming",
            "status": "completed",
            "attempt_count": 2,
            "last_http_status": 200,
            "latest_latency_ms": 100.0,
            "has_content": True,
            "has_reasoning": True,
            "error": None,
            "detail_available": True,
            "stream_event_count": 0,
            "stream_received_bytes": 0,
            "stream_first_event_latency_ms": None,
            "provider_error_status": None,
        }
    ]
    assert "messages" not in snapshot["requests"]["items"][0]
    detail = diagnostics.request_detail("REQ-1")
    assert detail["messages"][0]["content"] == "private source"
    assert detail["response_content"] == "private response"
    assert detail["reasoning_content"] == "private chain"
    assert detail["segment_id_map"] == {"1": "F0001-S000001"}
    persisted = (tmp_path / "logs" / "app.log").read_text("utf-8")
    assert "private source" not in persisted
    assert "private response" not in persisted
    assert "private chain" not in persisted

    diagnostics.set_usage(
        {
            "input_tokens": 12,
            "output_tokens": 3,
            "total_tokens": 15,
            "available": False,
            "partial": False,
        }
    )
    unavailable = diagnostics.snapshot()["metrics"]
    assert unavailable["input_tokens"] == 0
    assert unavailable["output_tokens"] == 0
    assert unavailable["total_requests"] == 1
    assert unavailable["throughput_input_tokens_per_second"] is None
    assert unavailable["throughput_output_tokens_per_second"] is None
    assert unavailable["throughput_tokens_per_second"] is None

    diagnostics.set_usage(
        {
            "input_tokens": 12,
            "output_tokens": 3,
            "total_tokens": 15,
            "available": False,
            "partial": True,
        }
    )
    partial = diagnostics.snapshot()["metrics"]
    assert partial["usage_available"] is False
    assert partial["usage_partial"] is True
    assert partial["input_tokens"] == 12
    assert partial["output_tokens"] == 3
    assert partial["throughput_tokens_per_second"] is None


def test_stream_progress_is_visible_without_partial_response(
    tmp_path: Path,
) -> None:
    diagnostics = Diagnostics(tmp_path / "logs" / "app.log")
    with diagnostics.activate("sample", "translation"):
        diagnostics.begin_request(
            request_id="REQ-SSE",
            model="stream-model",
            messages=[{"role": "user", "content": "source"}],
            max_attempts=2,
            transport="sse",
        )
        diagnostics.request_started("REQ-SSE")
        diagnostics.stream_progress(
            "REQ-SSE",
            event_count=3,
            received_bytes=128,
            first_event_latency_ms=42.5,
        )
        summary = diagnostics.snapshot()["requests"]["items"][0]
        assert summary["transport"] == "sse"
        assert summary["stream_event_count"] == 3
        assert summary["stream_received_bytes"] == 128
        assert summary["stream_first_event_latency_ms"] == 42.5
        detail = diagnostics.request_detail("REQ-SSE")
        assert detail["response_content"] is None
        assert detail["attempts"] == []


def test_total_request_count_resets_for_each_run(tmp_path: Path) -> None:
    diagnostics = Diagnostics(tmp_path / "logs" / "app.log")
    with diagnostics.activate("first", "translation"):
        diagnostics.begin_request(
            request_id="REQ-1",
            model="test-model",
            messages=[],
            max_attempts=1,
        )
        assert diagnostics.snapshot()["metrics"]["total_requests"] == 1
        assert diagnostics.snapshot()["metrics"]["average_latency_ms"] is None
        assert diagnostics.snapshot()["metrics"]["p95_latency_ms"] is None
    with diagnostics.activate("second", "translation"):
        assert diagnostics.snapshot()["metrics"]["total_requests"] == 0
        assert diagnostics.snapshot()["metrics"]["average_latency_ms"] is None
        assert diagnostics.snapshot()["metrics"]["p95_latency_ms"] is None


def test_latency_metrics_aggregate_all_attempts_and_use_nearest_rank_p95(
    tmp_path: Path,
) -> None:
    diagnostics = Diagnostics(tmp_path / "logs" / "app.log")

    with diagnostics.activate("sample", "translation"):
        for request_id in ("REQ-429", "REQ-HTTP", "REQ-NETWORK", "REQ-OK"):
            diagnostics.begin_request(
                request_id=request_id,
                model="model",
                messages=[],
                max_attempts=2 if request_id == "REQ-429" else 1,
            )

        # Complete attempts in an order that differs from their latency order.
        diagnostics.request_finished(
            request_id="REQ-429",
            attempt=1,
            latency_seconds=0.4,
            status=429,
            error=True,
        )
        diagnostics.request_finished(
            request_id="REQ-429",
            attempt=2,
            latency_seconds=0.1,
            status=200,
            error=False,
        )
        diagnostics.complete_request(
            "REQ-429", content="ok", reasoning_content=None
        )
        diagnostics.request_finished(
            request_id="REQ-HTTP",
            attempt=1,
            latency_seconds=0.2,
            status=500,
            error=True,
        )
        diagnostics.request_finished(
            request_id="REQ-NETWORK",
            attempt=1,
            latency_seconds=0.3,
            status=None,
            error=True,
        )
        diagnostics.request_finished(
            request_id="REQ-OK",
            attempt=1,
            latency_seconds=0.05,
            status=200,
            error=False,
        )
        diagnostics.complete_request("REQ-OK", content="ok", reasoning_content=None)

        metrics = diagnostics.snapshot()["metrics"]
        assert metrics["average_latency_ms"] == 210.0
        assert metrics["p95_latency_ms"] == 400.0
        assert metrics["http_errors"] == 3
        assert diagnostics.snapshot()["requests"]["items"][0][
            "latest_latency_ms"
        ] == 100.0


@pytest.mark.asyncio
async def test_rate_limit_waiting_requests_track_queue_and_cancellation(
    tmp_path: Path,
) -> None:
    diagnostics = Diagnostics(tmp_path / "logs" / "app.log")
    now = [0.0]
    waits: list[float] = []
    release = asyncio.Event()

    async def sleeper(delay: float) -> None:
        waits.append(delay)
        await release.wait()
        now[0] += delay

    limiter = SlidingWindowLimiter(
        1,
        0,
        clock=lambda: now[0],
        sleeper=sleeper,
    )
    with diagnostics.activate("sample", "translation"):
        await limiter.acquire(1)
        first = asyncio.create_task(
            limiter.acquire(
                1,
                on_wait_start=diagnostics.rate_limit_wait_started,
                on_wait_end=diagnostics.rate_limit_wait_finished,
            )
        )
        for _ in range(10):
            await asyncio.sleep(0)
            if waits:
                break
        second = asyncio.create_task(
            limiter.acquire(
                1,
                on_wait_start=diagnostics.rate_limit_wait_started,
                on_wait_end=diagnostics.rate_limit_wait_finished,
            )
        )
        for _ in range(10):
            await asyncio.sleep(0)
            if diagnostics.snapshot()["metrics"]["rate_limit_waiting_requests"] == 2:
                break
        assert diagnostics.snapshot()["metrics"]["rate_limit_waiting_requests"] == 2
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        assert diagnostics.snapshot()["metrics"]["rate_limit_waiting_requests"] == 1
        release.set()
        await first
        assert diagnostics.snapshot()["metrics"]["rate_limit_waiting_requests"] == 0

    assert diagnostics.snapshot()["metrics"]["rate_limit_waiting_requests"] == 0


def test_request_details_are_bounded_while_summaries_remain(tmp_path: Path) -> None:
    diagnostics = Diagnostics(tmp_path / "logs" / "app.log")

    with diagnostics.activate("sample", "translation"):
        for index in range(201):
            request_id = f"REQ-{index}"
            diagnostics.begin_request(
                request_id=request_id,
                model="model",
                messages=[
                    {
                        "role": "user",
                        "content": "m" * 100_001 if index == 200 else "message",
                    }
                ],
                max_attempts=1,
            )
            diagnostics.complete_request(
                request_id,
                content="c" * 100_001 if index == 200 else "content",
                reasoning_content=(
                    "r" * 20_001 if index == 200 else "reasoning"
                ),
            )

    feed = diagnostics.snapshot()["requests"]
    assert feed["total"] == 201
    assert len(feed["items"]) == 201
    assert sum(item["detail_available"] for item in feed["items"]) == 200
    assert feed["items"][0]["has_content"] is True
    assert feed["items"][0]["has_reasoning"] is True
    with pytest.raises(ValueError, match="已从内存释放.*REQ-0"):
        diagnostics.request_detail("REQ-0")
    detail = diagnostics.request_detail("REQ-200")
    assert len(detail["messages"][0]["content"]) == 100_000
    assert detail["messages"][0]["truncated"] is True
    assert len(detail["response_content"]) == 100_000
    assert detail["response_content_truncated"] is True
    assert len(detail["reasoning_content"]) == 20_000
    assert detail["reasoning_content_truncated"] is True

    with diagnostics.activate("next", "proofreading"):
        assert diagnostics.snapshot()["requests"]["items"] == []
        diagnostics.begin_request(
            request_id="REQ-INTERRUPTED",
            model="model",
            messages=[],
            max_attempts=1,
        )
    assert diagnostics.snapshot()["requests"]["items"][0]["status"] == "interrupted"

    with diagnostics.activate("third", "polishing"):
        assert diagnostics.snapshot()["requests"]["items"] == []


def test_request_summary_feed_is_incremental_and_resets_per_run(
    tmp_path: Path,
) -> None:
    diagnostics = Diagnostics(tmp_path / "logs" / "app.log")

    with diagnostics.activate("sample", "translation"):
        diagnostics.begin_request(
            request_id="REQ-1",
            model="model",
            messages=[],
            max_attempts=1,
        )
        initial = diagnostics.snapshot()["requests"]
        assert initial["reset"] is True
        assert [item["request_id"] for item in initial["items"]] == ["REQ-1"]

        unchanged = diagnostics.snapshot(
            request_session=initial["session_id"],
            request_after=initial["cursor"],
        )["requests"]
        assert unchanged["reset"] is False
        assert unchanged["items"] == []

        diagnostics.complete_request(
            "REQ-1", content="response", reasoning_content=None
        )
        changed = diagnostics.snapshot(
            request_session=initial["session_id"],
            request_after=initial["cursor"],
        )["requests"]
        assert changed["reset"] is False
        assert changed["items"][0]["status"] == "completed"

    with diagnostics.activate("next", "proofreading"):
        reset = diagnostics.snapshot(
            request_session=initial["session_id"],
            request_after=changed["cursor"],
        )["requests"]
        assert reset["reset"] is True
        assert reset["items"] == []


def test_running_request_detail_is_not_pruned_by_terminal_limit(
    tmp_path: Path,
) -> None:
    diagnostics = Diagnostics(tmp_path / "logs" / "app.log")

    with diagnostics.activate("sample", "translation"):
        diagnostics.begin_request(
            request_id="REQ-ACTIVE",
            model="model",
            messages=[{"role": "user", "content": "active"}],
            max_attempts=1,
        )
        for index in range(201):
            request_id = f"REQ-DONE-{index}"
            diagnostics.begin_request(
                request_id=request_id,
                model="model",
                messages=[],
                max_attempts=1,
            )
            diagnostics.complete_request(
                request_id, content="done", reasoning_content=None
            )

        assert diagnostics.request_detail("REQ-ACTIVE")["status"] == "running"
        active_summary = next(
            item
            for item in diagnostics.snapshot()["requests"]["items"]
            if item["request_id"] == "REQ-ACTIVE"
        )
        assert active_summary["detail_available"] is True

        diagnostics.complete_request(
            "REQ-ACTIVE", content="active done", reasoning_content=None
        )
        assert diagnostics.request_detail("REQ-ACTIVE")["response_content"] == (
            "active done"
        )


@pytest.mark.asyncio
async def test_llm_runtime_reports_safe_diagnostics(tmp_path: Path) -> None:
    current = config()
    current["retry"]["base_delay_seconds"] = 0
    current["retry"]["jitter_seconds"] = 0
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="slow")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                "<think>session reasoning</think>\n"
                                '{"type":"end"}'
                            )
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 2,
                    "total_tokens": 10,
                },
            },
        )

    diagnostics = Diagnostics(tmp_path / "logs" / "app.log")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "never-log-this-secret"
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
                on_usage=diagnostics.set_usage,
            ) as llm:
                response, _ = await llm.chat(
                    messages=render_messages(
                        "prompt", {"segments": [{"source": "source secret"}]}
                    ),
                    temperature=0.2,
                    estimated_input_tokens=10,
                )
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()

    assert response.reasoning_content == "session reasoning"
    snapshot = diagnostics.snapshot()
    assert snapshot["metrics"]["http_errors"] == 1
    assert snapshot["metrics"]["retry_count"] == 1
    assert snapshot["metrics"]["rate_limit_waiting_requests"] == 0
    assert snapshot["metrics"]["usage_available"] is False
    assert snapshot["metrics"]["usage_partial"] is True
    assert snapshot["requests"]["total"] == 1
    request_summary = snapshot["requests"]["items"][0]
    assert request_summary["status"] == "completed"
    assert request_summary["attempt_count"] == 2
    assert request_summary["has_content"] is True
    assert request_summary["has_reasoning"] is True
    request_detail = diagnostics.request_detail(request_summary["request_id"])
    assert request_detail["response_content"] == '{"type":"end"}'
    assert request_detail["reasoning_content"] == "session reasoning"
    assert "source secret" in request_detail["messages"][1]["content"]
    persisted = (tmp_path / "logs" / "app.log").read_text("utf-8")
    assert "session reasoning" not in persisted
    assert "source secret" not in persisted
    assert "never-log-this-secret" not in persisted


@pytest.mark.asyncio
async def test_429_wait_is_reported_only_while_sleeping(tmp_path: Path) -> None:
    current = config()
    current["retry"]["http_max_attempts"] = 2
    calls = 0
    wait_started = asyncio.Event()
    release = asyncio.Event()

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "5"},
                text="rate limited",
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"type":"end"}'}}]},
        )

    async def sleeper(_: float) -> None:
        wait_started.set()
        await release.wait()

    diagnostics = Diagnostics(tmp_path / "logs" / "app.log")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "never-log-this-secret"
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
                sleeper=sleeper,
            ) as llm:
                task = asyncio.create_task(
                    llm.chat(
                        messages=render_messages(
                            "prompt", {"segments": [{"source": "source"}]}
                        ),
                        temperature=0.2,
                        estimated_input_tokens=10,
                    )
                )
                await wait_started.wait()
                assert (
                    diagnostics.snapshot()["metrics"][
                        "rate_limit_waiting_requests"
                    ]
                    == 1
                )
                release.set()
                await task
                assert (
                    diagnostics.snapshot()["metrics"][
                        "rate_limit_waiting_requests"
                    ]
                    == 0
                )
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()


@pytest.mark.asyncio
async def test_regular_retry_backoff_is_not_rate_limit_wait(
    tmp_path: Path,
) -> None:
    current = config()
    current["retry"]["http_max_attempts"] = 2
    current["retry"]["base_delay_seconds"] = 5
    current["retry"]["jitter_seconds"] = 0
    calls = 0
    wait_started = asyncio.Event()
    release = asyncio.Event()

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500, text="temporary")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"type":"end"}'}}]},
        )

    async def sleeper(_: float) -> None:
        wait_started.set()
        await release.wait()

    diagnostics = Diagnostics(tmp_path / "logs" / "app.log")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "never-log-this-secret"
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
                sleeper=sleeper,
            ) as llm:
                task = asyncio.create_task(
                    llm.chat(
                        messages=render_messages(
                            "prompt", {"segments": [{"source": "source"}]}
                        ),
                        temperature=0.2,
                        estimated_input_tokens=10,
                    )
                )
                await wait_started.wait()
                assert (
                    diagnostics.snapshot()["metrics"][
                        "rate_limit_waiting_requests"
                    ]
                    == 0
                )
                release.set()
                await task
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_error", "expected_status"),
    [
        ("authentication", "authentication_error", 401),
        ("network", "network_error", None),
        ("parsing", "response_parse_error", 200),
    ],
)
async def test_llm_runtime_reports_safe_failure_categories(
    tmp_path: Path,
    failure: str,
    expected_error: str,
    expected_status: int | None,
) -> None:
    current = config()
    current["retry"]["http_max_attempts"] = 1

    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "authentication":
            return httpx.Response(401, text="secret provider response")
        if failure == "network":
            raise httpx.ReadTimeout("secret network detail", request=request)
        return httpx.Response(200, json={"choices": []})

    diagnostics = Diagnostics(tmp_path / "logs" / "app.log")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "never-log-this-secret"
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
                with pytest.raises((ExternalError, KeyError, IndexError)):
                    await llm.chat(
                        messages=render_messages(
                            "prompt", {"segments": [{"source": "source secret"}]}
                        ),
                        temperature=0.2,
                        estimated_input_tokens=10,
                    )
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()

    summary = diagnostics.snapshot()["requests"]["items"][0]
    assert summary["status"] == "failed"
    assert summary["error"] == expected_error
    detail = diagnostics.request_detail(summary["request_id"])
    assert detail["attempts"][0]["http_status"] == expected_status
    persisted = (tmp_path / "logs" / "app.log").read_text("utf-8")
    assert "source secret" not in persisted
    assert "secret provider response" not in persisted
    assert "secret network detail" not in persisted
    assert "never-log-this-secret" not in persisted


def test_diagnostics_api_filters_and_rejects_unknown_level(tmp_path: Path) -> None:
    app = create_app(projects_root=tmp_path / "projects", app_root=tmp_path)
    logger = logging.getLogger("another_llm_translator.api-test")
    logger.info("visible api log")
    client = TestClient(app)

    response = client.post("/api/v1/diagnostics", json={"q": "VISIBLE"})
    assert response.status_code == 200
    assert [item["message"] for item in response.json()["logs"]] == [
        "visible api log"
    ]
    metrics = response.json()["metrics"]
    assert metrics["average_latency_ms"] is None
    assert metrics["p95_latency_ms"] is None
    assert "latest_latency_ms" not in metrics
    feed = response.json()["requests"]
    delta = client.post(
        "/api/v1/diagnostics",
        json={
            "request_session": feed["session_id"],
            "request_after": feed["cursor"],
        },
    )
    assert delta.status_code == 200
    assert delta.json()["requests"]["reset"] is False
    assert delta.json()["requests"]["items"] == []
    rejected = client.post("/api/v1/diagnostics", json={"level": "trace"})
    assert rejected.status_code == 400
    assert "未知日志级别" in rejected.json()["error"]
    negative_cursor = client.post(
        "/api/v1/diagnostics", json={"request_after": -1}
    )
    assert negative_cursor.status_code == 400
    assert "request_after" in negative_cursor.json()["error"]


def test_diagnostics_request_detail_api_uses_existing_error_contract(
    tmp_path: Path,
) -> None:
    app = create_app(projects_root=tmp_path / "projects", app_root=tmp_path)
    app.state.diagnostics.begin_request(
        request_id="REQ-DETAIL",
        model="model",
        messages=[{"role": "user", "content": "source"}],
        max_attempts=1,
    )
    client = TestClient(app)

    response = client.get("/api/v1/diagnostics/requests/REQ-DETAIL")
    assert response.status_code == 200
    assert response.json()["messages"][0]["content"] == "source"
    missing = client.get("/api/v1/diagnostics/requests/REQ-MISSING")
    assert missing.status_code == 400
    assert "REQ-MISSING" in missing.json()["error"]
