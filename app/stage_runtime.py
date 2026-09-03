from __future__ import annotations
import asyncio
import hashlib
import sys
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, NamedTuple
import httpx
from .config import load_project_config
from .documents import (
    aozora_safe_split_positions,
)
from .errors import (
    ConfigError,
    ContextLengthError,
    ExternalError,
    FatalExternalError,
    IncompleteError,
    RequestSizeError,
    StorageError,
    UsageError,
)
from .execution import (
    ChunkPlan,
    Scope,
    classify_stage,
    contiguous_groups,
    continue_run,
    create_run,
    dispatch_chunks,
    estimate_messages,
    estimate_single_segment_preflight,
    finalize_run,
    full_prompt,
    iter_chunk_plans,
    load_stage_history,
    localize_request_ids,
    materialize_chunk_stream,
    render_messages,
    save_debug_chunks,
    scope_from_run,
    segment_model_source,
    segment_model_text,
)
from .llm_client import LLMClient, SlidingWindowLimiter
from .i18n import SUPPORTED_LANGUAGES, resolve_language
from .llm_keys import KeyPool
from .logging_utils import get_logger
from .plugins import (
    get_document_adapter,
)
from .project import (
    PROMPT_LANGUAGES,
    load_segments,
    load_source_files,
    prompt_file,
)
from .sqlite_storage import (
    latest_stage_states,
    read_json,
)

_FORMAT_CORRECTION = {
    "zh-CN": (
        "只处理当前待处理内容，并完整覆盖本次内容，遵守固定字段。"
        "严格 JSONL 结构：每个非空物理行一个紧凑 JSON 对象，仅用换行分隔记录，"
        '末行精确为 {"type":"end"}。'
    ),
    "en": (
        "Process only the current pending content, cover it completely, and follow "
        "the fixed fields. Use strict JSONL structure: one compact JSON object per "
        'non-empty physical line, separated only by newlines, ending exactly with {"type":"end"}.'
    ),
}

_VALIDATION_REPAIR = {
    "zh-CN": (
        "以 failed_candidate 为基准，仅修复 validation_matches 所列问题，"
        "返回完整且格式合规的译文。对于 advisory 术语建议，先判断推荐译名"
        "是否适合当前语境；适用时采用，不适用时可以保留候选。"
    ),
    "en": (
        "Use failed_candidate as the base, fix only the issues in "
        "validation_matches, and return a complete, format-compliant translation. "
        "For advisory terminology suggestions, first decide whether the "
        "recommended translation fits this context; use it when it does, but "
        "you may keep the candidate when it does not."
    ),
}



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
    adapter_prompt_requirements: dict[str, dict[str, str]] = {}
    for file_record in files:
        file_id = str(file_record["file_id"])
        adapters[file_id] = {
            "adapter_id": str(file_record["document_adapter_id"]),
            "version": str(file_record["document_adapter_version"]),
        }
        state_path = file_record.get("document_adapter_state")
        state_record = (
            read_json(project, project / state_path)
            if isinstance(state_path, str)
            else None
        )
        state = state_record.get("state") if isinstance(state_record, dict) else None
        if (
            stage is not None
            and state_record is not None
            and not isinstance(state, dict)
        ):
            raise ConfigError(
                f"Document Adapter 状态缺少有效 state：{file_record['file_id']}"
            )
        if isinstance(state, dict):
            adapter_options[file_id] = {
                key: state[key]
                for key in (
                    "ruby_mode",
                    "inline_format_mode",
                    "inline_format_policy",
                )
                if key in state
            }
        if stage is not None:
            adapter = get_document_adapter(
                str(file_record["document_adapter_id"])
            )
            requirements: dict[str, str] = {}
            for language in PROMPT_LANGUAGES:
                requirement = adapter.model_prompt_requirements(
                    stage=stage,
                    language=language,
                    opaque_state=state,
                )
                if requirement is not None and not isinstance(requirement, str):
                    raise ConfigError(
                        "Document Adapter 返回了无效的模型 Prompt 要求："
                        f"{file_record['document_adapter_id']}"
                    )
                if requirement:
                    requirements[language] = requirement
            adapter_prompt_requirements[file_id] = requirements
    config["_document_adapter_options"] = adapter_options
    config["_document_adapters"] = adapters
    if stage is not None:
        config["_document_adapter_prompt_requirements"] = (
            adapter_prompt_requirements
        )
    return config, metadata, files, segments

def _require_nonempty_segments(segments: list[dict[str, Any]]) -> None:
    if not any(not segment["is_empty"] for segment in segments):
        raise UsageError("项目没有可处理的非空 Segment；请先添加源文件")

def _segment_model_payload_value(segment: dict[str, Any], value: Any) -> Any:
    if isinstance(value, str):
        return segment_model_text(segment, value)
    if isinstance(value, list):
        return [_segment_model_payload_value(segment, item) for item in value]
    if isinstance(value, dict):
        return {
            key: _segment_model_payload_value(segment, item)
            for key, item in value.items()
        }
    return value

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
) -> tuple[
    str | None,
    Path | None,
    int,
    Callable[[BaseException], None],
]:
    run_id: str | None = None
    run_dir: Path | None = None
    continuation_index = 0
    if not scope.dry_run:
        if resume_run_id is not None:
            run_id, run_dir, continuation_index = continue_run(
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

    return run_id, run_dir, continuation_index, fail_planning

@dataclass
class _Preflight:
    request_segments: list[dict[str, Any]]
    part_original: dict[str, str]
    original_parts: dict[str, list[str]]
    preflight_failed: list[dict[str, Any]]
    fast_checked: int
    exact_checked: int

def _split_oversized_preflight(
    work: Iterable[dict[str, Any]],
    *,
    config: dict[str, Any],
    prompt: str,
    payload_builder: Callable[[list[dict[str, Any]]], dict[str, Any]],
    prompt_builder: Callable[[list[dict[str, Any]]], str] | None = None,
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
    fast_checked = 0
    exact_checked = 0
    prompt_builder = prompt_builder or (lambda _items: prompt)
    for segment in work:
        try:
            fast = estimate_single_segment_preflight(
                segment,
                config=config,
                prompt=prompt_builder([segment]),
                payload_builder=payload_builder,
            )
            if fast:
                fast_checked += 1
            else:
                exact_checked += 1
            request_segments.append(segment)
            continue
        except RequestSizeError as exc:
            exact_checked += 1
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
                fast = estimate_single_segment_preflight(
                    probe,
                    config=config,
                    prompt=prompt_builder([probe]),
                    payload_builder=payload_builder,
                )
                if fast:
                    fast_checked += 1
                else:
                    exact_checked += 1
                accepted_parts.append(part)
            except RequestSizeError as exc:
                exact_checked += 1
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
        fast_checked=fast_checked,
        exact_checked=exact_checked,
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
    continuation_index: int = 0
    on_usage: Callable[[dict[str, Any] | None], None] | None = None
    preparation_started_at: float | None = None
    llm: LLMClient | None = None

async def _execute_stage_run(
    state: StageRunState,
    *,
    request_segments: list[dict[str, Any]],
    part_original: dict[str, str],
    original_parts: dict[str, list[str]],
    preflight_failed: list[dict[str, Any]],
    limiter: SlidingWindowLimiter | KeyPool | None,
    payload_builder: Callable[[list[dict[str, Any]]], dict[str, Any]],
    prompt_builder: Callable[[list[dict[str, Any]]], str],
    prompt_partition_key: Callable[[dict[str, Any]], object],
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
        prompt_builder=prompt_builder,
        partition_key=prompt_partition_key,
    )
    chunks = materialize_chunk_stream(
        state.run_id,
        state.stage,
        planned,
        continuation_index=state.continuation_index,
    )
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
        execution = state.config["execution"]
        limiter = KeyPool(
            int(execution["requests_per_minute"]),
            int(execution["input_tokens_per_minute"]),
            int(execution["max_parallel"]),
            int(
                execution.get(
                    "max_parallel_per_key", execution["max_parallel"]
                )
            ),
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

    def key_audit() -> dict[str, Any] | None:
        if state.llm is None or state.llm._api_keys is None:
            return None
        return state.llm.key_audit_summary(
            execution_index=state.continuation_index + 1
        )

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
            preparation_started_at=state.preparation_started_at,
        ) as llm:
            state.llm = llm
            llm._prepare_keys()
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
        if state.llm is not None:
            _extend_unique(state.warnings, state.llm.warnings)
        usage = state.llm.usage_summary() if state.llm is not None else None
        finalize_run(
            state.project,
            state.run_dir,
            status="interrupted",
            completed=exception_completed(),
            failed=0,
            warnings=[*state.warnings, "任务已由用户取消"],
            usage=usage,
            failure_counts=dict(failure_counts),
            key_audit=key_audit(),
        )
        raise
    except (FatalExternalError, ConfigError) as exc:
        if state.llm is not None:
            _extend_unique(state.warnings, state.llm.warnings)
        if isinstance(exc, FatalExternalError) and state.llm is not None:
            usage = state.llm.usage_summary()
        finalize_run(
            state.project,
            state.run_dir,
            status="failed",
            completed=exception_completed(),
            failed=exception_failed(),
            warnings=state.warnings,
            usage=usage,
            failure_counts=dict(failure_counts),
            key_audit=key_audit(),
        )
        logger.error(
            "run failed run=%s error_type=%s",
            state.run_id,
            type(exc).__name__,
        )
        raise
    except StorageError as exc:
        if state.llm is not None:
            _extend_unique(state.warnings, state.llm.warnings)
            usage = state.llm.usage_summary()
            usage_invoked = state.llm.send_count > 0
        else:
            usage_invoked = False
        finalize_run(
            state.project,
            state.run_dir,
            status="failed",
            completed=exception_completed(),
            failed=exception_failed(),
            warnings=[*state.warnings, f"存储失败：{exc}"],
            usage=usage,
            failure_counts=dict(failure_counts),
            usage_invoked=usage_invoked,
            key_audit=key_audit(),
        )
        logger.error(
            "run failed run=%s error_type=%s error=%s",
            state.run_id,
            type(exc).__name__,
            exc,
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
        key_audit=key_audit(),
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
        _SegmentParseResult,
    ],
    format_correction: str,
    prompt_language: str,
    by_id: dict[str, dict[str, Any]],
    segments: list[dict[str, Any]],
    prompt_partition_key: Callable[[dict[str, Any]], object],
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
        ]
    ] = [(group, initial_parent_request_id, 0, group[:1])]
    while tasks:
        items, parent_request_id, format_attempt, anchor = tasks.pop(0)
        expected = [str(item["segment_id"]) for item in items]
        payload = payload_builder(items or anchor)
        if not items:
            payload["segments"] = []
        if repair_candidates is not None:
            payload["segments"] = [
                {
                    "id": item["segment_id"],
                    "source": segment_model_source(item),
                    "failed_candidate": segment_model_text(
                        item,
                        str(
                            repair_candidates[str(item["segment_id"])][
                                "candidate"
                            ]
                        ),
                    ),
                    "validation_matches": repair_candidates[
                        str(item["segment_id"])
                    ]["findings"],
                }
                for item in items
            ]
            payload["validation_repair"] = _VALIDATION_REPAIR[prompt_language]
        if format_attempt:
            payload["format_correction"] = format_correction
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
            parsed = parse(response.content, id_map)
            valid = parsed.valid
            unresolved = parsed.unresolved
            parse_errors = parsed.errors
            response_complete = parsed.complete
        except FatalExternalError:
            raise
        except ContextLengthError as exc:
            if exc.segment_ids is None:
                exc.segment_ids = tuple(expected)
            raise
        except ExternalError as exc:
            await save_error(expected, request_id, str(exc))
            continue
        complete_id_mismatch = parsed.has_valid_end and not parsed.ids_complete
        if complete_id_mismatch:
            valid = {}
            unresolved = expected.copy()
            parse_errors.append("合法 end 响应的 Segment ID 与请求不一致")
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
            tasks.append(([], request_id, format_attempt + 1, anchor))
            continue
        unresolved_groups = contiguous_groups(
            (by_id[segment_id] for segment_id in unresolved),
            all_segments=segments,
            cross_boundary=stage in config["chunking"]["cross_boundary_batching"],
            partition_key=prompt_partition_key,
        )
        tasks.extend(
            (
                unresolved_group,
                request_id,
                format_attempt + 1,
                unresolved_group[:1],
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
    factory = _prompt_factory(project, stage, language)
    return factory(())

def _prompt_factory(
    project: Path, stage: str, language: str | None = None
) -> Callable[[Iterable[str]], str]:
    language = _prompt_language(project, stage, language)
    name = prompt_file(stage, language)
    try:
        middle = (project / "prompts" / name).read_text(encoding="utf-8")
    except OSError as exc:
        raise StorageError(f"无法读取 Prompt：{name}: {exc}") from exc

    def build(requirements: Iterable[str]) -> str:
        return full_prompt(
            stage,
            middle,
            language,
            document_requirements=requirements,
        )

    return build

def _document_prompt_requirement_helpers(
    config: dict[str, Any],
    language: str,
) -> tuple[
    Callable[[list[dict[str, Any]]], tuple[str, ...]],
    Callable[[dict[str, Any]], object],
]:
    by_file = config.get("_document_adapter_prompt_requirements", {})
    if not isinstance(by_file, dict):
        raise ConfigError("Document Adapter Prompt 要求索引无效")

    def file_requirements(item: dict[str, Any]) -> dict[str, Any]:
        value = by_file.get(str(item["file_id"]), {})
        if not isinstance(value, dict):
            raise ConfigError("Document Adapter Prompt 要求记录无效")
        return value

    def requirements_for(items: list[dict[str, Any]]) -> tuple[str, ...]:
        values: list[str] = []
        for item in items:
            requirement = file_requirements(item).get(language)
            if isinstance(requirement, str) and requirement not in values:
                values.append(requirement)
        return tuple(values)

    def partition_key(item: dict[str, Any]) -> object:
        requirement = file_requirements(item).get(language)
        return requirement if isinstance(requirement, str) else None

    return requirements_for, partition_key

def _split_source_once(source: str) -> tuple[str, str]:
    if len(source) < 2:
        raise ConfigError("固定 Prompt 与单字符输入仍超过模型硬限制")
    midpoint = len(source) // 2
    punctuation = "。！？!?；;，,"
    safe_positions = set(aozora_safe_split_positions(source))
    candidates = [
        index + 1
        for index, character in enumerate(source)
        if character in punctuation and index + 1 in safe_positions
    ]
    if candidates:
        split_at = min(candidates, key=lambda index: abs(index - midpoint))
    elif safe_positions:
        split_at = min(safe_positions, key=lambda index: abs(index - midpoint))
    else:
        raise ConfigError("单个 Aozora Ruby 超过模型硬限制，无法安全拆分")
    return source[:split_at], source[split_at:]

def _split_segment_source(
    segment: dict[str, Any], segment_id: str, source: str
) -> dict[str, Any]:
    result = {**segment, "segment_id": segment_id, "source": source}
    if segment.get("_ruby_mode") in {"short_xml", "compact"}:
        result["model_source"] = segment_model_text(result, source)
    return result

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
        _split_segment_source(segment, part_ids[0], left_source),
        _split_segment_source(segment, part_ids[1], right_source),
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

class _SegmentParseResult(NamedTuple):
    valid: dict[str, Any]
    unresolved: list[str]
    errors: list[str]
    complete: bool
    has_valid_end: bool
    ids_complete: bool

def _base_results(
    project: Path,
    stage: str,
    *,
    segment_ids: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    if segment_ids is not None:
        values = [str(value) for value in segment_ids]
        translations = {
            key: state["completed"]
            for key, state in latest_stage_states(
                project, "translation", values
            ).items()
            if isinstance(state.get("completed"), dict)
        }
        if stage == "proofreading":
            return translations
        applied = {
            key: state["completed"]
            for key, state in latest_stage_states(
                project, "proofreading_applied", values
            ).items()
            if isinstance(state.get("completed"), dict)
        }
        return {**translations, **applied}
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
