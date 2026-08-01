from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.diagnostics import Diagnostics
from app.errors import ExternalError
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


def test_request_exchange_and_exact_usage_are_session_only(tmp_path: Path) -> None:
    diagnostics = Diagnostics(tmp_path / "logs" / "app.log")
    logger = get_logger("diagnostics-test")

    with diagnostics.activate("sample", "translation"):
        diagnostics.begin_request(
            request_id="REQ-1",
            model="test-model",
            messages=[{"role": "user", "content": "private source"}],
            max_attempts=2,
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
        diagnostics.rate_limit_waited()
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
            }
        )
        logger.info("safe summary")

    snapshot = diagnostics.snapshot()
    metrics = snapshot["metrics"]
    assert metrics["active_requests"] == 0
    assert metrics["http_errors"] == 1
    assert metrics["retry_count"] == 1
    assert metrics["rate_limit_wait_count"] == 1
    assert metrics["latest_latency_ms"] == 100.0
    assert metrics["usage_available"] is True
    assert metrics["input_tokens"] == 12
    assert metrics["output_tokens"] == 3
    assert metrics["throughput_tokens_per_second"] is not None
    assert "reasoning" not in snapshot
    assert snapshot["requests"] == [
        {
            "timestamp": snapshot["requests"][0]["timestamp"],
            "project": "sample",
            "stage": "translation",
            "request_id": "REQ-1",
            "model": "test-model",
            "status": "completed",
            "attempt_count": 2,
            "last_http_status": 200,
            "latest_latency_ms": 100.0,
            "has_content": True,
            "has_reasoning": True,
            "error": None,
        }
    ]
    assert "messages" not in snapshot["requests"][0]
    detail = diagnostics.request_detail("REQ-1")
    assert detail["messages"][0]["content"] == "private source"
    assert detail["response_content"] == "private response"
    assert detail["reasoning_content"] == "private chain"
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
        }
    )
    unavailable = diagnostics.snapshot()["metrics"]
    assert unavailable["input_tokens"] == 0
    assert unavailable["output_tokens"] == 0
    assert unavailable["throughput_tokens_per_second"] is None


def test_request_exchanges_are_bounded_truncated_and_cleared(tmp_path: Path) -> None:
    diagnostics = Diagnostics(tmp_path / "logs" / "app.log")

    with diagnostics.activate("sample", "translation"):
        for index in range(51):
            request_id = f"REQ-{index}"
            diagnostics.begin_request(
                request_id=request_id,
                model="model",
                messages=[{"role": "user", "content": "m" * 100_001}],
                max_attempts=1,
            )
            diagnostics.complete_request(
                request_id,
                content="c" * 100_001,
                reasoning_content="r" * 20_001,
            )

    assert len(diagnostics.snapshot()["requests"]) == 50
    with pytest.raises(ValueError, match="REQ-0"):
        diagnostics.request_detail("REQ-0")
    detail = diagnostics.request_detail("REQ-50")
    assert len(detail["messages"][0]["content"]) == 100_000
    assert detail["messages"][0]["truncated"] is True
    assert len(detail["response_content"]) == 100_000
    assert detail["response_content_truncated"] is True
    assert len(detail["reasoning_content"]) == 20_000
    assert detail["reasoning_content_truncated"] is True

    with diagnostics.activate("next", "proofreading"):
        assert diagnostics.snapshot()["requests"] == []
        diagnostics.begin_request(
            request_id="REQ-INTERRUPTED",
            model="model",
            messages=[],
            max_attempts=1,
        )
    assert diagnostics.snapshot()["requests"][0]["status"] == "interrupted"

    with diagnostics.activate("third", "polishing"):
        assert diagnostics.snapshot()["requests"] == []


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
    assert len(snapshot["requests"]) == 1
    request_summary = snapshot["requests"][0]
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

    summary = diagnostics.snapshot()["requests"][0]
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
