from __future__ import annotations
import asyncio
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any
import httpx
from .errors import (
    IncompleteError,
    UsageError,
)
from .term_library import (load_terms, term_normalization)
from .term_matching import _TermMatchCache
from .execution import (
    ChunkPlan,
    PreviousContextIndex,
    Scope,
    build_chunk_plans,
    classify_stage,
    classify_stage_states,
    create_run,
    finalize_run,
    load_stage_history,
    segment_model_source,
    segment_model_text,
    select_scope,
    stage_fingerprint,
    stage_result_path,
)
from .llm_client import SlidingWindowLimiter
from .llm_response import parse_jsonl_document
from .llm_keys import KeyPool
from .logging_utils import get_logger
from .plugins import (
    normalize_model_text,
)
from .sqlite_storage import (
    append_jsonl,
    latest_stage_states,
    record_header,
)

from .stage_runtime import (StageRunState, _SegmentParseResult, _assemble_warnings, _base_results, _create_or_continue_run, _document_prompt_requirement_helpers, _execute_stage_run, _localized_request_loop, _project_context, _prompt_factory, _prompt_language, _require_nonempty_segments, _restore_leading_whitespace, _resume_scope, _scope_record, _segment_model_payload_value, _split_oversized_preflight, _split_segment_source, _split_source_once, prompt_middle_digests, _FORMAT_CORRECTION)

def _parse_review_items(
    content: str, expected_ids: list[str]
) -> _SegmentParseResult:
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
    return _SegmentParseResult(
        valid,
        unresolved,
        errors,
        document.complete and not errors,
        document.has_valid_end,
        counts == Counter(expected_ids),
    )

def _map_local_review_response(
    content: str,
    id_map: dict[str, str],
) -> _SegmentParseResult:
    result = _parse_review_items(content, list(id_map))
    return _SegmentParseResult(
        {id_map[local_id]: parsed for local_id, parsed in result.valid.items()},
        [id_map[local_id] for local_id in result.unresolved],
        result.errors,
        result.complete,
        result.has_valid_end,
        result.ids_complete,
    )

async def run_review(
    project: Path,
    stage: str,
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
    if stage not in {"proofreading", "polishing"}:
        raise ValueError(f"unsupported review stage: {stage}")
    logger = get_logger(stage)
    preparation_started_at = time.perf_counter()
    scope, resume_arguments_ignored = _resume_scope(project, scope, resume_run_id)
    config, metadata, files, segments = _project_context(
        project, stage=stage
    )
    logger.info(
        "stage preparation context ready elapsed=%.3fs files=%d segments=%d",
        time.perf_counter() - preparation_started_at,
        len(files),
        len(segments),
    )
    _require_nonempty_segments(segments)
    language = _prompt_language(project, stage, prompt_language)
    prompt_factory = _prompt_factory(project, stage, language)
    prompt = prompt_factory(())
    requirements_for_items, prompt_partition_key = (
        _document_prompt_requirement_helpers(config, language)
    )
    prompt_for_items = lambda items: prompt_factory(requirements_for_items(items))
    library = load_terms(project)
    terms_revision = int(library["terms_revision"]) if library else None
    fingerprint = stage_fingerprint(
        config,
        stage,
        prompt_middle_digests(project, stage),
        terms_revision=terms_revision,
    )
    selected_segments = select_scope(segments, files, scope)
    active_segment_ids = [
        str(segment["segment_id"])
        for segment in segments
        if not segment["is_empty"]
    ]
    bases = _base_results(
        project,
        stage,
        segment_ids=active_segment_ids,
    )
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
    history_states = latest_stage_states(
        project,
        stage,
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
    context_index = PreviousContextIndex(segments)
    term_match_cache = _TermMatchCache(
        library,
        term_normalization(config),
        int(config["terminology"]["max_terms_per_segment"]),
    )

    def payload_builder(items: list[dict[str, Any]]) -> dict[str, Any]:
        resolver = None
        if config["execution"]["scheduling_mode"] == "ordered_by_file":
            resolver = lambda segment_id: (
                str(bases[segment_id]["text"]) if segment_id in bases else None
            )
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
                    "current_text": segment_model_text(
                        item, str(bases[str(item["segment_id"])]["text"])
                    ),
                }
                for item in items
            ],
        }

    run_id, run_dir, continuation_index, fail_planning = _create_or_continue_run(
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
        return _split_segment_source(segment, probe_id, part[0])

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
        return _split_segment_source(segment, part_id, part[0])

    preflight = _split_oversized_preflight(
        selection.work,
        config=config,
        prompt=prompt,
        payload_builder=payload_builder,
        prompt_builder=prompt_for_items,
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
            stage=stage,
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
        continuation_index=continuation_index,
        on_usage=on_usage,
        preparation_started_at=preparation_started_at,
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
                    suggested_text = normalize_model_text(
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
            prompt=prompt_for_items(list(chunk.segments)),
            config=config,
            llm=state.llm,
            stage=stage,
            accept=accept_result,
            save_error=save_external_error,
            parse=_map_local_review_response,
            format_correction=_FORMAT_CORRECTION[language],
            prompt_language=language,
            by_id=by_id,
            segments=segments,
            prompt_partition_key=prompt_partition_key,
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
