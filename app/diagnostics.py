from __future__ import annotations

import contextvars
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def current_diagnostics() -> Diagnostics | None:
    return _ACTIVE.get()


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
        self.reasoning: deque[dict[str, Any]] = deque(maxlen=200)
        self.project: str | None = None
        self.stage: str | None = None
        self.active_requests = 0
        self.request_count = 0
        self.http_errors = 0
        self.retry_count = 0
        self.rate_limit_wait_count = 0
        self.rate_limit_wait_seconds = 0.0
        self.total_latency_seconds = 0.0
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
        self.request_count = 0
        self.http_errors = 0
        self.retry_count = 0
        self.rate_limit_wait_count = 0
        self.rate_limit_wait_seconds = 0.0
        self.total_latency_seconds = 0.0
        self.latest_latency_seconds = None
        self.usage = None
        self.reasoning.clear()
        self._started_monotonic = time.monotonic()
        self._elapsed_seconds = 0.0
        self._running = True
        try:
            yield
        finally:
            self.active_requests = 0
            if self._started_monotonic is not None:
                self._elapsed_seconds = time.monotonic() - self._started_monotonic
            self._running = False
            _PROJECT.reset(project_token)
            _ACTIVE.reset(active_token)

    def request_started(self) -> None:
        self.active_requests += 1

    def request_finished(
        self, *, latency_seconds: float, status: int | None, error: bool
    ) -> None:
        self.active_requests = max(0, self.active_requests - 1)
        self.request_count += 1
        self.total_latency_seconds += latency_seconds
        self.latest_latency_seconds = latency_seconds
        if error or (status is not None and status >= 400):
            self.http_errors += 1

    def retried(self) -> None:
        self.retry_count += 1

    def rate_limit_waited(self, seconds: float) -> None:
        self.rate_limit_wait_count += 1
        self.rate_limit_wait_seconds += seconds

    def set_usage(self, usage: dict[str, Any]) -> None:
        self.usage = dict(usage)

    def add_reasoning(self, request_id: str, content: str) -> None:
        self.reasoning.append(
            {
                "timestamp": _now(),
                "project": self.project,
                "stage": self.stage,
                "request_id": request_id,
                "content": content[:20000],
            }
        )

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
                "request_count": self.request_count,
                "http_errors": self.http_errors,
                "retry_count": self.retry_count,
                "rate_limit_wait_count": self.rate_limit_wait_count,
                "rate_limit_wait_seconds": round(
                    self.rate_limit_wait_seconds, 3
                ),
                "average_latency_ms": (
                    round(self.total_latency_seconds / self.request_count * 1000, 1)
                    if self.request_count
                    else None
                ),
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
            "reasoning": list(self.reasoning),
            "filters": {
                "levels": sorted({item["level"] for item in self.logs}),
                "projects": sorted({item["project"] for item in self.logs}),
                "stages": sorted({item["stage"] for item in self.logs}),
            },
        }
