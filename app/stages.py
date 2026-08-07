from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import re
import shlex
import sys
import unicodedata
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx

from .config import load_project_config
from .documents import (
    DocumentExportJob,
    normalize_document_output,
    publish_document_exports,
)
from .errors import (
    ConfigError,
    ContextLengthError,
    ExternalError,
    FatalExternalError,
    IncompleteError,
    ProjectError,
    RequestSizeError,
    StorageError,
    UsageError,
)
from .execution import (
    ChunkPlan,
    LLMClient,
    Scope,
    SlidingWindowLimiter,
    build_chunk_plans,
    classify_stage,
    continue_run,
    contiguous_groups,
    create_run,
    dispatch_chunks,
    estimate_messages,
    find_running_runs,
    finalize_run,
    full_prompt,
    iter_chunk_plans,
    load_stage_history,
    localize_request_ids,
    materialize_chunk_stream,
    parse_jsonl_document,
    previous_context,
    render_messages,
    segment_model_source,
    save_debug_chunks,
    select_scope,
    scope_from_run,
    stage_fingerprint,
    stage_result_path,
)
from .logging_utils import get_logger
from .project import (
    PROMPT_LANGUAGES,
    load_segments,
    load_source_files,
    prompt_file,
)
from .plugins import get_document_adapter
from .sqlite_storage import (
    append_jsonl,
    atomic_write_text,
    read_json,
    read_jsonl,
    record_exists,
    record_header,
    write_json,
)
from .i18n import SUPPORTED_LANGUAGES, resolve_language

JAPANESE_RE = re.compile(
    "[\u3040-\u30ff\u31f0-\u31ff\uff66-\uff9f"
    "\U0001b000-\U0001b0ff\U0001b100-\U0001b12f"
    "\U0001b130-\U0001b16f\U0001aff0-\U0001afff]"
)
KOREAN_RE = re.compile("[\u1100-\u11ff\u3130-\u318f\ua960-\ua97f\uac00-\ud7ff]")


def _normalize_model_text(
    files: list[dict[str, Any]],
    segment: dict[str, Any],
    text: str,
    stage: str,
) -> str:
    file_id = str(segment["file_id"])
    file_record = next(
        (item for item in files if str(item["file_id"]) == file_id), None
    )
    if file_record is None:
        raise ProjectError(f"模型文本引用了未知文件：{file_id}")
    adapter = get_document_adapter(str(file_record["document_adapter_id"]))
    return normalize_document_output(
        adapter, segment=segment, text=text, stage=stage
    )


def _project_context(
    project: Path, *, stage: str | None = None
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    config = load_project_config(project, stage=stage)
    metadata = read_json(project, project / "project.json")
    files = load_source_files(project)
    segments = load_segments(project)
    adapter_options: dict[str, dict[str, Any]] = {}
    adapters: dict[str, dict[str, str]] = {}
    for file_record in files:
        adapters[str(file_record["file_id"])] = {
            "adapter_id": str(file_record["document_adapter_id"]),
            "version": str(file_record["document_adapter_version"]),
        }
        state_path = file_record.get("document_adapter_state")
        if not isinstance(state_path, str):
            continue
        state = read_json(project, project / state_path)
        adapter_options[str(file_record["file_id"])] = {
            key: state[key]
            for key in ("ruby_mode", "inline_format_mode", "inline_format_policy")
            if key in state
        }
    config["_document_adapter_options"] = adapter_options
    config["_document_adapters"] = adapters
    return config, metadata, files, segments


def _require_nonempty_segments(segments: list[dict[str, Any]]) -> None:
    if not any(not segment["is_empty"] for segment in segments):
        raise UsageError("项目没有可处理的非空 Segment；请先添加源文件")


def _scope_record(scope: Scope, *, force_all: bool = False) -> dict[str, Any]:
    return {
        "all_nonempty": force_all
        or not (
            scope.from_file
            or scope.only_file
            or scope.only_segment
            or scope.segment_ids
        ),
        "from_file": None if force_all else scope.from_file,
        "only_file": None if force_all else scope.only_file,
        "only_segment": None if force_all else scope.only_segment,
        "segment_ids": None if force_all else list(scope.segment_ids or []) or None,
        "force": scope.force,
    }


def _configured_output_warning(config: dict[str, Any]) -> str | None:
    maximum_available = (
        config["llm"]["context_window_tokens"]
        - config["llm"]["context_safety_margin_tokens"]
    )
    configured = config["llm"]["max_output_tokens"]
    if configured <= maximum_available:
        return None
    return (
        f"max_output_tokens={configured} 超过上下文可用上限 "
        f"{maximum_available}；实际请求将按剩余空间自动收窄"
    )


def _finalize_planning_failure(
    project: Path,
    run_dir: Path | None,
    *,
    requested_count: int,
    reused_count: int,
    warnings: list[str],
    error: BaseException,
) -> None:
    if run_dir is None:
        return
    finalize_run(
        project,
        run_dir,
        status="failed",
        completed=reused_count,
        failed=requested_count,
        warnings=[*warnings, f"Chunk 规划失败：{error}"],
        usage=None,
    )


def _extend_unique(target: list[str], values: list[str]) -> None:
    target.extend(value for value in values if value not in target)


def _resume_scope(
    project: Path, scope: Scope, resume_run_id: str | None
) -> tuple[Scope, bool]:
    if resume_run_id is None:
        return scope, False
    resumed_scope = scope_from_run(project, resume_run_id, dry_run=scope.dry_run)
    resume_arguments_ignored = (
        scope.from_file,
        scope.only_file,
        scope.only_segment,
        scope.force,
    ) != (
        resumed_scope.from_file,
        resumed_scope.only_file,
        resumed_scope.only_segment,
        resumed_scope.force,
    )
    return resumed_scope, resume_arguments_ignored


def _assemble_warnings(
    *,
    stage: str,
    resume_run_id: str | None,
    resume_arguments_ignored: bool,
    resume_message: str,
    config: dict[str, Any],
    fingerprint: str,
    existing_fingerprints: set[str] | frozenset[str],
    reusable_count: int,
    force: bool,
    reuse_allowed: bool,
    dry_run: bool,
    extra: list[str],
) -> list[str]:
    warnings: list[str] = []
    if resume_run_id is not None:
        warnings.append(resume_message)
        if resume_arguments_ignored:
            warnings.append("续作已忽略本次命令的范围参数或 --force")
    configured_output_warning = _configured_output_warning(config)
    if configured_output_warning:
        warnings.append(configured_output_warning)
    warnings.extend(extra)
    fingerprint_warning = _confirm_fingerprint_reuse(
        stage,
        existing_fingerprints,
        fingerprint,
        reusable_count,
        force=force,
        resume_run_id=resume_run_id,
        reuse_allowed=reuse_allowed,
        dry_run=dry_run,
    )
    if fingerprint_warning:
        warnings.append(fingerprint_warning)
    return warnings


def _create_or_continue_run(
    project: Path,
    stage: str,
    *,
    scope: Scope,
    config: dict[str, Any],
    fingerprint: str,
    prompt: str,
    resume_run_id: str | None,
    selected_count: int,
    requested_count: int,
    reused_count: int,
    details: dict[str, Any] | None,
    warnings: list[str],
) -> tuple[str | None, Path | None, Callable[[BaseException], None]]:
    run_id: str | None = None
    run_dir: Path | None = None
    if not scope.dry_run:
        if resume_run_id is not None:
            run_id, run_dir = continue_run(
                project,
                resume_run_id,
                config=config,
                stage=stage,
                fingerprint=fingerprint,
                prompt=prompt,
                scope=scope,
                selected_count=selected_count,
                requested_count=requested_count,
                reused_count=reused_count,
            )
        else:
            run_id, run_dir = create_run(
                project,
                config=config,
                stage=stage,
                fingerprint=fingerprint,
                prompt=prompt,
                selected_count=selected_count,
                requested_count=requested_count,
                reused_count=reused_count,
                details=details,
            )

    def fail_planning(error: BaseException) -> None:
        _finalize_planning_failure(
            project,
            run_dir,
            requested_count=requested_count,
            reused_count=reused_count,
            warnings=warnings,
            error=error,
        )

    return run_id, run_dir, fail_planning


@dataclass
class _Preflight:
    request_segments: list[dict[str, Any]]
    part_original: dict[str, str]
    original_parts: dict[str, list[str]]
    preflight_failed: list[dict[str, Any]]


def _split_oversized_preflight(
    work: Iterable[dict[str, Any]],
    *,
    stage: str,
    config: dict[str, Any],
    segments: list[dict[str, Any]],
    prompt: str,
    payload_builder: Callable[[list[dict[str, Any]]], dict[str, Any]],
    fail_planning: Callable[[BaseException], None],
    make_probe: Callable[[dict[str, Any], Any], dict[str, Any]],
    split_part: Callable[[Any], list[Any]],
    accept_part: Callable[[dict[str, Any], str, Any], dict[str, Any]],
    initial_part: Callable[[dict[str, Any]], Any] | None = None,
    cleanup_probe: Callable[[str], None] | None = None,
) -> _Preflight:
    request_segments: list[dict[str, Any]] = []
    part_original: dict[str, str] = {}
    original_parts: dict[str, list[str]] = {}
    preflight_failed: list[dict[str, Any]] = []
    for segment in work:
        try:
            build_chunk_plans(
                [segment],
                all_segments=segments,
                config=config,
                stage=stage,
                prompt=prompt,
                payload_builder=payload_builder,
            )
            request_segments.append(segment)
            continue
        except RequestSizeError as exc:
            if exc.reason != "context":
                fail_planning(exc)
                raise
            if not config["chunking"]["allow_split_oversized_segment"]:
                preflight_failed.append(segment)
                continue
        pending_parts: list[Any] = [
            initial_part(segment) if initial_part is not None else str(segment["source"])
        ]
        accepted_parts: list[Any] = []
        while pending_parts:
            part = pending_parts.pop(0)
            probe = make_probe(segment, part)
            try:
                build_chunk_plans(
                    [probe],
                    all_segments=segments,
                    config=config,
                    stage=stage,
                    prompt=prompt,
                    payload_builder=payload_builder,
                )
                accepted_parts.append(part)
            except RequestSizeError as exc:
                if exc.reason != "context":
                    fail_planning(exc)
                    raise
                try:
                    children = split_part(part)
                except ConfigError as split_error:
                    fail_planning(split_error)
                    raise
                pending_parts[0:0] = children
            finally:
                if cleanup_probe is not None:
                    cleanup_probe(f"{segment['segment_id']}-PROBE")
        part_ids: list[str] = []
        for index, part in enumerate(accepted_parts, start=1):
            part_id = f"{segment['segment_id']}-P{index:03d}"
            request_segments.append(accept_part(segment, part_id, part))
            part_original[part_id] = str(segment["segment_id"])
            part_ids.append(part_id)
        original_parts[str(segment["segment_id"])] = part_ids
    return _Preflight(
        request_segments=request_segments,
        part_original=part_original,
        original_parts=original_parts,
        preflight_failed=preflight_failed,
    )


@dataclass
class StageRunState:
    project: Path
    stage: str
    config: dict[str, Any]
    metadata: dict[str, Any]
    segments: list[dict[str, Any]]
    prompt: str
    fingerprint: str
    resume_run_id: str | None
    warnings: list[str]
    run_id: str
    run_dir: Path
    on_usage: Callable[[dict[str, Any] | None], None] | None = None
    llm: LLMClient | None = None


async def _execute_stage_run(
    state: StageRunState,
    *,
    request_segments: list[dict[str, Any]],
    part_original: dict[str, str],
    original_parts: dict[str, list[str]],
    preflight_failed: list[dict[str, Any]],
    limiter: SlidingWindowLimiter | None,
    payload_builder: Callable[[list[dict[str, Any]]], dict[str, Any]],
    process_once: Callable[..., Awaitable[None]],
    record_preflight_failure: Callable[[list[dict[str, Any]]], Awaitable[None]],
    record_context_failure: Callable[[list[dict[str, Any]]], Awaitable[None]],
    before_finalize: Callable[[], Awaitable[None]],
    completed_count: Callable[[], int],
    failed_count: Callable[[], int],
    exception_completed: Callable[[], int],
    exception_failed: Callable[[], int],
    failure_counts: Counter[str],
    http_client: httpx.AsyncClient | None = None,
    runtime_parts_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    logger = get_logger(state.stage)
    logger.info("run start run=%s", state.run_id)
    planned = iter_chunk_plans(
        request_segments,
        all_segments=state.segments,
        config=state.config,
        stage=state.stage,
        prompt=state.prompt,
        payload_builder=payload_builder,
    )
    chunks = materialize_chunk_stream(state.run_id, state.stage, planned)
    if state.config["debug"]["enabled"]:
        planned_chunks = chunks

        def debug_chunks() -> Iterable[ChunkPlan]:
            for chunk in planned_chunks:
                save_debug_chunks(
                    state.project,
                    state.run_dir,
                    str(state.metadata["project_id"]),
                    state.run_id,
                    state.stage,
                    [chunk],
                )
                yield chunk

        chunks = debug_chunks()
    if limiter is None:
        limiter = SlidingWindowLimiter(
            state.config["execution"]["requests_per_minute"],
            state.config["execution"]["input_tokens_per_minute"],
        )

    def ensure_runtime_chunk(chunk: ChunkPlan) -> ChunkPlan:
        if chunk.chunk_id is not None:
            return chunk
        materialized = replace(
            chunk,
            chunk_id=f"CHK-{state.run_id}-R-{uuid.uuid4().hex[:10].upper()}",
        )
        if state.config["debug"]["enabled"]:
            save_debug_chunks(
                state.project,
                state.run_dir,
                str(state.metadata["project_id"]),
                state.run_id,
                state.stage,
                [materialized],
            )
        return materialized

    async def process(
        chunk: ChunkPlan,
        split_parent_request_id: str | None = None,
    ) -> None:
        chunk = ensure_runtime_chunk(chunk)
        try:
            await process_once(chunk, split_parent_request_id)
            return
        except ContextLengthError as exc:
            logger.warning(
                "context split parent_request=%s segments=%d",
                exc.request_id,
                len(chunk.segments),
            )
            requested_ids = (
                set(exc.segment_ids) if exc.segment_ids is not None else None
            )
            items = [
                item
                for item in chunk.segments
                if requested_ids is None
                or str(item["segment_id"]) in requested_ids
            ]
            if not items:
                return
            if len(items) > 1:
                midpoint = len(items) // 2
                groups = (items[:midpoint], items[midpoint:])
            elif (
                state.config["chunking"]["allow_split_oversized_segment"]
                and len(str(items[0]["source"])) > 1
            ):
                groups = tuple(
                    [part]
                    for part in _replace_with_runtime_parts(
                        items[0],
                        part_original=part_original,
                        original_parts=original_parts,
                        **(runtime_parts_kwargs or {}),
                    )
                )
            else:
                groups = ()
            if not groups:
                await record_context_failure(items)
                return
            for group in groups:
                await process(
                    ChunkPlan(
                        file_id=str(group[0]["file_id"]),
                        segments=tuple(group),
                        payload={},
                        estimated_input_tokens=0,
                    ),
                    exc.request_id,
                )

    usage: dict[str, Any] | None = None
    try:
        async with LLMClient(
            state.config,
            limiter,
            run_dir=state.run_dir,
            project_id=str(state.metadata["project_id"]),
            run_id=state.run_id,
            stage=state.stage,
            client=http_client,
            on_usage=state.on_usage,
        ) as llm:
            state.llm = llm
            await record_preflight_failure(preflight_failed)
            await dispatch_chunks(
                chunks,
                process,
                mode=state.config["execution"]["scheduling_mode"],
                max_parallel=state.config["execution"]["max_parallel"],
            )
            await before_finalize()
        _extend_unique(state.warnings, llm.warnings)
        usage = llm.usage_summary()
    except asyncio.CancelledError:
        usage = llm.usage_summary()
        finalize_run(
            state.project,
            state.run_dir,
            status="interrupted",
            completed=exception_completed(),
            failed=0,
            warnings=[*state.warnings, "任务已由用户取消"],
            usage=usage,
            failure_counts=dict(failure_counts),
        )
        raise
    except (FatalExternalError, ConfigError) as exc:
        if isinstance(exc, FatalExternalError):
            usage = llm.usage_summary()
        finalize_run(
            state.project,
            state.run_dir,
            status="failed",
            completed=exception_completed(),
            failed=exception_failed(),
            warnings=state.warnings,
            usage=usage,
            failure_counts=dict(failure_counts),
        )
        logger.error(
            "run failed run=%s error_type=%s",
            state.run_id,
            type(exc).__name__,
        )
        raise
    return finalize_run(
        state.project,
        state.run_dir,
        status="completed" if failed_count() == 0 else "failed",
        completed=completed_count(),
        failed=failed_count(),
        warnings=state.warnings,
        usage=usage,
        failure_counts=dict(failure_counts),
    )


async def _localized_request_loop(
    group: list[dict[str, Any]],
    *,
    payload_builder: Callable[[list[dict[str, Any]]], dict[str, Any]],
    prompt: str,
    config: dict[str, Any],
    llm: LLMClient,
    stage: str,
    accept: Callable[[str, str, Any], Awaitable[None]],
    save_error: Callable[[list[str], str, str], Awaitable[None]],
    parse: Callable[
        [str, dict[str, str]],
        tuple[dict[str, Any], list[str], list[str], bool],
    ],
    format_correction: str,
    by_id: dict[str, dict[str, Any]],
    segments: list[dict[str, Any]],
    logger: Any,
    initial_parent_request_id: str | None = None,
    repair_candidates: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    exhausted: list[str] = []
    tasks: list[
        tuple[
            list[dict[str, Any]],
            str | None,
            int,
            list[dict[str, Any]],
            list[str],
        ]
    ] = [(group, initial_parent_request_id, 0, group[:1], [])]
    while tasks:
        items, parent_request_id, format_attempt, anchor, format_errors = (
            tasks.pop(0)
        )
        expected = [str(item["segment_id"]) for item in items]
        payload = payload_builder(items or anchor)
        if not items:
            payload["segments"] = []
        if repair_candidates is not None:
            payload["segments"] = [
                {
                    "id": item["segment_id"],
                    "source": segment_model_source(item),
                    "failed_candidate": repair_candidates[
                        str(item["segment_id"])
                    ]["candidate"],
                    "validation_matches": repair_candidates[
                        str(item["segment_id"])
                    ]["findings"],
                }
                for item in items
            ]
            payload["validation_repair"] = (
                "返回不含所列残留字符的完整修正版译文。"
            )
        if format_attempt:
            correction = format_correction
            if format_errors:
                correction = (
                    f"{correction}\n错误详情：{'；'.join(format_errors[:5])}"
                )
            payload["format_correction"] = correction
        payload, id_map = localize_request_ids(payload, items)
        messages = render_messages(prompt, payload)
        request_id = f"REQ-{uuid.uuid4().hex[:12].upper()}"
        estimated = _request_estimate(messages, config, request_id)
        try:
            response, _ = await llm.chat(
                messages=messages,
                temperature=config["llm"][f"temperature_{stage}"],
                estimated_input_tokens=estimated,
                request_id=request_id,
                parent_request_id=parent_request_id,
                segment_id_map=id_map,
            )
            valid, unresolved, parse_errors, response_complete = parse(
                response.content, id_map
            )
        except FatalExternalError:
            raise
        except ContextLengthError as exc:
            if exc.segment_ids is None:
                exc.segment_ids = tuple(expected)
            raise
        except ExternalError as exc:
            await save_error(expected, request_id, str(exc))
            continue
        for segment_id, value in valid.items():
            try:
                await accept(segment_id, request_id, value)
            except IncompleteError as exc:
                parse_errors.append(str(exc))
                if segment_id not in unresolved:
                    unresolved.append(segment_id)
                continue
        if response_complete and not unresolved:
            continue
        logger.warning(
            "format correction request=%s attempt=%d unresolved=%d errors=%d",
            request_id,
            format_attempt + 1,
            len(unresolved),
            len(parse_errors),
        )
        if format_attempt >= config["retry"]["format_max_attempts"]:
            exhausted.extend(unresolved)
            continue
        if not unresolved:
            tasks.append(([], request_id, format_attempt + 1, anchor, parse_errors))
            continue
        unresolved_groups = contiguous_groups(
            (by_id[segment_id] for segment_id in unresolved),
            all_segments=segments,
            cross_boundary=stage in config["chunking"]["cross_boundary_batching"],
        )
        tasks.extend(
            (
                unresolved_group,
                request_id,
                format_attempt + 1,
                unresolved_group[:1],
                parse_errors,
            )
            for unresolved_group in unresolved_groups
        )
    return list(dict.fromkeys(exhausted))


def _restore_leading_whitespace(source: str, text: str) -> str:
    prefix_end = 0
    while prefix_end < len(source) and source[prefix_end].isspace():
        prefix_end += 1
    return source[:prefix_end] + text.lstrip()


def _confirm_fingerprint_reuse(
    stage: str,
    existing_fingerprints: set[str] | frozenset[str],
    current_fingerprint: str,
    reusable_count: int,
    *,
    force: bool,
    resume_run_id: str | None,
    reuse_allowed: bool,
    dry_run: bool,
    interactive: bool | None = None,
    choice: str | None = None,
) -> str | None:
    if (
        force
        or reusable_count == 0
        or not existing_fingerprints
        or existing_fingerprints == {current_fingerprint}
    ):
        return None
    message = (
        f"{stage} 选定范围有 {reusable_count} 个可复用 Segment，"
        f"来自 {len(existing_fingerprints)} 个设置指纹，"
        f"与当前指纹 {current_fingerprint} 不一致"
    )
    if resume_run_id is not None:
        return f"{message}；已通过续用 Run 明确复用"
    if reuse_allowed:
        return f"{message}；已显式复用"
    if dry_run:
        return (
            f"{message}；正式执行必须选择 "
            "--reuse-mixed-fingerprints 或 --force"
        )
    interactive = sys.stdin.isatty() if interactive is None else interactive
    if choice is None and not interactive:
        raise UsageError(
            f"{message}；非交互环境必须指定 "
            "--reuse-mixed-fingerprints 或 --force"
        )
    if choice is None:
        print(
            f"{message}\n是否复用这些已完成结果？[r]euse/[n]ew: ",
            end="",
            file=sys.stderr,
            flush=True,
        )
        while True:
            answer = input().strip().casefold()
            if answer in {"r", "reuse"}:
                choice = "reuse"
                break
            if answer in {"n", "new"}:
                choice = "new"
                break
            print(
                "请输入 reuse 或 new: ",
                end="",
                file=sys.stderr,
                flush=True,
            )
    if choice == "reuse":
        return f"{message}；已由用户确认复用"
    if choice == "new":
        raise UsageError(f"{message}；已拒绝复用，请使用 --force 重做选定范围")
    raise UsageError("指纹复用选择必须是 reuse 或 new")


def _request_estimate(
    messages: list[dict[str, str]],
    config: dict[str, Any],
    request_id: str,
) -> int:
    estimated = estimate_messages(
        messages,
        config["execution"]["token_safety_factor"],
    )
    input_limit = (
        config["llm"]["context_window_tokens"]
        - config["llm"]["context_safety_margin_tokens"]
    )
    if estimated > input_limit:
        raise ContextLengthError(
            "渲染后的 Prompt 超过模型硬限制",
            request_id=request_id,
        )
    if (
        config["execution"]["input_tokens_per_minute"] > 0
        and estimated > config["execution"]["input_tokens_per_minute"]
    ):
        raise RequestSizeError(
            "单请求预测 Token 超过 ITPM",
            reason="itpm",
        )
    return estimated


def _prompt_language(project: Path, stage: str, requested: str | None) -> str:
    """Resolve the run prompt language, falling back to zh-CN."""
    value = requested or resolve_language()
    if (
        value in SUPPORTED_LANGUAGES
        and (project / "prompts" / prompt_file(stage, value)).is_file()
    ):
        return value
    return "zh-CN"


def prompt_middle_digests(project: Path, stage: str) -> dict[str, str]:
    """Per-language middle content digests; missing languages are omitted."""
    digests: dict[str, str] = {}
    for language in PROMPT_LANGUAGES:
        path = project / "prompts" / prompt_file(stage, language)
        if path.is_file():
            digests[language] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def _prompt(project: Path, stage: str, language: str | None = None) -> str:
    language = _prompt_language(project, stage, language)
    name = prompt_file(stage, language)
    try:
        middle = (project / "prompts" / name).read_text(encoding="utf-8")
    except OSError as exc:
        raise StorageError(f"无法读取 Prompt：{name}: {exc}") from exc
    return full_prompt(stage, middle, language)


def load_terms(project: Path) -> dict[str, Any] | None:
    path = project / "terminology" / "terms.json"
    return read_json(project, path) if record_exists(project, path) else None


@dataclass(frozen=True)
class TermNormalization:
    form: str | None
    casefold: bool


def term_normalization(config: dict[str, Any]) -> TermNormalization:
    terminology = config["terminology"]
    return TermNormalization(
        form=terminology["unicode_normalization"] or None,
        casefold=terminology["case_insensitive"],
    )


def normalize_term(value: str, spec: TermNormalization) -> str:
    if spec.form:
        value = unicodedata.normalize(spec.form, value)
    if spec.casefold:
        value = value.casefold()
    return value.strip()


def _term_bucket() -> dict[str, Any]:
    return {
        "sources": [],
        "categories": [],
        "descriptions": [],
        "translations": [],
        "aliases": [],
        "alias_conflicts": [],
        "canonical_source": None,
    }


def _add_term_candidate(
    merged: dict[str, dict[str, Any]],
    candidate: dict[str, Any],
    spec: TermNormalization,
) -> None:
    normalized = normalize_term(str(candidate["source"]), spec)
    current = merged.setdefault(normalized, _term_bucket())
    current["sources"].append(str(candidate["source"]))
    category = candidate.get("category")
    if category:
        current["categories"].append(str(category))
    current["categories"].extend(
        str(value)
        for value in candidate.get("conflicts", {}).get("categories", [])
        if value
    )
    description = candidate.get("description")
    if description:
        current["descriptions"].append(str(description))
    preferred = candidate.get("preferred_translation")
    if preferred:
        current["translations"].append(str(preferred))
    current["translations"].extend(
        str(value)
        for value in candidate.get("conflicts", {}).get(
            "preferred_translations", []
        )
        if value
    )
    current["aliases"].extend(
        str(alias) for alias in candidate.get("aliases", []) if alias
    )


def _seed_published_terms(
    merged: dict[str, dict[str, Any]],
    library: dict[str, Any] | None,
    spec: TermNormalization,
) -> None:
    for term in (library or {}).get("terms", []):
        _add_term_candidate(merged, term, spec)
        description = term.get("description")
        if description and "；" in str(description):
            current = merged[normalize_term(str(term["source"]), spec)]
            current["descriptions"].remove(str(description))
            current["descriptions"].extend(
                part for part in str(description).split("；") if part
            )


def _apply_term_overrides(
    merged: dict[str, dict[str, Any]],
    overrides: dict[str, dict[str, Any]],
) -> None:
    for normalized, override in overrides.items():
        if override.get("disabled"):
            merged.pop(normalized, None)
            continue
        current = merged.setdefault(normalized, _term_bucket())
        if override.get("source"):
            current["sources"] = [override["source"]]
        for source_key, target_key in (
            ("category", "categories"),
            ("description", "descriptions"),
            ("preferred_translation", "translations"),
        ):
            if override.get(source_key):
                current[target_key] = [override[source_key]]
        if "aliases" in override:
            current["aliases"] = list(override.get("aliases") or [])


def _alias_primary_collisions(
    merged: dict[str, dict[str, Any]],
    *,
    policy: str,
    spec: TermNormalization,
) -> None:
    primary_sources = {
        normalized: sorted(
            set(item["sources"]), key=lambda text: (len(text), text)
        )[0]
        for normalized, item in merged.items()
        if item["sources"]
    }
    claims: dict[str, list[tuple[str, str]]] = {}
    for owner, item in merged.items():
        for alias in sorted(set(item["aliases"])):
            target = normalize_term(alias, spec)
            if target in merged and target != owner:
                claims.setdefault(target, []).append((owner, alias))
    if not claims:
        return

    parent = {
        target: owners[0][0]
        for target, owners in claims.items()
        if len({owner for owner, _ in owners}) == 1
    }
    cycle_nodes: set[str] = set()
    for node in parent:
        seen: list[str] = []
        current = node
        while current in parent:
            if current in seen:
                cycle_nodes.update(seen[seen.index(current) :])
                break
            seen.append(current)
            current = parent[current]

    unsafe_targets = {
        target for target, owners in claims.items() if len({o for o, _ in owners}) > 1
    } | cycle_nodes
    if policy == "merge":
        roots: dict[str, str] = {}
        for node in merged:
            current = node
            path: set[str] = set()
            while (
                current in parent
                and current not in unsafe_targets
                and parent[current] not in unsafe_targets
                and current not in path
            ):
                path.add(current)
                current = parent[current]
            roots[node] = current
        for node, root in list(roots.items()):
            if node == root or node not in merged or root not in merged:
                continue
            target = merged[node]
            owner = merged[root]
            if owner["canonical_source"] is None and owner["sources"]:
                owner["canonical_source"] = sorted(
                    set(owner["sources"]), key=lambda text: (len(text), text)
                )[0]
            owner["categories"].extend(target["categories"])
            owner["descriptions"].extend(target["descriptions"])
            owner["translations"].extend(target["translations"])
            owner["aliases"].extend(target["aliases"])
            owner["aliases"].extend(target["sources"])
            merged.pop(node)

    for target, owners in claims.items():
        if policy == "merge" and target not in unsafe_targets:
            continue
        reason = (
            "multiple_owners"
            if len({owner for owner, _ in owners}) > 1
            else "cycle"
            if target in cycle_nodes
            else "policy"
        )
        for owner, alias in owners:
            if owner not in merged:
                continue
            merged[owner]["alias_conflicts"].append(
                {
                    "alias": alias,
                    "primary_source": primary_sources.get(target, target),
                    "reason": reason,
                }
            )


def _build_term_rows(
    merged: dict[str, dict[str, Any]],
    *,
    alias_policy: str,
    spec: TermNormalization,
) -> list[dict[str, Any]]:
    _alias_primary_collisions(merged, policy=alias_policy, spec=spec)
    terms: list[dict[str, Any]] = []
    for index, (normalized, item) in enumerate(sorted(merged.items()), start=1):
        categories = sorted(set(item["categories"]))
        descriptions = sorted(set(item["descriptions"]))
        translations = sorted(set(item["translations"]))
        sources = sorted(set(item["sources"]), key=lambda text: (len(text), text))
        aliases = sorted(
            {
                alias
                for alias in item["aliases"]
                if normalize_term(alias, spec) != normalized
            }
        )
        terms.append(
            {
                "record_id": f"TERM-{index:06d}",
                "source": item["canonical_source"]
                or (sources[0] if sources else normalized),
                "normalized": normalized,
                "category": categories[0] if len(categories) == 1 else None,
                "description": "；".join(descriptions),
                "preferred_translation": (
                    translations[0] if len(translations) == 1 else None
                ),
                "aliases": aliases,
                "conflicts": {
                    "categories": categories if len(categories) > 1 else [],
                    "preferred_translations": (
                        translations if len(translations) > 1 else []
                    ),
                    "alias_primaries": sorted(
                        item["alias_conflicts"],
                        key=lambda value: (
                            value["alias"],
                            value["primary_source"],
                            value["reason"],
                        ),
                    ),
                },
            }
        )
    return terms


def build_term_library_rows(
    project: Path,
    base_terms: list[dict[str, Any]],
    overrides: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    config = load_project_config(project)
    spec = term_normalization(config)
    merged: dict[str, dict[str, Any]] = {}
    _seed_published_terms(merged, {"terms": base_terms}, spec)
    _apply_term_overrides(merged, overrides)
    return _build_term_rows(
        merged,
        alias_policy=str(config["terminology"]["alias_primary_collision"]),
        spec=spec,
    )


TERM_CSV_FIELDS = (
    "source",
    "preferred_translation",
    "category",
    "description",
    "aliases_json",
    "disabled",
    "category_conflicts_json",
    "preferred_translation_conflicts_json",
)


def _exchange_term(
    value: Any, location: str, spec: TermNormalization
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UsageError(f"术语必须是对象：{location}")
    allowed = {
        "source",
        "preferred_translation",
        "category",
        "description",
        "aliases",
        "disabled",
        "conflicts",
    }
    unknown = set(value) - allowed
    if unknown:
        raise UsageError(
            f"术语包含未知字段：{location}: {', '.join(sorted(unknown))}"
        )
    source = value.get("source")
    if not isinstance(source, str) or not source.strip():
        raise UsageError(f"术语 source 不能为空：{location}")
    for key in ("preferred_translation", "category", "description"):
        field = value.get(key)
        if field is not None and not isinstance(field, str):
            raise UsageError(f"术语 {key} 必须是字符串或 null：{location}")
    aliases = value.get("aliases", [])
    if not isinstance(aliases, list) or not all(
        isinstance(alias, str) for alias in aliases
    ):
        raise UsageError(f"术语 aliases 必须是字符串数组：{location}")
    disabled = value.get("disabled", False)
    if not isinstance(disabled, bool):
        raise UsageError(f"术语 disabled 必须是布尔值：{location}")
    conflicts = value.get("conflicts", {})
    if not isinstance(conflicts, dict):
        raise UsageError(f"术语 conflicts 必须是对象：{location}")
    unknown_conflicts = set(conflicts) - {
        "categories",
        "preferred_translations",
    }
    if unknown_conflicts:
        raise UsageError(
            f"术语 conflicts 包含未知字段：{location}: "
            f"{', '.join(sorted(unknown_conflicts))}"
        )
    for key in ("categories", "preferred_translations"):
        candidates = conflicts.get(key, [])
        if not isinstance(candidates, list) or not all(
            isinstance(candidate, str) for candidate in candidates
        ):
            raise UsageError(f"术语冲突 {key} 必须是字符串数组：{location}")
    return {
        "source": source.strip(),
        "preferred_translation": (
            value["preferred_translation"].strip()
            if value.get("preferred_translation")
            else None
        ),
        "category": value["category"].strip() if value.get("category") else None,
        "description": (
            value["description"].strip() if value.get("description") else ""
        ),
        "aliases": [
            alias.strip()
            for alias in aliases
            if alias.strip()
            and normalize_term(alias, spec) != normalize_term(source, spec)
        ],
        "disabled": disabled,
        "conflicts": {
            "categories": [
                candidate.strip()
                for candidate in conflicts.get("categories", [])
                if candidate.strip()
            ],
            "preferred_translations": [
                candidate.strip()
                for candidate in conflicts.get("preferred_translations", [])
                if candidate.strip()
            ],
        },
    }


def _load_term_exchange(
    path: Path, spec: TermNormalization
) -> list[dict[str, Any]]:
    suffix = path.suffix.casefold()
    try:
        content = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise UsageError(f"无法读取术语文件：{path}: {exc}") from exc
    if suffix == ".json":
        try:
            document = json.loads(content)
        except json.JSONDecodeError as exc:
            raise UsageError(f"术语 JSON 无效：{path}: {exc}") from exc
        if not isinstance(document, dict):
            raise UsageError("术语 JSON 顶层必须是对象")
        if set(document) != {"schema_version", "record_type", "terms"}:
            raise UsageError("术语 JSON 必须只包含 schema_version、record_type、terms")
        if (
            document.get("schema_version") != 1
            or document.get("record_type") != "terminology_exchange"
        ):
            raise UsageError("不支持的术语交换格式版本")
        values = document.get("terms")
        if not isinstance(values, list):
            raise UsageError("术语 JSON 的 terms 必须是数组")
        return [
            _exchange_term(value, f"terms[{index}]", spec)
            for index, value in enumerate(values)
        ]
    if suffix != ".csv":
        raise UsageError("术语文件扩展名必须是 .json 或 .csv")
    try:
        reader = csv.DictReader(io.StringIO(content))
        if tuple(reader.fieldnames or ()) != TERM_CSV_FIELDS:
            raise UsageError(
                "术语 CSV 表头必须是：" + ",".join(TERM_CSV_FIELDS)
            )
        values = []
        for index, row in enumerate(reader, start=2):
            try:
                aliases = json.loads(row["aliases_json"] or "[]")
                category_conflicts = json.loads(
                    row["category_conflicts_json"] or "[]"
                )
                preferred_conflicts = json.loads(
                    row["preferred_translation_conflicts_json"] or "[]"
                )
            except json.JSONDecodeError as exc:
                raise UsageError(f"术语 CSV 数组字段无效：第 {index} 行") from exc
            disabled_text = (row["disabled"] or "").strip().casefold()
            if disabled_text not in {"true", "false"}:
                raise UsageError(f"术语 CSV disabled 必须是 true 或 false：第 {index} 行")
            values.append(
                _exchange_term(
                    {
                        "source": row["source"],
                        "preferred_translation": row["preferred_translation"] or None,
                        "category": row["category"] or None,
                        "description": row["description"] or "",
                        "aliases": aliases,
                        "disabled": disabled_text == "true",
                        "conflicts": {
                            "categories": category_conflicts,
                            "preferred_translations": preferred_conflicts,
                        },
                    },
                    f"第 {index} 行",
                    spec,
                )
            )
        return values
    except csv.Error as exc:
        raise UsageError(f"术语 CSV 无效：{path}: {exc}") from exc


def _term_exchange_rows(
    project: Path,
    *,
    include_disabled: bool,
    source: str = "published",
) -> list[dict[str, Any]]:
    if source not in {"published", "scanned"}:
        raise UsageError("术语导出 source 必须是 published 或 scanned")
    if source == "scanned":
        config = load_project_config(project)
        spec = term_normalization(config)
        active_path = project / "terminology" / "active_task.json"
        active = read_json(project, active_path) if record_exists(project, active_path) else None
        if not active or active.get("status") != "active":
            return []
        task_id = str(active.get("active_task_id", ""))
        records = [
            record
            for record in read_jsonl(
                project,
                project / "terminology" / "candidates.jsonl",
                task_id=task_id,
            )
        ]
        merged: dict[str, dict[str, Any]] = {}
        for record in records:
            for candidate in record.get("terms", []):
                if isinstance(candidate, dict):
                    _add_term_candidate(merged, candidate, spec)
        overrides_document = read_json(project, project / "terminology" / "overrides.json")
        overrides = {
            str(item["normalized"]): dict(item)
            for item in overrides_document.get("overrides", [])
        }
        _apply_term_overrides(merged, overrides)
        alias_policy = str(config["terminology"]["alias_primary_collision"])
        candidates = _build_term_rows(merged, alias_policy=alias_policy, spec=spec)
        rows: list[dict[str, Any]] = []
        for term in candidates:
            normalized = normalize_term(str(term["source"]), spec)
            override = overrides.get(normalized, {})
            disabled = bool(override.get("disabled", False))
            if disabled and not include_disabled:
                continue
            rows.append(
                {
                    "source": override.get("source", term["source"]),
                    "preferred_translation": override.get(
                        "preferred_translation", term.get("preferred_translation")
                    ),
                    "category": override.get("category", term.get("category")),
                    "description": override.get("description", term.get("description", "")),
                    "aliases": list(override.get("aliases", term.get("aliases", []))),
                    "disabled": disabled,
                    "conflicts": {
                        "categories": list(term.get("conflicts", {}).get("categories", [])),
                        "preferred_translations": list(
                            term.get("conflicts", {}).get("preferred_translations", [])
                        ),
                    },
                }
            )
        return rows
    library = load_terms(project)
    current = {
        str(item["normalized"]): dict(item)
        for item in (library or {}).get("terms", [])
    }
    overrides_document = read_json(project, project / "terminology" / "overrides.json")
    overrides = {
        str(item["normalized"]): dict(item)
        for item in overrides_document.get("overrides", [])
    }
    rows: list[dict[str, Any]] = []
    for normalized in sorted(set(current) | set(overrides)):
        term = current.get(normalized, {})
        override = overrides.get(normalized, {})
        disabled = bool(override.get("disabled", False))
        if disabled and not include_disabled:
            continue
        conflicts = term.get("conflicts", {})
        rows.append(
            {
                "source": override.get("source", term.get("source", normalized)),
                "preferred_translation": override.get(
                    "preferred_translation", term.get("preferred_translation")
                ),
                "category": override.get("category", term.get("category")),
                "description": override.get(
                    "description", term.get("description", "")
                ),
                "aliases": list(override.get("aliases", term.get("aliases", []))),
                "disabled": disabled,
                "conflicts": {
                    "categories": list(conflicts.get("categories", [])),
                    "preferred_translations": list(
                        conflicts.get("preferred_translations", [])
                    ),
                },
            }
        )
    return rows


def export_terms(
    project: Path,
    output: Path,
    *,
    include_disabled: bool,
    source: str = "published",
) -> dict[str, Any]:
    rows = _term_exchange_rows(
        project,
        include_disabled=include_disabled,
        source=source,
    )
    if output.suffix.casefold() == ".json":
        atomic_write_text(
            output,
            json.dumps(
                {
                    "schema_version": 1,
                    "record_type": "terminology_exchange",
                    "terms": rows,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
    elif output.suffix.casefold() == ".csv":
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=TERM_CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "source": row["source"],
                    "preferred_translation": row["preferred_translation"] or "",
                    "category": row["category"] or "",
                    "description": row["description"] or "",
                    "aliases_json": json.dumps(
                        row["aliases"], ensure_ascii=False, separators=(",", ":")
                    ),
                    "disabled": "true" if row["disabled"] else "false",
                    "category_conflicts_json": json.dumps(
                        row["conflicts"]["categories"],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "preferred_translation_conflicts_json": json.dumps(
                        row["conflicts"]["preferred_translations"],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                }
            )
        atomic_write_text(output, "\ufeff" + buffer.getvalue())
    else:
        raise UsageError("术语输出扩展名必须是 .json 或 .csv")
    return {
        "output": str(output),
        "format": output.suffix.casefold().removeprefix("."),
        "exported": len(rows),
        "include_disabled": include_disabled,
        "source": source,
    }


def import_terms(
    project: Path,
    input_path: Path,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    config = load_project_config(project)
    spec = term_normalization(config)
    imported = _load_term_exchange(input_path, spec)
    disabled_by_normalized: dict[str, bool] = {}
    merged_import: dict[str, dict[str, Any]] = {}
    for item in imported:
        normalized = normalize_term(item["source"], spec)
        previous_disabled = disabled_by_normalized.setdefault(
            normalized, item["disabled"]
        )
        if previous_disabled != item["disabled"]:
            raise UsageError(f"同一 normalized 术语的 disabled 冲突：{item['source']}")
        if not item["disabled"]:
            _add_term_candidate(merged_import, item, spec)

    library = load_terms(project)
    merged: dict[str, dict[str, Any]] = {}
    _seed_published_terms(merged, library, spec)
    for normalized, item in merged_import.items():
        target = merged.setdefault(normalized, _term_bucket())
        for key in (
            "sources",
            "categories",
            "descriptions",
            "translations",
            "aliases",
        ):
            target[key].extend(item[key])

    overrides_path = project / "terminology" / "overrides.json"
    overrides_document = read_json(project, overrides_path)
    original_overrides = [
        dict(item) for item in overrides_document.get("overrides", [])
    ]
    overrides = {
        str(item["normalized"]): dict(item) for item in original_overrides
    }
    for item in imported:
        if not item["disabled"]:
            continue
        normalized = normalize_term(item["source"], spec)
        current = overrides.get(normalized, {"normalized": normalized})
        overrides[normalized] = {
            **current,
            "source": current.get("source", item["source"]),
            "disabled": True,
        }
    _apply_term_overrides(merged, overrides)
    terms = _build_term_rows(
        merged,
        alias_policy=str(config["terminology"]["alias_primary_collision"]),
        spec=spec,
    )
    overrides_list = [overrides[key] for key in sorted(overrides)]
    existing_terms = list((library or {}).get("terms", []))
    changed = terms != existing_terms or overrides_list != original_overrides
    next_revision = int(library["terms_revision"]) + 1 if library else 1
    summary = {
        "input": str(input_path),
        "format": input_path.suffix.casefold().removeprefix("."),
        "imported": len(imported),
        "changed": changed,
        "terms_revision": next_revision if changed else (
            int(library["terms_revision"]) if library else None
        ),
        "dry_run": dry_run,
        "warnings": [],
    }
    if dry_run or not changed:
        return summary
    override_record = record_header(
        "terminology_overrides",
        str(read_json(project, project / "project.json")["project_id"]),
        record_id="TERMINOLOGY-OVERRIDES",
        overrides=overrides_list,
        origin="terms_import",
    )
    library_record = record_header(
        "terminology_library",
        str(read_json(project, project / "project.json")["project_id"]),
        record_id=f"TERMS-{next_revision}",
        terms_revision=next_revision,
        published_run_id=library.get("published_run_id") if library else None,
        active_task_id=library.get("active_task_id") if library else None,
        terms=terms,
        origin="terms_import",
    )
    write_json(project, overrides_path, override_record)
    write_json(project, project / "terminology" / "terms.json", library_record)
    return summary


def _validate_term_items(
    content: str,
) -> tuple[list[dict[str, Any]], list[str], bool]:
    document = parse_jsonl_document(content, record_type="term")
    terms: list[dict[str, Any]] = []
    errors = list(document.errors)
    for index, item in enumerate(document.records, start=1):
        item_errors: list[str] = []
        for key in ("source", "category", "description"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                item_errors.append(f"术语记录 {index} 缺少有效 {key}")
        preferred = item.get("preferred_translation")
        if preferred is not None and not isinstance(preferred, str):
            item_errors.append(
                f"术语记录 {index} 的 preferred_translation 类型错误"
            )
        aliases = item.get("aliases", [])
        if not isinstance(aliases, list) or not all(
            isinstance(alias, str) for alias in aliases
        ):
            item_errors.append(f"术语记录 {index} 的 aliases 类型错误")
        if item_errors:
            errors.extend(item_errors)
            continue
        terms.append(
            {
                "source": item["source"].strip(),
                "category": item["category"].strip(),
                "description": item["description"].strip(),
                "preferred_translation": preferred.strip() if preferred else None,
                "aliases": [alias.strip() for alias in aliases if alias.strip()],
            }
        )
    return terms, errors, document.complete and not errors


def _merge_and_publish_terms(
    project: Path,
    *,
    task_id: str,
    project_id: str,
    published_run_id: str,
    active_status: str = "completed",
) -> dict[str, Any]:
    config = load_project_config(project)
    spec = term_normalization(config)
    previous = load_terms(project)
    candidates = [
        record
        for record in read_jsonl(
            project,
            project / "terminology" / "candidates.jsonl",
            task_id=task_id,
        )
    ]
    merged: dict[str, dict[str, Any]] = {}
    _seed_published_terms(merged, previous, spec)
    for record in candidates:
        for candidate in record.get("terms", []):
            _add_term_candidate(merged, candidate, spec)

    overrides_data = read_json(project, project / "terminology" / "overrides.json")
    overrides = {
        str(item["normalized"]): item for item in overrides_data.get("overrides", [])
    }
    _apply_term_overrides(merged, overrides)
    alias_policy = str(config["terminology"]["alias_primary_collision"])
    terms = _build_term_rows(merged, alias_policy=alias_policy, spec=spec)
    revision = int(previous["terms_revision"]) + 1 if previous else 1
    library = record_header(
        "terminology_library",
        project_id,
        record_id=f"TERMS-{revision}",
        terms_revision=revision,
        published_run_id=published_run_id,
        active_task_id=task_id,
        terms=terms,
    )
    write_json(project, project / "terminology" / "terms.json", library)
    active = read_json(project, project / "terminology" / "active_task.json")
    active["status"] = active_status
    active["terms_revision"] = revision
    write_json(project, project / "terminology" / "active_task.json", active)
    return library


def publish_partial_terms(project: Path) -> dict[str, Any]:
    """Publish current candidates without closing or deleting scan history."""
    if find_running_runs(project, "terminology"):
        raise UsageError("术语扫描仍在运行，结束 Run 后才能发布现有结果")
    active_path = project / "terminology" / "active_task.json"
    if not record_exists(project, active_path):
        raise UsageError("当前没有可发布的活动术语扫描")
    active = read_json(project, active_path)
    if active.get("status") != "active":
        raise UsageError("当前没有可发布的活动术语扫描")
    task_id = str(active.get("active_task_id", ""))
    candidates = [
        record
        for record in read_jsonl(
            project,
            project / "terminology" / "candidates.jsonl",
            task_id=task_id,
        )
        if record.get("terms")
    ]
    if not candidates:
        raise UsageError("当前活动扫描没有可发布的候选术语")
    config = load_project_config(project)
    spec = term_normalization(config)
    candidate_sources = {
        normalize_term(str(term.get("source")), spec)
        for record in candidates
        for term in record.get("terms", [])
        if isinstance(term, dict) and term.get("source")
    }
    metadata = read_json(project, project / "project.json")
    published_run_id = str(candidates[-1].get("run_id") or task_id)
    library = _merge_and_publish_terms(
        project,
        task_id=task_id,
        project_id=str(metadata["project_id"]),
        published_run_id=published_run_id,
        active_status="partial_published",
    )
    return {
        "published": True,
        "active_task_id": task_id,
        "terms_revision": library["terms_revision"],
        "published_terms": len(library.get("terms", [])),
        "scanned_terms": len(candidate_sources),
    }


async def run_terminology(
    project: Path,
    scope: Scope,
    *,
    http_client: httpx.AsyncClient | None = None,
    limiter: SlidingWindowLimiter | None = None,
    resume_run_id: str | None = None,
    reuse_mixed_fingerprints: bool = False,
    prompt_language: str | None = None,
    on_progress: Callable[[int, int, int], None] | None = None,
    on_usage: Callable[[dict[str, Any] | None], None] | None = None,
) -> dict[str, Any]:
    logger = get_logger("terminology")
    scope, resume_arguments_ignored = _resume_scope(project, scope, resume_run_id)
    config, metadata, files, segments = _project_context(
        project, stage="terminology"
    )
    _require_nonempty_segments(segments)
    language = _prompt_language(project, "terminology", prompt_language)
    prompt = _prompt(project, "terminology", language)
    fingerprint = stage_fingerprint(
        config, "terminology", prompt_middle_digests(project, "terminology")
    )
    active_path = project / "terminology" / "active_task.json"
    active = read_json(project, active_path) if record_exists(project, active_path) else None
    published = load_terms(project)

    resume_manifest = (
        read_json(project, project / "runs" / resume_run_id / "manifest.json")
        if resume_run_id is not None
        else None
    )
    create_task = resume_run_id is None and (
        scope.force
        or active is None
        or active.get("status") == "partial_published"
    )
    if resume_manifest is not None:
        task_id = str(resume_manifest.get("active_task_id", ""))
        if not task_id:
            raise StorageError(f"术语 Run 缺少 active_task_id：{resume_run_id}")
        if active is None or active.get("active_task_id") != task_id:
            raise StorageError(
                f"术语 Run 的 active task 不再可用：{resume_run_id}"
            )
    if create_task:
        task_id = f"TERM-TASK-{uuid.uuid4().hex[:10].upper()}"
        active = record_header(
            "terminology_task",
            str(metadata["project_id"]),
            record_id=task_id,
            active_task_id=task_id,
            status="active",
            initial_stage_fingerprint=fingerprint,
        )
        if not scope.dry_run:
            write_json(project, active_path, active)
    elif active and active.get("status") == "active":
        task_id = str(active["active_task_id"])
    else:
        task_id = str(active.get("active_task_id", "none")) if active else "none"

    selected = select_scope(segments, files, scope)
    if scope.force:
        # A forced terminology run rescans the full project and merges at publish.
        selected = [segment for segment in segments if not segment["is_empty"]]
    scans = [
        record
        for record in read_jsonl(
            project,
            project / "terminology" / "scans.jsonl",
            task_id=task_id,
        )
    ]
    completed_ids = {
        str(record["segment_id"])
        for record in scans
        if record.get("status") == "completed"
    }
    work = (
        selected
        if scope.force and not create_task
        else [
            segment
            for segment in selected
            if str(segment["segment_id"]) not in completed_ids
        ]
    )
    selected_ids = {str(segment["segment_id"]) for segment in selected}
    existing_fingerprints = {
        str(record["stage_fingerprint"])
        for record in scans
        if record.get("status") == "completed"
        and str(record.get("segment_id")) in selected_ids
        and record.get("stage_fingerprint")
    }
    usage: dict[str, Any] | None = None
    warnings = _assemble_warnings(
        stage="terminology",
        resume_run_id=resume_run_id,
        resume_arguments_ignored=resume_arguments_ignored,
        resume_message=(
            f"续用 Run {resume_run_id} 的原始范围和术语任务；"
            "本次使用当前 config 和 Prompt"
        ),
        config=config,
        fingerprint=fingerprint,
        existing_fingerprints=existing_fingerprints,
        reusable_count=len(selected_ids & completed_ids),
        force=scope.force,
        reuse_allowed=reuse_mixed_fingerprints,
        dry_run=scope.dry_run,
        extra=[],
    )
    context_config = config["context"]["terminology"]

    def payload_builder(items: list[dict[str, Any]]) -> dict[str, Any]:
        raw_context = (
            previous_context(
                segments,
                items[0],
                context_config["previous_segments"],
            )
            if context_config["enabled"]
            else []
        )
        return {
            "target_language": config["project"]["target_language"],
            "reference_context": [
                {"source": item["source"]} for item in raw_context
            ],
            "source_segments": [
                {"source": item["source"]} for item in items
            ],
        }

    run_id, run_dir, fail_planning = _create_or_continue_run(
        project,
        "terminology",
        scope=scope,
        config=config,
        fingerprint=fingerprint,
        prompt=prompt,
        resume_run_id=resume_run_id,
        selected_count=len(selected),
        requested_count=len(work),
        reused_count=len(selected) - len(work),
        details={
            "active_task_id": task_id,
            "scope": _scope_record(scope, force_all=scope.force),
            "prompt_language": language,
        },
        warnings=warnings,
    )

    preflight = _split_oversized_preflight(
        work,
        stage="terminology",
        config=config,
        segments=segments,
        prompt=prompt,
        payload_builder=payload_builder,
        fail_planning=fail_planning,
        make_probe=lambda segment, part: {
            **segment,
            "segment_id": f"{segment['segment_id']}-PROBE",
            "source": part,
        },
        split_part=lambda part: list(_split_source_once(part)),
        accept_part=lambda segment, part_id, part: {
            **segment,
            "segment_id": part_id,
            "source": part,
        },
    )
    request_segments = preflight.request_segments
    part_original = preflight.part_original
    original_parts = preflight.original_parts
    preflight_failed = preflight.preflight_failed

    if scope.dry_run:
        plans = build_chunk_plans(
            request_segments,
            all_segments=segments,
            config=config,
            stage="terminology",
            prompt=prompt,
            payload_builder=payload_builder,
        )
        logger.info(
            "stage plan selected=%d requested=%d reused=%d chunks=%d",
            len(selected),
            len(work),
            len(selected) - len(work),
            len(plans),
        )
        return {
            "stage": "terminology",
            "dry_run": True,
            "resume_run_id": resume_run_id,
            "active_task_id": task_id,
            "selected": len(selected),
            "requested": len(work),
            "chunks": len(plans),
            "estimated_input_tokens": sum(
                plan.estimated_input_tokens for plan in plans
            ),
            "warnings": warnings,
        }

    assert run_id is not None and run_dir is not None
    write_lock = asyncio.Lock()
    part_success: dict[str, set[str]] = {}
    failed_originals: set[str] = set()
    failure_counts: Counter[str] = Counter()
    completed_original_ids: set[str] = set()
    state = StageRunState(
        project=project,
        stage="terminology",
        config=config,
        metadata=metadata,
        segments=segments,
        prompt=prompt,
        fingerprint=fingerprint,
        resume_run_id=resume_run_id,
        warnings=warnings,
        run_id=run_id,
        run_dir=run_dir,
        on_usage=on_usage,
    )

    def report_progress() -> None:
        if on_progress is not None:
            on_progress(
                len(selected) - len(work)
                + len(completed_original_ids),
                len(failed_originals),
                len(selected),
            )

    report_progress()

    async def process_once(
        chunk: ChunkPlan,
        initial_parent_request_id: str | None = None,
    ) -> tuple[int, int]:
        unresolved = list(chunk.segments)
        parent_request_id = initial_parent_request_id
        for format_attempt in range(config["retry"]["format_max_attempts"] + 1):
            payload = payload_builder(unresolved)
            if format_attempt:
                correction = (
                    "上一次响应不符合 JSONL 协议。每行只输出一个紧凑 JSON "
                    "对象，不要解释，最后一行必须严格输出 {\"type\":\"end\"}，"
                    "不要输出 {\"type\":\"type\":\"end\"} 或其他字段。"
                )
                if parse_errors:
                    correction = (
                        f"{correction}\n错误详情：{'；'.join(parse_errors[:5])}"
                    )
                payload["format_correction"] = correction
            messages = render_messages(prompt, payload)
            request_id = f"REQ-{uuid.uuid4().hex[:12].upper()}"
            estimated = _request_estimate(messages, config, request_id)
            try:
                response, _ = await state.llm.chat(
                    messages=messages,
                    temperature=config["llm"]["temperature_terminology"],
                    estimated_input_tokens=estimated,
                    request_id=request_id,
                    parent_request_id=parent_request_id,
                )
                terms, parse_errors, response_complete = _validate_term_items(
                    response.content
                )
            except FatalExternalError:
                raise
            except ContextLengthError as exc:
                if exc.segment_ids is None:
                    exc.segment_ids = tuple(
                        str(segment["segment_id"]) for segment in unresolved
                    )
                raise
            except ExternalError as exc:
                async with write_lock:
                    for segment in unresolved:
                        segment_id = part_original.get(
                            str(segment["segment_id"]), str(segment["segment_id"])
                        )
                        if segment_id in failed_originals:
                            continue
                        failed_originals.add(segment_id)
                        failure_counts["external_error"] += 1
                        report_progress()
                        append_jsonl(
                            project,
                            project / "terminology" / "scans.jsonl",
                            record_header(
                                "terminology_scan",
                                str(metadata["project_id"]),
                                stage="terminology",
                                segment_id=segment_id,
                                status="failed",
                                run_id=run_id,
                                request_id=request_id,
                                active_task_id=task_id,
                                stage_fingerprint=fingerprint,
                                error_class=(
                                    "external_error"
                                ),
                                error_message=str(exc),
                            ),
                        )
                return 0, len(
                    {
                        part_original.get(
                            str(segment["segment_id"]), str(segment["segment_id"])
                        )
                        for segment in unresolved
                    }
                )
            async with write_lock:
                if terms:
                    append_jsonl(
                        project,
                        project / "terminology" / "candidates.jsonl",
                        record_header(
                            "terminology_candidates",
                            str(metadata["project_id"]),
                            stage="terminology",
                            status="completed",
                            run_id=run_id,
                            request_id=request_id,
                            active_task_id=task_id,
                            stage_fingerprint=fingerprint,
                            segment_ids=[
                                segment["segment_id"] for segment in unresolved
                            ],
                            terms=terms,
                        ),
                    )
            if not response_complete:
                logger.warning(
                    "format correction request=%s attempt=%d errors=%d",
                    request_id,
                    format_attempt + 1,
                    len(parse_errors),
                )
                if format_attempt < config["retry"]["format_max_attempts"]:
                    parent_request_id = request_id
                    continue
                exc = ValueError("; ".join(parse_errors[:3]))
                async with write_lock:
                    for segment in unresolved:
                        segment_id = part_original.get(
                            str(segment["segment_id"]), str(segment["segment_id"])
                        )
                        if segment_id in failed_originals:
                            continue
                        failed_originals.add(segment_id)
                        failure_counts["format_error"] += 1
                        report_progress()
                        append_jsonl(
                            project,
                            project / "terminology" / "scans.jsonl",
                            record_header(
                                "terminology_scan",
                                str(metadata["project_id"]),
                                stage="terminology",
                                segment_id=segment_id,
                                status="failed",
                                run_id=run_id,
                                request_id=request_id,
                                active_task_id=task_id,
                                stage_fingerprint=fingerprint,
                                error_class="format_error",
                                error_message=str(exc),
                            ),
                        )
                return 0, len(
                    {
                        part_original.get(
                            str(segment["segment_id"]), str(segment["segment_id"])
                        )
                        for segment in unresolved
                    }
                )
            async with write_lock:
                completed_originals: list[str] = []
                for segment in unresolved:
                    request_segment_id = str(segment["segment_id"])
                    original_id = part_original.get(request_segment_id)
                    if original_id is None:
                        completed_originals.append(request_segment_id)
                        continue
                    part_success.setdefault(original_id, set()).add(request_segment_id)
                    if (
                        original_id not in failed_originals
                        and set(original_parts[original_id])
                        <= part_success[original_id]
                    ):
                        completed_originals.append(original_id)
                for segment_id in completed_originals:
                    append_jsonl(
                        project,
                        project / "terminology" / "scans.jsonl",
                        record_header(
                            "terminology_scan",
                            str(metadata["project_id"]),
                            stage="terminology",
                            segment_id=segment_id,
                            status="completed",
                            run_id=run_id,
                            request_id=request_id,
                            active_task_id=task_id,
                            stage_fingerprint=fingerprint,
                        ),
                    )
                    completed_original_ids.add(segment_id)
                report_progress()
            logger.info(
                "chunk complete chunk=%s completed=%d",
                chunk.chunk_id or "runtime",
                len(completed_originals),
            )
            return len(completed_originals), 0
        return 0, len(unresolved)

    async def record_preflight_failure(
        failed: list[dict[str, Any]],
    ) -> None:
        async with write_lock:
            for segment in failed:
                segment_id = str(segment["segment_id"])
                if segment_id in failed_originals:
                    continue
                failed_originals.add(segment_id)
                failure_counts["context_error"] += 1
                report_progress()
                append_jsonl(
                    project,
                    project / "terminology" / "scans.jsonl",
                    record_header(
                        "terminology_scan",
                        str(metadata["project_id"]),
                        stage="terminology",
                        segment_id=segment_id,
                        status="failed",
                        run_id=run_id,
                        request_id=None,
                        active_task_id=task_id,
                        stage_fingerprint=fingerprint,
                        error_class="context_error",
                        error_message=(
                            "单 Segment 超过模型限制且内部拆分已关闭"
                        ),
                    ),
                )

    async def record_context_failure(
        items: list[dict[str, Any]],
    ) -> None:
        original_id = part_original.get(
            str(items[0]["segment_id"]), str(items[0]["segment_id"])
        )
        async with write_lock:
            if original_id not in failed_originals:
                failed_originals.add(original_id)
                failure_counts["context_error"] += 1
                report_progress()
                append_jsonl(
                    project,
                    project / "terminology" / "scans.jsonl",
                    record_header(
                        "terminology_scan",
                        str(metadata["project_id"]),
                        stage="terminology",
                        segment_id=original_id,
                        status="failed",
                        run_id=run_id,
                        request_id=None,
                        active_task_id=task_id,
                        stage_fingerprint=fingerprint,
                        error_class="context_error",
                        error_message="模型报告上下文过长",
                    ),
                )

    published_now = False
    task_completed_ids: set[str] = set()

    async def before_finalize() -> None:
        nonlocal published, published_now, task_completed_ids
        all_nonempty = [segment for segment in segments if not segment["is_empty"]]
        task_scans = [
            record
            for record in read_jsonl(
                project,
                project / "terminology" / "scans.jsonl",
                task_id=task_id,
            )
        ]
        task_completed_ids = {
            str(record["segment_id"])
            for record in task_scans
            if record.get("status") == "completed"
        }
        if active and active.get("status") == "active" and all(
            str(segment["segment_id"]) in task_completed_ids for segment in all_nonempty
        ):
            published = _merge_and_publish_terms(
                project,
                task_id=task_id,
                project_id=str(metadata["project_id"]),
                published_run_id=run_id,
            )
            published_now = True

    def completed_count() -> int:
        if resume_run_id:
            return sum(
                str(segment["segment_id"]) in task_completed_ids
                for segment in selected
            )
        return len(completed_original_ids)

    usage = await _execute_stage_run(
        state,
        request_segments=request_segments,
        part_original=part_original,
        original_parts=original_parts,
        preflight_failed=preflight_failed,
        limiter=limiter,
        payload_builder=payload_builder,
        process_once=process_once,
        record_preflight_failure=record_preflight_failure,
        record_context_failure=record_context_failure,
        before_finalize=before_finalize,
        completed_count=completed_count,
        failed_count=lambda: len(failed_originals),
        exception_completed=lambda: (
            (len(selected) - len(work)) if resume_run_id else 0
        ),
        exception_failed=lambda: len(work),
        failure_counts=failure_counts,
        http_client=http_client,
    )
    failed = len(failed_originals)
    all_nonempty = [segment for segment in segments if not segment["is_empty"]]
    logger.info(
        "run complete run=%s completed=%d failed=%d pending=%d",
        run_id,
        len(completed_original_ids),
        failed,
        len(all_nonempty) - len(task_completed_ids),
    )
    return {
        "stage": "terminology",
        "run_id": run_id,
        "active_task_id": task_id,
        "completed": len(completed_original_ids),
        "reused": len(selected) - len(work),
        "failed": failed,
        "failure_counts": dict(failure_counts),
        "pending": len(all_nonempty) - len(task_completed_ids),
        "published": published_now,
        "terms_revision": published["terms_revision"] if published else None,
        "warnings": warnings,
        "usage": usage,
    }


def match_terms(
    source: str,
    library: dict[str, Any] | None,
    limit: int,
    spec: TermNormalization,
) -> list[dict]:
    if library is None:
        return []
    normalized_source = normalize_term(source, spec)
    matched: list[tuple[int, int, int, dict[str, Any]]] = []
    for term in library.get("terms", []):
        main_name = normalize_term(str(term.get("source", "")), spec)
        conflicted_aliases = {
            normalize_term(str(item.get("alias", "")), spec)
            for item in term.get("conflicts", {}).get("alias_primaries", [])
        }
        alias_names = [
            normalize_term(str(name), spec)
            for name in term.get("aliases", [])
            if name
            and normalize_term(str(name), spec) not in conflicted_aliases
        ]
        main_hit = bool(main_name and main_name in normalized_source)
        hits = [
            name
            for name in ([main_name] if main_hit else alias_names)
            if name and name in normalized_source
        ]
        if not hits:
            continue
        matched.append(
            (
                1 if main_hit else 0,
                max(len(name) for name in hits),
                1 if term.get("preferred_translation") else 0,
                term,
            )
        )
    matched.sort(
        key=lambda item: (-item[0], -item[1], -item[2], item[3].get("source", ""))
    )
    return [
        {
            key: term.get(key)
            for key in (
                "source",
                "category",
                "description",
                "preferred_translation",
                "aliases",
            )
        }
        for _, _, _, term in matched[:limit]
    ]


def validate_translation_text(
    text: str, validation: dict[str, Any]
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    validators = []
    if validation["japanese_kana"]:
        validators.append(("japanese_kana", JAPANESE_RE))
    if validation["korean_hangul"]:
        validators.append(("korean_hangul", KOREAN_RE))
    for name, pattern in validators:
        for match in pattern.finditer(text):
            character = match.group()
            findings.append(
                {
                    "validator": name,
                    "character": character,
                    "code_point": f"U+{ord(character):04X}",
                    "start": match.start(),
                    "end": match.end(),
                }
            )
    return findings


def _split_source_once(source: str) -> tuple[str, str]:
    if len(source) < 2:
        raise ConfigError("固定 Prompt 与单字符输入仍超过模型硬限制")
    midpoint = len(source) // 2
    punctuation = "。！？!?；;，,"
    candidates = [
        index + 1
        for index, character in enumerate(source)
        if character in punctuation
    ]
    split_at = min(candidates, key=lambda index: abs(index - midpoint)) if candidates else midpoint
    if split_at <= 0 or split_at >= len(source):
        split_at = midpoint
    return source[:split_at], source[split_at:]


def _replace_with_runtime_parts(
    segment: dict[str, Any],
    *,
    part_original: dict[str, str],
    original_parts: dict[str, list[str]],
    by_id: dict[str, dict[str, Any]] | None = None,
    bases: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    segment_id = str(segment["segment_id"])
    original_id = part_original.get(segment_id, segment_id)
    left_source, right_source = _split_source_once(str(segment["source"]))
    suffix = uuid.uuid4().hex[:6].upper()
    part_ids = [
        f"{segment_id}-R1-{suffix}",
        f"{segment_id}-R2-{suffix}",
    ]
    parts = [
        {**segment, "segment_id": part_ids[0], "source": left_source},
        {**segment, "segment_id": part_ids[1], "source": right_source},
    ]
    expected = original_parts.setdefault(original_id, [segment_id])
    index = expected.index(segment_id)
    expected[index : index + 1] = part_ids
    for part in parts:
        part_id = str(part["segment_id"])
        part_original[part_id] = original_id
        if by_id is not None:
            by_id[part_id] = part
    if bases is not None:
        base = bases.get(segment_id, bases[original_id])
        text = str(base["text"])
        split_at = round(len(text) * len(left_source) / len(str(segment["source"])))
        for part_id, part_text in zip(
            part_ids,
            (text[:split_at], text[split_at:]),
            strict=True,
        ):
            bases[part_id] = {
                "record_id": base["record_id"],
                "text": part_text,
            }
    return parts


def _parse_translation_items(
    content: str, expected_ids: list[str]
) -> tuple[dict[str, str], list[str], list[str], bool]:
    document = parse_jsonl_document(content, record_type="segment")
    counts = Counter(
        item.get("id")
        for item in document.records
        if isinstance(item.get("id"), str)
    )
    expected = set(expected_ids)
    valid: dict[str, str] = {}
    errors: list[str] = list(document.errors)
    for item in document.records:
        segment_id = item.get("id")
        translation = item.get("translation")
        if segment_id not in expected:
            errors.append(f"未知 ID：{segment_id}")
            continue
        if counts[segment_id] != 1 or not isinstance(translation, str):
            errors.append(f"重复或字段错误：{segment_id}")
            continue
        valid[segment_id] = translation
    unresolved = [segment_id for segment_id in expected_ids if segment_id not in valid]
    return valid, unresolved, errors, document.complete and not errors


def _map_local_translation_response(
    content: str,
    id_map: dict[str, str],
) -> tuple[dict[str, str], list[str], list[str], bool]:
    valid, unresolved, errors, complete = _parse_translation_items(
        content, list(id_map)
    )
    return (
        {id_map[local_id]: text for local_id, text in valid.items()},
        [id_map[local_id] for local_id in unresolved],
        errors,
        complete,
    )


async def run_translation(
    project: Path,
    scope: Scope,
    *,
    http_client: httpx.AsyncClient | None = None,
    limiter: SlidingWindowLimiter | None = None,
    resume_run_id: str | None = None,
    reuse_mixed_fingerprints: bool = False,
    prompt_language: str | None = None,
    on_progress: Callable[[int, int, int], None] | None = None,
    on_usage: Callable[[dict[str, Any] | None], None] | None = None,
) -> dict[str, Any]:
    logger = get_logger("translation")
    scope, resume_arguments_ignored = _resume_scope(project, scope, resume_run_id)
    config, metadata, files, segments = _project_context(
        project, stage="translation"
    )
    _require_nonempty_segments(segments)
    language = _prompt_language(project, "translation", prompt_language)
    prompt = _prompt(project, "translation", language)
    library = load_terms(project)
    terms_revision = int(library["terms_revision"]) if library else None
    fingerprint = stage_fingerprint(
        config,
        "translation",
        prompt_middle_digests(project, "translation"),
        terms_revision=terms_revision,
    )
    history = load_stage_history(
        project, "translation"
    )
    selected_segments = select_scope(segments, files, scope)
    selection = classify_stage(selected_segments, history, force=scope.force)
    usage: dict[str, Any] | None = None
    warnings = _assemble_warnings(
        stage="translation",
        resume_run_id=resume_run_id,
        resume_arguments_ignored=resume_arguments_ignored,
        resume_message=(
            f"续用 Run {resume_run_id} 的原始范围；本次使用当前 config 和 Prompt"
        ),
        config=config,
        fingerprint=fingerprint,
        existing_fingerprints=selection.fingerprints,
        reusable_count=len(selection.reusable),
        force=scope.force,
        reuse_allowed=reuse_mixed_fingerprints,
        dry_run=scope.dry_run,
        extra=(
            ["没有已发布术语库；本次翻译 terms_revision = null"]
            if library is None
            else []
        ),
    )
    latest_text = {
        segment_id: str(record["text"])
        for segment_id, record in selection.latest_completed.items()
    }
    context_config = config["context"]["translation"]

    def payload_builder(items: list[dict[str, Any]]) -> dict[str, Any]:
        resolver = None
        if config["execution"]["scheduling_mode"] == "ordered_by_file":
            resolver = latest_text.get
        context = (
            previous_context(
                segments,
                items[0],
                context_config["previous_segments"],
                target_resolver=resolver,
                source_key="model_source",
            )
            if context_config["enabled"]
            else []
        )
        terms_by_source: dict[str, dict[str, Any]] = {}
        term_spec = term_normalization(config)
        for item in items:
            for term in match_terms(
                str(item["source"]),
                library,
                config["terminology"]["max_terms_per_segment"],
                term_spec,
            ):
                terms_by_source[str(term["source"])] = term
        return {
            "target_language": config["project"]["target_language"],
            "reference_context": context,
            "terms": list(terms_by_source.values()),
            "segments": [
                {
                    "id": item["segment_id"],
                    "source": segment_model_source(item),
                }
                for item in items
            ],
        }

    run_id, run_dir, fail_planning = _create_or_continue_run(
        project,
        "translation",
        scope=scope,
        config=config,
        fingerprint=fingerprint,
        prompt=prompt,
        resume_run_id=resume_run_id,
        selected_count=len(selection.selected),
        requested_count=len(selection.work),
        reused_count=len(selection.reusable),
        details={
            "terms_revision": terms_revision,
            "scope": _scope_record(scope),
            "prompt_language": language,
        },
        warnings=warnings,
    )

    preflight = _split_oversized_preflight(
        selection.work,
        stage="translation",
        config=config,
        segments=segments,
        prompt=prompt,
        payload_builder=payload_builder,
        fail_planning=fail_planning,
        make_probe=lambda segment, part: {
            **segment,
            "segment_id": f"{segment['segment_id']}-PROBE",
            "source": part,
        },
        split_part=lambda part: list(_split_source_once(part)),
        accept_part=lambda segment, part_id, part: {
            **segment,
            "segment_id": part_id,
            "source": part,
        },
    )
    request_segments = preflight.request_segments
    part_original = preflight.part_original
    original_parts = preflight.original_parts
    preflight_failed = preflight.preflight_failed

    if scope.dry_run:
        plans = build_chunk_plans(
            request_segments,
            all_segments=segments,
            config=config,
            stage="translation",
            prompt=prompt,
            payload_builder=payload_builder,
        )
        logger.info(
            "stage plan selected=%d requested=%d reused=%d chunks=%d",
            len(selection.selected),
            len(selection.work),
            len(selection.reusable),
            len(plans),
        )
        return {
            "stage": "translation",
            "dry_run": True,
            "resume_run_id": resume_run_id,
            "selected": len(selection.selected),
            "requested": len(selection.work),
            "reused": len(selection.reusable),
            "chunks": len(plans),
            "estimated_input_tokens": sum(
                plan.estimated_input_tokens for plan in plans
            ),
            "warnings": warnings,
        }

    assert run_id is not None and run_dir is not None
    result_path = stage_result_path(project, "translation")
    write_lock = asyncio.Lock()
    validation_pending: dict[str, dict[str, Any]] = {}
    failed_ids: set[str] = set()
    failure_counts: Counter[str] = Counter()
    completed_ids: set[str] = set()
    by_id = {str(item["segment_id"]): item for item in segments}
    by_id.update(
        {str(item["segment_id"]): item for item in request_segments}
    )
    part_results: dict[str, dict[str, tuple[str, str]]] = {}

    def report_progress() -> None:
        if on_progress is not None:
            on_progress(
                len(selection.reusable) + len(completed_ids),
                len(failed_ids),
                len(selection.selected),
            )

    report_progress()

    async def save_completed(
        segment_id: str,
        text: str,
        request_id: str,
        *,
        validation_status: str = "passed",
        findings: list[dict[str, Any]] | None = None,
    ) -> None:
        text = _restore_leading_whitespace(
            str(by_id[segment_id]["source"]),
            text,
        )
        async with write_lock:
            append_jsonl(
                project,
                result_path,
                record_header(
                    "stage_result",
                    str(metadata["project_id"]),
                    stage="translation",
                    segment_id=segment_id,
                    status="completed",
                    text=text,
                    validation_status=validation_status,
                    validation_findings=findings or [],
                    stage_fingerprint=fingerprint,
                    terms_revision=terms_revision,
                    run_id=run_id,
                    request_id=request_id,
                ),
            )
        completed_ids.add(segment_id)
        report_progress()
        latest_text[segment_id] = text
        logger.info(
            "segment complete segment=%s completed=%d failed=%d",
            segment_id,
            len(completed_ids),
            len(failed_ids),
        )

    async def save_failed(
        segment_id: str,
        request_id: str,
        error_class: str,
        message: str,
        *,
        candidate: str | None = None,
        findings: list[dict[str, Any]] | None = None,
    ) -> None:
        segment_id = part_original.get(segment_id, segment_id)
        if segment_id in failed_ids:
            return
        failure_counts[error_class] += 1
        async with write_lock:
            append_jsonl(
                project,
                result_path,
                record_header(
                    "stage_result",
                    str(metadata["project_id"]),
                    stage="translation",
                    segment_id=segment_id,
                    status="failed",
                    text=None,
                    candidate_text=candidate,
                    validation_findings=findings or [],
                    error_class=error_class,
                    error_message=message,
                    stage_fingerprint=fingerprint,
                    terms_revision=terms_revision,
                    run_id=run_id,
                    request_id=request_id,
                ),
            )
        failed_ids.add(segment_id)
        report_progress()
        logger.warning(
            "segment failed segment=%s class=%s completed=%d failed=%d",
            segment_id,
            error_class,
            len(completed_ids),
            len(failed_ids),
        )

    async def accept_candidate(
        segment_id: str, text: str, request_id: str
    ) -> None:
        text = _normalize_model_text(
            files, by_id[segment_id], str(text), "translation"
        )
        original_id = part_original.get(segment_id)
        if original_id is None:
            findings = validate_translation_text(
                text, config["validation"]["translation"]
            )
            if findings:
                validation_pending[segment_id] = {
                    "segment": by_id[segment_id],
                    "candidate": text,
                    "findings": findings,
                    "request_id": request_id,
                }
            else:
                await save_completed(segment_id, text, request_id)
            return
        part_results.setdefault(original_id, {})[segment_id] = (text, request_id)
        expected_parts = original_parts[original_id]
        if not all(part_id in part_results[original_id] for part_id in expected_parts):
            return
        combined = "".join(part_results[original_id][part_id][0] for part_id in expected_parts)
        combined_request_id = part_results[original_id][expected_parts[-1]][1]
        findings = validate_translation_text(
            combined, config["validation"]["translation"]
        )
        if findings:
            validation_pending[original_id] = {
                "segment": by_id[original_id],
                "candidate": combined,
                "findings": findings,
                "request_id": combined_request_id,
            }
        else:
            await save_completed(original_id, combined, combined_request_id)

    state = StageRunState(
        project=project,
        stage="translation",
        config=config,
        metadata=metadata,
        segments=segments,
        prompt=prompt,
        fingerprint=fingerprint,
        resume_run_id=resume_run_id,
        warnings=warnings,
        run_id=run_id,
        run_dir=run_dir,
        on_usage=on_usage,
    )

    async def accept_translation(
        segment_id: str, request_id: str, text: Any
    ) -> None:
        await accept_candidate(segment_id, str(text), request_id)

    async def save_external_error(
        expected: list[str], request_id: str, message: str
    ) -> None:
        for segment_id in expected:
            await save_failed(
                segment_id,
                request_id,
                "external_error",
                message,
            )

    async def process_once(
        chunk: ChunkPlan,
        initial_parent_request_id: str | None = None,
    ) -> None:
        group = list(chunk.segments)
        exhausted = await _localized_request_loop(
            group,
            payload_builder=payload_builder,
            prompt=prompt,
            config=config,
            llm=state.llm,
            stage="translation",
            accept=accept_translation,
            save_error=save_external_error,
            parse=_map_local_translation_response,
            format_correction=(
                "上一次响应不符合 JSONL 协议或缺少 Segment。只返回未决 "
                "ID，每行一个紧凑 JSON 对象，最后输出 {\"type\":\"end\"}。"
            ),
            by_id=by_id,
            segments=segments,
            logger=logger,
            initial_parent_request_id=initial_parent_request_id,
        )
        for segment_id in exhausted:
            await save_failed(
                segment_id,
                f"REQ-{uuid.uuid4().hex[:12].upper()}",
                "format_error",
                "格式修正次数耗尽",
            )

    async def repair_group(
        group: list[dict[str, Any]],
        subset: dict[str, dict[str, Any]],
        parent_request_id: str | None = None,
    ) -> None:
        try:
            exhausted = await _localized_request_loop(
                group,
                payload_builder=payload_builder,
                prompt=prompt,
                config=config,
                llm=state.llm,
                stage="translation",
                accept=accept_translation,
                save_error=save_external_error,
                parse=_map_local_translation_response,
                format_correction=(
                    "上一次响应不符合 JSONL 协议或缺少 Segment。只返回未决 "
                    "ID，每行一个紧凑 JSON 对象，最后输出 {\"type\":\"end\"}。"
                ),
                by_id=by_id,
                segments=segments,
                logger=logger,
                initial_parent_request_id=parent_request_id,
                repair_candidates=subset,
            )
        except ContextLengthError as exc:
            if len(group) > 1:
                midpoint = len(group) // 2
                child_groups = (group[:midpoint], group[midpoint:])
                for child_group in child_groups:
                    child_subset = {
                        str(item["segment_id"]): subset[str(item["segment_id"])]
                        for item in child_group
                    }
                    await repair_group(child_group, child_subset, exc.request_id)
                return
            item = group[0]
            segment_id = str(item["segment_id"])
            if (
                not config["chunking"]["allow_split_oversized_segment"]
                or len(str(item["source"])) < 2
            ):
                validation_pending[segment_id] = subset[segment_id]
                return
            parts = _replace_with_runtime_parts(
                item,
                part_original=part_original,
                original_parts=original_parts,
                by_id=by_id,
            )
            candidate = str(subset[segment_id]["candidate"])
            left_length = round(
                len(candidate)
                * len(str(parts[0]["source"]))
                / len(str(item["source"]))
            )
            candidate_parts = (candidate[:left_length], candidate[left_length:])
            for part, candidate_part in zip(parts, candidate_parts, strict=True):
                part_id = str(part["segment_id"])
                child_subset = {
                    part_id: {
                        "segment": part,
                        "candidate": candidate_part,
                        "findings": validate_translation_text(
                            candidate_part,
                            config["validation"]["translation"],
                        ),
                        "request_id": subset[segment_id]["request_id"],
                    }
                }
                await repair_group([part], child_subset, exc.request_id)
            return
        for segment_id in exhausted:
            validation_pending[segment_id] = subset[segment_id]

    async def record_preflight_failure(
        failed: list[dict[str, Any]],
    ) -> None:
        for segment in failed:
            await save_failed(
                str(segment["segment_id"]),
                f"REQ-{uuid.uuid4().hex[:12].upper()}",
                "context_error",
                "单 Segment 超过模型限制且内部拆分已关闭",
            )

    async def record_context_failure(
        items: list[dict[str, Any]],
    ) -> None:
        await save_failed(
            str(items[0]["segment_id"]),
            "REQ-NONE",
            "context_error",
            "模型报告上下文过长",
        )

    async def before_finalize() -> None:
        max_repairs = config["validation"]["translation"]["max_retry_attempts"]
        for repair_attempt in range(1, max_repairs + 1):
            if not validation_pending:
                break
            current_pending = dict(validation_pending)
            validation_pending.clear()
            groups = contiguous_groups(
                (item["segment"] for item in current_pending.values()),
                all_segments=segments,
                cross_boundary="translation"
                in config["chunking"]["cross_boundary_batching"],
            )
            logger.warning(
                "validation repair attempt=%d segments=%d chunks=%d",
                repair_attempt,
                len(current_pending),
                len(groups),
            )
            for group in groups:
                subset = {
                    str(item["segment_id"]): current_pending[
                        str(item["segment_id"])
                    ]
                    for item in group
                }
                await repair_group(group, subset)
        exhausted_mode = config["validation"]["translation"]["exhausted_mode"]
        if exhausted_mode == "warning":
            pending_part_originals = {
                part_original[segment_id]
                for segment_id in validation_pending
                if segment_id in part_original
            }
            for original_id in pending_part_originals:
                expected = original_parts[original_id]
                combined_parts: list[str] = []
                request_id = "REQ-NONE"
                for part_id in expected:
                    if part_id in validation_pending:
                        item = validation_pending[part_id]
                        combined_parts.append(str(item["candidate"]))
                        request_id = str(item["request_id"])
                    elif part_id in part_results.get(original_id, {}):
                        text, request_id = part_results[original_id][part_id]
                        combined_parts.append(text)
                    else:
                        break
                else:
                    combined = "".join(combined_parts)
                    await save_completed(
                        original_id,
                        combined,
                        request_id,
                        validation_status="warning",
                        findings=validate_translation_text(
                            combined,
                            config["validation"]["translation"],
                        ),
                    )
                    for part_id in expected:
                        validation_pending.pop(part_id, None)
        for segment_id, item in validation_pending.items():
            if exhausted_mode == "warning":
                await save_completed(
                    segment_id,
                    item["candidate"],
                    item["request_id"],
                    validation_status="warning",
                    findings=item["findings"],
                )
            else:
                await save_failed(
                    segment_id,
                    item["request_id"],
                    "validation_error",
                    "翻译文字校验修复次数耗尽",
                    candidate=item["candidate"],
                    findings=item["findings"],
                )

    def completed_count() -> int:
        return (
            len(selection.reusable) + len(completed_ids)
            if resume_run_id
            else len(completed_ids)
        )

    usage = await _execute_stage_run(
        state,
        request_segments=request_segments,
        part_original=part_original,
        original_parts=original_parts,
        preflight_failed=preflight_failed,
        limiter=limiter,
        payload_builder=payload_builder,
        process_once=process_once,
        record_preflight_failure=record_preflight_failure,
        record_context_failure=record_context_failure,
        before_finalize=before_finalize,
        completed_count=completed_count,
        failed_count=lambda: len(failed_ids),
        exception_completed=completed_count,
        exception_failed=lambda: len(selection.work) - len(completed_ids),
        failure_counts=failure_counts,
        http_client=http_client,
        runtime_parts_kwargs={"by_id": by_id},
    )
    failed_count = len(failed_ids)
    logger.info(
        "run complete run=%s completed=%d failed=%d",
        run_id,
        len(completed_ids),
        failed_count,
    )
    return {
        "stage": "translation",
        "run_id": run_id,
        "selected": len(selection.selected),
        "requested": len(selection.work),
        "reused": len(selection.reusable),
        "completed": len(completed_ids),
        "failed": failed_count,
        "failure_counts": dict(failure_counts),
        "last_attempt_failed": len(selection.last_attempt_failed),
        "warnings": warnings,
        "usage": usage,
    }


def require_success(summary: dict[str, Any]) -> None:
    if summary.get("failed") or summary.get("pending"):
        raise IncompleteError("选定范围仍有 pending 或 failed")


def _base_results(
    project: Path,
    stage: str,
) -> dict[str, dict[str, Any]]:
    translations = {
        str(key): value
        for key, value in classify_stage(
            [],
            load_stage_history(
                project, "translation"
            ),
            force=False,
        ).latest_completed.items()
    }
    if stage == "proofreading":
        return translations
    applied = classify_stage(
        [],
        load_stage_history(
            project, "proofreading_applied"
        ),
        force=False,
    ).latest_completed
    return {**translations, **{str(key): value for key, value in applied.items()}}


def _parse_review_items(
    content: str, expected_ids: list[str]
) -> tuple[dict[str, dict[str, Any]], list[str], list[str], bool]:
    document = parse_jsonl_document(content, record_type="segment")
    counts = Counter(
        item.get("id")
        for item in document.records
        if isinstance(item.get("id"), str)
    )
    expected = set(expected_ids)
    valid: dict[str, dict[str, Any]] = {}
    errors: list[str] = list(document.errors)
    for item in document.records:
        segment_id = item.get("id")
        review_status = item.get("status")
        if segment_id not in expected or counts[segment_id] != 1:
            errors.append(f"未知或重复 ID：{segment_id}")
            continue
        if review_status not in {"accepted", "suggested"}:
            errors.append(f"status 字段错误：{segment_id}")
            continue
        if review_status == "accepted":
            valid[str(segment_id)] = {
                "review_status": "accepted",
                "suggested_text": None,
                "reason": None,
            }
            continue
        suggested = item.get("suggested_text")
        reason = item.get("reason")
        if not isinstance(suggested, str) or not suggested:
            errors.append(f"suggested 缺少建议文本：{segment_id}")
            continue
        if reason is not None and not isinstance(reason, str):
            errors.append(f"reason 字段错误：{segment_id}")
            continue
        valid[str(segment_id)] = {
            "review_status": review_status,
            "suggested_text": suggested,
            "reason": reason,
        }
    unresolved = [segment_id for segment_id in expected_ids if segment_id not in valid]
    return valid, unresolved, errors, document.complete and not errors


def _map_local_review_response(
    content: str,
    id_map: dict[str, str],
) -> tuple[dict[str, dict[str, Any]], list[str], list[str], bool]:
    valid, unresolved, errors, complete = _parse_review_items(
        content, list(id_map)
    )
    return (
        {id_map[local_id]: parsed for local_id, parsed in valid.items()},
        [id_map[local_id] for local_id in unresolved],
        errors,
        complete,
    )


async def run_review(
    project: Path,
    stage: str,
    scope: Scope,
    *,
    http_client: httpx.AsyncClient | None = None,
    limiter: SlidingWindowLimiter | None = None,
    resume_run_id: str | None = None,
    reuse_mixed_fingerprints: bool = False,
    prompt_language: str | None = None,
    on_progress: Callable[[int, int, int], None] | None = None,
    on_usage: Callable[[dict[str, Any] | None], None] | None = None,
) -> dict[str, Any]:
    if stage not in {"proofreading", "polishing"}:
        raise ValueError(f"unsupported review stage: {stage}")
    logger = get_logger(stage)
    scope, resume_arguments_ignored = _resume_scope(project, scope, resume_run_id)
    config, metadata, files, segments = _project_context(
        project, stage=stage
    )
    _require_nonempty_segments(segments)
    language = _prompt_language(project, stage, prompt_language)
    prompt = _prompt(project, stage, language)
    library = load_terms(project)
    terms_revision = int(library["terms_revision"]) if library else None
    fingerprint = stage_fingerprint(
        config,
        stage,
        prompt_middle_digests(project, stage),
        terms_revision=terms_revision,
    )
    selected_segments = select_scope(segments, files, scope)
    bases = _base_results(project, stage)
    missing_base = [
        str(segment["segment_id"])
        for segment in selected_segments
        if str(segment["segment_id"]) not in bases
    ]
    if missing_base and not scope.dry_run:
        raise IncompleteError(
            f"{stage} 缺少上游结果，整个阶段未启动：{', '.join(missing_base[:10])}"
        )
    if scope.dry_run:
        for segment in selected_segments:
            segment_id = str(segment["segment_id"])
            bases.setdefault(
                segment_id,
                {
                    "record_id": f"DRY-BASE-{segment_id}",
                    "text": str(segment["source"]),
                },
            )
    history = load_stage_history(project, stage)
    selection = classify_stage(selected_segments, history, force=scope.force)
    usage: dict[str, Any] | None = None
    warnings = _assemble_warnings(
        stage=stage,
        resume_run_id=resume_run_id,
        resume_arguments_ignored=resume_arguments_ignored,
        resume_message=(
            f"续用 Run {resume_run_id} 的原始范围；本次使用当前 config 和 Prompt"
        ),
        config=config,
        fingerprint=fingerprint,
        existing_fingerprints=selection.fingerprints,
        reusable_count=len(selection.reusable),
        force=scope.force,
        reuse_allowed=reuse_mixed_fingerprints,
        dry_run=scope.dry_run,
        extra=(
            [
                f"{stage} dry-run 使用源文占位估算；"
                f"实际运行仍缺少 {len(missing_base)} 条上游结果"
            ]
            if missing_base
            else []
        ),
    )
    context_config = config["context"][stage]

    def payload_builder(items: list[dict[str, Any]]) -> dict[str, Any]:
        resolver = None
        if config["execution"]["scheduling_mode"] == "ordered_by_file":
            resolver = lambda segment_id: (
                str(bases[segment_id]["text"]) if segment_id in bases else None
            )
        context = (
            previous_context(
                segments,
                items[0],
                context_config["previous_segments"],
                target_resolver=resolver,
                source_key="model_source",
            )
            if context_config["enabled"]
            else []
        )
        terms_by_source: dict[str, dict[str, Any]] = {}
        term_spec = term_normalization(config)
        for item in items:
            for term in match_terms(
                str(item["source"]),
                library,
                config["terminology"]["max_terms_per_segment"],
                term_spec,
            ):
                terms_by_source[str(term["source"])] = term
        return {
            "target_language": config["project"]["target_language"],
            "reference_context": context,
            "terms": list(terms_by_source.values()),
            "segments": [
                {
                    "id": item["segment_id"],
                    "source": segment_model_source(item),
                    "current_text": bases[str(item["segment_id"])]["text"],
                }
                for item in items
            ],
        }

    run_id, run_dir, fail_planning = _create_or_continue_run(
        project,
        stage,
        scope=scope,
        config=config,
        fingerprint=fingerprint,
        prompt=prompt,
        resume_run_id=resume_run_id,
        selected_count=len(selection.selected),
        requested_count=len(selection.work),
        reused_count=len(selection.reusable),
        details={
            "terms_revision": terms_revision,
            "scope": _scope_record(scope),
            "prompt_language": language,
        },
        warnings=warnings,
    )

    def make_probe(segment: dict[str, Any], part: tuple[str, str]) -> dict[str, Any]:
        probe_id = f"{segment['segment_id']}-PROBE"
        bases[probe_id] = {
            "record_id": bases[str(segment["segment_id"])]["record_id"],
            "text": part[1],
        }
        return {**segment, "segment_id": probe_id, "source": part[0]}

    def initial_part(segment: dict[str, Any]) -> tuple[str, str]:
        return (
            str(segment["source"]),
            str(bases[str(segment["segment_id"])]["text"]),
        )

    def split_part(part: tuple[str, str]) -> list[tuple[str, str]]:
        left_source, right_source = _split_source_once(part[0])
        split_at = round(len(part[1]) * len(left_source) / len(part[0]))
        return [
            (left_source, part[1][:split_at]),
            (right_source, part[1][split_at:]),
        ]

    def accept_part(
        segment: dict[str, Any], part_id: str, part: tuple[str, str]
    ) -> dict[str, Any]:
        bases[part_id] = {
            "record_id": bases[str(segment["segment_id"])]["record_id"],
            "text": part[1],
        }
        return {**segment, "segment_id": part_id, "source": part[0]}

    preflight = _split_oversized_preflight(
        selection.work,
        stage=stage,
        config=config,
        segments=segments,
        prompt=prompt,
        payload_builder=payload_builder,
        fail_planning=fail_planning,
        make_probe=make_probe,
        split_part=split_part,
        accept_part=accept_part,
        initial_part=initial_part,
        cleanup_probe=lambda probe_id: bases.pop(probe_id, None),
    )
    request_segments = preflight.request_segments
    part_original = preflight.part_original
    original_parts = preflight.original_parts
    preflight_failed = preflight.preflight_failed

    if scope.dry_run:
        plans = build_chunk_plans(
            request_segments,
            all_segments=segments,
            config=config,
            stage=stage,
            prompt=prompt,
            payload_builder=payload_builder,
        )
        logger.info(
            "stage plan selected=%d requested=%d reused=%d chunks=%d",
            len(selection.selected),
            len(selection.work),
            len(selection.reusable),
            len(plans),
        )
        return {
            "stage": stage,
            "dry_run": True,
            "resume_run_id": resume_run_id,
            "selected": len(selection.selected),
            "requested": len(selection.work),
            "reused": len(selection.reusable),
            "chunks": len(plans),
            "estimated_input_tokens": sum(
                plan.estimated_input_tokens for plan in plans
            ),
            "warnings": warnings,
        }
    assert run_id is not None and run_dir is not None
    result_path = stage_result_path(project, stage)
    write_lock = asyncio.Lock()
    completed_ids: set[str] = set()
    failed_ids: set[str] = set()
    failure_counts: Counter[str] = Counter()
    by_id = {str(item["segment_id"]): item for item in segments}
    by_id.update(
        {str(item["segment_id"]): item for item in request_segments}
    )
    part_results: dict[str, dict[str, tuple[dict[str, Any], str]]] = {}
    state = StageRunState(
        project=project,
        stage=stage,
        config=config,
        metadata=metadata,
        segments=segments,
        prompt=prompt,
        fingerprint=fingerprint,
        resume_run_id=resume_run_id,
        warnings=warnings,
        run_id=run_id,
        run_dir=run_dir,
        on_usage=on_usage,
    )

    def report_progress() -> None:
        if on_progress is not None:
            on_progress(
                len(selection.reusable) + len(completed_ids),
                len(failed_ids),
                len(selection.selected),
            )

    report_progress()

    async def save_result(
        segment_id: str,
        request_id: str,
        *,
        parsed: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        segment_id = part_original.get(segment_id, segment_id)
        if parsed is None and segment_id in failed_ids:
            return
        base = bases[segment_id]
        async with write_lock:
            if parsed is not None:
                suggested_text = parsed["suggested_text"]
                if suggested_text is not None:
                    suggested_text = _normalize_model_text(
                        files,
                        by_id[segment_id],
                        str(suggested_text),
                        stage,
                    )
                    suggested_text = _restore_leading_whitespace(
                        str(by_id[segment_id]["source"]),
                        suggested_text,
                    )
                append_jsonl(
                    project,
                    result_path,
                    record_header(
                        "stage_result",
                        str(metadata["project_id"]),
                        stage=stage,
                        segment_id=segment_id,
                        status="completed",
                        review_status=parsed["review_status"],
                        suggested_text=suggested_text,
                        reason=parsed["reason"],
                        base_result_id=base["record_id"],
                        stage_fingerprint=fingerprint,
                        terms_revision=terms_revision,
                        run_id=run_id,
                        request_id=request_id,
                    ),
                )
                completed_ids.add(segment_id)
                report_progress()
                logger.info(
                    "segment complete segment=%s completed=%d failed=%d",
                    segment_id,
                    len(completed_ids),
                    len(failed_ids),
                )
            else:
                failure_counts["stage_error"] += 1
                append_jsonl(
                    project,
                    result_path,
                    record_header(
                        "stage_result",
                        str(metadata["project_id"]),
                        stage=stage,
                        segment_id=segment_id,
                        status="failed",
                        base_result_id=base["record_id"],
                        error_class="stage_error",
                        error_message=error,
                        stage_fingerprint=fingerprint,
                        terms_revision=terms_revision,
                        run_id=run_id,
                        request_id=request_id,
                    ),
                )
                failed_ids.add(segment_id)
                report_progress()
                logger.warning(
                    "segment failed segment=%s completed=%d failed=%d",
                    segment_id,
                    len(completed_ids),
                    len(failed_ids),
                )

    async def accept_result(
        segment_id: str,
        request_id: str,
        parsed: dict[str, Any],
    ) -> None:
        original_id = part_original.get(segment_id)
        if original_id is None:
            await save_result(segment_id, request_id, parsed=parsed)
            return
        part_results.setdefault(original_id, {})[segment_id] = (parsed, request_id)
        expected = original_parts[original_id]
        if not all(part_id in part_results[original_id] for part_id in expected):
            return
        values = [part_results[original_id][part_id][0] for part_id in expected]
        suggested = any(item["review_status"] == "suggested" for item in values)
        combined_text = "".join(
            (
                str(item["suggested_text"])
                if item["review_status"] == "suggested"
                else str(bases[part_id]["text"])
            )
            for part_id, item in zip(expected, values, strict=True)
        )
        reasons = [
            str(item["reason"]) for item in values if item.get("reason")
        ]
        await save_result(
            original_id,
            part_results[original_id][expected[-1]][1],
            parsed={
                "review_status": "suggested" if suggested else "accepted",
                "suggested_text": combined_text if suggested else None,
                "reason": "；".join(reasons) or None,
            },
        )

    async def save_external_error(
        expected: list[str], request_id: str, message: str
    ) -> None:
        for segment_id in expected:
            await save_result(segment_id, request_id, error=message)

    async def process_once(
        chunk: ChunkPlan,
        initial_parent_request_id: str | None = None,
    ) -> None:
        exhausted = await _localized_request_loop(
            list(chunk.segments),
            payload_builder=payload_builder,
            prompt=prompt,
            config=config,
            llm=state.llm,
            stage=stage,
            accept=accept_result,
            save_error=save_external_error,
            parse=_map_local_review_response,
            format_correction=(
                "上一次响应不符合 JSONL 协议或缺少 Segment。只返回未决 "
                'ID。accepted 每行使用 {"type":"segment","id":"...",'
                '"status":"accepted"}；suggested 每行使用 '
                '{"type":"segment","id":"...","status":"suggested",'
                '"suggested_text":"完整建议","reason":"原因"}，其中 reason '
                '也可为 null。最后输出 {"type":"end"}。'
            ),
            by_id=by_id,
            segments=segments,
            logger=logger,
            initial_parent_request_id=initial_parent_request_id,
        )
        for segment_id in dict.fromkeys(exhausted):
            await save_result(
                segment_id,
                "REQ-NONE",
                error="格式修正次数耗尽",
            )

    async def record_preflight_failure(
        failed: list[dict[str, Any]],
    ) -> None:
        for segment in failed:
            await save_result(
                str(segment["segment_id"]),
                "REQ-NONE",
                error="单 Segment 超过模型限制且内部拆分已关闭",
            )

    async def record_context_failure(
        items: list[dict[str, Any]],
    ) -> None:
        await save_result(
            str(items[0]["segment_id"]),
            "REQ-NONE",
            error="模型报告上下文过长",
        )

    async def before_finalize() -> None:
        return None

    def completed_count() -> int:
        return (
            len(selection.reusable) + len(completed_ids)
            if resume_run_id
            else len(completed_ids)
        )

    usage = await _execute_stage_run(
        state,
        request_segments=request_segments,
        part_original=part_original,
        original_parts=original_parts,
        preflight_failed=preflight_failed,
        limiter=limiter,
        payload_builder=payload_builder,
        process_once=process_once,
        record_preflight_failure=record_preflight_failure,
        record_context_failure=record_context_failure,
        before_finalize=before_finalize,
        completed_count=completed_count,
        failed_count=lambda: len(failed_ids),
        exception_completed=completed_count,
        exception_failed=lambda: len(selection.work) - len(completed_ids),
        failure_counts=failure_counts,
        http_client=http_client,
        runtime_parts_kwargs={"by_id": by_id, "bases": bases},
    )
    logger.info(
        "run complete run=%s completed=%d failed=%d",
        run_id,
        len(completed_ids),
        len(failed_ids),
    )
    return {
        "stage": stage,
        "run_id": run_id,
        "selected": len(selection.selected),
        "requested": len(selection.work),
        "reused": len(selection.reusable),
        "completed": len(completed_ids),
        "failed": len(failed_ids),
        "failure_counts": dict(failure_counts),
        "warnings": warnings,
        "usage": usage,
    }


def run_apply(
    project: Path,
    review_stage: str,
    scope: Scope,
    *,
    allow_outdated_base: bool,
    confirmed_all: bool,
) -> dict[str, Any]:
    if review_stage not in {"proofreading", "polishing"}:
        raise ValueError(f"unsupported apply stage: {review_stage}")
    if not confirmed_all:
        raise UsageError("apply 必须显式传入 --all")
    logger = get_logger("apply")
    config, metadata, files, segments = _project_context(project)
    _require_nonempty_segments(segments)
    selected = select_scope(segments, files, scope)
    suggestions = classify_stage(
        selected,
        load_stage_history(project, review_stage),
        force=False,
    ).latest_completed
    bases = _base_results(project, review_stage)
    missing = [
        str(item["segment_id"])
        for item in selected
        if str(item["segment_id"]) not in suggestions
        or str(item["segment_id"]) not in bases
    ]
    if missing:
        raise IncompleteError(
            f"apply 缺少建议或基准，整个范围未应用：{', '.join(missing[:10])}"
        )
    outdated = [
        str(item["segment_id"])
        for item in selected
        if suggestions[str(item["segment_id"])].get("base_result_id")
        != bases[str(item["segment_id"])]["record_id"]
    ]
    if outdated and not allow_outdated_base:
        raise IncompleteError(
            "建议基于旧上游结果；使用 --allow-outdated-base 才可强制"
        )
    applied_stage = f"{review_stage}_applied"
    fingerprint = stage_fingerprint(
        config,
        applied_stage,
        None,
        apply_semantics={
            "review_stage": review_stage,
            "allow_outdated_base": allow_outdated_base,
        },
    )
    if scope.dry_run:
        return {
            "stage": applied_stage,
            "dry_run": True,
            "selected": len(selected),
            "outdated_base": len(outdated),
            "warnings": ["存在旧基准建议"] if outdated else [],
        }
    run_id, run_dir = create_run(
        project,
        config=config,
        stage=applied_stage,
        fingerprint=fingerprint,
        prompt=None,
        selected_count=len(selected),
        requested_count=len(selected),
        reused_count=0,
        details={
            "review_stage": review_stage,
            "scope": _scope_record(scope),
        },
    )
    logger.info(
        "run start run=%s review_stage=%s selected=%d",
        run_id,
        review_stage,
        len(selected),
    )
    result_path = stage_result_path(project, applied_stage)
    for segment in selected:
        segment_id = str(segment["segment_id"])
        suggestion = suggestions[segment_id]
        base = bases[segment_id]
        text = (
            suggestion["suggested_text"]
            if suggestion["review_status"] == "suggested"
            else base["text"]
        )
        text = _restore_leading_whitespace(
            str(segment["source"]),
            str(text),
        )
        append_jsonl(
            project,
            result_path,
            record_header(
                "stage_result",
                str(metadata["project_id"]),
                stage=applied_stage,
                segment_id=segment_id,
                status="completed",
                text=text,
                suggestion_result_id=suggestion["record_id"],
                base_result_id=base["record_id"],
                allowed_outdated_base=allow_outdated_base,
                stage_fingerprint=fingerprint,
                run_id=run_id,
                request_id=None,
            ),
        )
    warnings = ["已强制应用旧基准建议"] if outdated else []
    finalize_run(
        project,
        run_dir,
        status="completed",
        completed=len(selected),
        failed=0,
        warnings=warnings,
    )
    logger.info(
        "run complete run=%s completed=%d outdated_base=%d",
        run_id,
        len(selected),
        len(outdated),
    )
    return {
        "stage": applied_stage,
        "run_id": run_id,
        "completed": len(selected),
        "failed": 0,
        "warnings": warnings,
    }


def export_project(
    project: Path,
    export_stage: str,
    *,
    bilingual: bool,
    allow_missing: bool,
    output_format: str = "original",
    file_ids: list[str] | None = None,
) -> dict[str, Any]:
    if export_stage not in {"translated", "proofread", "polished"}:
        raise UsageError(f"不支持的导出阶段：{export_stage}")
    if output_format not in {"original", "txt"}:
        raise UsageError(f"不支持的导出格式：{output_format}")
    logger = get_logger("export")
    config, _, files, segments = _project_context(project)
    if file_ids is not None:
        if not file_ids:
            raise UsageError("导出文件范围不能为空")
        if len(file_ids) != len(set(file_ids)):
            raise UsageError("导出文件 ID 不能重复")
        known_file_ids = {str(item["file_id"]) for item in files}
        unknown = [
            file_id for file_id in file_ids if file_id not in known_file_ids
        ]
        if unknown:
            raise UsageError(f"未知文件 ID：{', '.join(unknown)}")
        selected_file_ids = set(file_ids)
        files = [
            item for item in files if str(item["file_id"]) in selected_file_ids
        ]
        segments = [
            item
            for item in segments
            if str(item["file_id"]) in selected_file_ids
        ]
    _require_nonempty_segments(segments)
    stage_name = {
        "translated": "translation",
        "proofread": "proofreading_applied",
        "polished": "polishing_applied",
    }[export_stage]
    histories = {
        stage: load_stage_history(project, stage)
        for stage in (
            "translation",
            "proofreading",
            "proofreading_applied",
            "polishing",
            "polishing_applied",
        )
    }
    primary = classify_stage(
        [],
        histories[stage_name],
        force=False,
    ).latest_completed
    translation = classify_stage(
        [], histories["translation"], force=False
    ).latest_completed
    proofread = classify_stage(
        [], histories["proofreading_applied"], force=False
    ).latest_completed
    records_by_id = {
        str(record["record_id"]): record
        for history in histories.values()
        for record in history
        if record.get("record_id")
    }

    def result_lineage(record: dict[str, Any]) -> list[dict[str, Any]]:
        lineage: list[dict[str, Any]] = []
        pending = [record]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            record_id = str(current.get("record_id", ""))
            if record_id in seen:
                continue
            seen.add(record_id)
            lineage.append(current)
            for key in ("base_result_id", "suggestion_result_id"):
                parent = records_by_id.get(str(current.get(key, "")))
                if parent is not None:
                    pending.append(parent)
        return lineage

    fallback_records: list[str] = []
    missing: list[str] = []
    output_text: dict[str, str] = {}
    validation_warnings = 0
    used_fingerprints: set[str] = set()
    for segment in segments:
        if segment["is_empty"]:
            continue
        segment_id = str(segment["segment_id"])
        record = primary.get(segment_id)
        if record is None and allow_missing:
            if export_stage == "polished":
                record = proofread.get(segment_id) or translation.get(segment_id)
            elif export_stage == "proofread":
                record = translation.get(segment_id)
            if record is None:
                output_text[segment_id] = str(segment["source"])
            else:
                output_text[segment_id] = str(record["text"])
            fallback_records.append(segment_id)
        elif record is None:
            missing.append(segment_id)
        else:
            output_text[segment_id] = str(record["text"])
        if segment_id in output_text:
            output_text[segment_id] = _restore_leading_whitespace(
                str(segment["source"]),
                output_text[segment_id],
            )
        if record is not None:
            lineage = result_lineage(record)
            if any(
                item.get("validation_status") == "warning" for item in lineage
            ):
                validation_warnings += 1
            used_fingerprints.update(
                str(item["stage_fingerprint"])
                for item in lineage
                if item.get("stage_fingerprint")
            )
    if missing:
        raise IncompleteError(
            f"导出缺少 {export_stage} 结果：{', '.join(missing[:10])}"
        )

    directory = (
        project / "output" / "bilingual" / export_stage
        if bilingual
        else project / "output" / export_stage
    )
    required_capability = (
        "bilingual_export" if bilingual else "translated_export"
    )
    segments_by_file: dict[str, list[dict[str, Any]]] = {}
    for segment in segments:
        segments_by_file.setdefault(str(segment["file_id"]), []).append(segment)
    jobs: list[DocumentExportJob] = []
    for file_record in files:
        source_adapter_id = str(file_record["document_adapter_id"])
        adapter_id = "txt" if output_format == "txt" else source_adapter_id
        adapter = get_document_adapter(adapter_id)
        if required_capability not in adapter.capabilities:
            raise IncompleteError(
                f"Document Adapter 不支持此导出模式：{adapter_id} "
                f"{required_capability}"
            )
        opaque_state = None
        export_file = dict(file_record)
        if output_format == "txt":
            export_file["original_name"] = str(
                Path(str(file_record["original_name"])).with_suffix(".txt")
            )
        else:
            project_version = str(file_record["document_adapter_version"])
            if project_version != adapter.version:
                raise IncompleteError(
                    f"Document Adapter 版本不兼容：文件 "
                    f"{file_record['file_id']} 使用 {project_version}，"
                    f"当前 {adapter.version}"
                )
            state_path = file_record.get("document_adapter_state")
            if state_path is not None:
                state_record = read_json(project, project / str(state_path))
                if (
                    state_record.get("adapter_id") != adapter_id
                    or str(state_record.get("adapter_version"))
                    != project_version
                    or state_record.get("file_id")
                    not in {None, file_record["file_id"]}
                    or not isinstance(state_record.get("state"), dict)
                ):
                    raise IncompleteError(
                        f"Document Adapter 状态损坏或版本不匹配："
                        f"{file_record['file_id']}"
                    )
                opaque_state = state_record["state"]
        jobs.append(
            DocumentExportJob(
                adapter=adapter,
                file=export_file,
                segments=segments_by_file[str(file_record["file_id"])],
                opaque_state=opaque_state,
            )
        )
    encoding = str(config["project"]["output_encoding"])
    written = publish_document_exports(
        jobs,
        project=project,
        directory=directory,
        output_text=output_text,
        bilingual=bilingual,
        output_encoding=encoding,
    )
    for path in written:
        logger.info("file written path=%s", path)
    logger.info(
        "export complete stage=%s files=%d fallback_segments=%d",
        export_stage,
        len(written),
        len(fallback_records),
    )
    return {
        "stage": export_stage,
        "bilingual": bilingual,
        "format": output_format,
        "selected_file_ids": [str(item["file_id"]) for item in files],
        "files": len(written),
        "written": written,
        "fallback_segments": fallback_records,
        "validation_warnings": validation_warnings,
        "mixed_fingerprints": len(used_fingerprints) > 1,
        "output_encoding": encoding,
    }


async def run_all(
    project: Path,
    scope: Scope,
    *,
    http_client: httpx.AsyncClient | None = None,
    reuse_mixed_fingerprints: bool = False,
    prompt_language: str | None = None,
) -> dict[str, Any]:
    _require_nonempty_segments(load_segments(project))
    stages = ("terminology", "translation", "proofreading", "polishing")
    configs = {
        stage: load_project_config(project, stage=stage) for stage in stages
    }
    resource_keys = {
        stage: (
            str(configs[stage]["_llm_preset_id"]),
            str(configs[stage]["_llm_preset_hash"]),
        )
        for stage in stages
    }
    limiters: dict[tuple[str, str], SlidingWindowLimiter] = {}
    for stage, key in resource_keys.items():
        if key not in limiters:
            limiters[key] = SlidingWindowLimiter(
                configs[stage]["execution"]["requests_per_minute"],
                configs[stage]["execution"]["input_tokens_per_minute"],
            )

    async def execute(
        clients: dict[tuple[str, str], httpx.AsyncClient] | None,
    ) -> dict[str, Any]:
        def client_for(stage: str) -> httpx.AsyncClient | None:
            if http_client is not None:
                return http_client
            return clients[resource_keys[stage]] if clients is not None else None

        summaries: list[dict[str, Any]] = []
        terms = load_terms(project)
        active_path = project / "terminology" / "active_task.json"
        active = read_json(project, active_path) if record_exists(project, active_path) else None
        if scope.force or terms is None or (
            active and active.get("status") == "active"
        ):
            term_summary = await run_terminology(
                project,
                scope,
                http_client=client_for("terminology"),
                limiter=limiters[resource_keys["terminology"]],
                reuse_mixed_fingerprints=reuse_mixed_fingerprints,
                prompt_language=prompt_language,
            )
            summaries.append(term_summary)
            require_success(term_summary)
        translation = await run_translation(
            project,
            scope,
            http_client=client_for("translation"),
            limiter=limiters[resource_keys["translation"]],
            reuse_mixed_fingerprints=reuse_mixed_fingerprints,
            prompt_language=prompt_language,
        )
        summaries.append(translation)
        require_success(translation)
        proofreading = await run_review(
            project,
            "proofreading",
            scope,
            http_client=client_for("proofreading"),
            limiter=limiters[resource_keys["proofreading"]],
            reuse_mixed_fingerprints=reuse_mixed_fingerprints,
            prompt_language=prompt_language,
        )
        summaries.append(proofreading)
        require_success(proofreading)
        polishing = await run_review(
            project,
            "polishing",
            scope,
            http_client=client_for("polishing"),
            limiter=limiters[resource_keys["polishing"]],
            reuse_mixed_fingerprints=reuse_mixed_fingerprints,
            prompt_language=prompt_language,
        )
        summaries.append(polishing)
        require_success(polishing)
        return {
            "stage": "run-all",
            "steps": summaries,
            "failed": 0,
            "pending": 0,
        }

    if http_client is not None or scope.dry_run:
        return await execute(None)
    async with AsyncExitStack() as stack:
        clients: dict[tuple[str, str], httpx.AsyncClient] = {}
        for stage, key in resource_keys.items():
            if key in clients:
                continue
            config = configs[stage]
            maximum = int(config["execution"]["max_parallel"])
            clients[key] = await stack.enter_async_context(
                httpx.AsyncClient(
                    timeout=float(config["execution"]["request_timeout_seconds"]),
                    limits=httpx.Limits(
                        max_connections=maximum,
                        max_keepalive_connections=maximum,
                    ),
                    proxy=config["llm"]["proxy_url"] or None,
                )
            )
        return await execute(clients)


def inspect_full(project: Path, *, dry_run: bool = False) -> dict[str, Any]:
    config, metadata, files, segments = _project_context(project)
    nonempty = [item for item in segments if not item["is_empty"]]
    active_segment_ids = {
        str(item["segment_id"]) for item in nonempty
    }
    summary: dict[str, Any] = {
        "name": metadata["name"],
        "files": len(files),
        "segments": len(segments),
        "empty_segments": len(segments) - len(nonempty),
        "terms_revision": None,
        "terminology": {
            "completed": 0,
            "failed": 0,
            "pending": len(nonempty),
            "fingerprint_count": 0,
            "current_fingerprint_completed": 0,
        },
        "stages": {},
        "outdated_suggestions": {},
        "validation_warnings": 0,
        "running_runs": [
            {
                "run_id": item["run_id"],
                "stage": item["stage"],
                "started_at": item.get("started_at"),
                "scope": item.get("scope"),
            }
            for stage in ("terminology", "translation", "proofreading", "polishing")
            for item in find_running_runs(project, stage)
        ],
    }
    library = load_terms(project)
    terms_revision = int(library["terms_revision"]) if library else None
    current_term_fingerprint = stage_fingerprint(
        load_project_config(project, stage="terminology"),
        "terminology",
        prompt_middle_digests(project, "terminology"),
    )
    summary["terminology"]["current_fingerprint"] = current_term_fingerprint
    if library:
        summary["terms_revision"] = library["terms_revision"]
    active_path = project / "terminology" / "active_task.json"
    if record_exists(project, active_path):
        active = read_json(project, active_path)
        if active.get("status") in {"active", "completed"}:
            scans = [
                item
                for item in read_jsonl(
                    project,
                    project / "terminology" / "scans.jsonl",
                    task_id=active.get("active_task_id"),
                )
                if str(item.get("segment_id")) in active_segment_ids
            ]
            completed = {
                item["segment_id"] for item in scans if item["status"] == "completed"
            }
            failed = {
                item["segment_id"]
                for item in scans
                if item["status"] == "failed" and item["segment_id"] not in completed
            }
            summary["terminology"] = {
                "active_task_id": active["active_task_id"],
                "completed": len(completed),
                "failed": len(failed),
                "pending": len(nonempty) - len(completed) - len(failed),
                "fingerprint_count": len(
                    {
                        str(item["stage_fingerprint"])
                        for item in scans
                        if item.get("stage_fingerprint")
                    }
                ),
                "current_fingerprint": current_term_fingerprint,
                "current_fingerprint_completed": sum(
                    item.get("status") == "completed"
                    and item.get("stage_fingerprint") == current_term_fingerprint
                    for item in scans
                ),
            }
    histories: dict[str, list[dict[str, Any]]] = {}
    for stage in (
        "translation",
        "proofreading",
        "proofreading_applied",
        "polishing",
        "polishing_applied",
    ):
        history = load_stage_history(project, stage)
        active_history = [
            item
            for item in history
            if str(item.get("segment_id")) in active_segment_ids
        ]
        histories[stage] = active_history
        completed = classify_stage(
            nonempty, active_history, force=False
        ).latest_completed
        failed = {
            str(item["segment_id"])
            for item in active_history
            if item.get("status") == "failed"
            and str(item.get("segment_id")) not in completed
        }
        latest_failed_after_success = 0
        by_segment: dict[str, list[dict[str, Any]]] = {}
        for item in active_history:
            by_segment.setdefault(str(item.get("segment_id")), []).append(item)
        for segment_id in completed:
            if by_segment[segment_id][-1].get("status") == "failed":
                latest_failed_after_success += 1
        fingerprints = {
            str(item["stage_fingerprint"])
            for item in completed.values()
            if item.get("stage_fingerprint")
        }
        if stage in {"translation", "proofreading", "polishing"}:
            current_fingerprint = stage_fingerprint(
                load_project_config(project, stage=stage),
                stage,
                prompt_middle_digests(project, stage),
                terms_revision=terms_revision,
            )
        else:
            current_fingerprint = stage_fingerprint(
                config,
                stage,
                None,
                apply_semantics={
                    "review_stage": stage.removesuffix("_applied"),
                    "allow_outdated_base": False,
                },
            )
        summary["stages"][stage] = {
            "completed": len(completed),
            "failed": len(failed),
            "pending": len(nonempty) - len(completed) - len(failed),
            "fingerprint_count": len(fingerprints),
            "current_fingerprint": current_fingerprint,
            "current_fingerprint_completed": sum(
                record.get("stage_fingerprint") == current_fingerprint
                for record in completed.values()
            ),
            "last_attempt_failed": latest_failed_after_success,
        }
    latest_translations = classify_stage(
        [], histories["translation"], force=False
    ).latest_completed
    summary["validation_warnings"] = sum(
        item.get("validation_status") == "warning"
        for item in latest_translations.values()
    )
    for review_stage in ("proofreading", "polishing"):
        suggestions = classify_stage(
            [], histories[review_stage], force=False
        ).latest_completed
        bases = _base_results(project, review_stage)
        summary["outdated_suggestions"][review_stage] = sum(
            suggestion.get("base_result_id")
            != bases.get(segment_id, {}).get("record_id")
            for segment_id, suggestion in suggestions.items()
        )
    project_arg = shlex.quote(str(project))
    if not nonempty:
        summary["next_command"] = (
            f"python -m app.main files-add {project_arg} INPUT"
        )
    elif summary["terminology"]["pending"] or summary["terminology"]["failed"]:
        summary["next_command"] = (
            f"python -m app.main terminology {project_arg}"
        )
    elif summary["terms_revision"] is None:
        summary["next_command"] = (
            f"python -m app.main terminology {project_arg}"
        )
    elif summary["stages"]["translation"]["pending"] or summary["stages"][
        "translation"
    ]["failed"]:
        summary["next_command"] = f"python -m app.main translate {project_arg}"
    elif summary["stages"]["proofreading"]["pending"] or summary["stages"][
        "proofreading"
    ]["failed"]:
        summary["next_command"] = f"python -m app.main proofread {project_arg}"
    elif summary["stages"]["polishing"]["pending"] or summary["stages"][
        "polishing"
    ]["failed"]:
        summary["next_command"] = f"python -m app.main polish {project_arg}"
    else:
        summary["next_command"] = (
            f"python -m app.main export {project_arg} --stage translated"
        )
    return summary
