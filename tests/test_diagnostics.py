from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.diagnostics import Diagnostics
from app.execution import LLMClient, SlidingWindowLimiter, render_messages
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


def test_reasoning_and_exact_usage_are_session_only(tmp_path: Path) -> None:
    diagnostics = Diagnostics(tmp_path / "logs" / "app.log")
    logger = get_logger("diagnostics-test")

    with diagnostics.activate("sample", "translation"):
        diagnostics.request_started()
        diagnostics.request_finished(latency_seconds=0.25, status=429, error=True)
        diagnostics.retried()
        diagnostics.rate_limit_waited()
        diagnostics.add_reasoning("REQ-1", "private chain")
        diagnostics.set_usage(
            {
                "input_tokens": 12,
                "output_tokens": 3,
                "total_tokens": 15,
                "available": True,
            }
        )
        logger.info("safe summary")

    snapshot = diagnostics.snapshot()
    metrics = snapshot["metrics"]
    assert metrics["active_requests"] == 0
    assert metrics["http_errors"] == 1
    assert metrics["retry_count"] == 1
    assert metrics["rate_limit_wait_count"] == 1
    assert metrics["latest_latency_ms"] == 250.0
    assert metrics["usage_available"] is True
    assert metrics["input_tokens"] == 12
    assert metrics["output_tokens"] == 3
    assert metrics["throughput_tokens_per_second"] is not None
    assert snapshot["reasoning"][0]["content"] == "private chain"
    assert "private chain" not in (tmp_path / "logs" / "app.log").read_text("utf-8")

    diagnostics.set_usage(
        {
            "input_tokens": 12,
            "output_tokens": 3,
            "total_tokens": 15,
            "available": False,
        }
    )
    unavailable = diagnostics.snapshot()["metrics"]
    assert unavailable["input_tokens"] == 0
    assert unavailable["output_tokens"] == 0
    assert unavailable["throughput_tokens_per_second"] is None


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
    assert snapshot["metrics"]["rate_limit_wait_count"] == 1
    assert snapshot["metrics"]["usage_available"] is True
    assert snapshot["reasoning"][0]["content"] == "session reasoning"
    persisted = (tmp_path / "logs" / "app.log").read_text("utf-8")
    assert "session reasoning" not in persisted
    assert "source secret" not in persisted
    assert "never-log-this-secret" not in persisted


def test_diagnostics_api_filters_and_rejects_unknown_level(tmp_path: Path) -> None:
    app = create_app(projects_root=tmp_path / "projects", app_root=tmp_path)
    logger = logging.getLogger("minimal_llm_translator.api-test")
    logger.info("visible api log")
    client = TestClient(app)

    response = client.get("/api/v1/diagnostics", params={"q": "VISIBLE"})
    assert response.status_code == 200
    assert [item["message"] for item in response.json()["logs"]] == [
        "visible api log"
    ]
    rejected = client.get("/api/v1/diagnostics", params={"level": "trace"})
    assert rejected.status_code == 400
    assert "未知日志级别" in rejected.json()["error"]
