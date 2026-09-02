from __future__ import annotations
import asyncio
import csv
import hashlib
import io
import json
import shlex
import sys
import time
import unicodedata
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, NamedTuple
import httpx
from .config import load_project_config
from .documents import (
    DocumentExportJob,
    aozora_match_views,
    aozora_safe_split_positions,
    compact_emphasis_aozora,
    document_adapter_reads_version,
    publish_document_exports,
)
from .errors import (
    ConfigError,
    ContextLengthError,
    ExportError,
    ExternalError,
    FatalExternalError,
    IncompleteError,
    RequestSizeError,
    StorageError,
    UsageError,
)
from .term_library import (_merge_and_publish_terms, build_term_library_rows, load_terms, normalize_term, publish_partial_terms, term_normalization)
from .term_exchange import export_terms, import_terms
from .term_matching import _TermMatchCache, match_term_validation, match_terms
from .execution import (
    ChunkPlan,
    LLMClient,
    PreviousContextIndex,
    Scope,
    SlidingWindowLimiter,
    build_chunk_plans,
    classify_stage,
    classify_stage_states,
    combine_usage,
    contiguous_groups,
    continue_run,
    create_run,
    dispatch_chunks,
    estimate_messages,
    estimate_single_segment_preflight,
    finalize_run,
    find_running_runs,
    full_prompt,
    iter_chunk_plans,
    load_stage_history,
    localize_request_ids,
    materialize_chunk_stream,
    parse_jsonl_document,
    render_messages,
    save_debug_chunks,
    scope_from_run,
    segment_model_source,
    segment_model_text,
    select_scope,
    stage_fingerprint,
    stage_result_path,
    unavailable_usage,
)
from .i18n import SUPPORTED_LANGUAGES, resolve_language
from .llm_keys import KeyPool
from .logging_utils import get_logger
from .plugins import (
    get_document_adapter,
    normalize_model_text,
)
from .project import (
    PROMPT_LANGUAGES,
    load_segments,
    load_source_files,
    prompt_file,
)
from .sqlite_storage import (
    append_jsonl,
    atomic_write_text,
    latest_stage_states,
    read_json,
    read_jsonl,
    record_exists,
    record_header,
    terminology_scan_state,
    write_json,
)
from .translation_validation import (
    TranslationTermMatch,
    TranslationValidationContext,
    validate_translation_text,
)

from .stage_runtime import (StageRunState, _Preflight, _SegmentParseResult, _assemble_warnings, _base_results, _configured_output_warning, _confirm_fingerprint_reuse, _create_or_continue_run, _document_prompt_requirement_helpers, _execute_stage_run, _extend_unique, _finalize_planning_failure, _localized_request_loop, _project_context, _prompt, _prompt_factory, _prompt_language, _replace_with_runtime_parts, _request_estimate, _require_nonempty_segments, _restore_leading_whitespace, _resume_scope, _scope_record, _segment_model_payload_value, _split_oversized_preflight, _split_segment_source, _split_source_once, prompt_middle_digests, _FORMAT_CORRECTION)

def _has_hard_validation_findings(findings: list[dict[str, Any]]) -> bool:
    return any(
        str(item.get("severity", "error")) == "error" for item in findings
    )

def _parse_translation_items(
    content: str, expected_ids: list[str]
) -> _SegmentParseResult:
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
    return _SegmentParseResult(
        valid,
        unresolved,
        errors,
        document.complete and not errors,
        document.has_valid_end,
        counts == Counter(expected_ids),
    )

def _map_local_translation_response(
    content: str,
    id_map: dict[str, str],
) -> _SegmentParseResult:
    result = _parse_translation_items(content, list(id_map))
    return _SegmentParseResult(
        {id_map[local_id]: text for local_id, text in result.valid.items()},
        [id_map[local_id] for local_id in result.unresolved],
        result.errors,
        result.complete,
        result.has_valid_end,
        result.ids_complete,
    )

async def run_translation(
    project: Path,
    scope: Scope,
    *,
    http_client: httpx.AsyncClient | None = None,
    limiter: SlidingWindowLimiter | KeyPool | None = None,
    resume_run_id: str | None = None,
    reuse_mixed_fingerprints: bool = False,
    prompt_language: str | None = None,
    on_progress: Callable[[int, int, int], None] | None = None,
    on_usage: Callable[[dict[str, Any] | None], None] | None = None,
) -> dict[str, Any]:
    logger = get_logger("translation")
    preparation_started_at = time.perf_counter()
    scope, resume_arguments_ignored = _resume_scope(project, scope, resume_run_id)
    config, metadata, files, segments = _project_context(
        project, stage="translation"
    )
    logger.info(
        "stage preparation context ready elapsed=%.3fs files=%d segments=%d",
        time.perf_counter() - preparation_started_at,
        len(files),
        len(segments),
    )
    _require_nonempty_segments(segments)
    translation_validators = config["_translation_validator_instances"]
    language = _prompt_language(project, "translation", prompt_language)
    prompt_factory = _prompt_factory(project, "translation", language)
    prompt = prompt_factory(())
    requirements_for_items, prompt_partition_key = (
        _document_prompt_requirement_helpers(config, language)
    )
    prompt_for_items = lambda items: prompt_factory(requirements_for_items(items))
    library = load_terms(project)
    terms_revision = int(library["terms_revision"]) if library else None
    fingerprint = stage_fingerprint(
        config,
        "translation",
        prompt_middle_digests(project, "translation"),
        terms_revision=terms_revision,
    )
    selected_segments = select_scope(segments, files, scope)
    active_segment_ids = [
        str(segment["segment_id"])
        for segment in segments
        if not segment["is_empty"]
    ]
    history_states = latest_stage_states(
        project,
        "translation",
        active_segment_ids,
    )
    selection = classify_stage_states(
        selected_segments,
        history_states,
        force=scope.force,
    )
    logger.info(
        "stage preparation selection/history ready elapsed=%.3fs selected=%d requested=%d reusable=%d",
        time.perf_counter() - preparation_started_at,
        len(selection.selected),
        len(selection.work),
        len(selection.reusable),
    )
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
    context_index = PreviousContextIndex(segments)
    term_match_cache = _TermMatchCache(
        library,
        term_normalization(config),
        int(config["terminology"]["max_terms_per_segment"]),
    )

    def payload_builder(items: list[dict[str, Any]]) -> dict[str, Any]:
        resolver = None
        if config["execution"]["scheduling_mode"] == "ordered_by_file":
            resolver = latest_text.get
        context = (
            context_index.previous(
                items[0],
                context_config["previous_segments"],
                target_resolver=resolver,
                target_transform=segment_model_text,
                source_key="model_source",
            )
            if context_config["enabled"]
            else []
        )
        if config["execution"]["scheduling_mode"] == "parallel":
            context = [item["source"] for item in context]
        return {
            "target_language": config["project"]["target_language"],
            "reference_context": context,
            "terms": _segment_model_payload_value(
                items[0], term_match_cache.for_items(items)
            ),
            "segments": [
                {
                    "id": item["segment_id"],
                    "source": segment_model_source(item),
                }
                for item in items
            ],
        }

    run_id, run_dir, continuation_index, fail_planning = _create_or_continue_run(
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
        config=config,
        prompt=prompt,
        payload_builder=payload_builder,
        prompt_builder=prompt_for_items,
        fail_planning=fail_planning,
        make_probe=lambda segment, part: {
            **_split_segment_source(
                segment, f"{segment['segment_id']}-PROBE", part
            ),
        },
        split_part=lambda part: list(_split_source_once(part)),
        accept_part=lambda segment, part_id, part: _split_segment_source(
            segment, part_id, part
        ),
    )
    request_segments = preflight.request_segments
    part_original = preflight.part_original
    original_parts = preflight.original_parts
    preflight_failed = preflight.preflight_failed
    logger.info(
        "stage preparation preflight complete elapsed=%.3fs requested=%d failed=%d fast=%d exact=%d",
        time.perf_counter() - preparation_started_at,
        len(request_segments),
        len(preflight_failed),
        preflight.fast_checked,
        preflight.exact_checked,
    )

    if scope.dry_run:
        plans = build_chunk_plans(
            request_segments,
            all_segments=segments,
            config=config,
            stage="translation",
            prompt=prompt,
            payload_builder=payload_builder,
            prompt_builder=prompt_for_items,
            partition_key=prompt_partition_key,
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
    advisory_repair_attempted: set[str] = set()
    failed_ids: set[str] = set()
    failure_counts: Counter[str] = Counter()
    completed_ids: set[str] = set()
    by_id = {str(item["segment_id"]): item for item in segments}
    by_id.update(
        {str(item["segment_id"]): item for item in request_segments}
    )
    part_results: dict[str, dict[str, tuple[str, str]]] = {}

    def validation_context(
        segment_id: str, translation: str
    ) -> TranslationValidationContext:
        original_id = part_original.get(segment_id)
        item = by_id[original_id or segment_id]
        return TranslationValidationContext(
            source=str(item["source"]),
            translation=translation,
            terms=term_match_cache.validation_matches_for_item(item),
        )

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
        text = normalize_model_text(
            files, by_id[segment_id], str(text), "translation"
        )
        original_id = part_original.get(segment_id)
        if original_id is None:
            findings = validate_translation_text(
                validation_context(segment_id, text), translation_validators
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
            validation_context(original_id, combined), translation_validators
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
        continuation_index=continuation_index,
        on_usage=on_usage,
        preparation_started_at=preparation_started_at,
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
            prompt=prompt_for_items(group),
            config=config,
            llm=state.llm,
            stage="translation",
            accept=accept_translation,
            save_error=save_external_error,
            parse=_map_local_translation_response,
            format_correction=_FORMAT_CORRECTION[language],
            prompt_language=language,
            by_id=by_id,
            segments=segments,
            prompt_partition_key=prompt_partition_key,
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
                prompt=prompt_for_items(group),
                config=config,
                llm=state.llm,
                stage="translation",
                accept=accept_translation,
                save_error=save_external_error,
                parse=_map_local_translation_response,
                format_correction=_FORMAT_CORRECTION[language],
                prompt_language=language,
                by_id=by_id,
                segments=segments,
                prompt_partition_key=prompt_partition_key,
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
                            validation_context(part_id, candidate_part),
                            translation_validators,
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
        hard_repairs = 0
        while validation_pending:
            hard_pending = {
                segment_id: item
                for segment_id, item in validation_pending.items()
                if _has_hard_validation_findings(item["findings"])
            }
            if hard_pending:
                if hard_repairs >= max_repairs:
                    break
                hard_repairs += 1
                for segment_id in hard_pending:
                    validation_pending.pop(segment_id, None)
                groups = contiguous_groups(
                    (item["segment"] for item in hard_pending.values()),
                    all_segments=segments,
                    cross_boundary="translation"
                    in config["chunking"]["cross_boundary_batching"],
                    partition_key=prompt_partition_key,
                )
                logger.warning(
                    "validation repair attempt=%d segments=%d chunks=%d",
                    hard_repairs,
                    len(hard_pending),
                    len(groups),
                )
                for group in groups:
                    subset = {
                        str(item["segment_id"]): hard_pending[
                            str(item["segment_id"])
                        ]
                        for item in group
                    }
                    await repair_group(group, subset)
                continue

            advisory_pending = {
                segment_id: item
                for segment_id, item in validation_pending.items()
                if segment_id not in advisory_repair_attempted
            }
            if not advisory_pending:
                break
            for segment_id in advisory_pending:
                advisory_repair_attempted.add(segment_id)
                validation_pending.pop(segment_id, None)
            groups = contiguous_groups(
                (item["segment"] for item in advisory_pending.values()),
                all_segments=segments,
                cross_boundary="translation"
                in config["chunking"]["cross_boundary_batching"],
                partition_key=prompt_partition_key,
            )
            logger.warning(
                "advisory validation repair segments=%d chunks=%d",
                len(advisory_pending),
                len(groups),
            )
            for group in groups:
                subset = {
                    str(item["segment_id"]): advisory_pending[
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
                            validation_context(original_id, combined),
                            translation_validators,
                        ),
                    )
                    for part_id in expected:
                        validation_pending.pop(part_id, None)
        for segment_id, item in validation_pending.items():
            if (
                not _has_hard_validation_findings(item["findings"])
                or exhausted_mode == "warning"
            ):
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
        prompt_builder=prompt_for_items,
        prompt_partition_key=prompt_partition_key,
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
