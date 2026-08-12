from __future__ import annotations

import contextvars
import copy
import logging
import time
import uuid
from collections import deque
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterator

from .logging_utils import (
    LOGGER_NAME,
    _ContextFilter,
    _HANDLER_MARKER,
    _MemoryHandler,
    _PROJECT,
    _now,
    _remove_handler,
)


_ACTIVE: contextvars.ContextVar[Diagnostics | None] = contextvars.ContextVar(
    "diagnostics", default=None
)
_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_REQUEST_DETAIL_LIMIT = 200
_MESSAGE_LIMIT = 100_000
_CONTENT_LIMIT = 100_000
_REASONING_LIMIT = 20_000
_TERMINAL_REQUEST_STATUSES = {"completed", "failed", "interrupted"}


def current_diagnostics() -> Diagnostics | None:
    return _ACTIVE.get()


def _bounded(value: str, limit: int) -> tuple[str, bool]:
    return value[:limit], len(value) > limit


class Diagnostics:
    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self.logs: deque[dict[str, Any]] = deque(maxlen=1000)
        self.requests: dict[str, dict[str, Any]] = {}
        self._retained_terminal_details: deque[str] = deque()
        self._request_session = uuid.uuid4().hex
        self._request_cursor = 0
        self.project: str | None = None
        self.stage: str | None = None
        self.active_requests = 0
        self.total_requests = 0
        self.http_errors = 0
        self.retry_count = 0
        self.rate_limit_waiting_requests = 0
        self.latest_latency_seconds: float | None = None
        self.usage: dict[str, Any] | None = None
        self._started_monotonic: float | None = None
        self._elapsed_seconds = 0.0
        self._running = False
        self._install_handlers()

    def _install_handlers(self) -> None:
        logger = logging.getLogger(LOGGER_NAME)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        for kind in ("web-global", "web-memory"):
            _remove_handler(kind)
        context_filter = _ContextFilter()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        disk = RotatingFileHandler(
            self.log_path,
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        setattr(disk, _HANDLER_MARKER, "web-global")
        disk.setLevel(logging.INFO)
        disk.addFilter(context_filter)
        disk.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s [%(project)s] [%(stage)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        memory = _MemoryHandler(self)
        setattr(memory, _HANDLER_MARKER, "web-memory")
        memory.addFilter(context_filter)
        logger.addHandler(disk)
        logger.addHandler(memory)

    @contextmanager
    def activate(self, project: str, stage: str) -> Iterator[None]:
        active_token = _ACTIVE.set(self)
        project_token = _PROJECT.set(project)
        self.project = project
        self.stage = stage
        self.active_requests = 0
        self.total_requests = 0
        self.http_errors = 0
        self.retry_count = 0
        self.rate_limit_waiting_requests = 0
        self.latest_latency_seconds = None
        self.usage = None
        self.requests.clear()
        self._retained_terminal_details.clear()
        self._request_session = uuid.uuid4().hex
        self._request_cursor = 0
        self._started_monotonic = time.monotonic()
        self._elapsed_seconds = 0.0
        self._running = True
        try:
            yield
        finally:
            for request in self.requests.values():
                if request["status"] in {"running", "retrying"}:
                    self._finish_request(request, status="interrupted")
            self.active_requests = 0
            self.rate_limit_waiting_requests = 0
            if self._started_monotonic is not None:
                self._elapsed_seconds = time.monotonic() - self._started_monotonic
            self._running = False
            _PROJECT.reset(project_token)
            _ACTIVE.reset(active_token)

    def begin_request(
        self,
        *,
        request_id: str,
        model: str,
        messages: list[dict[str, str]],
        max_attempts: int,
        segment_id_map: dict[str, str] | None = None,
    ) -> None:
        self.total_requests += 1
        normalized_messages = []
        for message in messages:
            content, truncated = _bounded(
                str(message.get("content", "")), _MESSAGE_LIMIT
            )
            normalized_messages.append(
                {
                    "role": str(message.get("role", "")),
                    "content": content,
                    "truncated": truncated,
                }
            )
        request = {
            "timestamp": _now(),
            "finished_at": None,
            "project": self.project,
            "stage": self.stage,
            "request_id": request_id,
            "model": model,
            "segment_id_map": dict(segment_id_map or {}),
            "status": "running",
            "max_attempts": max_attempts,
            "messages": normalized_messages,
            "response_content": None,
            "response_content_truncated": False,
            "reasoning_content": None,
            "reasoning_content_truncated": False,
            "has_content": False,
            "has_reasoning": False,
            "attempts": [],
            "error": None,
            "detail_available": True,
            "_revision": 0,
        }
        self.requests[request_id] = request
        self._touch_request(request)

    def _request(self, request_id: str) -> dict[str, Any] | None:
        return self.requests.get(request_id)

    def _touch_request(self, request: dict[str, Any]) -> None:
        self._request_cursor += 1
        request["_revision"] = self._request_cursor

    def _finish_request(
        self,
        request: dict[str, Any],
        *,
        status: str,
        error: str | None = None,
    ) -> None:
        was_terminal = request["status"] in _TERMINAL_REQUEST_STATUSES
        request["status"] = status
        if error is not None:
            request["error"] = error
        if not was_terminal:
            request["finished_at"] = _now()
            self._retained_terminal_details.append(request["request_id"])
        self._touch_request(request)
        self._prune_terminal_details()

    def _prune_terminal_details(self) -> None:
        while len(self._retained_terminal_details) > _REQUEST_DETAIL_LIMIT:
            request_id = self._retained_terminal_details.popleft()
            request = self.requests.get(request_id)
            if request is None or not request["detail_available"]:
                continue
            request["detail_available"] = False
            request["segment_id_map"] = {}
            request["messages"] = []
            request["response_content"] = None
            request["reasoning_content"] = None
            self._touch_request(request)

    def request_started(self, request_id: str) -> None:
        self.active_requests += 1
        request = self._request(request_id)
        if request is not None:
            request["status"] = "running"
            self._touch_request(request)

    def request_finished(
        self,
        *,
        request_id: str,
        attempt: int,
        latency_seconds: float,
        status: int | None,
        error: bool,
    ) -> None:
        self.active_requests = max(0, self.active_requests - 1)
        self.latest_latency_seconds = latency_seconds
        if error or (status is not None and status >= 400):
            self.http_errors += 1
        request = self._request(request_id)
        if request is None:
            return
        outcome = (
            "network_error"
            if status is None
            else "http_error"
            if status >= 400
            else "succeeded"
        )
        request["attempts"].append(
            {
                "attempt": attempt,
                "http_status": status,
                "latency_ms": round(latency_seconds * 1000, 1),
                "outcome": outcome,
            }
        )
        if error:
            if attempt < request["max_attempts"]:
                request["status"] = "retrying"
                self._touch_request(request)
            else:
                self._finish_request(request, status="failed")
        else:
            self._touch_request(request)

    def retried(self) -> None:
        self.retry_count += 1

    def rate_limit_wait_started(self) -> None:
        if self._running:
            self.rate_limit_waiting_requests += 1

    def rate_limit_wait_finished(self) -> None:
        self.rate_limit_waiting_requests = max(
            0, self.rate_limit_waiting_requests - 1
        )

    def set_usage(self, usage: dict[str, Any]) -> None:
        self.usage = dict(usage)

    def complete_request(
        self, request_id: str, *, content: str, reasoning_content: str | None
    ) -> None:
        request = self._request(request_id)
        if request is None:
            return
        response_content, content_truncated = _bounded(content, _CONTENT_LIMIT)
        request["response_content"] = response_content
        request["response_content_truncated"] = content_truncated
        request["has_content"] = True
        if reasoning_content is not None:
            reasoning, reasoning_truncated = _bounded(
                reasoning_content, _REASONING_LIMIT
            )
            request["reasoning_content"] = reasoning
            request["reasoning_content_truncated"] = reasoning_truncated
            request["has_reasoning"] = True
        self._finish_request(request, status="completed")

    def fail_request(self, request_id: str, error: str) -> None:
        request = self._request(request_id)
        if request is not None:
            self._finish_request(request, status="failed", error=error)

    def request_detail(self, request_id: str) -> dict[str, Any]:
        request = self._request(request_id)
        if request is None:
            raise ValueError(f"本次运行中不存在请求：{request_id}")
        if not request["detail_available"]:
            raise ValueError(f"请求详情已从内存释放：{request_id}")
        return copy.deepcopy(
            {key: value for key, value in request.items() if key != "_revision"}
        )

    def snapshot(
        self,
        *,
        level: str | None = None,
        project: str | None = None,
        stage: str | None = None,
        query: str | None = None,
        request_session: str | None = None,
        request_after: int | None = None,
    ) -> dict[str, Any]:
        normalized_level = level.upper() if level else None
        if normalized_level is not None and normalized_level not in _LEVELS:
            raise ValueError(f"未知日志级别：{level}")
        if request_after is not None and request_after < 0:
            raise ValueError("request_after 不能小于 0")
        query_text = query.casefold() if query else None
        logs = [
            item
            for item in self.logs
            if (normalized_level is None or item["level"] == normalized_level)
            and (project is None or item["project"] == project)
            and (stage is None or item["stage"] == stage)
            and (
                query_text is None
                or query_text in str(item["message"]).casefold()
            )
        ]
        elapsed = self._elapsed_seconds
        if self._started_monotonic is not None and self._running:
            elapsed = time.monotonic() - self._started_monotonic
        usage_available = bool(self.usage and self.usage.get("available") is True)
        input_tokens = int(self.usage["input_tokens"]) if usage_available else 0
        output_tokens = int(self.usage["output_tokens"]) if usage_available else 0
        throughput_input = None
        throughput_output = None
        throughput_total = None
        if usage_available and elapsed > 0:
            throughput_input = round(input_tokens / elapsed, 2)
            throughput_output = round(output_tokens / elapsed, 2)
            throughput_total = round((input_tokens + output_tokens) / elapsed, 2)
        return {
            "metrics": {
                "project": self.project,
                "stage": self.stage,
                "active_requests": self.active_requests,
                "total_requests": self.total_requests,
                "http_errors": self.http_errors,
                "retry_count": self.retry_count,
                "rate_limit_waiting_requests": self.rate_limit_waiting_requests,
                "latest_latency_ms": (
                    round(self.latest_latency_seconds * 1000, 1)
                    if self.latest_latency_seconds is not None
                    else None
                ),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "usage_available": usage_available,
                "throughput_input_tokens_per_second": throughput_input,
                "throughput_output_tokens_per_second": throughput_output,
                "throughput_tokens_per_second": throughput_total,
            },
            "logs": logs,
            "requests": {
                "session_id": self._request_session,
                "cursor": self._request_cursor,
                "reset": (
                    request_session != self._request_session
                    or request_after is None
                ),
                "total": len(self.requests),
                "items": [
                    {
                        "timestamp": request["timestamp"],
                        "finished_at": request["finished_at"],
                        "project": request["project"],
                        "stage": request["stage"],
                        "request_id": request["request_id"],
                        "model": request["model"],
                        "status": request["status"],
                        "attempt_count": len(request["attempts"]),
                        "last_http_status": (
                            request["attempts"][-1]["http_status"]
                            if request["attempts"]
                            else None
                        ),
                        "latest_latency_ms": (
                            request["attempts"][-1]["latency_ms"]
                            if request["attempts"]
                            else None
                        ),
                        "has_content": request["has_content"],
                        "has_reasoning": request["has_reasoning"],
                        "error": request["error"],
                        "detail_available": request["detail_available"],
                    }
                    for request in self.requests.values()
                    if (
                        request_session != self._request_session
                        or request_after is None
                        or request["_revision"] > request_after
                    )
                ],
            },
            "filters": {
                "levels": sorted({item["level"] for item in self.logs}),
                "projects": sorted({item["project"] for item in self.logs}),
                "stages": sorted({item["stage"] for item in self.logs}),
            },
        }
