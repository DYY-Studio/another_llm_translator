from __future__ import annotations

import contextvars
import copy
import logging
import json
import math
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .request_overview import llm_overview

from .logging_utils import (
    _HANDLER_MARKER,
    _PROJECT,
    LOGGER_NAME,
    _ContextFilter,
    _MemoryHandler,
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
    def __init__(
        self,
        log_path: Path,
        *,
        task_id: str | None = None,
        install_handlers: bool = True,
        log_sink: Any | None = None,
        revision_sink: Callable[[], int] | None = None,
        terminal_sink: Callable[[Diagnostics, str], None] | None = None,
        request_reset_sink: Callable[[], None] | None = None,
    ) -> None:
        self.log_path = log_path
        self.task_id = task_id
        self._log_sink = log_sink or self
        self._revision_sink = revision_sink
        self._terminal_sink = terminal_sink
        self._request_reset_sink = request_reset_sink
        self.logs: deque[dict[str, Any]] = deque(maxlen=1000)
        self.requests: dict[str, dict[str, Any]] = {}
        self._retained_terminal_details: dict[str, deque[str]] = {"llm": deque(), "decision": deque()}
        self._decision_activities: dict[str, dict[str, Any]] = {}
        self._request_session = uuid.uuid4().hex
        self._request_cursor = 0
        self.project: str | None = None
        self.stage: str | None = None
        self.active_requests = 0
        self.total_requests = 0
        self.http_errors = 0
        self.retry_count = 0
        self.rate_limit_waiting_requests = 0
        self._latency_samples_seconds: list[float] = []
        self._latency_total_seconds = 0.0
        self.usage: dict[str, Any] | None = None
        self._invocation_usage: dict[str, Any] | None = None
        self._started_monotonic: float | None = None
        self._elapsed_seconds = 0.0
        self._running = False
        if install_handlers:
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
        memory = _MemoryHandler(self._log_sink)
        setattr(memory, _HANDLER_MARKER, "web-memory")
        memory.addFilter(context_filter)
        logger.addHandler(disk)
        logger.addHandler(memory)

    def _reset_run_state(self) -> None:
        self.active_requests = 0
        self.total_requests = 0
        self.http_errors = 0
        self.retry_count = 0
        self.rate_limit_waiting_requests = 0
        self._latency_samples_seconds.clear()
        self._latency_total_seconds = 0.0
        self.usage = None
        self._invocation_usage = None
        self.requests.clear()
        for retained in self._retained_terminal_details.values():
            retained.clear()
        self._decision_activities.clear()
        self._request_session = uuid.uuid4().hex
        self._request_cursor = 0
        self._started_monotonic = None
        self._elapsed_seconds = 0.0
        self._running = False

    @contextmanager
    def activate(
        self, project: str, stage: str, *, task_id: str | None = None
    ) -> Iterator[None]:
        active_token = _ACTIVE.set(self)
        project_token = _PROJECT.set(project)
        self._reset_run_state()
        self.project = project
        self.stage = stage
        self.task_id = task_id
        self._started_monotonic = time.monotonic()
        self._running = True
        try:
            yield
        finally:
            for request in list(self.requests.values()):
                if request["status"] in {"running", "retrying"}:
                    self._finish_request(request, status="interrupted")
            for activity in self._decision_activities.values():
                activity["active_requests"] = 0
                activity["rate_limit_waiting_requests"] = 0
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
        transport: str = "non_streaming",
        request_kind: str = "llm",
        segment_id: str | None = None,
        question_count: int = 0,
        request_body: str | None = None,
        overview: dict[str, Any] | None = None,
    ) -> None:
        if request_kind == "decision":
            activity = self._decision_activities.setdefault(model, {
                "model": model, "total_requests": 0, "active_requests": 0,
                "completed": 0, "failed": 0, "interrupted": 0, "questions": 0,
                "http_errors": 0, "retry_count": 0, "rate_limit_waiting_requests": 0,
                "latency_total": 0.0, "attempt_count": 0,
                "samples": deque(maxlen=1000), "started": time.monotonic(), "finished": None,
            })
            activity["total_requests"] += 1
            activity["questions"] += question_count
        else:
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
        overview = overview if overview is not None else llm_overview(messages, segment_id_map)
        overview_truncated = len(json.dumps(overview, ensure_ascii=False)) > _MESSAGE_LIMIT
        request = {
            "overview": None if overview_truncated else copy.deepcopy(overview),
            "overview_truncated": overview_truncated,
            "timestamp": _now(),
            "finished_at": None,
            "project": self.project,
            "stage": self.stage,
            "request_id": request_id,
            "task_id": self.task_id,
            "model": model,
            "request_kind": request_kind,
            "segment_id": segment_id,
            "question_count": question_count,
            "request_body": _bounded(request_body, _MESSAGE_LIMIT)[0] if request_body is not None else None,
            "request_body_truncated": request_body is not None and len(request_body) > _MESSAGE_LIMIT,
            "transport": transport,
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
            "stream_event_count": 0,
            "stream_received_bytes": 0,
            "stream_first_event_latency_ms": None,
            "provider_error_status": None,
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
        if self._revision_sink is not None:
            request["_hub_revision"] = self._revision_sink()

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
            self._retained_terminal_details[request["request_kind"]].append(request["request_id"])
            if request["request_kind"] == "decision":
                activity = self._decision_activities[request["model"]]
                activity[status] += 1
                activity["finished"] = time.monotonic()
        self._touch_request(request)
        self._prune_terminal_details()
        if not was_terminal and self._terminal_sink is not None:
            self._terminal_sink(self, request["request_id"])

    def _prune_terminal_details(self) -> None:
        for retained in self._retained_terminal_details.values():
            while len(retained) > _REQUEST_DETAIL_LIMIT:
                request_id = retained.popleft()
                if self.requests.pop(request_id, None) is None:
                    continue
                self._request_session = uuid.uuid4().hex
                if self._request_reset_sink is not None:
                    self._request_reset_sink()

    def request_started(self, request_id: str) -> None:
        request = self._request(request_id)
        if request is not None and request["request_kind"] == "decision":
            self._decision_activities[request["model"]]["active_requests"] += 1
        else:
            self.active_requests += 1
        if request is not None:
            request["status"] = "running"
            request["stream_event_count"] = 0
            request["stream_received_bytes"] = 0
            request["stream_first_event_latency_ms"] = None
            request["provider_error_status"] = None
            self._touch_request(request)

    def stream_progress(
        self,
        request_id: str,
        *,
        event_count: int,
        received_bytes: int,
        first_event_latency_ms: float | None,
    ) -> None:
        request = self._request(request_id)
        if request is None:
            return
        request["stream_event_count"] = event_count
        request["stream_received_bytes"] = received_bytes
        request["stream_first_event_latency_ms"] = first_event_latency_ms
        self._touch_request(request)

    def request_finished(
        self,
        *,
        request_id: str,
        attempt: int,
        retry_round: int | None = None,
        key_index: int | None = None,
        latency_seconds: float,
        status: int | None,
        error: bool,
        retrying: bool = False,
        stream_event_count: int | None = None,
        stream_received_bytes: int | None = None,
        stream_first_event_latency_ms: float | None = None,
        provider_error_status: int | None = None,
        outcome: str | None = None,
    ) -> None:
        request = self._request(request_id)
        if request is not None and request["request_kind"] == "decision":
            activity = self._decision_activities[request["model"]]
            activity["active_requests"] = max(0, activity["active_requests"] - 1)
            activity["samples"].append(latency_seconds)
            activity["latency_total"] += latency_seconds
            activity["attempt_count"] += 1
            if error or (status is not None and status >= 400):
                activity["http_errors"] += 1
        else:
            self.active_requests = max(0, self.active_requests - 1)
            self._latency_samples_seconds.append(latency_seconds)
            self._latency_total_seconds += latency_seconds
            if error or (status is not None and status >= 400):
                self.http_errors += 1
        if request is None:
            return
        request["provider_error_status"] = provider_error_status
        resolved_outcome = outcome or (
            "network_error"
            if status is None
            else "http_error"
            if status >= 400
            else "succeeded"
        )
        request["attempts"].append(
            {
                "attempt": attempt,
                "retry_round": retry_round,
                "key_index": key_index,
                "transport": request["transport"],
                "http_status": status,
                "latency_ms": round(latency_seconds * 1000, 1),
                "outcome": resolved_outcome,
                "provider_error_status": provider_error_status,
                "stream_event_count": (
                    request["stream_event_count"]
                    if stream_event_count is None
                    else stream_event_count
                ),
                "stream_received_bytes": (
                    request["stream_received_bytes"]
                    if stream_received_bytes is None
                    else stream_received_bytes
                ),
                "stream_first_event_latency_ms": (
                    request["stream_first_event_latency_ms"]
                    if stream_first_event_latency_ms is None
                    else stream_first_event_latency_ms
                ),
            }
        )
        if error:
            if request["status"] not in _TERMINAL_REQUEST_STATUSES and (
                retrying or attempt < request["max_attempts"]
            ):
                request["status"] = "retrying"
                self._touch_request(request)
            elif request["status"] not in _TERMINAL_REQUEST_STATUSES:
                if request["request_kind"] == "llm":
                    self._finish_request(request, status="failed")
                else:
                    self._touch_request(request)
        else:
            self._touch_request(request)

    def retried(self, request_id: str | None = None) -> None:
        request = self._request(request_id) if request_id is not None else None
        if request is not None and request["request_kind"] == "decision":
            self._decision_activities[request["model"]]["retry_count"] += 1
        else:
            self.retry_count += 1

    def rate_limit_wait_started(self, request_id: str | None = None) -> None:
        request = self._request(request_id) if request_id is not None else None
        if request is not None and request["request_kind"] == "decision":
            self._decision_activities[request["model"]]["rate_limit_waiting_requests"] += 1
        elif self._running:
            self.rate_limit_waiting_requests += 1

    def rate_limit_wait_finished(self, request_id: str | None = None) -> None:
        request = self._request(request_id) if request_id is not None else None
        if request is not None and request["request_kind"] == "decision":
            activity = self._decision_activities[request["model"]]
            activity["rate_limit_waiting_requests"] = max(0, activity["rate_limit_waiting_requests"] - 1)
        else:
            self.rate_limit_waiting_requests = max(0, self.rate_limit_waiting_requests - 1)

    def set_usage(
        self, usage: dict[str, Any], *, invocation_usage: dict[str, Any] | None = None
    ) -> None:
        self.usage = dict(usage)
        self._invocation_usage = dict(
            usage if invocation_usage is None else invocation_usage
        )

    def complete_request(
        self, request_id: str, *, content: str, reasoning_content: str | None,
        overview_answers: dict[str, Any] | None = None,
    ) -> None:
        request = self._request(request_id)
        if request is None:
            return
        response_content, content_truncated = _bounded(content, _CONTENT_LIMIT)
        if request["overview"] is not None:
            if request["request_kind"] == "llm":
                request["overview"]["response_content"] = response_content
            elif overview_answers is not None:
                request["overview"]["answers"] = copy.deepcopy(overview_answers)
            if len(json.dumps(request["overview"], ensure_ascii=False)) > _CONTENT_LIMIT:
                request["overview"] = None
                request["overview_truncated"] = True
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

    def fail_request(self, request_id: str, error: str, *, interrupted: bool = False) -> None:
        request = self._request(request_id)
        if request is not None:
            if request["attempts"] and request["request_kind"] == "llm":
                request["attempts"][-1]["outcome"] = error
            self._finish_request(request, status="interrupted" if interrupted else "failed", error=error)

    def request_detail(self, request_id: str) -> dict[str, Any]:
        request = self._request(request_id)
        if request is None:
            raise ValueError(f"本次运行中不存在请求：{request_id}")
        if not request["detail_available"]:
            raise ValueError(f"请求详情已从内存释放：{request_id}")
        detail = copy.deepcopy(
            {
                key: value
                for key, value in request.items()
                if key not in {"_revision", "_hub_revision"}
            }
        )
        if self.task_id is None:
            detail.pop("task_id", None)
        return detail

    def _request_summary(self, request: dict[str, Any]) -> dict[str, Any]:
        summary = {
            "timestamp": request["timestamp"],
            "finished_at": request["finished_at"],
            "project": request["project"],
            "stage": request["stage"],
            "request_id": request["request_id"],
            "model": request["model"],
            "request_kind": request["request_kind"],
            "segment_id": request["segment_id"],
            "question_count": request["question_count"],
            "transport": request["transport"],
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
            "stream_event_count": request["stream_event_count"],
            "stream_received_bytes": request["stream_received_bytes"],
            "stream_first_event_latency_ms": request[
                "stream_first_event_latency_ms"
            ],
            "provider_error_status": request["provider_error_status"],
        }
        if request["request_kind"] == "decision":
            summary["overview"] = request["overview"]
            summary["overview_truncated"] = request["overview_truncated"]
        if self.task_id is not None:
            summary["task_id"] = self.task_id
        return summary

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
        usage_partial = bool(self.usage and self.usage.get("partial") is True)
        usage_available = bool(
            self.usage
            and self.usage.get("available") is True
            and not usage_partial
        )
        usage_observed = usage_available or usage_partial
        input_tokens = int(self.usage["input_tokens"]) if usage_observed else 0
        output_tokens = int(self.usage["output_tokens"]) if usage_observed else 0
        throughput_input = None
        throughput_output = None
        throughput_total = None
        invocation = self._invocation_usage
        if (
            invocation
            and (invocation.get("available") or invocation.get("partial"))
            and elapsed > 0
        ):
            throughput_input = round(int(invocation["input_tokens"]) / elapsed, 2)
            throughput_output = round(int(invocation["output_tokens"]) / elapsed, 2)
            throughput_total = round(
                (int(invocation["input_tokens"]) + int(invocation["output_tokens"])) / elapsed,
                2,
            )
        latency_count = len(self._latency_samples_seconds)
        average_latency_ms = None
        p95_latency_ms = None
        if latency_count:
            average_latency_ms = round(
                self._latency_total_seconds / latency_count * 1000, 1
            )
            p95_rank = math.ceil(0.95 * latency_count)
            p95_latency_ms = round(
                sorted(self._latency_samples_seconds)[p95_rank - 1] * 1000, 1
            )
        return {
            "metrics": {
                "project": self.project,
                "stage": self.stage,
                "active_requests": self.active_requests,
                "total_requests": self.total_requests,
                "http_errors": self.http_errors,
                "retry_count": self.retry_count,
                "rate_limit_waiting_requests": self.rate_limit_waiting_requests,
                "average_latency_ms": average_latency_ms,
                "p95_latency_ms": p95_latency_ms,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "usage_available": usage_available,
                "usage_partial": usage_partial,
                "throughput_input_tokens_per_second": throughput_input,
                "throughput_output_tokens_per_second": throughput_output,
                "throughput_tokens_per_second": throughput_total,
            },
            "decision": _decision_snapshot([self]),
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
                    self._request_summary(request)
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


def _decision_snapshot(sessions: list[Diagnostics]) -> dict[str, Any]:
    activities = []
    samples = []
    attempts = 0
    latency_total = 0.0
    for session in sessions:
        for activity in session._decision_activities.values():
            pending = activity["total_requests"] - sum(activity[key] for key in ("completed", "failed", "interrupted"))
            end = time.monotonic() if session._running else activity["finished"] or activity["started"]
            elapsed = end - activity["started"]
            recent = sorted(activity["samples"])
            count = activity["attempt_count"]
            activities.append({
                "task_id": session.task_id, "project": session.project, "stage": session.stage,
                **{key: activity[key] for key in (
                    "model", "total_requests", "active_requests", "completed", "failed", "interrupted",
                    "questions", "http_errors", "retry_count", "rate_limit_waiting_requests",
                )},
                "pending": pending,
                "requests_per_second": round(activity["total_requests"] / elapsed, 2) if elapsed > 0 else None,
                "average_latency_ms": round(activity["latency_total"] / count * 1000, 1) if count else None,
                "p95_latency_ms": round(recent[math.ceil(len(recent) * .95) - 1] * 1000, 1) if recent else None,
            })
            samples.extend(recent)
            attempts += count
            latency_total += activity["latency_total"]
    samples.sort()
    metrics = {key: sum(item[key] for item in activities) for key in (
        "total_requests", "active_requests", "http_errors", "retry_count", "rate_limit_waiting_requests",
    )}
    metrics.update(
        average_latency_ms=round(latency_total / attempts * 1000, 1) if attempts else None,
        p95_latency_ms=round(samples[math.ceil(len(samples) * .95) - 1] * 1000, 1) if samples else None,
    )
    return {"metrics": metrics, "activities": activities}


class RunDiagnostics(Diagnostics):
    """Diagnostics state owned by one running Web task."""


class DiagnosticsHub(Diagnostics):
    """Aggregate independent diagnostics sessions for concurrent Web tasks."""

    def __init__(self, log_path: Path) -> None:
        super().__init__(log_path)
        self.sessions: dict[str, Diagnostics] = {}
        self._hub_request_session = uuid.uuid4().hex
        self._hub_request_cursor = 0
        self._hub_retained_terminal_details: dict[str, deque[tuple[Diagnostics, str]]] = {"llm": deque(), "decision": deque()}

    def _next_hub_revision(self) -> int:
        self._hub_request_cursor += 1
        return self._hub_request_cursor

    def _reset_hub_request_feed(self) -> None:
        self._hub_request_session = uuid.uuid4().hex

    def _touch_request(self, request: dict[str, Any]) -> None:
        super()._touch_request(request)
        request["_hub_revision"] = self._next_hub_revision()

    def _active_session(self) -> Diagnostics | None:
        active = current_diagnostics()
        if active is self or active is None:
            return None
        if active.task_id is None or self.sessions.get(active.task_id) is not active:
            return None
        return active

    def _retain_terminal_detail(
        self, session: Diagnostics, request_id: str
    ) -> None:
        request = session.requests.get(request_id)
        if request is None or not request["detail_available"]:
            return
        retained = self._hub_retained_terminal_details[request["request_kind"]]
        retained.append((session, request_id))
        while len(retained) > _REQUEST_DETAIL_LIMIT:
            old_session, old_request_id = retained.popleft()
            old_request = old_session.requests.get(old_request_id)
            if old_request is None:
                continue
            del old_session.requests[old_request_id]
            self._reset_hub_request_feed()

    def _prune_terminal_details(self) -> None:
        previous_count = len(self.requests)
        super()._prune_terminal_details()
        if len(self.requests) < previous_count:
            self._reset_hub_request_feed()

    def _has_running_sessions(self) -> bool:
        return self._running or any(
            session._running for session in self.sessions.values()
        )

    def _reset_batch(self) -> None:
        self.sessions.clear()
        for retained in self._hub_retained_terminal_details.values():
            retained.clear()
        self._reset_run_state()
        self.project = None
        self.stage = None
        self.task_id = None
        self._reset_hub_request_feed()

    def begin_request(self, **kwargs: Any) -> None:
        target = self._active_session()
        if target is not None:
            target.begin_request(**kwargs)
            return
        super().begin_request(**kwargs)

    def request_started(self, request_id: str) -> None:
        target = self._active_session()
        if target is not None:
            target.request_started(request_id)
            return
        super().request_started(request_id)

    def stream_progress(self, request_id: str, **kwargs: Any) -> None:
        target = self._active_session()
        if target is not None:
            target.stream_progress(request_id, **kwargs)
            return
        super().stream_progress(request_id, **kwargs)

    def request_finished(self, **kwargs: Any) -> None:
        target = self._active_session()
        if target is not None:
            target.request_finished(**kwargs)
            return
        super().request_finished(**kwargs)

    def retried(self, request_id: str | None = None) -> None:
        target = self._active_session()
        if target is not None:
            target.retried(request_id)
            return
        super().retried(request_id)

    def rate_limit_wait_started(self, request_id: str | None = None) -> None:
        target = self._active_session()
        if target is not None:
            target.rate_limit_wait_started(request_id)
            return
        super().rate_limit_wait_started(request_id)

    def rate_limit_wait_finished(self, request_id: str | None = None) -> None:
        target = self._active_session()
        if target is not None:
            target.rate_limit_wait_finished(request_id)
            return
        super().rate_limit_wait_finished(request_id)

    def complete_request(
        self, request_id: str, *, content: str, reasoning_content: str | None,
        overview_answers: dict[str, Any] | None = None,
    ) -> None:
        target = self._active_session()
        if target is not None:
            target.complete_request(
                request_id, content=content, reasoning_content=reasoning_content, overview_answers=overview_answers
            )
            return
        super().complete_request(
            request_id, content=content, reasoning_content=reasoning_content, overview_answers=overview_answers
        )

    def fail_request(self, request_id: str, error: str, *, interrupted: bool = False) -> None:
        target = self._active_session()
        if target is not None:
            target.fail_request(request_id, error, interrupted=interrupted)
            return
        super().fail_request(request_id, error, interrupted=interrupted)

    @contextmanager
    def activate(
        self,
        project: str,
        stage: str,
        *,
        task_id: str | None = None,
    ) -> Iterator[None]:
        if not self._has_running_sessions():
            self._reset_batch()
        if task_id is None:
            with super().activate(project, stage):
                yield
            return
        session = RunDiagnostics(
            self.log_path,
            task_id=task_id,
            install_handlers=False,
            log_sink=self,
            revision_sink=self._next_hub_revision,
            terminal_sink=self._retain_terminal_detail,
            request_reset_sink=self._reset_hub_request_feed,
        )
        self.sessions[task_id] = session
        with session.activate(project, stage, task_id=task_id):
            yield

    def set_usage(
        self, usage: dict[str, Any], *, invocation_usage: dict[str, Any] | None = None
    ) -> None:
        target = self._active_session()
        if target is not None:
            target.set_usage(usage, invocation_usage=invocation_usage)
            return
        super().set_usage(usage, invocation_usage=invocation_usage)

    @staticmethod
    def _session_matches(
        session: Diagnostics,
        *,
        project: str | None,
        stage: str | None,
    ) -> bool:
        return (
            (project is None or session.project == project)
            and (stage is None or session.stage == stage)
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
        metric_sessions = [
            session
            for session in self.sessions.values()
            if self._session_matches(session, project=project, stage=stage)
        ]
        if (
            (self._running or self.project is not None)
            and self._session_matches(self, project=project, stage=stage)
        ):
            metric_sessions.append(self)
        running_sessions = [
            session for session in metric_sessions if session._running
        ]

        active_requests = sum(session.active_requests for session in metric_sessions)
        total_requests = sum(session.total_requests for session in metric_sessions)
        http_errors = sum(session.http_errors for session in metric_sessions)
        retry_count = sum(session.retry_count for session in metric_sessions)
        waiting_requests = sum(
            session.rate_limit_waiting_requests for session in metric_sessions
        )
        latency_samples = [
            sample
            for session in metric_sessions
            for sample in session._latency_samples_seconds
        ]
        latency_count = len(latency_samples)
        average_latency_ms = (
            round(sum(latency_samples) / latency_count * 1000, 1)
            if latency_count
            else None
        )
        p95_latency_ms = None
        if latency_count:
            p95_rank = math.ceil(0.95 * latency_count)
            p95_latency_ms = round(
                sorted(latency_samples)[p95_rank - 1] * 1000, 1
            )

        usages = [session.usage for session in metric_sessions]
        usage_values = [usage for usage in usages if isinstance(usage, dict)]
        has_partial_usage = any(
            usage.get("partial") is True for usage in usage_values
        )
        has_exact_usage = any(
            usage.get("available") is True
            and usage.get("partial") is not True
            for usage in usage_values
        )
        has_unavailable_usage = len(usage_values) != len(usages) or any(
            usage.get("available") is not True
            and usage.get("partial") is not True
            for usage in usage_values
        )
        complete_usage = bool(usages) and not has_partial_usage and not has_unavailable_usage
        partial_usage = has_partial_usage or (has_exact_usage and has_unavailable_usage)
        observed_usage = complete_usage or partial_usage
        input_tokens = (
            sum(int(usage["input_tokens"]) for usage in usages if usage is not None)
            if observed_usage
            else 0
        )
        output_tokens = (
            sum(int(usage["output_tokens"]) for usage in usages if usage is not None)
            if observed_usage
            else 0
        )
        start_times = [
            session._started_monotonic
            for session in metric_sessions
            if session._started_monotonic is not None
        ]
        finish_times = [
            (
                time.monotonic()
                if session._running
                else session._started_monotonic + session._elapsed_seconds
            )
            for session in metric_sessions
            if session._started_monotonic is not None
        ]
        elapsed = (
            max(finish_times) - min(start_times)
            if start_times and finish_times
            else 0.0
        )
        throughput_input = None
        throughput_output = None
        throughput_total = None
        invocations = [
            session._invocation_usage
            for session in metric_sessions
            if session._invocation_usage is not None
            and (
                session._invocation_usage.get("available")
                or session._invocation_usage.get("partial")
            )
        ]
        if invocations and elapsed > 0:
            invocation_input = sum(int(usage["input_tokens"]) for usage in invocations)
            invocation_output = sum(int(usage["output_tokens"]) for usage in invocations)
            throughput_input = round(invocation_input / elapsed, 2)
            throughput_output = round(invocation_output / elapsed, 2)
            throughput_total = round((invocation_input + invocation_output) / elapsed, 2)

        all_requests: list[tuple[Diagnostics, dict[str, Any]]] = [
            (session, request)
            for session in metric_sessions
            for request in session.requests.values()
        ]
        reset = request_session != self._hub_request_session or request_after is None
        request_items = [
            session._request_summary(request)
            for session, request in all_requests
            if reset
            or request.get("_hub_revision", -1) > int(request_after or 0)
        ]
        if len(running_sessions) == 1:
            metric_project = running_sessions[0].project
            metric_stage = running_sessions[0].stage
        else:
            metric_project = None
            metric_stage = None
        return {
            "metrics": {
                "project": metric_project,
                "stage": metric_stage,
                "active_requests": active_requests,
                "total_requests": total_requests,
                "http_errors": http_errors,
                "retry_count": retry_count,
                "rate_limit_waiting_requests": waiting_requests,
                "average_latency_ms": average_latency_ms,
                "p95_latency_ms": p95_latency_ms,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "usage_available": complete_usage,
                "usage_partial": partial_usage,
                "throughput_input_tokens_per_second": throughput_input,
                "throughput_output_tokens_per_second": throughput_output,
                "throughput_tokens_per_second": throughput_total,
            },
            "decision": _decision_snapshot(metric_sessions),
            "logs": logs,
            "requests": {
                "session_id": self._hub_request_session,
                "cursor": self._hub_request_cursor,
                "reset": reset,
                "total": len(all_requests),
                "items": request_items,
            },
            "filters": {
                "levels": sorted({item["level"] for item in self.logs}),
                "projects": sorted({item["project"] for item in self.logs}),
                "stages": sorted({item["stage"] for item in self.logs}),
            },
        }

    def request_detail(self, request_id: str) -> dict[str, Any]:
        request = self.requests.get(request_id)
        if request is not None:
            return super().request_detail(request_id)
        for session in self.sessions.values():
            if request_id in session.requests:
                return session.request_detail(request_id)
        raise ValueError(f"本次运行中不存在请求：{request_id}")
