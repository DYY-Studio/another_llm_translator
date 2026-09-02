from __future__ import annotations
import asyncio
import hashlib
import json
import math
import random
import re
import shutil
import sys
import time
import uuid
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TypeVar
import httpx
from .config import load_project_config, load_run_config
from .credentials import resolve_api_keys
from .diagnostics import current_diagnostics
from .documents import aozora_to_model_ruby
from .errors import (
    ConfigError,
    ContextLengthError,
    ExternalError,
    FatalExternalError,
    RequestSizeError,
    StorageError,
    UsageError,
)
from .i18n import SUPPORTED_LANGUAGES
from .llm_adapter import JSONLLMAdapter, LLMResponse, Usage
from .llm_keys import KeyPool, NoAvailableKey
from .llm_preset import endpoint_url
from .logging_utils import get_logger
from .sqlite_storage import (
    append_jsonl,
    append_jsonl_file,
    atomic_write_json,
    read_json,
    read_jsonl,
    record_header,
    utc_now,
    write_json,
)
from .term_decision_protocol import terminology_decision_protocol

from .llm_response import extract_jsonl_content, normalize_llm_response

class _StreamRetryable(Exception):
    """A stream ended or reported an error before a complete response."""

    def __init__(
        self,
        message: str,
        *,
        events: list[str] | None = None,
        event_count: int = 0,
        received_bytes: int = 0,
        first_event_latency_ms: float | None = None,
        status: int | None = None,
        provider_error_status: int | None = None,
        retry_after: str | None = None,
        usage_values: dict[str, int] | None = None,
    ) -> None:
        super().__init__(message)
        self.events = events or []
        self.event_count = event_count
        self.received_bytes = received_bytes
        self.first_event_latency_ms = first_event_latency_ms
        self.status = status
        self.provider_error_status = provider_error_status
        self.retry_after = retry_after
        self.usage_values = dict(usage_values or {})

class _StreamProtocolError(ExternalError):
    """A malformed successful SSE response that must not be retried."""

    def __init__(
        self,
        message: str,
        *,
        status: int,
        events: list[str],
        event_count: int,
        received_bytes: int,
        first_event_latency_ms: float | None,
        usage_values: dict[str, int] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.events = events
        self.event_count = event_count
        self.received_bytes = received_bytes
        self.first_event_latency_ms = first_event_latency_ms
        self.usage_values = dict(usage_values or {})

async def _iter_sse_data(
    response: httpx.Response,
) -> AsyncIterator[tuple[str, int]]:
    """Yield complete SSE data blocks while preserving exact byte counts."""
    pending = bytearray()
    data_lines: list[str] = []
    received_bytes = 0

    def process_line(line: str) -> tuple[str | None, bool]:
        if line == "":
            if not data_lines:
                return None, True
            value = "\n".join(data_lines)
            data_lines.clear()
            return value, True
        if line.startswith(":"):
            return None, False
        if line.startswith("data:"):
            value = line[5:].removeprefix(" ")
            data_lines.append(value)
        return None, False

    byte_stream = response.aiter_bytes()
    try:
        while True:
            try:
                chunk = await byte_stream.__anext__()
            except StopAsyncIteration:
                break
            pending.extend(chunk)
            while True:
                try:
                    newline = pending.index(10)
                except ValueError:
                    break
                raw_line = bytes(pending[:newline])
                del pending[: newline + 1]
                received_bytes += newline + 1
                if raw_line.endswith(b"\r"):
                    raw_line = raw_line[:-1]
                line = raw_line.decode("utf-8", errors="strict")
                value, boundary = process_line(line)
                if boundary and value is not None:
                    yield value, received_bytes
        if pending:
            received_bytes += len(pending)
            line = bytes(pending).decode("utf-8", errors="strict")
            value, boundary = process_line(line)
            if boundary and value is not None:
                yield value, received_bytes
    except UnicodeDecodeError as exc:
        raise ExternalError("LLM 流式 SSE 不是合法 UTF-8") from exc
    finally:
        close = getattr(byte_stream, "aclose", None)
        if close is not None:
            await close()
    if data_lines:
        yield "\n".join(data_lines), received_bytes

class SlidingWindowLimiter:
    def __init__(
        self,
        requests_per_minute: int,
        input_tokens_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.requests_per_minute = requests_per_minute
        self.input_tokens_per_minute = input_tokens_per_minute
        self.clock = clock
        self.sleeper = sleeper
        self.records: deque[tuple[float, int]] = deque()
        self.lock = asyncio.Lock()
        self.pacing_lock = asyncio.Lock() if requests_per_minute > 0 else None
        self.last_admitted_at: float | None = None

    async def acquire(
        self,
        estimated_tokens: int,
        *,
        on_wait_start: Callable[[], None] | None = None,
        on_wait_end: Callable[[], None] | None = None,
    ) -> float:
        waited = 0.0
        if self.requests_per_minute == 0 and self.input_tokens_per_minute == 0:
            return waited
        if (
            self.input_tokens_per_minute > 0
            and estimated_tokens > self.input_tokens_per_minute
        ):
            raise ConfigError("单请求预测 Token 超过 ITPM")
        waiting = False

        def begin_wait() -> None:
            nonlocal waiting
            if waiting:
                return
            waiting = True
            if on_wait_start is not None:
                on_wait_start()

        async def sleep_for(delay: float) -> None:
            nonlocal waited
            begin_wait()
            await self.sleeper(delay)
            waited += delay

        pacing_lock = self.pacing_lock
        pacing_acquired = False
        try:
            if pacing_lock is not None:
                if pacing_lock.locked():
                    begin_wait()
                await pacing_lock.acquire()
                pacing_acquired = True
            while True:
                async with self.lock:
                    now = self.clock()
                    while self.records and now - self.records[0][0] >= 60:
                        self.records.popleft()
                    pace_wait = 0.0
                    if (
                        self.requests_per_minute > 0
                        and self.last_admitted_at is not None
                    ):
                        pace_wait = max(
                            0.0,
                            self.last_admitted_at + 60 / self.requests_per_minute - now,
                        )
                    request_full = (
                        self.requests_per_minute > 0
                        and len(self.records) >= self.requests_per_minute
                    )
                    token_full = self.input_tokens_per_minute > 0 and (
                        sum(tokens for _, tokens in self.records) + estimated_tokens
                        > self.input_tokens_per_minute
                    )
                    window_wait = 0.0
                    if request_full or token_full:
                        window_wait = max(
                            0.01,
                            60 - (now - self.records[0][0]),
                        )
                    wait = max(pace_wait, window_wait)
                    if wait <= 0:
                        self.records.append((now, estimated_tokens))
                        self.last_admitted_at = now
                        return waited
                await sleep_for(wait)
        finally:
            if waiting and on_wait_end is not None:
                on_wait_end()
            if pacing_lock is not None and pacing_acquired:
                pacing_lock.release()

class LLMClient:
    def __init__(
        self,
        config: dict[str, Any],
        limiter: SlidingWindowLimiter | KeyPool,
        *,
        run_dir: Path,
        project_id: str,
        run_id: str,
        stage: str,
        client: httpx.AsyncClient | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_usage: Callable[[dict[str, Any] | None], None] | None = None,
        preparation_started_at: float | None = None,
    ) -> None:
        self.config = config
        self.limiter = limiter
        self.key_pool = limiter if isinstance(limiter, KeyPool) else None
        self._api_keys: tuple[str, ...] | None = None
        self._key_ids: tuple[str, ...] | None = None
        self._key_audits: list[dict[str, Any]] = []
        self._key_resolution_error: FatalExternalError | None = None
        self._disabled_keys: set[int] = set()
        self.run_dir = run_dir
        self.project_id = project_id
        self.run_id = run_id
        self.stage = stage
        self.client = client
        self.owns_client = client is None
        self.sleeper = sleeper
        self.on_usage = on_usage
        self.preparation_started_at = preparation_started_at
        self.log_lock = asyncio.Lock()
        self.send_count = 0
        self.warnings: list[str] = []
        self._reported_output_clamp = False
        self.usage = Usage(input_tokens=0, output_tokens=0, total_tokens=0)
        self.usage_observed = False
        self.usage_complete = True
        self.logger = get_logger(stage)
        adapter = config.get("_llm_adapter")
        if not isinstance(adapter, JSONLLMAdapter):
            raise ConfigError("项目配置缺少已加载的 LLM Adapter")
        self.adapter = adapter

    def _prepare_keys(self) -> None:
        if self._api_keys is not None:
            return
        if self._key_resolution_error is not None:
            raise self._key_resolution_error
        try:
            self._api_keys = resolve_api_keys(self.config["llm"]["credential"])
        except ExternalError as exc:
            self._key_resolution_error = FatalExternalError(str(exc))
            raise self._key_resolution_error from exc
        self._key_ids = tuple(
            hashlib.sha256(value.encode("utf-8")).hexdigest()
            for value in self._api_keys
        )
        self._key_audits = [
            {
                "key_index": index + 1,
                "request_count": 0,
                "attempt_count": 0,
                "authentication_error_count": 0,
                "rate_limit_count": 0,
                "usage_observed": False,
                "usage_complete": True,
                "usage": Usage(input_tokens=0, output_tokens=0, total_tokens=0),
            }
            for index in range(len(self._api_keys))
        ]
        if self.key_pool is None and len(self._api_keys) > 1:
            execution = self.config["execution"]
            self.key_pool = KeyPool(
                int(execution["requests_per_minute"]),
                int(execution["input_tokens_per_minute"]),
                int(execution["max_parallel"]),
                int(
                    execution.get(
                        "max_parallel_per_key", execution["max_parallel"]
                    )
                ),
                clock=getattr(self.limiter, "clock", time.monotonic),
                sleeper=self.sleeper,
            )

    def _audit_request_key(self, key_index: int, used: set[int]) -> None:
        if key_index in used:
            return
        self._key_audits[key_index]["request_count"] += 1
        used.add(key_index)

    def _audit_attempt(self, key_index: int) -> None:
        self._key_audits[key_index]["attempt_count"] += 1

    def _audit_authentication_error(self, key_index: int) -> None:
        self._key_audits[key_index]["authentication_error_count"] += 1

    def _audit_rate_limit_error(self, key_index: int) -> None:
        self._key_audits[key_index]["rate_limit_count"] += 1

    def _audit_usage_missing(
        self, key_index: int, *, affects_global: bool = False
    ) -> None:
        if self.adapter.usage_pointers is not None:
            if affects_global:
                self.usage_complete = False
            self._key_audits[key_index]["usage_complete"] = False

    def _record_usage(self, usage: Usage, key_index: int) -> None:
        self.usage = Usage(
            input_tokens=self.usage.input_tokens + usage.input_tokens,
            output_tokens=self.usage.output_tokens + usage.output_tokens,
            total_tokens=self.usage.total_tokens + usage.total_tokens,
        )
        self.usage_observed = True
        entry = self._key_audits[key_index]
        previous = entry["usage"]
        entry["usage"] = Usage(
            input_tokens=previous.input_tokens + usage.input_tokens,
            output_tokens=previous.output_tokens + usage.output_tokens,
            total_tokens=previous.total_tokens + usage.total_tokens,
        )
        entry["usage_observed"] = True

    def key_audit_summary(self, *, execution_index: int) -> dict[str, Any]:
        self._prepare_keys()
        keys: list[dict[str, Any]] = []
        for entry in self._key_audits:
            usage = entry["usage"]
            if self.adapter.usage_pointers is None:
                usage_value = None
            else:
                usage_value = {
                    "input_tokens": usage.input_tokens
                    if entry["usage_observed"]
                    else 0,
                    "output_tokens": usage.output_tokens
                    if entry["usage_observed"]
                    else 0,
                    "total_tokens": usage.total_tokens
                    if entry["usage_observed"]
                    else 0,
                    "available": bool(
                        entry["usage_observed"] and entry["usage_complete"]
                    ),
                    "partial": bool(
                        entry["usage_observed"] and not entry["usage_complete"]
                    ),
                }
            keys.append(
                {
                    "key_index": entry["key_index"],
                    "request_count": entry["request_count"],
                    "attempt_count": entry["attempt_count"],
                    "authentication_error_count": entry[
                        "authentication_error_count"
                    ],
                    "rate_limit_count": entry["rate_limit_count"],
                    "usage": usage_value,
                }
            )
        credential = self.config["llm"]["credential"]
        return {
            "credential": {
                "kind": str(credential["kind"]),
                "name": str(credential["name"]),
            },
            "execution_index": int(execution_index),
            "key_count": len(keys),
            "keys": keys,
        }

    async def _stream_attempt(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        request_id: str,
        started: float,
        diagnostics: Any | None,
    ) -> tuple[
        int,
        str,
        dict[str, str],
        LLMResponse,
        dict[str, int],
        list[str],
        int,
        int,
        float | None,
        str,
    ]:
        if self.client is None:
            raise RuntimeError("LLMClient must be used as an async context manager")
        async with self.client.stream(
            "POST", url, headers=headers, json=payload
        ) as response:
            status = response.status_code
            if not 200 <= status < 300:
                await response.aread()
                return (
                    status,
                    response.text,
                    dict(response.headers),
                    LLMResponse("", None),
                    {},
                    [],
                    0,
                    0,
                    None,
                    "",
                )
            content_type = response.headers.get("content-type", "")
            if content_type.split(";", 1)[0].strip().casefold() != "text/event-stream":
                raise _StreamProtocolError(
                    "LLM 流式响应 Content-Type 不是 text/event-stream",
                    status=status,
                    events=[],
                    event_count=0,
                    received_bytes=0,
                    first_event_latency_ms=None,
                    usage_values={},
                )
            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            usage_values: dict[str, int] = {}
            raw_events: list[str] = []
            event_count = 0
            received_bytes = 0
            first_event_latency_ms: float | None = None
            terminal = False
            termination = ""
            terminal_spec = self.adapter.streaming_spec
            if terminal_spec is None:
                raise ConfigError("LLM Adapter 未声明 streaming 规则")
            sentinel = terminal_spec["terminal"].get("sentinel")
            try:
                async for data, received_bytes in _iter_sse_data(response):
                    if not data:
                        continue
                    event_count += 1
                    if first_event_latency_ms is None:
                        first_event_latency_ms = round(
                            (time.monotonic() - started) * 1000, 1
                        )
                    if diagnostics is not None:
                        diagnostics.stream_progress(
                            request_id,
                            event_count=event_count,
                            received_bytes=received_bytes,
                            first_event_latency_ms=first_event_latency_ms,
                        )
                    if sentinel is not None and data == sentinel:
                        raw_events.append(data)
                        terminal = True
                        termination = "explicit"
                        break
                    raw_events.append(data)
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise ExternalError("LLM 流式 SSE data 不是合法 JSON") from exc
                    if not isinstance(event, dict):
                        raise ExternalError("LLM 流式 SSE data 必须是 JSON 对象")
                    for key, value in self.adapter.extract_stream_usage(event).items():
                        usage_values[key] = value
                    stream_error = self.adapter.stream_error_details(event)
                    if stream_error is not None:
                        raise _StreamRetryable(
                            stream_error.message,
                            events=raw_events,
                            event_count=event_count,
                            received_bytes=received_bytes,
                            first_event_latency_ms=first_event_latency_ms,
                            status=status,
                            provider_error_status=(stream_error.provider_error_status),
                            retry_after=response.headers.get("Retry-After"),
                            usage_values=usage_values,
                        )
                    content_parts.extend(self.adapter.stream_content_deltas(event))
                    reasoning_parts.extend(self.adapter.stream_reasoning_deltas(event))
                    if self.adapter.stream_terminal(event):
                        terminal = True
                        termination = "explicit"
                        break
            except _StreamRetryable:
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise _StreamRetryable(
                    str(exc) or "LLM 流式连接中断",
                    events=raw_events,
                    event_count=event_count,
                    received_bytes=received_bytes,
                    first_event_latency_ms=first_event_latency_ms,
                    status=status,
                    retry_after=response.headers.get("Retry-After"),
                    usage_values=usage_values,
                ) from exc
            except ExternalError as exc:
                raise _StreamProtocolError(
                    str(exc),
                    status=status,
                    events=raw_events,
                    event_count=event_count,
                    received_bytes=received_bytes,
                    first_event_latency_ms=first_event_latency_ms,
                    usage_values=usage_values,
                ) from exc
            if not terminal and terminal_spec["allow_clean_eof"] and event_count > 0:
                terminal = True
                termination = "clean_eof"
            if not terminal:
                raise _StreamRetryable(
                    "LLM 流式响应未正常结束",
                    events=raw_events,
                    event_count=event_count,
                    received_bytes=received_bytes,
                    first_event_latency_ms=first_event_latency_ms,
                    status=status,
                    retry_after=response.headers.get("Retry-After"),
                    usage_values=usage_values,
                )
            try:
                normalized = normalize_llm_response(
                    LLMResponse(
                        content="".join(content_parts),
                        reasoning_content=(
                            "".join(reasoning_parts) if reasoning_parts else None
                        ),
                    )
                )
            except ExternalError as exc:
                raise _StreamProtocolError(
                    str(exc),
                    status=status,
                    events=raw_events,
                    event_count=event_count,
                    received_bytes=received_bytes,
                    first_event_latency_ms=first_event_latency_ms,
                    usage_values=usage_values,
                ) from exc
            return (
                status,
                "",
                dict(response.headers),
                normalized,
                usage_values,
                raw_events,
                event_count,
                received_bytes,
                first_event_latency_ms,
                termination,
            )

    async def __aenter__(self) -> "LLMClient":
        if self.client is None:
            timeout = float(self.config["execution"]["request_timeout_seconds"])
            client_timeout: float | httpx.Timeout = timeout
            if (
                self.config["llm"].get("stream", False)
                and not self.config["llm"]["stream_read_timeout_enabled"]
            ):
                client_timeout = httpx.Timeout(timeout, read=None)
            limits = httpx.Limits(
                max_connections=self.config["execution"]["max_parallel"],
                max_keepalive_connections=self.config["execution"]["max_parallel"],
            )
            self.client = httpx.AsyncClient(
                timeout=client_timeout,
                limits=limits,
                proxy=self.config["llm"]["proxy_url"] or None,
            )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self.owns_client and self.client is not None:
            await self.client.aclose()

    async def _debug_attempt(
        self,
        request_id: str,
        attempt: int,
        payload: dict[str, Any],
        *,
        retry_round: int | None = None,
        key_index: int | None = None,
        response: dict[str, Any] | None = None,
        error: str | None = None,
        status: int | None = None,
        provider_error_status: int | None = None,
        outcome: str | None = None,
        stream_event_count: int | None = None,
        stream_received_bytes: int | None = None,
        stream_first_event_latency_ms: float | None = None,
        parent_request_id: str | None = None,
    ) -> None:
        if not self.config["debug"]["enabled"]:
            return
        payload_dir = self.run_dir / "payloads"
        payload_dir.mkdir(parents=True, exist_ok=True)
        base = f"{request_id}-A{attempt:03d}"
        atomic_write_json(payload_dir / f"{base}.request.json", payload)
        if response is not None:
            atomic_write_json(payload_dir / f"{base}.response.json", response)
        if error is not None:
            atomic_write_json(
                payload_dir / f"{base}.error.json",
                {
                    "schema_version": 1,
                    "error": error,
                    "http_status": status,
                    "provider_error_status": provider_error_status,
                    "outcome": outcome,
                    "retry_round": retry_round,
                    "key_index": (key_index + 1 if key_index is not None else None),
                    "stream_event_count": stream_event_count,
                    "stream_received_bytes": stream_received_bytes,
                    "stream_first_event_latency_ms": stream_first_event_latency_ms,
                },
            )
        async with self.log_lock:
            append_jsonl_file(
                self.run_dir / "attempts.jsonl",
                record_header(
                    "request_attempt",
                    self.project_id,
                    record_id=f"{base}",
                    run_id=self.run_id,
                    request_id=request_id,
                    parent_request_id=parent_request_id,
                    stage=self.stage,
                    attempt=attempt,
                    retry_round=retry_round,
                    key_index=(key_index + 1 if key_index is not None else None),
                    http_status=status,
                    provider_error_status=provider_error_status,
                    outcome=outcome,
                    status="failed" if error is not None else "completed",
                    error=error,
                ),
            )

    async def chat(
        self,
        *,
        messages: list[dict[str, str]],
        temperature: float,
        estimated_input_tokens: int,
        request_id: str | None = None,
        parent_request_id: str | None = None,
        segment_id_map: dict[str, str] | None = None,
    ) -> tuple[LLMResponse, str]:
        if self.client is None:
            raise RuntimeError("LLMClient must be used as an async context manager")
        self._prepare_keys()
        api_keys = self._api_keys
        request_id = request_id or f"REQ-{uuid.uuid4().hex[:12].upper()}"
        configured_output = int(self.config["llm"]["max_output_tokens"])
        available_output = max(
            1,
            int(self.config["llm"]["context_window_tokens"])
            - int(self.config["llm"]["context_safety_margin_tokens"])
            - estimated_input_tokens,
        )
        effective_output = min(configured_output, available_output)
        if effective_output < configured_output and not self._reported_output_clamp:
            warning = (
                "max_output_tokens "
                f"已从配置上限 {configured_output} 按本次剩余上下文"
                f"自动收窄为 {effective_output}"
            )
            self.warnings.append(warning)
            self.logger.warning("%s request=%s", warning, request_id)
            self._reported_output_clamp = True
        stream_enabled = bool(self.config["llm"].get("stream", False))
        endpoint = (
            self.config["llm"].get("stream_endpoint")
            if stream_enabled and self.config["llm"].get("stream_endpoint")
            else self.config["llm"]["endpoint"]
        )
        url = endpoint_url(
            self.config["llm"]["base_url"],
            endpoint,
            model=self.config["llm"]["model"],
        )
        attempts = int(self.config["retry"]["http_max_attempts"])
        diagnostics = current_diagnostics()
        if diagnostics is not None:
            diagnostics.begin_request(
                request_id=request_id,
                model=str(self.config["llm"]["model"]),
                messages=messages,
                max_attempts=attempts,
                segment_id_map=segment_id_map,
                transport="sse" if stream_enabled else "non_streaming",
            )
        attempt = 1
        send_attempt = 0
        disabled_keys = self._disabled_keys
        rate_limited_keys: set[int] = set()
        requested_key_indices: set[int] = set()
        while attempt <= attempts:
            lease = None
            key_index = 0
            if self.key_pool is None:
                waited = await self.limiter.acquire(
                    estimated_input_tokens,
                    on_wait_start=(
                        diagnostics.rate_limit_wait_started
                        if diagnostics is not None
                        else None
                    ),
                    on_wait_end=(
                        diagnostics.rate_limit_wait_finished
                        if diagnostics is not None
                        else None
                    ),
                )
            else:
                key_ids = self._key_ids
                if key_ids is None:
                    raise RuntimeError("API Key 身份尚未解析")
                try:
                    lease = await self.key_pool.acquire(
                        key_ids,
                        estimated_tokens=estimated_input_tokens,
                        excluded=frozenset(disabled_keys | rate_limited_keys),
                        on_wait_start=(
                            diagnostics.rate_limit_wait_started
                            if diagnostics is not None
                            else None
                        ),
                        on_wait_end=(
                            diagnostics.rate_limit_wait_finished
                            if diagnostics is not None
                            else None
                        ),
                    )
                except NoAvailableKey:
                    healthy_keys = set(range(len(api_keys))) - disabled_keys
                    if not healthy_keys:
                        raise FatalExternalError("本次执行没有可用的 API Key")
                    if rate_limited_keys == healthy_keys:
                        if attempt == attempts:
                            raise ExternalError("所有 API Key 均受到限流")
                        if diagnostics is not None:
                            diagnostics.retried()
                        attempt += 1
                        rate_limited_keys.clear()
                        continue
                    raise ExternalError("没有可用的 API Key")
                key_index = lease.key_index
                waited = 0.0
            api_key = api_keys[key_index]
            try:
                headers, payload = self.adapter.build_request(
                    api_key=api_key,
                    model=str(self.config["llm"]["model"]),
                    messages=messages,
                    temperature=temperature,
                    max_output_tokens=effective_output,
                    stream=stream_enabled,
                    extra_body=self.config.get("_llm_extra_body"),
                )
            except BaseException:
                if lease is not None:
                    await lease.release()
                raise
            self._audit_request_key(key_index, requested_key_indices)
            if waited:
                self.logger.info(
                    "rate-limit wait=%.2fs request=%s key=%d attempt=%d retry_round=%d",
                    waited,
                    request_id,
                    key_index + 1,
                    send_attempt + 1,
                    attempt,
                )
            self.send_count += 1
            send_attempt += 1
            if self.preparation_started_at is None:
                self.logger.info(
                    "request start request=%s key=%d attempt=%d retry_round=%d/%d input_tokens=%d max_tokens=%d",
                    request_id,
                    key_index + 1,
                    send_attempt,
                    attempt,
                    attempts,
                    estimated_input_tokens,
                    effective_output,
                )
            else:
                self.logger.info(
                    "request start request=%s key=%d attempt=%d retry_round=%d/%d input_tokens=%d max_tokens=%d preparation_elapsed=%.3fs",
                    request_id,
                    key_index + 1,
                    send_attempt,
                    attempt,
                    attempts,
                    estimated_input_tokens,
                    effective_output,
                    time.perf_counter() - self.preparation_started_at,
                )
            started = time.monotonic()
            response_status: int | None = None
            stream_result: (
                tuple[
                    int,
                    str,
                    dict[str, str],
                    LLMResponse,
                    dict[str, int],
                    list[str],
                    int,
                    int,
                    float | None,
                    str,
                ]
                | None
            ) = None
            attempt_error = False
            attempt_outcome: str | None = None
            attempt_provider_error_status: int | None = None
            attempt_stream_event_count: int | None = None
            attempt_stream_received_bytes: int | None = None
            attempt_stream_first_event_latency_ms: float | None = None
            request_retry_round = attempt
            if diagnostics is not None:
                diagnostics.request_started(request_id)
            self._audit_attempt(key_index)
            try:
                debug = self.config["debug"]
                if (
                    debug["enabled"]
                    and debug["inject_timeout_every"]
                    and self.send_count % debug["inject_timeout_every"] == 0
                ):
                    raise httpx.ReadTimeout("injected timeout")
                if stream_enabled:
                    stream_result = await self._stream_attempt(
                        url=url,
                        headers=headers,
                        payload=payload,
                        request_id=request_id,
                        started=started,
                        diagnostics=diagnostics,
                    )
                    response_status = stream_result[0]
                    attempt_outcome = (
                        "rate_limit_error"
                        if response_status == 429
                        else "authentication_error"
                        if response_status in {401, 403}
                        else "http_error"
                        if response_status >= 400
                        else "succeeded"
                    )
                if not stream_enabled:
                    if (
                        debug["enabled"]
                        and debug["inject_429_every"]
                        and self.send_count % debug["inject_429_every"] == 0
                    ):
                        response = httpx.Response(429, text="injected 429")
                    elif (
                        debug["enabled"]
                        and debug["inject_500_every"]
                        and self.send_count % debug["inject_500_every"] == 0
                    ):
                        response = httpx.Response(500, text="injected 500")
                    else:
                        response = await self.client.post(
                            url,
                            headers=headers,
                            json=payload,
                        )
                    response_status = response.status_code
                    attempt_outcome = (
                        "authentication_error"
                        if response_status in {401, 403}
                        else "rate_limit_error"
                        if response_status == 429
                        else "http_error"
                        if response_status >= 400
                        else "succeeded"
                    )
            except _StreamRetryable as exc:
                attempt_error = True
                provider_status = exc.provider_error_status
                attempt_outcome = (
                    "authentication_error"
                    if provider_status in {401, 403}
                    else "rate_limit_error"
                    if provider_status == 429
                    else "stream_error"
                )
                response_status = exc.status
                attempt_provider_error_status = exc.provider_error_status
                attempt_stream_event_count = exc.event_count
                attempt_stream_received_bytes = exc.received_bytes
                attempt_stream_first_event_latency_ms = exc.first_event_latency_ms
                if self.adapter.usage_pointers is not None:
                    self._record_stream_usage(
                        exc.usage_values,
                        key_index,
                        affects_global=True,
                    )
                elapsed = time.monotonic() - started
                await self._debug_attempt(
                    request_id,
                    send_attempt,
                    payload,
                    retry_round=attempt,
                    key_index=key_index,
                    response={"events": exc.events},
                    error=str(exc),
                    status=response_status,
                    provider_error_status=exc.provider_error_status,
                    outcome=attempt_outcome,
                    stream_event_count=exc.event_count,
                    stream_received_bytes=exc.received_bytes,
                    stream_first_event_latency_ms=exc.first_event_latency_ms,
                    parent_request_id=parent_request_id,
                )
                self.logger.warning(
                    "stream error request=%s key=%d attempt=%d retry_round=%d http_status=%s provider_error_status=%s elapsed=%.2fs error=%s",
                    request_id,
                    key_index + 1,
                    send_attempt,
                    attempt,
                    response_status,
                    exc.provider_error_status,
                    elapsed,
                    exc,
                )
                if provider_status in {401, 403}:
                    self._audit_authentication_error(key_index)
                    self.warnings.append(
                        f"Key #{key_index + 1} 鉴权失败，已在本次执行中隔离"
                    )
                    if self.key_pool is not None:
                        disabled_keys.add(key_index)
                        if len(disabled_keys) < len(api_keys):
                            if diagnostics is not None:
                                diagnostics.retried()
                            continue
                    if diagnostics is not None:
                        diagnostics.fail_request(request_id, "authentication_error")
                    raise FatalExternalError(
                        f"鉴权失败：上游 HTTP {provider_status}"
                    ) from exc
                if provider_status in {400, 404}:
                    if diagnostics is not None:
                        diagnostics.fail_request(
                            request_id, "request_configuration_error"
                        )
                    raise FatalExternalError(
                        f"请求或端点配置错误：上游 HTTP {provider_status}"
                    ) from exc
                if provider_status == 429 and self.key_pool is not None:
                    self._audit_rate_limit_error(key_index)
                    key_id = (
                        self._key_ids[key_index]
                        if self._key_ids is not None
                        else None
                    )
                    if key_id is None:
                        raise RuntimeError("API Key 身份尚未解析")
                    delay = self._stream_retry_after_delay(exc, attempt)
                    await self.key_pool.cool_down(key_id, delay)
                    rate_limited_keys.add(key_index)
                    healthy_keys = set(range(len(api_keys))) - disabled_keys
                    if rate_limited_keys < healthy_keys:
                        if diagnostics is not None:
                            diagnostics.retried()
                        continue
                    if attempt >= attempts:
                        if diagnostics is not None:
                            diagnostics.fail_request(request_id, "rate_limit_error")
                        raise ExternalError("所有 API Key 均受到限流") from exc
                    if diagnostics is not None:
                        diagnostics.retried()
                    attempt += 1
                    rate_limited_keys.clear()
                    continue
                if provider_status == 429:
                    if attempt == attempts:
                        if diagnostics is not None:
                            diagnostics.fail_request(request_id, "rate_limit_error")
                        raise ExternalError("LLM 流式请求重试耗尽：HTTP 429") from exc
                    if diagnostics is not None:
                        diagnostics.retried()
                    await self._retry_sleep(
                        self._stream_retry_after_delay(exc, attempt),
                        diagnostics=None,
                    )
                    rate_limited_keys.clear()
                    attempt += 1
                    continue
                if attempt == attempts:
                    if diagnostics is not None:
                        diagnostics.fail_request(request_id, "stream_error")
                    status_hint = (
                        f"（上游 HTTP {exc.provider_error_status}）"
                        if exc.provider_error_status is not None
                        else ""
                    )
                    raise ExternalError(
                        f"LLM 流式请求重试耗尽{status_hint}：{exc}"
                    ) from exc
                if diagnostics is not None:
                    diagnostics.retried()
                await self._backoff(attempt)
                rate_limited_keys.clear()
                attempt += 1
                continue
            except asyncio.CancelledError:
                attempt_error = True
                attempt_outcome = "cancelled"
                if diagnostics is not None:
                    diagnostics.fail_request(request_id, "cancelled")
                raise
            except _StreamProtocolError as exc:
                attempt_error = True
                attempt_outcome = "response_parse_error"
                response_status = exc.status
                attempt_stream_event_count = exc.event_count
                attempt_stream_received_bytes = exc.received_bytes
                attempt_stream_first_event_latency_ms = exc.first_event_latency_ms
                if self.adapter.usage_pointers is not None:
                    self._record_stream_usage(
                        exc.usage_values,
                        key_index,
                        affects_global=True,
                    )
                await self._debug_attempt(
                    request_id,
                    send_attempt,
                    payload,
                    retry_round=attempt,
                    key_index=key_index,
                    response={"events": exc.events},
                    error=str(exc),
                    status=response_status,
                    outcome=attempt_outcome,
                    stream_event_count=exc.event_count,
                    stream_received_bytes=exc.received_bytes,
                    stream_first_event_latency_ms=exc.first_event_latency_ms,
                    parent_request_id=parent_request_id,
                )
                if diagnostics is not None:
                    diagnostics.fail_request(request_id, "response_parse_error")
                raise
            except ExternalError:
                attempt_error = True
                attempt_outcome = "response_parse_error"
                if diagnostics is not None:
                    diagnostics.fail_request(request_id, "response_parse_error")
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                attempt_error = True
                attempt_outcome = "network_error"
                elapsed = time.monotonic() - started
                self.logger.warning(
                    "request network-error request=%s key=%d attempt=%d retry_round=%d elapsed=%.2fs kind=%s",
                    request_id,
                    key_index + 1,
                    send_attempt,
                    attempt,
                    elapsed,
                    type(exc).__name__,
                )
                await self._debug_attempt(
                    request_id,
                    send_attempt,
                    payload,
                    retry_round=attempt,
                    key_index=key_index,
                    error=str(exc),
                    outcome=attempt_outcome,
                    parent_request_id=parent_request_id,
                )
                if attempt == attempts:
                    if diagnostics is not None:
                        diagnostics.fail_request(request_id, "network_error")
                    raise ExternalError(f"HTTP 请求重试耗尽：{exc}") from exc
                if diagnostics is not None:
                    diagnostics.retried()
                await self._backoff(attempt)
                rate_limited_keys.clear()
                attempt += 1
                continue
            finally:
                if diagnostics is not None:
                    diagnostics.request_finished(
                        request_id=request_id,
                        attempt=send_attempt,
                        retry_round=request_retry_round,
                        key_index=key_index + 1,
                        latency_seconds=time.monotonic() - started,
                        status=response_status,
                        error=attempt_error
                        or response_status is None
                        or response_status >= 400,
                        retrying=(
                            (
                                attempt_error
                                and attempt < attempts
                            )
                            or (
                                self.key_pool is not None
                                and len(api_keys) > 1
                                and (
                                    response_status in {401, 403, 429}
                                    or attempt_provider_error_status in {401, 403, 429}
                                )
                            )
                        ),
                        stream_event_count=(
                            stream_result[6]
                            if stream_result is not None
                            else attempt_stream_event_count
                        ),
                        stream_received_bytes=(
                            stream_result[7]
                            if stream_result is not None
                            else attempt_stream_received_bytes
                        ),
                        stream_first_event_latency_ms=(
                            stream_result[8]
                            if stream_result is not None
                            else attempt_stream_first_event_latency_ms
                        ),
                        provider_error_status=attempt_provider_error_status,
                        outcome=attempt_outcome,
                    )
                if lease is not None:
                    await lease.release()
            if stream_enabled:
                if stream_result is None:
                    raise RuntimeError("流式请求没有返回结果")
                (
                    stream_status,
                    stream_error_text,
                    stream_headers,
                    normalized,
                    stream_usage,
                    raw_events,
                    stream_event_count,
                    stream_received_bytes,
                    _stream_first_event_latency_ms,
                    stream_termination,
                ) = stream_result
                response_status = stream_status
                if 200 <= stream_status < 300:
                    if self.config["debug"]["enabled"]:
                        await self._debug_attempt(
                            request_id,
                            send_attempt,
                            payload,
                            retry_round=attempt,
                            key_index=key_index,
                            response={"events": raw_events},
                            status=stream_status,
                            outcome="succeeded",
                            parent_request_id=parent_request_id,
                        )
                    normalized = _apply_debug_content_injections(
                        normalized,
                        self.config["debug"],
                        self.send_count,
                    )
                    self._record_stream_usage(stream_usage, key_index)
                    if diagnostics is not None:
                        diagnostics.complete_request(
                            request_id,
                            content=normalized.content,
                            reasoning_content=normalized.reasoning_content,
                        )
                    self.logger.info(
                        "stream complete request=%s key=%d attempt=%d retry_round=%d status=%d events=%d bytes=%d termination=%s elapsed=%.2fs",
                        request_id,
                        key_index + 1,
                        send_attempt,
                        attempt,
                        stream_status,
                        stream_event_count,
                        stream_received_bytes,
                        stream_termination,
                        time.monotonic() - started,
                    )
                    if self.on_usage is not None:
                        self.on_usage(self.usage_summary())
                    return normalized, request_id
                response = httpx.Response(
                    stream_status,
                    headers=stream_headers,
                    text=stream_error_text,
                )
            elapsed = time.monotonic() - started
            try:
                response_data = response.json()
            except ValueError:
                response_data = {"raw_text": response.text}
            extracted = self.adapter.extract_usage(response_data)
            if extracted is not None:
                self._record_usage(extracted, key_index)
            else:
                self._audit_usage_missing(key_index, affects_global=True)
            if 200 <= response.status_code < 300:
                debug = self.config["debug"]
                if (
                    debug["enabled"]
                    and debug["inject_invalid_json_every"]
                    and self.send_count % debug["inject_invalid_json_every"] == 0
                ):
                    self.adapter.replace_content(response_data, "{invalid json")
                elif (
                    debug["enabled"]
                    and debug["inject_missing_segment_every"]
                    and self.send_count % debug["inject_missing_segment_every"] == 0
                ):
                    try:
                        content = self.adapter.parse_response(response_data).content
                        lines = extract_jsonl_content(content).splitlines()
                        segment_indexes = []
                        for index, line in enumerate(lines):
                            value = json.loads(line)
                            if (
                                isinstance(value, dict)
                                and value.get("type") == "segment"
                            ):
                                segment_indexes.append(index)
                        if segment_indexes:
                            lines.pop(segment_indexes[-1])
                            self.adapter.replace_content(
                                response_data, "\n".join(lines)
                            )
                    except (
                        KeyError,
                        IndexError,
                        TypeError,
                        ValueError,
                        json.JSONDecodeError,
                    ):
                        pass
                await self._debug_attempt(
                    request_id,
                    send_attempt,
                    payload,
                    retry_round=attempt,
                    key_index=key_index,
                    response=response_data,
                    status=response.status_code,
                    parent_request_id=parent_request_id,
                )
                try:
                    parsed = self.adapter.parse_response(response_data)
                    normalized = normalize_llm_response(parsed)
                except Exception:
                    if diagnostics is not None:
                        diagnostics.fail_request(request_id, "response_parse_error")
                    raise
                if diagnostics is not None:
                    diagnostics.complete_request(
                        request_id,
                        content=normalized.content,
                        reasoning_content=normalized.reasoning_content,
                    )
                if self.on_usage is not None:
                    self.on_usage(self.usage_summary())
                self.logger.info(
                    "request complete request=%s key=%d attempt=%d retry_round=%d status=%d elapsed=%.2fs",
                    request_id,
                    key_index + 1,
                    send_attempt,
                    attempt,
                    response.status_code,
                    elapsed,
                )
                return normalized, request_id
            retryable = (
                response.status_code in {408, 429} or response.status_code >= 500
            )
            await self._debug_attempt(
                request_id,
                send_attempt,
                payload,
                retry_round=attempt,
                key_index=key_index,
                error=response.text,
                status=response.status_code,
                outcome=attempt_outcome,
                parent_request_id=parent_request_id,
            )
            if response.status_code in {401, 403}:
                self._audit_authentication_error(key_index)
                self.warnings.append(
                    f"Key #{key_index + 1} 鉴权失败，已在本次执行中隔离"
                )
                self.logger.error(
                    "request fatal request=%s key=%d attempt=%d retry_round=%d status=%d elapsed=%.2fs",
                    request_id,
                    key_index + 1,
                    send_attempt,
                    attempt,
                    response.status_code,
                    elapsed,
                )
                if self.key_pool is not None:
                    disabled_keys.add(key_index)
                    if len(disabled_keys) < len(api_keys):
                        if diagnostics is not None:
                            diagnostics.retried()
                        continue
                if diagnostics is not None:
                    diagnostics.fail_request(request_id, "authentication_error")
                raise FatalExternalError(f"鉴权失败：HTTP {response.status_code}")
            if response.status_code == 429 and self.key_pool is not None:
                self._audit_rate_limit_error(key_index)
                key_id = self._key_ids[key_index] if self._key_ids is not None else None
                if key_id is None:
                    raise RuntimeError("API Key 身份尚未解析")
                delay = self._retry_after_delay(response, attempt)
                await self.key_pool.cool_down(key_id, delay)
                rate_limited_keys.add(key_index)
                healthy_keys = set(range(len(api_keys))) - disabled_keys
                if rate_limited_keys < healthy_keys:
                    if diagnostics is not None:
                        diagnostics.retried()
                    self.logger.warning(
                        "request rate-limited request=%s attempt=%d key=%d cooldown=%.2fs",
                        request_id,
                        attempt,
                        key_index + 1,
                        delay,
                    )
                    continue
                if attempt >= attempts:
                    if diagnostics is not None:
                        diagnostics.fail_request(request_id, "rate_limit_error")
                    raise ExternalError("所有 API Key 均受到限流")
                if diagnostics is not None:
                    diagnostics.retried()
                attempt += 1
                rate_limited_keys.clear()
                continue
            response_hint = response.text.casefold()
            if response.status_code == 400 and (
                "context_length" in response_hint
                or (
                    "context" in response_hint
                    and ("token" in response_hint or "maximum" in response_hint)
                )
            ):
                self.logger.warning(
                    "request context-too-long request=%s key=%d attempt=%d retry_round=%d elapsed=%.2fs",
                    request_id,
                    key_index + 1,
                    send_attempt,
                    attempt,
                    elapsed,
                )
                if diagnostics is not None:
                    diagnostics.fail_request(request_id, "context_length_error")
                raise ContextLengthError(
                    "模型报告上下文过长",
                    request_id=request_id,
                )
            if response.status_code in {400, 404}:
                self.logger.error(
                    "request fatal request=%s key=%d attempt=%d retry_round=%d status=%d elapsed=%.2fs",
                    request_id,
                    key_index + 1,
                    send_attempt,
                    attempt,
                    response.status_code,
                    elapsed,
                )
                if diagnostics is not None:
                    diagnostics.fail_request(request_id, "request_configuration_error")
                raise FatalExternalError(
                    f"请求或端点配置错误：HTTP {response.status_code}"
                )
            if not retryable or attempt == attempts:
                self.logger.error(
                    "request failed request=%s key=%d attempt=%d retry_round=%d status=%d elapsed=%.2fs",
                    request_id,
                    key_index + 1,
                    send_attempt,
                    attempt,
                    response.status_code,
                    elapsed,
                )
                if diagnostics is not None:
                    diagnostics.fail_request(request_id, "http_error")
                raise ExternalError(f"LLM 请求失败：HTTP {response.status_code}")
            self.logger.warning(
                "request retry request=%s key=%d attempt=%d retry_round=%d status=%d elapsed=%.2fs",
                request_id,
                key_index + 1,
                send_attempt,
                attempt,
                response.status_code,
                elapsed,
            )
            if diagnostics is not None:
                diagnostics.retried()
            delay = self._retry_after_delay(response, attempt)
            if response.headers.get("Retry-After") is not None:
                self.logger.info(
                    "retry-after request=%s wait=%.2fs", request_id, delay
                )
            else:
                self.logger.info(
                    "retry backoff request=%s attempt=%d wait=%.2fs",
                    request_id,
                    attempt,
                    delay,
                )
            await self._retry_sleep(
                delay,
                diagnostics=diagnostics if response.status_code == 429 else None,
            )
            rate_limited_keys.clear()
            attempt += 1
        raise ExternalError("HTTP 请求重试耗尽")

    async def _retry_sleep(
        self,
        delay: float,
        *,
        diagnostics: Any | None,
    ) -> None:
        if diagnostics is not None:
            diagnostics.rate_limit_wait_started()
        try:
            await self.sleeper(delay)
        finally:
            if diagnostics is not None:
                diagnostics.rate_limit_wait_finished()

    async def _backoff(
        self,
        attempt: int,
        *,
        diagnostics: Any | None = None,
    ) -> None:
        delay = self._backoff_delay(attempt)
        self.logger.info("retry backoff attempt=%d wait=%.2fs", attempt, delay)
        await self._retry_sleep(delay, diagnostics=diagnostics)

    def _backoff_delay(self, attempt: int) -> float:
        delay = min(
            float(self.config["retry"]["max_delay_seconds"]),
            float(self.config["retry"]["base_delay_seconds"]) * (2 ** (attempt - 1)),
        )
        delay += random.uniform(0, float(self.config["retry"]["jitter_seconds"]))
        return delay

    def _retry_after_delay(
        self,
        response: httpx.Response,
        attempt: int,
    ) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                delay = float(retry_after)
                if math.isfinite(delay) and delay >= 0:
                    return delay
            except (TypeError, ValueError):
                pass
            warning = "Retry-After 无效，改用配置退避"
            self.warnings.append(warning)
            self.logger.warning("%s attempt=%d", warning, attempt)
        return self._backoff_delay(attempt)

    def _stream_retry_after_delay(
        self,
        error: _StreamRetryable,
        attempt: int,
    ) -> float:
        if error.retry_after is None:
            return self._backoff_delay(attempt)
        response = httpx.Response(
            429,
            headers={"Retry-After": error.retry_after},
        )
        return self._retry_after_delay(response, attempt)

    def usage_summary(self) -> dict[str, Any] | None:
        if self.adapter.usage_pointers is None:
            return None
        available = self.usage_observed and self.usage_complete
        partial = self.usage_observed and not self.usage_complete
        return {
            "input_tokens": self.usage.input_tokens if self.usage_observed else 0,
            "output_tokens": self.usage.output_tokens if self.usage_observed else 0,
            "total_tokens": self.usage.total_tokens if self.usage_observed else 0,
            "available": available,
            "partial": partial,
        }

    def _record_stream_usage(
        self,
        values: dict[str, int],
        key_index: int,
        *,
        affects_global: bool = True,
    ) -> None:
        if self.adapter.usage_pointers is None:
            return
        spec = self.adapter.streaming_spec
        if spec is None:
            if affects_global:
                self.usage_complete = False
            self._audit_usage_missing(key_index, affects_global=affects_global)
            return
        usage_spec = spec["usage"]
        base_pointers = self.adapter.usage_pointers
        if base_pointers is None:
            if affects_global:
                self.usage_complete = False
            self._audit_usage_missing(key_index, affects_global=affects_global)
            return
        required = {
            metric
            for metric, pointer in zip(
                ("input_tokens", "output_tokens", "total_tokens"),
                base_pointers,
            )
            if pointer is not None
        }
        declared_stream_metrics = {
            key.removesuffix("_pointers")
            for key, pointers in usage_spec.items()
            if pointers
        }
        if not required or not required.issubset(declared_stream_metrics):
            if affects_global:
                self.usage_complete = False
            self._audit_usage_missing(key_index, affects_global=affects_global)
            return
        observed = any(metric in values for metric in required)
        if observed:
            self._record_usage(
                Usage(
                    input_tokens=values.get("input_tokens", 0),
                    output_tokens=values.get("output_tokens", 0),
                    total_tokens=values.get("total_tokens", 0),
                ),
                key_index,
            )
        if not required.issubset(values):
            if affects_global:
                self.usage_complete = False
            self._audit_usage_missing(key_index, affects_global=affects_global)
            return

def _apply_debug_content_injections(
    response: LLMResponse, debug: dict[str, Any], send_count: int
) -> LLMResponse:
    if (
        debug["enabled"]
        and debug["inject_invalid_json_every"]
        and send_count % debug["inject_invalid_json_every"] == 0
    ):
        return LLMResponse("{invalid json", response.reasoning_content)
    if not (
        debug["enabled"]
        and debug["inject_missing_segment_every"]
        and send_count % debug["inject_missing_segment_every"] == 0
    ):
        return response
    try:
        lines = extract_jsonl_content(response.content).splitlines()
        segment_indexes = []
        for index, line in enumerate(lines):
            value = json.loads(line)
            if isinstance(value, dict) and value.get("type") == "segment":
                segment_indexes.append(index)
        if segment_indexes:
            lines.pop(segment_indexes[-1])
            return LLMResponse("\n".join(lines), response.reasoning_content)
    except (
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        pass
    return response
