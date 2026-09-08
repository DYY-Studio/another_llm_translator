from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import httpx

from .config import load_project_config
from .errors import (
    AppError,
    ContextLengthError,
    ExportError,
    ExternalError,
    FatalExternalError,
    UsageError,
)
from .execution import (
    create_run,
    estimate_messages,
    finalize_run,
    render_messages,
    run_bounded,
    segment_model_source,
    stage_fingerprint,
)
from .llm_client import LLMClient, SlidingWindowLimiter
from .llm_keys import KeyPool
from .llm_response import TerminologyResponseMode, parse_terminology_response
from .project import load_segments
from .sqlite_storage import (
    atomic_write_text,
    publish_content_summary_fulls,
    read_content_summaries,
    record_header,
    utc_now,
    write_content_summary,
    write_summary_run,
)
from .stage_runtime import (
    _document_prompt_requirement_helpers,
    _project_context,
    _prompt_factory,
    _prompt_language,
    prompt_middle_digests,
)
from .summary_provenance import (
    assess_full_summary,
    build_provenance,
    full_summary_context_usable,
)

MAX_REDUCTION_DEPTH = 8


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _boundaries(values: Iterable[dict[str, Any]]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for value in values:
        if not isinstance(value, dict):
            raise UsageError("聚合选择必须是 file_id/part_id 对象数组")
        file_id = value.get("file_id")
        part_id = value.get("part_id")
        if not isinstance(file_id, str) or not file_id:
            raise UsageError("聚合选择缺少 file_id")
        if not isinstance(part_id, str) or not part_id:
            raise UsageError("聚合选择缺少 part_id")
        boundary = (file_id, part_id)
        if boundary in result:
            raise UsageError(f"聚合选择不能重复：{file_id}/{part_id}")
        result.append(boundary)
    if not result:
        raise UsageError("聚合选择不能为空")
    return result


def _current_segments(project: Path) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for segment in load_segments(project):
        if segment.get("is_empty"):
            continue
        boundary = (str(segment["file_id"]), str(segment["part_id"]))
        grouped.setdefault(boundary, []).append(segment)
    for values in grouped.values():
        values.sort(key=lambda item: (int(item["line_index"]), str(item["segment_id"])))
    return grouped


def _range_segments(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    source_range = artifact.get("source_range")
    if not isinstance(source_range, dict):
        return []
    values = source_range.get("segments")
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, dict)]


def full_summary_expired(
    artifact: dict[str, Any],
    current_segments: list[dict[str, Any]],
    summary_artifacts: list[dict[str, Any]],
) -> bool:
    """Return whether a full summary should be shown with an expiry warning."""
    return assess_full_summary(artifact, current_segments, summary_artifacts).expired


def _validate_fragments(
    project: Path,
    boundary: tuple[str, str],
    current: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    current_by_id = {str(item["segment_id"]): item for item in current}
    expected_ids = set(current_by_id)
    artifacts = read_content_summaries(
        project,
        file_id=boundary[0],
        part_id=boundary[1],
        kind="fragment",
        status="completed",
    )
    valid: list[dict[str, Any]] = []
    changed = False
    covered_by: dict[str, str] = {}
    for artifact in artifacts:
        if artifact.get("source_changed"):
            changed = True
            continue
        ranges = _range_segments(artifact)
        if not ranges:
            continue
        artifact_ids = {
            str(value.get("original_segment_id") or value.get("segment_id"))
            for value in ranges
        }
        if not artifact_ids <= expected_ids or not artifact_ids:
            changed = True
            continue
        valid_artifact = True
        for value in ranges:
            stable_id = str(value.get("original_segment_id") or value.get("segment_id"))
            source = current_by_id[stable_id]
            current_source_digest = _digest(str(source["source"]))
            current_model_digest = _digest(segment_model_source(source))
            if value.get("original_source_digest", value.get("source_digest")) != current_source_digest:
                valid_artifact = False
            if value.get("original_model_text_digest", value.get("model_text_digest")) != current_model_digest:
                valid_artifact = False
        if not valid_artifact:
            changed = True
            continue
        for stable_id in artifact_ids:
            previous = covered_by.get(stable_id)
            if previous is not None and previous != str(artifact["record_id"]):
                raise UsageError(
                    f"{boundary[0]}/{boundary[1]} 的片段摘要范围重叠："
                    f"{previous} 与 {artifact['record_id']} 包含 {stable_id}"
                )
            covered_by[stable_id] = str(artifact["record_id"])
        valid.append(artifact)
    covered = {
        str(value.get("original_segment_id") or value.get("segment_id"))
        for artifact in valid
        for value in _range_segments(artifact)
    }
    if covered != expected_ids:
        if changed:
            raise UsageError(f"{boundary[0]}/{boundary[1]} 的片段摘要源已变化")
        raise UsageError(f"{boundary[0]}/{boundary[1]} 的片段摘要覆盖不完整")
    valid.sort(
        key=lambda artifact: min(
            int(current_by_id[stable_id]["line_index"])
            for stable_id in {
                str(value.get("original_segment_id") or value.get("segment_id"))
                for value in _range_segments(artifact)
            }
        )
    )
    return valid


def aggregation_preflight(
    project: Path, selected: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    """Validate selected boundaries without invoking the model."""
    boundaries = _boundaries(selected)
    grouped = _current_segments(project)
    result: list[dict[str, Any]] = []
    for boundary in boundaries:
        current = grouped.get(boundary)
        if not current:
            raise UsageError(f"未知或空内容边界：{boundary[0]}/{boundary[1]}")
        fragments = _validate_fragments(project, boundary, current)
        result.append(
            {
                "file_id": boundary[0],
                "part_id": boundary[1],
                "fragment_count": len(fragments),
                "can_adopt": len(fragments) == 1,
            }
        )
    return {"boundaries": result, "selected": len(result)}


def _merge_source_range(
    boundary: tuple[str, str],
    artifacts: Iterable[dict[str, Any]],
    current: list[dict[str, Any]],
) -> dict[str, Any]:
    by_id = {str(item["segment_id"]): item for item in current}
    ranges: list[dict[str, Any]] = []
    seen_slices: set[str] = set()
    for artifact in artifacts:
        for value in _range_segments(artifact):
            slice_id = str(value.get("slice_id") or "")
            key = slice_id or _digest(value)
            if key in seen_slices:
                continue
            seen_slices.add(key)
            ranges.append(dict(value))
    ranges.sort(
        key=lambda value: (
            int(by_id.get(str(value.get("original_segment_id") or value.get("segment_id")), {}).get("line_index", 0)),
            int(value.get("slice_index", 0)),
            str(value.get("slice_id", "")),
        )
    )
    segment_ids = list(
        dict.fromkeys(
            str(value.get("original_segment_id") or value.get("segment_id"))
            for value in ranges
        )
    )
    return {
        "file_id": boundary[0],
        "part_id": boundary[1],
        "segment_ids": segment_ids,
        "segments": ranges,
    }


def _refs_for_children(children: list[dict[str, Any]], refs: Iterable[str]) -> list[str]:
    values: list[str] = []
    for ref in refs:
        try:
            child = children[int(ref) - 1]
        except (ValueError, IndexError):
            continue
        source_range = child.get("source_range")
        if not isinstance(source_range, dict):
            continue
        for segment_id in source_range.get("segment_ids", []):
            if isinstance(segment_id, str) and segment_id not in values:
                values.append(segment_id)
    return values


def _children_from_artifacts(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "record_id": str(artifact["record_id"]),
            "summary_id": str(artifact["record_id"]),
            "kind": str(artifact["kind"]),
            "text": str(artifact.get("text") or ""),
            "refs": list(artifact.get("refs") or []),
            "source_range": artifact["source_range"],
            "source_digest": artifact["source_digest"],
        }
        for artifact in artifacts
    ]


def _child_from_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_id": str(artifact["record_id"]),
        "summary_id": str(artifact["record_id"]),
        "kind": str(artifact["kind"]),
        "text": str(artifact.get("text") or ""),
        "refs": list(artifact.get("refs") or []),
        "source_range": artifact["source_range"],
        "source_digest": artifact["source_digest"],
    }


def _make_artifact(
    *,
    metadata: dict[str, Any],
    boundary: tuple[str, str],
    kind: str,
    text: str,
    refs: list[str],
    source_range: dict[str, Any],
    children: list[dict[str, Any]],
    config: dict[str, Any],
    prompt_digest: str,
    run_id: str,
    origin: str,
) -> dict[str, Any]:
    source_digest = _digest(source_range.get("segments", []))
    provenance, input_digest = build_provenance(origin, children)
    summary_id = f"SUMMARY-{kind.upper()}-{_digest([boundary, source_digest, input_digest, prompt_digest, text])[7:31].upper()}"
    record = record_header(
        "content_summary",
        str(metadata["project_id"]),
        record_id=summary_id,
        kind=kind,
        file_id=boundary[0],
        part_id=boundary[1],
        status="completed",
        text=text,
        refs=refs,
        source_range=source_range,
        source_digest=source_digest,
        input_digest=input_digest,
        prompt_digest=prompt_digest,
        model=str(config["llm"]["model"]),
        run_id=run_id,
        provenance=provenance,
    )
    return record


async def aggregate_summaries(
    project: Path,
    selected: Iterable[dict[str, Any]],
    *,
    http_client: httpx.AsyncClient | None = None,
    limiter: SlidingWindowLimiter | KeyPool | None = None,
    prompt_language: str | None = None,
    on_progress: Any = None,
    on_usage: Callable[[dict[str, Any] | None], None] | None = None,
) -> dict[str, Any]:
    boundaries = _boundaries(selected)
    grouped = _current_segments(project)
    checked: list[tuple[tuple[str, str], list[dict[str, Any]], list[dict[str, Any]]]] = []
    for boundary in boundaries:
        current = grouped.get(boundary)
        if not current:
            raise UsageError(f"未知或空内容边界：{boundary[0]}/{boundary[1]}")
        fragments = _validate_fragments(project, boundary, current)
        checked.append((boundary, current, fragments))

    config = load_project_config(project, stage="content_summary")
    context_config, metadata, _, _ = _project_context(
        project, stage="content_summary"
    )
    for key in (
        "_document_adapter_options",
        "_document_adapters",
        "_document_adapter_prompt_requirements",
    ):
        config[key] = context_config.get(key, {})
    language = _prompt_language(project, "content_summary", prompt_language)
    prompt_factory = _prompt_factory(project, "content_summary", language)
    requirements_for_items, _ = _document_prompt_requirement_helpers(
        config, language
    )
    prompt_details: dict[tuple[str, str], tuple[tuple[str, ...], str, str]] = {}
    for boundary, current, _ in checked:
        requirements = requirements_for_items(current)
        prompt = prompt_factory(requirements)
        prompt_details[boundary] = (requirements, prompt, _digest(prompt))
    summary_prompt_digest = prompt_details[checked[0][0]][2]
    source_ranges = [
        _merge_source_range(boundary, fragments, current)
        for boundary, current, fragments in checked
    ]
    all_adopted = all(len(fragments) == 1 for _, _, fragments in checked)
    run_id: str | None = None
    run_dir: Path | None = None
    if not all_adopted:
        primary_boundary = next(
            boundary for boundary, _, fragments in checked if len(fragments) > 1
        )
        _, primary_prompt, primary_prompt_digest = prompt_details[primary_boundary]
        summary_prompt_digest = primary_prompt_digest
        prompt_variants: dict[str, str] = {}
        prompt_variant_requirements: dict[str, tuple[str, ...]] = {}
        for boundary, _, fragments in checked:
            if len(fragments) == 1:
                continue
            requirements, prompt, prompt_digest = prompt_details[boundary]
            if prompt_digest == primary_prompt_digest:
                continue
            variant_name = f"content-summary-{prompt_digest[7:23]}"
            prompt_variants[variant_name] = prompt
            prompt_variant_requirements[variant_name] = requirements
        fingerprint = stage_fingerprint(
            config,
            "content_summary",
            prompt_middle_digests(project, "content_summary"),
        )
        run_id, run_dir = create_run(
            project,
            config=config,
            stage="content_summary",
            fingerprint=fingerprint,
            prompt=primary_prompt,
            selected_count=len(checked),
            requested_count=len(checked),
            reused_count=0,
            details={"prompt_language": language, "summary_boundaries": source_ranges},
            prompt_variants=prompt_variants,
            prompt_variant_requirements=prompt_variant_requirements,
        )
        write_summary_run(
            project,
            record_header(
                "summary_run",
                str(metadata["project_id"]),
                record_id=run_id,
                run_id=run_id,
                mode="aggregation",
                status="running",
                source_ranges=source_ranges,
                input_digest=_digest(source_ranges),
                prompt_digest=primary_prompt_digest,
                model=str(config["llm"]["model"]),
                started_at=utc_now(),
            ),
        )

    if on_progress is not None:
        on_progress(0, 0, len(checked))

    max_parallel = int(config["execution"]["max_parallel"])
    request_semaphore = asyncio.Semaphore(max_parallel)
    if limiter is None:
        execution = config["execution"]
        limiter = KeyPool(
            int(execution["requests_per_minute"]),
            int(execution["input_tokens_per_minute"]),
            int(execution["max_parallel"]),
            int(execution.get("max_parallel_per_key", execution["max_parallel"])),
        )

    pending_full: list[dict[str, Any]] = []
    calls = 0
    completed = 0
    failed = 0
    boundary_warnings: dict[tuple[str, str], str] = {}
    usage: dict[str, Any] | None = None
    active_llm: LLMClient | None = None

    def ordered_boundary_warnings() -> list[str]:
        return [
            boundary_warnings[boundary]
            for boundary, _, _ in checked
            if boundary in boundary_warnings
        ]

    try:
        async with LLMClient(
            config,
            limiter,
            run_dir=run_dir or project / "runs",
            project_id=str(metadata["project_id"]),
            run_id=run_id or f"SUMMARY-{uuid.uuid4().hex[:12].upper()}",
            stage="content_summary",
            client=http_client,
            on_usage=on_usage,
        ) as llm:
            active_llm = llm

            def aggregation_summaries(children: list[dict[str, Any]]) -> list[dict[str, str]]:
                return [
                    {
                        "id": str(index + 1),
                        "text": item["text"],
                    }
                    for index, item in enumerate(children)
                ]

            async def summarize(
                boundary: tuple[str, str],
                current: list[dict[str, Any]],
                children: list[dict[str, Any]],
                *,
                kind: str,
            ) -> dict[str, Any]:
                nonlocal calls
                _, prompt, prompt_digest = prompt_details[boundary]
                payload = {
                    "target_language": config["project"]["target_language"],
                    "summaries": aggregation_summaries(children),
                }
                messages = render_messages(prompt, payload)
                estimated = estimate_messages(
                    messages, config["execution"]["token_safety_factor"]
                )
                limit = (
                    config["llm"]["context_window_tokens"]
                    - config["llm"]["context_safety_margin_tokens"]
                )
                if estimated > limit:
                    raise ContextLengthError(
                        "聚合请求超过模型上下文预算",
                        request_id=f"PRECHECK-{uuid.uuid4().hex[:10].upper()}",
                    )
                request_id = f"REQ-{uuid.uuid4().hex[:12].upper()}"
                async with request_semaphore:
                    response, _ = await llm.chat(
                        messages=messages,
                        temperature=config["llm"]["temperature_content_summary"],
                        estimated_input_tokens=estimated,
                        request_id=request_id,
                        segment_id_map={
                            str(index + 1): str(item["summary_id"])
                            for index, item in enumerate(children)
                        },
                    )
                calls += 1
                parsed = parse_terminology_response(
                    response.content,
                    mode=TerminologyResponseMode.SUMMARY_ONLY,
                    source_refs=tuple(str(index + 1) for index in range(len(children))),
                    source_texts=tuple(str(item["text"]) for item in children),
                )
                if not parsed.complete or not parsed.summaries:
                    raise UsageError(
                        "聚合失败：LLM 响应缺少有效 summary 或 end"
                    )
                if len(parsed.summaries) != 1:
                    raise UsageError("聚合失败：只允许一条 summary")
                refs = list(parsed.summaries[0]["refs"])
                if set(refs) != {str(index + 1) for index in range(len(children))}:
                    raise UsageError("聚合失败：summary 未覆盖全部输入概括")
                source_range = _merge_source_range(
                    boundary,
                    [
                        {
                            "source_range": item["source_range"],
                            "record_id": item["summary_id"],
                        }
                        for item in children
                    ],
                    current,
                )
                artifact = _make_artifact(
                    metadata=metadata,
                    boundary=boundary,
                    kind=kind,
                    text=str(parsed.summaries[0]["text"]),
                    refs=_refs_for_children(children, refs),
                    source_range=source_range,
                    children=children,
                    config=config,
                    prompt_digest=prompt_digest,
                    run_id=str(run_id),
                    origin="llm",
                )
                if kind == "reduction":
                    write_content_summary(project, artifact)
                return artifact

            async def reduce_boundary(
                boundary: tuple[str, str],
                current: list[dict[str, Any]],
                fragments: list[dict[str, Any]],
            ) -> dict[str, Any]:
                _, prompt, prompt_digest = prompt_details[boundary]
                original = _children_from_artifacts(fragments)
                if len(original) == 1:
                    artifact = _make_artifact(
                        metadata=metadata,
                        boundary=boundary,
                        kind="full",
                        text=original[0]["text"],
                        refs=_refs_for_children(original, ["1"]),
                        source_range=_merge_source_range(boundary, fragments, current),
                        children=original,
                        config=config,
                        prompt_digest=prompt_digest,
                        run_id=str(run_id or ""),
                        origin="adopted",
                    )
                    return artifact
                limit = (
                    config["llm"]["context_window_tokens"]
                    - config["llm"]["context_safety_margin_tokens"]
                )

                def estimate(children: list[dict[str, Any]]) -> int:
                    payload = {
                        "target_language": config["project"]["target_language"],
                        "summaries": aggregation_summaries(children),
                    }
                    return estimate_messages(
                        render_messages(prompt, payload),
                        config["execution"]["token_safety_factor"],
                    )

                def is_global_error(error: BaseException) -> bool:
                    return isinstance(
                        error, (asyncio.CancelledError, FatalExternalError)
                    ) or not isinstance(error, (UsageError, ExternalError))

                async def fit(
                    children: list[dict[str, Any]],
                    final: bool,
                    depth: int = 0,
                ) -> dict[str, Any]:
                    before_estimate = estimate(children)
                    if before_estimate <= limit:
                        try:
                            return await summarize(
                                boundary,
                                current,
                                children,
                                kind="full" if final else "reduction",
                            )
                        except ContextLengthError:
                            pass
                    if len(children) < 2 or depth >= MAX_REDUCTION_DEPTH:
                        raise UsageError(
                            "聚合失败：递归压缩不收敛，最小请求仍超过模型上下文预算"
                        )
                    midpoint = len(children) // 2
                    child_tasks = [
                        asyncio.create_task(fit(children[:midpoint], False)),
                        asyncio.create_task(fit(children[midpoint:], False)),
                    ]
                    try:
                        done, _ = await asyncio.wait(
                            child_tasks,
                            return_when=asyncio.FIRST_EXCEPTION,
                        )
                        for task in done:
                            error = (
                                asyncio.CancelledError()
                                if task.cancelled()
                                else task.exception()
                            )
                            if error is not None and is_global_error(error):
                                for child_task in child_tasks:
                                    child_task.cancel()
                                await asyncio.gather(
                                    *child_tasks,
                                    return_exceptions=True,
                                )
                                raise error
                        left, right = await asyncio.gather(
                            *child_tasks,
                            return_exceptions=True,
                        )
                    except asyncio.CancelledError:
                        for child_task in child_tasks:
                            child_task.cancel()
                        await asyncio.gather(
                            *child_tasks,
                            return_exceptions=True,
                        )
                        raise
                    reduction_errors = [
                        result
                        for result in (left, right)
                        if isinstance(result, BaseException)
                    ]
                    for error in reduction_errors:
                        if is_global_error(error):
                            raise error
                    if reduction_errors:
                        raise reduction_errors[0]
                    reduced = [_child_from_artifact(left), _child_from_artifact(right)]
                    after_estimate = estimate(reduced)
                    before_digest = _digest([str(item["text"]) for item in children])
                    after_digest = _digest([str(item["text"]) for item in reduced])
                    if after_estimate >= before_estimate or after_digest == before_digest:
                        raise UsageError("聚合失败：递归压缩不收敛，输入未缩小")
                    return await fit(reduced, final, depth + 1)

                return await fit(original, True)

            async def boundary_worker(
                item: tuple[
                    tuple[str, str],
                    list[dict[str, Any]],
                    list[dict[str, Any]],
                ]
            ) -> dict[str, Any] | None:
                nonlocal completed, failed
                boundary, current, fragments = item
                try:
                    artifact = await reduce_boundary(boundary, current, fragments)
                    publish_content_summary_fulls(project, [artifact])
                except FatalExternalError:
                    raise
                except (UsageError, ExternalError) as exc:
                    warning = f"{boundary[0]}/{boundary[1]}：{exc}"
                    boundary_warnings[boundary] = warning
                    failed += 1
                    if on_progress is not None:
                        on_progress(completed, failed, len(checked))
                    return None
                completed += 1
                if on_progress is not None:
                    on_progress(completed, failed, len(checked))
                return artifact

            results = await run_bounded(
                checked,
                boundary_worker,
                max_parallel=max_parallel,
            )
            pending_full = [
                artifact
                for artifact in results
                if artifact is not None
            ]
        usage = active_llm.usage_summary() if active_llm is not None else None
    except asyncio.CancelledError:
        usage = active_llm.usage_summary() if active_llm is not None else None
        warnings = [*ordered_boundary_warnings(), "任务已由用户取消"]
        if run_id is not None:
            write_summary_run(
                project,
                record_header(
                    "summary_run",
                    str(metadata["project_id"]),
                    record_id=run_id,
                    run_id=run_id,
                    mode="aggregation",
                    status="interrupted",
                    source_ranges=source_ranges,
                    input_digest=_digest(source_ranges),
                    prompt_digest=summary_prompt_digest,
                    model=str(config["llm"]["model"]),
                    warnings=warnings,
                ),
            )
            if run_dir is not None:
                finalize_run(
                    project,
                    run_dir,
                    status="interrupted",
                    completed=completed,
                    failed=failed,
                    warnings=warnings,
                    usage=usage,
                )
        raise
    except Exception as exc:
        usage = active_llm.usage_summary() if active_llm is not None else None
        warnings = [*ordered_boundary_warnings(), str(exc)]
        if run_id is not None:
            write_summary_run(
                project,
                record_header(
                    "summary_run",
                    str(metadata["project_id"]),
                    record_id=run_id,
                    run_id=run_id,
                    mode="aggregation",
                    status="failed",
                    source_ranges=source_ranges,
                    input_digest=_digest(source_ranges),
                    prompt_digest=summary_prompt_digest,
                    model=str(config["llm"]["model"]),
                    error=str(exc),
                    warnings=warnings,
                ),
            )
            if run_dir is not None:
                finalize_run(
                    project,
                    run_dir,
                    status="failed",
                    completed=completed,
                    failed=len(checked) - completed,
                    warnings=warnings,
                    usage=usage,
                )
        if isinstance(exc, UsageError):
            raise
        if isinstance(exc, ContextLengthError):
            raise UsageError(f"聚合失败：{exc}") from exc
        if isinstance(exc, AppError):
            raise
        raise UsageError(f"聚合失败：{exc}") from exc

    warnings = ordered_boundary_warnings()
    status = "failed" if failed else "completed"
    if run_id is not None:
        write_summary_run(
            project,
            record_header(
                "summary_run",
                str(metadata["project_id"]),
                record_id=run_id,
                run_id=run_id,
                mode="aggregation",
                status=status,
                source_ranges=source_ranges,
                input_digest=_digest(source_ranges),
                prompt_digest=summary_prompt_digest,
                model=str(config["llm"]["model"]),
                warnings=warnings,
            ),
        )
        if run_dir is not None:
            finalize_run(
                project,
                run_dir,
                status=status,
                completed=completed,
                failed=failed,
                warnings=warnings,
                usage=usage,
            )
    return {
        "run_id": run_id,
        "selected": len(checked),
        "completed": completed,
        "failed": failed,
        "pending": 0,
        "calls": calls,
        "usage": usage,
        "warnings": warnings,
        "boundaries": [
            {
                "file_id": str(artifact["file_id"]),
                "part_id": str(artifact["part_id"]),
                "summary_id": str(artifact["record_id"]),
                "origin": artifact.get("provenance", {}).get("origin", "llm"),
                "calls": calls,
            }
            for artifact in pending_full
        ],
    }


def _latest_full(
    project: Path,
    boundary: tuple[str, str],
    current: list[dict[str, Any]],
) -> dict[str, Any]:
    artifacts = read_content_summaries(
        project,
        file_id=boundary[0],
        part_id=boundary[1],
    )
    candidates = [item for item in artifacts if item.get("kind") == "full"]
    usable = [
        item
        for item in candidates
        if item.get("status") in {"completed", "stale"}
        and item.get("text") is not None
    ]
    usable.sort(
        key=lambda item: (
            str(item.get("created_at") or ""),
            str(item.get("updated_at") or ""),
            str(item.get("record_id") or ""),
        )
    )
    for artifact in reversed(usable):
        if full_summary_context_usable(artifact, current):
            return artifact
    for artifact in reversed(usable):
        if not assess_full_summary(artifact, current, artifacts).expired:
            return artifact
    if usable:
        return usable[-1]
    raise ExportError(
        f"{boundary[0]}/{boundary[1]} 缺少完整或有效的内容概括",
        reason="missing_or_stale_summary",
        file_id=boundary[0],
        part_id=boundary[1],
    )


def export_summary_markdown(
    project: Path,
    selected: Iterable[dict[str, Any]],
    relative_path: str = "summary.md",
) -> Path:
    selected_values = list(selected)
    if not selected_values:
        raise ExportError("导出选择不能为空", reason="empty_selection")
    boundaries = _boundaries(selected_values)
    if "\0" in relative_path:
        raise ExportError("导出路径无效", reason="invalid_path")
    path = Path(relative_path)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ExportError("导出路径必须位于 output 目录内", reason="invalid_path")
    output_root = (project / "output").resolve()
    destination = (output_root / path).resolve()
    if not destination.is_relative_to(output_root):
        raise ExportError("导出路径必须位于 output 目录内", reason="invalid_path")
    current = _current_segments(project)
    sections: list[str] = ["# 内容概括", ""]
    for boundary in boundaries:
        values = current.get(boundary)
        if not values:
            raise ExportError(
                f"未知或空内容边界：{boundary[0]}/{boundary[1]}",
                reason="unknown_boundary",
            )
        artifact = _latest_full(project, boundary, values)
        sections.extend(
            [
                f"## {boundary[0]} / {boundary[1]}",
                "",
                str(artifact.get("text") or ""),
                "",
                "### 引用与原文",
                "",
            ]
        )
        for value in _range_segments(artifact):
            segment_id = str(value.get("original_segment_id") or value.get("segment_id"))
            source = str(value.get("source") or "")
            sections.append(f"- `{segment_id}`：{source}")
        sections.append("")
    try:
        atomic_write_text(destination, "\n".join(sections))
    except OSError as exc:
        raise ExportError(f"无法写入 Markdown 导出：{destination}", reason="write_failed") from exc
    return destination
