from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import random
import re
import shutil
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx

from .errors import (
    ConfigError,
    ContextLengthError,
    ExternalError,
    FatalExternalError,
    IncompleteError,
    UsageError,
)
from .storage import (
    append_jsonl,
    atomic_write_json,
    read_json,
    read_jsonl,
    record_header,
    utc_now,
)


STAGE_FILES = {
    "translation": "translation.jsonl",
    "proofreading": "proofreading.jsonl",
    "proofreading_applied": "proofreading_applied.jsonl",
    "polishing": "polishing.jsonl",
    "polishing_applied": "polishing_applied.jsonl",
}

STAGE_CODES = {
    "terminology": "TERM",
    "translation": "TR",
    "proofreading": "PR",
    "polishing": "PO",
    "proofreading_applied": "PRA",
    "polishing_applied": "POA",
}

CJK_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]"
)


@dataclass(frozen=True)
class Scope:
    from_file: str | None = None
    only_file: str | None = None
    only_segment: str | None = None
    force: bool = False
    dry_run: bool = False

    def validate(self) -> None:
        selectors = [self.from_file, self.only_file, self.only_segment]
        if sum(value is not None for value in selectors) > 1:
            raise UsageError(
                "--from-file、--only-file 和 --only-segment 不能同时使用"
            )


@dataclass(frozen=True)
class ChunkPlan:
    file_id: str
    segments: tuple[dict[str, Any], ...]
    payload: dict[str, Any]
    estimated_input_tokens: int
    chunk_id: str | None = None


@dataclass(frozen=True)
class StageSelection:
    selected: tuple[dict[str, Any], ...]
    work: tuple[dict[str, Any], ...]
    reusable: tuple[dict[str, Any], ...]
    latest_completed: dict[str, dict[str, Any]]
    last_attempt_failed: tuple[str, ...]
    fingerprints: frozenset[str]


def select_scope(
    segments: Iterable[dict[str, Any]],
    files: Iterable[dict[str, Any]],
    scope: Scope,
) -> list[dict[str, Any]]:
    scope.validate()
    file_order = {str(item["file_id"]): int(item["file_order"]) for item in files}
    selected = [item for item in segments if not item["is_empty"]]
    if scope.only_segment:
        selected = [
            item for item in selected if item["segment_id"] == scope.only_segment
        ]
    elif scope.only_file:
        selected = [item for item in selected if item["file_id"] == scope.only_file]
    elif scope.from_file:
        if scope.from_file not in file_order:
            raise UsageError(f"未知 file_id：{scope.from_file}")
        start = file_order[scope.from_file]
        selected = [
            item for item in selected if file_order[str(item["file_id"])] >= start
        ]
    if (scope.only_file or scope.only_segment) and not selected:
        raise UsageError("选择范围为空或 ID 不存在")
    return selected


def stage_result_path(project: Path, stage: str) -> Path:
    try:
        filename = STAGE_FILES[stage]
    except KeyError as exc:
        raise UsageError(f"未知阶段：{stage}") from exc
    return project / "stages" / filename


def load_stage_history(
    project: Path, stage: str, *, repair_tail: bool = True
) -> list[dict[str, Any]]:
    return read_jsonl(stage_result_path(project, stage), repair_tail=repair_tail)


def latest_completed_by_segment(
    history: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for record in history:
        if record.get("status") == "completed" and record.get("segment_id"):
            latest[str(record["segment_id"])] = record
    return latest


def classify_stage(
    selected: Iterable[dict[str, Any]],
    history: Iterable[dict[str, Any]],
    *,
    force: bool,
) -> StageSelection:
    selected_list = list(selected)
    history_list = list(history)
    completed = latest_completed_by_segment(history_list)
    records_by_segment: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in history_list:
        if record.get("segment_id"):
            records_by_segment[str(record["segment_id"])].append(record)
    reusable = [
        segment
        for segment in selected_list
        if str(segment["segment_id"]) in completed
    ]
    work = (
        selected_list
        if force
        else [
            segment
            for segment in selected_list
            if str(segment["segment_id"]) not in completed
        ]
    )
    last_failed = tuple(
        str(segment["segment_id"])
        for segment in selected_list
        if str(segment["segment_id"]) in completed
        and records_by_segment[str(segment["segment_id"])]
        and records_by_segment[str(segment["segment_id"])][-1].get("status") == "failed"
    )
    fingerprints = frozenset(
        str(record["stage_fingerprint"])
        for record in completed.values()
        if record.get("stage_fingerprint")
    )
    return StageSelection(
        selected=tuple(selected_list),
        work=tuple(work),
        reusable=tuple(reusable),
        latest_completed=completed,
        last_attempt_failed=last_failed,
        fingerprints=fingerprints,
    )


def full_prompt(stage: str, middle: str) -> str:
    common = (
        "只处理 user 消息 segments 中的内容。reference_context 仅供理解，"
        "不得输出或计入进度。保持 Segment ID 不变。只返回合法 JSON，"
        "不要使用 Markdown 代码块或解释文字。"
    )
    stage_rules = {
        "terminology": (
            "提取 terms 数组。每项包含 source、category、description，"
            "preferred_translation 和 aliases 可选。"
        ),
        "translation": (
            "返回 segments 数组，每项只包含 id 和完整 translation。"
        ),
        "proofreading": (
            "返回 segments 数组，每项包含 id、status、suggested_text、reason；"
            "status 只能是 accepted 或 suggested。"
        ),
        "polishing": (
            "返回 segments 数组，每项包含 id、status、suggested_text、reason；"
            "status 只能是 accepted 或 suggested。"
        ),
    }
    if stage not in stage_rules:
        raise UsageError(f"阶段没有 LLM Prompt：{stage}")
    return f"{common}\n\n{stage_rules[stage]}\n\n{middle.strip()}"


def stage_fingerprint(
    config: dict[str, Any],
    stage: str,
    prompt: str | None,
    *,
    terms_revision: int | None = None,
    apply_semantics: dict[str, Any] | None = None,
) -> str:
    if stage.endswith("_applied"):
        data = {
            "stage": stage,
            "apply_rule_version": 1,
            **(apply_semantics or {}),
        }
    else:
        temperature_key = f"temperature_{stage}"
        data = {
            "stage": stage,
            "target_language": config["project"]["target_language"],
            "model": config["llm"]["model"],
            "prompt": prompt,
            "temperature": config["llm"][temperature_key],
            "context": config["context"][stage],
            "scheduling_mode": config["execution"]["scheduling_mode"],
            "terms_revision": terms_revision,
        }
        if stage == "terminology":
            data["terminology"] = config["terminology"]
        if stage == "translation":
            data["validation"] = {
                key: value
                for key, value in config["validation"]["translation"].items()
                if key != "max_retry_attempts"
            }
    encoded = json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk_count = len(CJK_RE.findall(text))
    non_cjk = CJK_RE.sub("", text)
    non_space = sum(not char.isspace() for char in non_cjk)
    whitespace = sum(char.isspace() for char in non_cjk)
    return max(1, math.ceil(cjk_count * 1.1 + non_space / 4 + whitespace / 8))


def estimate_messages(messages: list[dict[str, str]], factor: float) -> int:
    rendered = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    return math.ceil(estimate_tokens(rendered) * factor)


def render_messages(prompt: str, payload: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        },
    ]


def previous_context(
    all_segments: list[dict[str, Any]],
    first: dict[str, Any],
    count: int,
    *,
    target_resolver: Callable[[str], str | None] | None = None,
) -> list[dict[str, str]]:
    if count <= 0:
        return []
    candidates = [
        item
        for item in all_segments
        if item["file_id"] == first["file_id"]
        and int(item["line_index"]) < int(first["line_index"])
        and not item["is_empty"]
    ][-count:]
    result: list[dict[str, str]] = []
    for item in candidates:
        context = {"id": str(item["segment_id"]), "source": str(item["source"])}
        if target_resolver is not None:
            target = target_resolver(str(item["segment_id"]))
            if target is not None:
                context["translation"] = target
        result.append(context)
    return result


def contiguous_groups(
    segments: Iterable[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    ordered = sorted(
        segments, key=lambda item: (str(item["file_id"]), int(item["line_index"]))
    )
    groups: list[list[dict[str, Any]]] = []
    for segment in ordered:
        if (
            not groups
            or groups[-1][-1]["file_id"] != segment["file_id"]
            or int(segment["line_index"])
            != int(groups[-1][-1]["line_index"]) + 1
        ):
            groups.append([segment])
        else:
            groups[-1].append(segment)
    return groups


def build_chunk_plans(
    work: Iterable[dict[str, Any]],
    *,
    config: dict[str, Any],
    prompt: str,
    payload_builder: Callable[[list[dict[str, Any]]], dict[str, Any]],
) -> list[ChunkPlan]:
    hard_limit = (
        config["llm"]["context_window_tokens"]
        - config["llm"]["max_output_tokens"]
        - config["llm"]["context_safety_margin_tokens"]
    )
    target_limits = [
        config["chunking"]["target_chunk_input_tokens"],
        hard_limit,
    ]
    if config["execution"]["input_tokens_per_minute"] > 0:
        target_limits.append(config["execution"]["input_tokens_per_minute"])
    target = min(target_limits)
    factor = config["execution"]["token_safety_factor"]
    plans: list[ChunkPlan] = []
    for group in contiguous_groups(work):
        current: list[dict[str, Any]] = []
        for segment in group:
            candidate = [*current, segment]
            payload = payload_builder(candidate)
            estimated = estimate_messages(render_messages(prompt, payload), factor)
            if current and estimated > target:
                current_payload = payload_builder(current)
                current_estimate = estimate_messages(
                    render_messages(prompt, current_payload), factor
                )
                plans.append(
                    ChunkPlan(
                        file_id=str(current[0]["file_id"]),
                        segments=tuple(current),
                        payload=current_payload,
                        estimated_input_tokens=current_estimate,
                    )
                )
                current = [segment]
                payload = payload_builder(current)
                estimated = estimate_messages(render_messages(prompt, payload), factor)
            if estimated > hard_limit:
                raise ConfigError(
                    f"单 Segment Prompt 超过模型硬限制：{segment['segment_id']}"
                )
            if (
                config["execution"]["input_tokens_per_minute"] > 0
                and estimated > config["execution"]["input_tokens_per_minute"]
            ):
                raise ConfigError(
                    f"单请求预测 Token 超过 ITPM：{segment['segment_id']}"
                )
            if len(current) == 1 and current[0] is segment:
                continue
            current = candidate
        if current:
            payload = payload_builder(current)
            estimated = estimate_messages(render_messages(prompt, payload), factor)
            plans.append(
                ChunkPlan(
                    file_id=str(current[0]["file_id"]),
                    segments=tuple(current),
                    payload=payload,
                    estimated_input_tokens=estimated,
                )
            )
    return plans


def materialize_chunks(run_id: str, stage: str, plans: list[ChunkPlan]) -> list[ChunkPlan]:
    code = STAGE_CODES[stage]
    return [
        replace(
            plan,
            chunk_id=(
                f"CHK-{run_id}-{code}-{plan.file_id}-C{index:05d}"
            ),
        )
        for index, plan in enumerate(plans, start=1)
    ]


def create_run(
    project: Path,
    *,
    stage: str,
    fingerprint: str,
    prompt: str | None,
    selected_count: int,
    requested_count: int,
    reused_count: int,
    details: dict[str, Any] | None = None,
) -> tuple[str, Path]:
    project_metadata = read_json(project / "project.json")
    suffix = uuid.uuid4().hex[:6].upper()
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    run_id = f"RUN-{timestamp}-{STAGE_CODES[stage]}-{suffix}"
    run_dir = project / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    shutil.copy2(project / "config.toml", run_dir / "config.toml")
    if prompt is not None:
        (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    manifest = record_header(
        "run",
        str(project_metadata["project_id"]),
        record_id=run_id,
        run_id=run_id,
        stage=stage,
        status="running",
        stage_fingerprint=fingerprint,
        selected_segment_count=selected_count,
        requested_segment_count=requested_count,
        reused_segment_count=reused_count,
        **(details or {}),
        started_at=utc_now(),
        completed_at=None,
    )
    atomic_write_json(run_dir / "manifest.json", manifest)
    return run_id, run_dir


def finalize_run(
    run_dir: Path,
    *,
    status: str,
    completed: int,
    failed: int,
    warnings: list[str] | None = None,
) -> None:
    manifest = read_json(run_dir / "manifest.json")
    manifest.update(
        status=status,
        completed_segment_count=completed,
        failed_segment_count=failed,
        warnings=warnings or [],
        completed_at=utc_now(),
    )
    atomic_write_json(run_dir / "manifest.json", manifest)


def save_debug_chunks(
    run_dir: Path,
    project_id: str,
    run_id: str,
    stage: str,
    chunks: Iterable[ChunkPlan],
) -> None:
    for chunk in chunks:
        append_jsonl(
            run_dir / "chunks.jsonl",
            record_header(
                "chunk_manifest",
                project_id,
                record_id=str(chunk.chunk_id),
                run_id=run_id,
                stage=stage,
                chunk_id=chunk.chunk_id,
                file_id=chunk.file_id,
                segment_ids=[
                    str(segment["segment_id"]) for segment in chunk.segments
                ],
                estimated_input_tokens=chunk.estimated_input_tokens,
            ),
        )


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

    async def acquire(self, estimated_tokens: int) -> None:
        if self.requests_per_minute == 0 and self.input_tokens_per_minute == 0:
            return
        if (
            self.input_tokens_per_minute > 0
            and estimated_tokens > self.input_tokens_per_minute
        ):
            raise ConfigError("单请求预测 Token 超过 ITPM")
        while True:
            async with self.lock:
                now = self.clock()
                while self.records and now - self.records[0][0] >= 60:
                    self.records.popleft()
                request_full = (
                    self.requests_per_minute > 0
                    and len(self.records) >= self.requests_per_minute
                )
                token_full = (
                    self.input_tokens_per_minute > 0
                    and (
                        sum(tokens for _, tokens in self.records)
                        + estimated_tokens
                        > self.input_tokens_per_minute
                    )
                )
                if not request_full and not token_full:
                    self.records.append((now, estimated_tokens))
                    return
                wait = max(0.01, 60 - (now - self.records[0][0]))
            await self.sleeper(wait)


class LLMClient:
    def __init__(
        self,
        config: dict[str, Any],
        limiter: SlidingWindowLimiter,
        *,
        run_dir: Path,
        project_id: str,
        run_id: str,
        stage: str,
        client: httpx.AsyncClient | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.config = config
        self.limiter = limiter
        self.run_dir = run_dir
        self.project_id = project_id
        self.run_id = run_id
        self.stage = stage
        self.client = client
        self.owns_client = client is None
        self.sleeper = sleeper
        self.log_lock = asyncio.Lock()
        self.send_count = 0

    async def __aenter__(self) -> "LLMClient":
        if self.client is None:
            timeout = float(self.config["execution"]["request_timeout_seconds"])
            limits = httpx.Limits(
                max_connections=self.config["execution"]["max_parallel"],
                max_keepalive_connections=self.config["execution"]["max_parallel"],
            )
            self.client = httpx.AsyncClient(timeout=timeout, limits=limits)
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
        response: dict[str, Any] | None = None,
        error: str | None = None,
        status: int | None = None,
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
                {"schema_version": 1, "error": error, "http_status": status},
            )
        async with self.log_lock:
            append_jsonl(
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
                    http_status=status,
                    status="completed" if response is not None else "failed",
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
    ) -> tuple[str, str]:
        if self.client is None:
            raise RuntimeError("LLMClient must be used as an async context manager")
        api_key = os.getenv(str(self.config["llm"]["api_key_env"]))
        if not api_key:
            raise ExternalError(
                f"缺少环境变量：{self.config['llm']['api_key_env']}"
            )
        request_id = request_id or f"REQ-{uuid.uuid4().hex[:12].upper()}"
        payload = {
            "model": self.config["llm"]["model"],
            "messages": messages,
            "temperature": temperature,
            "max_tokens": self.config["llm"]["max_output_tokens"],
            "stream": False,
        }
        url = (
            self.config["llm"]["base_url"].rstrip("/")
            + "/"
            + self.config["llm"]["endpoint"].lstrip("/")
        )
        attempts = int(self.config["retry"]["http_max_attempts"])
        for attempt in range(1, attempts + 1):
            await self.limiter.acquire(estimated_input_tokens)
            self.send_count += 1
            try:
                debug = self.config["debug"]
                if (
                    debug["enabled"]
                    and debug["inject_timeout_every"]
                    and self.send_count % debug["inject_timeout_every"] == 0
                ):
                    raise httpx.ReadTimeout("injected timeout")
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
                        headers={"Authorization": f"Bearer {api_key}"},
                        json=payload,
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                await self._debug_attempt(
                    request_id,
                    attempt,
                    payload,
                    error=str(exc),
                    parent_request_id=parent_request_id,
                )
                if attempt == attempts:
                    raise ExternalError(f"HTTP 请求重试耗尽：{exc}") from exc
                await self._backoff(attempt)
                continue
            try:
                response_data = response.json()
            except ValueError:
                response_data = {"raw_text": response.text}
            if 200 <= response.status_code < 300:
                debug = self.config["debug"]
                if (
                    debug["enabled"]
                    and debug["inject_invalid_json_every"]
                    and self.send_count % debug["inject_invalid_json_every"] == 0
                ):
                    response_data = {
                        "choices": [{"message": {"content": "{invalid json"}}]
                    }
                elif (
                    debug["enabled"]
                    and debug["inject_missing_segment_every"]
                    and self.send_count % debug["inject_missing_segment_every"] == 0
                ):
                    try:
                        parsed_content = json.loads(
                            response_data["choices"][0]["message"]["content"]
                        )
                        if parsed_content.get("segments"):
                            parsed_content["segments"].pop()
                            response_data["choices"][0]["message"]["content"] = (
                                json.dumps(parsed_content, ensure_ascii=False)
                            )
                    except (KeyError, IndexError, TypeError, ValueError):
                        pass
                await self._debug_attempt(
                    request_id,
                    attempt,
                    payload,
                    response=response_data,
                    status=response.status_code,
                    parent_request_id=parent_request_id,
                )
                try:
                    content = response_data["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as exc:
                    raise ExternalError("响应缺少 choices[0].message.content") from exc
                if not isinstance(content, str):
                    raise ExternalError("LLM 响应正文不是字符串")
                return content, request_id
            retryable = response.status_code in {408, 429} or response.status_code >= 500
            await self._debug_attempt(
                request_id,
                attempt,
                payload,
                error=response.text,
                status=response.status_code,
                parent_request_id=parent_request_id,
            )
            if response.status_code in {401, 403}:
                raise FatalExternalError(f"鉴权失败：HTTP {response.status_code}")
            response_hint = response.text.casefold()
            if response.status_code == 400 and (
                "context_length" in response_hint
                or (
                    "context" in response_hint
                    and ("token" in response_hint or "maximum" in response_hint)
                )
            ):
                raise ContextLengthError(
                    "模型报告上下文过长",
                    request_id=request_id,
                )
            if response.status_code in {400, 404}:
                raise FatalExternalError(
                    f"请求或端点配置错误：HTTP {response.status_code}"
                )
            if not retryable or attempt == attempts:
                raise ExternalError(f"LLM 请求失败：HTTP {response.status_code}")
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    await self.sleeper(float(retry_after))
                    continue
                except ValueError:
                    pass
            await self._backoff(attempt)
        raise ExternalError("HTTP 请求重试耗尽")

    async def _backoff(self, attempt: int) -> None:
        delay = min(
            float(self.config["retry"]["max_delay_seconds"]),
            float(self.config["retry"]["base_delay_seconds"]) * (2 ** (attempt - 1)),
        )
        delay += random.uniform(0, float(self.config["retry"]["jitter_seconds"]))
        await self.sleeper(delay)


async def dispatch_chunks(
    chunks: list[ChunkPlan],
    worker: Callable[[ChunkPlan], Awaitable[Any]],
    *,
    mode: str,
    max_parallel: int,
) -> list[Any]:
    semaphore = asyncio.Semaphore(max_parallel)

    async def guarded(chunk: ChunkPlan) -> Any:
        async with semaphore:
            return await worker(chunk)

    if mode == "parallel":
        return list(await asyncio.gather(*(guarded(chunk) for chunk in chunks)))
    if mode != "ordered_by_file":
        raise ConfigError(f"未知调度模式：{mode}")
    by_file: dict[str, list[ChunkPlan]] = defaultdict(list)
    for chunk in chunks:
        by_file[chunk.file_id].append(chunk)

    async def file_chain(file_chunks: list[ChunkPlan]) -> list[Any]:
        values = []
        for chunk in file_chunks:
            values.append(await guarded(chunk))
        return values

    nested = await asyncio.gather(*(file_chain(value) for value in by_file.values()))
    return [item for group in nested for item in group]


def parse_json_object(content: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM 返回的不是合法 JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("LLM 返回 JSON 顶层必须是对象")
    return value


def ensure_complete_or_raise(selection: StageSelection) -> None:
    if any(
        segment["segment_id"] not in selection.latest_completed
        for segment in selection.selected
    ):
        raise IncompleteError("选定范围仍有 pending 或 failed")
