from __future__ import annotations
import asyncio
import time
import uuid
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any
import httpx
from .errors import (
    ContextLengthError,
    ExternalError,
    FatalExternalError,
    StorageError,
)
from .term_library import (_merge_and_publish_terms, load_terms)
from .execution import (
    ChunkPlan,
    PreviousContextIndex,
    Scope,
    build_chunk_plans,
    render_messages,
    segment_model_source,
    select_scope,
    stage_fingerprint,
)
from .llm_client import SlidingWindowLimiter
from .llm_response import parse_jsonl_document
from .llm_keys import KeyPool
from .logging_utils import get_logger
from .sqlite_storage import (
    append_jsonl,
    read_json,
    read_jsonl,
    record_exists,
    record_header,
    terminology_scan_state,
    write_json,
)

from .stage_runtime import (StageRunState, _assemble_warnings, _create_or_continue_run, _document_prompt_requirement_helpers, _execute_stage_run, _project_context, _prompt_factory, _prompt_language, _request_estimate, _require_nonempty_segments, _resume_scope, _scope_record, _split_oversized_preflight, _split_source_once, _split_segment_source, prompt_middle_digests, _FORMAT_CORRECTION)

def _validate_term_items(
    content: str,
) -> tuple[list[dict[str, Any]], list[str], bool]:
    document = parse_jsonl_document(content, record_type="term")
    terms: list[dict[str, Any]] = []
    errors = list(document.errors)
    for index, item in enumerate(document.records, start=1):
        item_errors: list[str] = []
        for key in ("source", "category"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                item_errors.append(f"术语记录 {index} 缺少有效 {key}")
        description = item.get("description")
        if description is not None and not isinstance(description, str):
            item_errors.append(f"术语记录 {index} 的 description 类型错误")
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
                "description": description.strip() if description else None,
                "preferred_translation": preferred.strip() if preferred else None,
                "aliases": [alias.strip() for alias in aliases if alias.strip()],
            }
        )
    return terms, errors, document.complete and not errors

def _terminology_scan_selection(
    project: Path,
    segments: list[dict[str, Any]],
    files: list[dict[str, Any]],
    scope: Scope,
    task_id: str,
    *,
    force_all: bool = False,
) -> tuple[list[dict[str, Any]], set[str], set[str], set[str]]:
    selected = (
        [segment for segment in segments if not segment["is_empty"]]
        if force_all
        else select_scope(segments, files, scope)
    )
    selected_ids = {str(segment["segment_id"]) for segment in selected}
    completed_ids, fingerprints = terminology_scan_state(
        project,
        task_id,
        selected_ids,
    )
    return selected, selected_ids, completed_ids, fingerprints

async def run_terminology(
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
    logger = get_logger("terminology")
    preparation_started_at = time.perf_counter()
    scope, resume_arguments_ignored = _resume_scope(project, scope, resume_run_id)
    config, metadata, files, segments = _project_context(
        project, stage="terminology"
    )
    logger.info(
        "stage preparation context ready elapsed=%.3fs files=%d segments=%d",
        time.perf_counter() - preparation_started_at,
        len(files),
        len(segments),
    )
    _require_nonempty_segments(segments)
    language = _prompt_language(project, "terminology", prompt_language)
    prompt_factory = _prompt_factory(project, "terminology", language)
    prompt = prompt_factory(())
    requirements_for_items, prompt_partition_key = (
        _document_prompt_requirement_helpers(config, language)
    )
    prompt_for_items = lambda items: prompt_factory(requirements_for_items(items))
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

    # A forced terminology run rescans the full project and merges at publish.
    (
        selected,
        selected_ids,
        completed_ids,
        existing_fingerprints,
    ) = _terminology_scan_selection(
        project,
        segments,
        files,
        scope,
        task_id,
        force_all=scope.force,
    )
    work = (
        selected
        if scope.force and not create_task
        else [
            segment
            for segment in selected
            if str(segment["segment_id"]) not in completed_ids
        ]
    )
    logger.info(
        "stage preparation selection/history ready elapsed=%.3fs selected=%d requested=%d completed=%d",
        time.perf_counter() - preparation_started_at,
        len(selected),
        len(work),
        len(completed_ids),
    )
    reopen_completed_task = (
        resume_run_id is None
        and not scope.force
        and bool(work)
        and active is not None
        and active.get("status") == "completed"
    )
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
    context_index = PreviousContextIndex(segments)

    def payload_builder(items: list[dict[str, Any]]) -> dict[str, Any]:
        raw_context = (
            context_index.previous(
                items[0],
                context_config["previous_segments"],
                source_key="model_source",
            )
            if context_config["enabled"]
            else []
        )
        return {
            "target_language": config["project"]["target_language"],
            "reference_context": [item["source"] for item in raw_context],
            "source_segments": [segment_model_source(item) for item in items],
        }

    run_id, run_dir, continuation_index, fail_planning = _create_or_continue_run(
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

    if reopen_completed_task:
        active = {**active, "status": "active"}
        write_json(project, active_path, active)

    preflight = _split_oversized_preflight(
        work,
        config=config,
        prompt=prompt,
        payload_builder=payload_builder,
        prompt_builder=prompt_for_items,
        fail_planning=fail_planning,
        make_probe=lambda segment, part: _split_segment_source(
            segment, f"{segment['segment_id']}-PROBE", part
        ),
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
            stage="terminology",
            prompt=prompt,
            payload_builder=payload_builder,
            prompt_builder=prompt_for_items,
            partition_key=prompt_partition_key,
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
        continuation_index=continuation_index,
        on_usage=on_usage,
        preparation_started_at=preparation_started_at,
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
        parse_errors: list[str] = []
        for format_attempt in range(config["retry"]["format_max_attempts"] + 1):
            payload = payload_builder(unresolved)
            if format_attempt:
                payload["format_correction"] = _FORMAT_CORRECTION[language]
            messages = render_messages(prompt_for_items(unresolved), payload)
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
        prompt_builder=prompt_for_items,
        prompt_partition_key=prompt_partition_key,
        process_once=process_once,
        record_preflight_failure=record_preflight_failure,
        record_context_failure=record_context_failure,
        before_finalize=before_finalize,
        completed_count=completed_count,
        failed_count=lambda: len(failed_originals),
        exception_completed=lambda: (
            len(selected) - len(work) + len(completed_original_ids)
        ),
        exception_failed=lambda: len(work) - len(completed_original_ids),
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
