from __future__ import annotations

import contextvars
import copy
import logging
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterator

from .logging_utils import LOGGER_NAME


_ACTIVE: contextvars.ContextVar[Diagnostics | None] = contextvars.ContextVar(
    "diagnostics", default=None
)
_PROJECT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "diagnostics_project", default="-"
)
_HANDLER_MARKER = "_minimal_llm_translator_handler"
_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_REQUEST_LIMIT = 50
_MESSAGE_LIMIT = 100_000
_CONTENT_LIMIT = 100_000
_REASONING_LIMIT = 20_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def current_diagnostics() -> Diagnostics | None:
    return _ACTIVE.get()


def _bounded(value: str, limit: int) -> tuple[str, bool]:
    return value[:limit], len(value) > limit


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "stage"):
            record.stage = "app"
        record.project = _PROJECT.get()
        return True


class _MemoryHandler(logging.Handler):
    def __init__(self, diagnostics: Diagnostics) -> None:
        super().__init__(logging.INFO)
        self.diagnostics = diagnostics

    def emit(self, record: logging.LogRecord) -> None:
        self.diagnostics.logs.append(
            {
                "timestamp": _now(),
                "level": record.levelname,
                "project": str(getattr(record, "project", "-")),
                "stage": str(getattr(record, "stage", "app")),
                "message": record.getMessage()[:2000],
            }
        )


class Diagnostics:
    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self.logs: deque[dict[str, Any]] = deque(maxlen=1000)
        self.requests: deque[dict[str, Any]] = deque(maxlen=_REQUEST_LIMIT)
        self.project: str | None = None
        self.stage: str | None = None
        self.active_requests = 0
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
        for handler in list(logger.handlers):
            if getattr(handler, _HANDLER_MARKER, None) in {
                "web-global",
                "web-memory",
            }:
                logger.removeHandler(handler)
                handler.close()
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
        self.http_errors = 0
        self.retry_count = 0
        self.rate_limit_waiting_requests = 0
        self.latest_latency_seconds = None
        self.usage = None
        self.requests.clear()
        self._started_monotonic = time.monotonic()
        self._elapsed_seconds = 0.0
        self._running = True
        try:
            yield
        finally:
            for request in self.requests:
                if request["status"] in {"running", "retrying"}:
                    request["status"] = "interrupted"
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
    ) -> None:
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
        self.requests.append(
            {
                "timestamp": _now(),
                "project": self.project,
                "stage": self.stage,
                "request_id": request_id,
                "model": model,
                "status": "running",
                "max_attempts": max_attempts,
                "messages": normalized_messages,
                "response_content": None,
                "response_content_truncated": False,
                "reasoning_content": None,
                "reasoning_content_truncated": False,
                "attempts": [],
                "error": None,
            }
        )

    def _request(self, request_id: str) -> dict[str, Any] | None:
        return next(
            (
                request
                for request in reversed(self.requests)
                if request["request_id"] == request_id
            ),
            None,
        )

    def request_started(self, request_id: str) -> None:
        self.active_requests += 1
        request = self._request(request_id)
        if request is not None:
            request["status"] = "running"

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
            request["status"] = (
                "retrying" if attempt < request["max_attempts"] else "failed"
            )

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
        if reasoning_content is not None:
            reasoning, reasoning_truncated = _bounded(
                reasoning_content, _REASONING_LIMIT
            )
            request["reasoning_content"] = reasoning
            request["reasoning_content_truncated"] = reasoning_truncated
        request["status"] = "completed"

    def fail_request(self, request_id: str, error: str) -> None:
        request = self._request(request_id)
        if request is not None:
            request["status"] = "failed"
            request["error"] = error

    def request_detail(self, request_id: str) -> dict[str, Any]:
        request = self._request(request_id)
        if request is None:
            raise ValueError(f"本次运行中不存在请求：{request_id}")
        return copy.deepcopy(request)

    def snapshot(
        self,
        *,
        level: str | None = None,
        project: str | None = None,
        stage: str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        normalized_level = level.upper() if level else None
        if normalized_level is not None and normalized_level not in _LEVELS:
            raise ValueError(f"未知日志级别：{level}")
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
        throughput = None
        if usage_available and elapsed > 0:
            throughput = round(
                (
                    int(self.usage["input_tokens"])
                    + int(self.usage["output_tokens"])
                )
                / elapsed,
                2,
            )
        return {
            "metrics": {
                "project": self.project,
                "stage": self.stage,
                "active_requests": self.active_requests,
                "http_errors": self.http_errors,
                "retry_count": self.retry_count,
                "rate_limit_waiting_requests": self.rate_limit_waiting_requests,
                "latest_latency_ms": (
                    round(self.latest_latency_seconds * 1000, 1)
                    if self.latest_latency_seconds is not None
                    else None
                ),
                "input_tokens": (
                    int(self.usage["input_tokens"]) if usage_available else 0
                ),
                "output_tokens": (
                    int(self.usage["output_tokens"]) if usage_available else 0
                ),
                "usage_available": usage_available,
                "throughput_tokens_per_second": throughput,
            },
            "logs": logs,
            "requests": [
                {
                    "timestamp": request["timestamp"],
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
                    "has_content": request["response_content"] is not None,
                    "has_reasoning": request["reasoning_content"] is not None,
                    "error": request["error"],
                }
                for request in self.requests
            ],
            "filters": {
                "levels": sorted({item["level"] for item in self.logs}),
                "projects": sorted({item["project"] for item in self.logs}),
                "stages": sorted({item["stage"] for item in self.logs}),
            },
        }
