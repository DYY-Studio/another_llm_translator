from __future__ import annotations

import asyncio
import os
import re
import shlex
import tempfile
import unicodedata
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

from .config import load_config
from .errors import (
    ConfigError,
    ContextLengthError,
    ExternalError,
    FatalExternalError,
    IncompleteError,
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
    contiguous_groups,
    create_run,
    dispatch_chunks,
    estimate_messages,
    finalize_run,
    full_prompt,
    load_stage_history,
    materialize_chunks,
    parse_jsonl_document,
    previous_context,
    render_messages,
    save_debug_chunks,
    select_scope,
    stage_fingerprint,
    stage_result_path,
)
from .logging_utils import get_logger
from .project import load_segments, load_source_files
from .storage import (
    append_jsonl,
    atomic_write_json,
    new_record_id,
    read_json,
    read_jsonl,
    record_header,
)

JAPANESE_RE = re.compile(
    "[\u3040-\u30ff\u31f0-\u31ff\uff66-\uff9f"
    "\U0001b000-\U0001b0ff\U0001b100-\U0001b12f"
    "\U0001b130-\U0001b16f\U0001aff0-\U0001afff]"
)
KOREAN_RE = re.compile("[\u1100-\u11ff\u3130-\u318f\ua960-\ua97f\uac00-\ud7ff]")


def _project_context(
    project: Path, *, dry_run: bool
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    config = load_config(project / "config.toml")
    metadata = read_json(project / "project.json")
    files = load_source_files(project, repair_tail=not dry_run)
    segments = load_segments(project, repair_tail=not dry_run)
    return config, metadata, files, segments


def _scope_record(scope: Scope, *, force_all: bool = False) -> dict[str, Any]:
    return {
        "all_nonempty": force_all
        or not (scope.from_file or scope.only_file or scope.only_segment),
        "from_file": None if force_all else scope.from_file,
        "only_file": None if force_all else scope.only_file,
        "only_segment": None if force_all else scope.only_segment,
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


def _extend_unique(target: list[str], values: list[str]) -> None:
    target.extend(value for value in values if value not in target)


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
        raise ConfigError("单请求预测 Token 超过 ITPM")
    return estimated


def _prompt(project: Path, stage: str) -> str:
    name = {
        "terminology": "terminology.middle.txt",
        "translation": "translation.middle.txt",
        "proofreading": "proofreading.middle.txt",
        "polishing": "polishing.middle.txt",
    }[stage]
    try:
        middle = (project / "prompts" / name).read_text(encoding="utf-8")
    except OSError as exc:
        raise StorageError(f"无法读取 Prompt：{name}: {exc}") from exc
    return full_prompt(stage, middle)


def load_terms(project: Path) -> dict[str, Any] | None:
    path = project / "terminology" / "terms.json"
    return read_json(path) if path.exists() else None


def normalize_term(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


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
) -> dict[str, Any]:
    candidates = [
        record
        for record in read_jsonl(project / "terminology" / "candidates.jsonl")
        if record.get("active_task_id") == task_id
    ]
    merged: dict[str, dict[str, Any]] = {}
    for record in candidates:
        for candidate in record.get("terms", []):
            normalized = normalize_term(str(candidate["source"]))
            current = merged.setdefault(
                normalized,
                {
                    "normalized": normalized,
                    "sources": [],
                    "categories": [],
                    "descriptions": [],
                    "translations": [],
                    "aliases": [],
                },
            )
            current["sources"].append(candidate["source"])
            current["categories"].append(candidate["category"])
            current["descriptions"].append(candidate["description"])
            if candidate.get("preferred_translation"):
                current["translations"].append(candidate["preferred_translation"])
            current["aliases"].extend(candidate.get("aliases", []))

    overrides_data = read_json(project / "terminology" / "overrides.json")
    overrides = {
        str(item["normalized"]): item for item in overrides_data.get("overrides", [])
    }
    for normalized, override in overrides.items():
        if override.get("disabled"):
            merged.pop(normalized, None)
            continue
        current = merged.setdefault(
            normalized,
            {
                "normalized": normalized,
                "sources": [override.get("source", normalized)],
                "categories": [],
                "descriptions": [],
                "translations": [],
                "aliases": [],
            },
        )
        for source_key, target_key in (
            ("category", "categories"),
            ("description", "descriptions"),
            ("preferred_translation", "translations"),
        ):
            if override.get(source_key):
                current[target_key] = [override[source_key]]
        if override.get("aliases"):
            current["aliases"] = list(override["aliases"])

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
                if normalize_term(alias) != normalized
            }
        )
        terms.append(
            {
                "record_id": f"TERM-{index:06d}",
                "source": sources[0] if sources else normalized,
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
                },
            }
        )
    previous = load_terms(project)
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
    atomic_write_json(project / "terminology" / "terms.json", library)
    active = read_json(project / "terminology" / "active_task.json")
    active["status"] = "completed"
    active["terms_revision"] = revision
    atomic_write_json(project / "terminology" / "active_task.json", active)
    return library


async def run_terminology(
    project: Path,
    scope: Scope,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    logger = get_logger("terminology")
    config, metadata, files, segments = _project_context(project, dry_run=scope.dry_run)
    prompt = _prompt(project, "terminology")
    fingerprint = stage_fingerprint(config, "terminology", prompt)
    active_path = project / "terminology" / "active_task.json"
    active = read_json(active_path) if active_path.exists() else None
    published = load_terms(project)

    create_task = (
        scope.force
        or active is None
        or (active.get("status") != "active" and published is None)
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
            atomic_write_json(active_path, active)
    elif active and active.get("status") == "active":
        task_id = str(active["active_task_id"])
    else:
        task_id = str(active.get("active_task_id", "none")) if active else "none"

    selected = select_scope(segments, files, scope)
    if scope.force:
        # A forced terminology run always starts a complete replacement scan.
        selected = [segment for segment in segments if not segment["is_empty"]]
    scans = [
        record
        for record in read_jsonl(
            project / "terminology" / "scans.jsonl",
            repair_tail=not scope.dry_run,
        )
        if record.get("active_task_id") == task_id
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
    existing_fingerprints = {
        str(record["stage_fingerprint"])
        for record in scans
        if record.get("stage_fingerprint")
    }
    warnings: list[str] = []
    configured_output_warning = _configured_output_warning(config)
    if configured_output_warning:
        warnings.append(configured_output_warning)
    if existing_fingerprints and existing_fingerprints != {fingerprint}:
        warnings.append(
            "活动术语任务包含不同设置指纹；继续复用并处理 pending"
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
                prompt=prompt,
                payload_builder=payload_builder,
            )
            request_segments.append(segment)
            continue
        except ConfigError as exc:
            if "单 Segment Prompt 超过模型硬限制" not in str(exc):
                raise
            if not config["chunking"]["allow_split_oversized_segment"]:
                preflight_failed.append(segment)
                continue
        sources = [str(segment["source"])]
        accepted_sources: list[str] = []
        while sources:
            source_part = sources.pop(0)
            probe = {
                **segment,
                "segment_id": f"{segment['segment_id']}-PROBE",
                "source": source_part,
            }
            try:
                build_chunk_plans(
                    [probe],
                    all_segments=segments,
                    config=config,
                    prompt=prompt,
                    payload_builder=payload_builder,
                )
                accepted_sources.append(source_part)
            except ConfigError as exc:
                if "单 Segment Prompt 超过模型硬限制" not in str(exc):
                    raise
                left, right = _split_source_once(source_part)
                sources[0:0] = [left, right]
        part_ids: list[str] = []
        for index, source_part in enumerate(accepted_sources, start=1):
            part_id = f"{segment['segment_id']}-P{index:03d}"
            request_segments.append(
                {**segment, "segment_id": part_id, "source": source_part}
            )
            part_original[part_id] = str(segment["segment_id"])
            part_ids.append(part_id)
        original_parts[str(segment["segment_id"])] = part_ids

    plans = build_chunk_plans(
        request_segments,
        all_segments=segments,
        config=config,
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
    if scope.dry_run:
        return {
            "stage": "terminology",
            "dry_run": True,
            "active_task_id": task_id,
            "selected": len(selected),
            "requested": len(work),
            "chunks": len(plans),
            "estimated_input_tokens": sum(
                plan.estimated_input_tokens for plan in plans
            ),
            "warnings": warnings,
        }

    run_id, run_dir = create_run(
        project,
        stage="terminology",
        fingerprint=fingerprint,
        prompt=prompt,
        selected_count=len(selected),
        requested_count=len(work),
        reused_count=len(selected) - len(work),
        details={
            "active_task_id": task_id,
            "scope": _scope_record(scope, force_all=scope.force),
        },
    )
    logger.info("run start run=%s", run_id)
    chunks = materialize_chunks(run_id, "terminology", plans)
    if config["debug"]["enabled"]:
        save_debug_chunks(
            run_dir,
            str(metadata["project_id"]),
            run_id,
            "terminology",
            chunks,
        )
    limiter = SlidingWindowLimiter(
        config["execution"]["requests_per_minute"],
        config["execution"]["input_tokens_per_minute"],
    )
    write_lock = asyncio.Lock()
    part_success: dict[str, set[str]] = {}
    failed_originals: set[str] = set()

    async def process_once(
        chunk: ChunkPlan,
        initial_parent_request_id: str | None = None,
    ) -> tuple[int, int]:
        unresolved = list(chunk.segments)
        parent_request_id = initial_parent_request_id
        for format_attempt in range(config["retry"]["format_max_attempts"] + 1):
            payload = payload_builder(unresolved)
            if format_attempt:
                payload["format_correction"] = (
                    "上一次响应不符合 JSONL 协议。每行只输出一个紧凑 JSON "
                    "对象，不要解释，最后一行输出 {\"type\":\"end\"}。"
                )
            messages = render_messages(prompt, payload)
            request_id = f"REQ-{uuid.uuid4().hex[:12].upper()}"
            estimated = _request_estimate(messages, config, request_id)
            try:
                content, _ = await llm.chat(
                    messages=messages,
                    temperature=config["llm"]["temperature_terminology"],
                    estimated_input_tokens=estimated,
                    request_id=request_id,
                    parent_request_id=parent_request_id,
                )
                terms, parse_errors, response_complete = _validate_term_items(
                    content
                )
            except FatalExternalError:
                raise
            except ContextLengthError:
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
                        append_jsonl(
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
                        append_jsonl(
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
            logger.info(
                "chunk complete chunk=%s completed=%d",
                chunk.chunk_id or "runtime",
                len(completed_originals),
            )
            return len(completed_originals), 0
        return 0, len(unresolved)

    async def process(
        chunk: ChunkPlan,
        split_parent_request_id: str | None = None,
    ) -> tuple[int, int]:
        try:
            return await process_once(chunk, split_parent_request_id)
        except ContextLengthError as exc:
            logger.warning(
                "context split parent_request=%s segments=%d",
                exc.request_id,
                len(chunk.segments),
            )
            items = list(chunk.segments)
            if len(items) > 1:
                midpoint = len(items) // 2
                groups = (items[:midpoint], items[midpoint:])
            elif (
                config["chunking"]["allow_split_oversized_segment"]
                and len(str(items[0]["source"])) > 1
            ):
                groups = tuple(
                    [part]
                    for part in _replace_with_runtime_parts(
                        items[0],
                        part_original=part_original,
                        original_parts=original_parts,
                    )
                )
            else:
                groups = ()
            if not groups:
                original_id = part_original.get(
                    str(items[0]["segment_id"]), str(items[0]["segment_id"])
                )
                async with write_lock:
                    if original_id not in failed_originals:
                        failed_originals.add(original_id)
                        append_jsonl(
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
                return 0, 1
            values = [
                await process(
                    ChunkPlan(
                        file_id=str(group[0]["file_id"]),
                        segments=tuple(group),
                        payload={},
                        estimated_input_tokens=0,
                    ),
                    exc.request_id,
                )
                for group in groups
            ]
            return sum(item[0] for item in values), sum(item[1] for item in values)

    try:
        async with LLMClient(
            config,
            limiter,
            run_dir=run_dir,
            project_id=str(metadata["project_id"]),
            run_id=run_id,
            stage="terminology",
            client=http_client,
        ) as llm:
            async with write_lock:
                for segment in preflight_failed:
                    segment_id = str(segment["segment_id"])
                    failed_originals.add(segment_id)
                    append_jsonl(
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
            results = await dispatch_chunks(
                chunks,
                process,
                mode=config["execution"]["scheduling_mode"],
                max_parallel=config["execution"]["max_parallel"],
            )
        _extend_unique(warnings, llm.warnings)
    except FatalExternalError:
        finalize_run(run_dir, status="failed", completed=0, failed=len(work))
        logger.error("run failed run=%s fatal_external_error=true", run_id)
        raise

    completed = sum(value[0] for value in results)
    failed = len(failed_originals)
    all_nonempty = [segment for segment in segments if not segment["is_empty"]]
    task_scans = [
        record
        for record in read_jsonl(project / "terminology" / "scans.jsonl")
        if record.get("active_task_id") == task_id
    ]
    task_completed_ids = {
        str(record["segment_id"])
        for record in task_scans
        if record.get("status") == "completed"
    }
    published_now = False
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
    finalize_run(
        run_dir,
        status="completed" if failed == 0 else "failed",
        completed=completed,
        failed=failed,
        warnings=warnings,
    )
    logger.info(
        "run complete run=%s completed=%d failed=%d pending=%d",
        run_id,
        completed,
        failed,
        len(all_nonempty) - len(task_completed_ids),
    )
    return {
        "stage": "terminology",
        "run_id": run_id,
        "active_task_id": task_id,
        "completed": completed,
        "failed": failed,
        "pending": len(all_nonempty) - len(task_completed_ids),
        "published": published_now,
        "terms_revision": published["terms_revision"] if published else None,
        "warnings": warnings,
    }


def match_terms(source: str, library: dict[str, Any] | None, limit: int) -> list[dict]:
    if library is None:
        return []
    normalized_source = normalize_term(source)
    matched: list[tuple[int, int, int, dict[str, Any]]] = []
    for term in library.get("terms", []):
        main_name = normalize_term(str(term.get("source", "")))
        alias_names = [
            normalize_term(str(name)) for name in term.get("aliases", []) if name
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


async def run_translation(
    project: Path,
    scope: Scope,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    logger = get_logger("translation")
    config, metadata, files, segments = _project_context(project, dry_run=scope.dry_run)
    prompt = _prompt(project, "translation")
    library = load_terms(project)
    terms_revision = int(library["terms_revision"]) if library else None
    fingerprint = stage_fingerprint(
        config, "translation", prompt, terms_revision=terms_revision
    )
    history = load_stage_history(
        project, "translation", repair_tail=not scope.dry_run
    )
    selected_segments = select_scope(segments, files, scope)
    selection = classify_stage(selected_segments, history, force=scope.force)
    warnings: list[str] = []
    configured_output_warning = _configured_output_warning(config)
    if configured_output_warning:
        warnings.append(configured_output_warning)
    if library is None:
        warnings.append("没有已发布术语库；本次翻译 terms_revision = null")
    if selection.fingerprints and selection.fingerprints != {fingerprint}:
        warnings.append("翻译结果包含不同设置指纹；继续复用并处理 pending")
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
            )
            if context_config["enabled"]
            else []
        )
        terms_by_source: dict[str, dict[str, Any]] = {}
        for item in items:
            for term in match_terms(
                str(item["source"]),
                library,
                config["terminology"]["max_terms_per_segment"],
            ):
                terms_by_source[str(term["source"])] = term
        return {
            "target_language": config["project"]["target_language"],
            "reference_context": context,
            "terms": list(terms_by_source.values()),
            "segments": [
                {"id": item["segment_id"], "source": item["source"]} for item in items
            ],
        }

    request_segments: list[dict[str, Any]] = []
    part_original: dict[str, str] = {}
    original_parts: dict[str, list[str]] = {}
    preflight_failed: list[dict[str, Any]] = []
    for segment in selection.work:
        try:
            build_chunk_plans(
                [segment],
                all_segments=segments,
                config=config,
                prompt=prompt,
                payload_builder=payload_builder,
            )
            request_segments.append(segment)
            continue
        except ConfigError as exc:
            if "单 Segment Prompt 超过模型硬限制" not in str(exc):
                raise
            if not config["chunking"]["allow_split_oversized_segment"]:
                preflight_failed.append(segment)
                continue
        sources = [str(segment["source"])]
        accepted_sources: list[str] = []
        while sources:
            source_part = sources.pop(0)
            probe = {
                **segment,
                "segment_id": f"{segment['segment_id']}-PROBE",
                "source": source_part,
            }
            try:
                build_chunk_plans(
                    [probe],
                    all_segments=segments,
                    config=config,
                    prompt=prompt,
                    payload_builder=payload_builder,
                )
                accepted_sources.append(source_part)
            except ConfigError as exc:
                if "单 Segment Prompt 超过模型硬限制" not in str(exc):
                    raise
                left, right = _split_source_once(source_part)
                sources[0:0] = [left, right]
        part_ids: list[str] = []
        for index, source_part in enumerate(accepted_sources, start=1):
            part_id = f"{segment['segment_id']}-P{index:03d}"
            part = {
                **segment,
                "segment_id": part_id,
                "source": source_part,
            }
            request_segments.append(part)
            part_original[part_id] = str(segment["segment_id"])
            part_ids.append(part_id)
        original_parts[str(segment["segment_id"])] = part_ids

    plans = build_chunk_plans(
        request_segments,
        all_segments=segments,
        config=config,
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
    if scope.dry_run:
        return {
            "stage": "translation",
            "dry_run": True,
            "selected": len(selection.selected),
            "requested": len(selection.work),
            "reused": len(selection.reusable),
            "chunks": len(plans),
            "estimated_input_tokens": sum(
                plan.estimated_input_tokens for plan in plans
            ),
            "warnings": warnings,
        }

    run_id, run_dir = create_run(
        project,
        stage="translation",
        fingerprint=fingerprint,
        prompt=prompt,
        selected_count=len(selection.selected),
        requested_count=len(selection.work),
        reused_count=len(selection.reusable),
        details={
            "terms_revision": terms_revision,
            "scope": _scope_record(scope),
        },
    )
    logger.info("run start run=%s", run_id)
    chunks = materialize_chunks(run_id, "translation", plans)
    if config["debug"]["enabled"]:
        save_debug_chunks(
            run_dir,
            str(metadata["project_id"]),
            run_id,
            "translation",
            chunks,
        )
    limiter = SlidingWindowLimiter(
        config["execution"]["requests_per_minute"],
        config["execution"]["input_tokens_per_minute"],
    )
    result_path = stage_result_path(project, "translation")
    write_lock = asyncio.Lock()
    validation_pending: dict[str, dict[str, Any]] = {}
    failed_ids: set[str] = set()
    completed_ids: set[str] = set()
    by_id = {str(item["segment_id"]): item for item in segments}
    by_id.update(
        {str(item["segment_id"]): item for item in request_segments}
    )
    part_results: dict[str, dict[str, tuple[str, str]]] = {}

    async def save_completed(
        segment_id: str,
        text: str,
        request_id: str,
        *,
        validation_status: str = "passed",
        findings: list[dict[str, Any]] | None = None,
    ) -> None:
        async with write_lock:
            append_jsonl(
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
        async with write_lock:
            append_jsonl(
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

    async def request_translations(
        group: list[dict[str, Any]],
        *,
        repair_candidates: dict[str, dict[str, Any]] | None = None,
        initial_parent_request_id: str | None = None,
    ) -> tuple[dict[str, tuple[str, str]], list[str]]:
        valid_total: dict[str, tuple[str, str]] = {}
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
                        "source": item["source"],
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
                payload["format_correction"] = (
                    "上一次响应不符合 JSONL 协议或缺少 Segment。只返回未决 "
                    "ID，每行一个紧凑 JSON 对象，最后输出 {\"type\":\"end\"}。"
                )
            messages = render_messages(prompt, payload)
            request_id = f"REQ-{uuid.uuid4().hex[:12].upper()}"
            estimated = _request_estimate(messages, config, request_id)
            try:
                content, _ = await llm.chat(
                    messages=messages,
                    temperature=config["llm"]["temperature_translation"],
                    estimated_input_tokens=estimated,
                    request_id=request_id,
                    parent_request_id=parent_request_id,
                )
                valid, unresolved, parse_errors, response_complete = (
                    _parse_translation_items(content, expected)
                )
            except FatalExternalError:
                raise
            except ContextLengthError:
                raise
            except ExternalError as exc:
                for segment_id in expected:
                    await save_failed(
                        segment_id,
                        request_id,
                        "external_error",
                        str(exc),
                    )
                continue
            for segment_id, text in valid.items():
                valid_total[segment_id] = (text, request_id)
                await accept_candidate(segment_id, text, request_id)
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
        return valid_total, list(dict.fromkeys(exhausted))

    async def process_once(
        chunk: ChunkPlan,
        initial_parent_request_id: str | None = None,
    ) -> None:
        group = list(chunk.segments)
        valid, unresolved = await request_translations(
            group,
            initial_parent_request_id=initial_parent_request_id,
        )
        for segment_id in unresolved:
            await save_failed(
                segment_id,
                f"REQ-{uuid.uuid4().hex[:12].upper()}",
                "format_error",
                "格式修正次数耗尽",
            )

    async def process(
        chunk: ChunkPlan,
        split_parent_request_id: str | None = None,
    ) -> None:
        try:
            await process_once(chunk, split_parent_request_id)
            return
        except ContextLengthError as exc:
            logger.warning(
                "context split parent_request=%s segments=%d",
                exc.request_id,
                len(chunk.segments),
            )
            items = list(chunk.segments)
            if len(items) > 1:
                midpoint = len(items) // 2
                groups = (items[:midpoint], items[midpoint:])
            elif (
                config["chunking"]["allow_split_oversized_segment"]
                and len(str(items[0]["source"])) > 1
            ):
                groups = tuple(
                    [part]
                    for part in _replace_with_runtime_parts(
                        items[0],
                        part_original=part_original,
                        original_parts=original_parts,
                        by_id=by_id,
                    )
                )
            else:
                groups = ()
            if not groups:
                await save_failed(
                    str(items[0]["segment_id"]),
                    "REQ-NONE",
                    "context_error",
                    "模型报告上下文过长",
                )
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

    async def repair_group(
        group: list[dict[str, Any]],
        subset: dict[str, dict[str, Any]],
        parent_request_id: str | None = None,
    ) -> None:
        try:
            valid, unresolved = await request_translations(
                group,
                repair_candidates=subset,
                initial_parent_request_id=parent_request_id,
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
        for segment_id in unresolved:
            validation_pending[segment_id] = subset[segment_id]

    try:
        async with LLMClient(
            config,
            limiter,
            run_dir=run_dir,
            project_id=str(metadata["project_id"]),
            run_id=run_id,
            stage="translation",
            client=http_client,
        ) as llm:
            for segment in preflight_failed:
                await save_failed(
                    str(segment["segment_id"]),
                    f"REQ-{uuid.uuid4().hex[:12].upper()}",
                    "context_error",
                    "单 Segment 超过模型限制且内部拆分已关闭",
                )
            await dispatch_chunks(
                chunks,
                process,
                mode=config["execution"]["scheduling_mode"],
                max_parallel=config["execution"]["max_parallel"],
            )
            max_repairs = config["validation"]["translation"]["max_retry_attempts"]
            for repair_attempt in range(1, max_repairs + 1):
                if not validation_pending:
                    break
                current_pending = dict(validation_pending)
                validation_pending.clear()
                groups = contiguous_groups(
                    (item["segment"] for item in current_pending.values()),
                    all_segments=segments,
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
        _extend_unique(warnings, llm.warnings)
    except FatalExternalError:
        finalize_run(
            run_dir,
            status="failed",
            completed=len(completed_ids),
            failed=len(selection.work) - len(completed_ids),
            warnings=warnings,
        )
        logger.error(
            "run failed run=%s completed=%d fatal_external_error=true",
            run_id,
            len(completed_ids),
        )
        raise

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
    failed_count = len(failed_ids)
    finalize_run(
        run_dir,
        status="completed" if failed_count == 0 else "failed",
        completed=len(completed_ids),
        failed=failed_count,
        warnings=warnings,
    )
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
        "last_attempt_failed": len(selection.last_attempt_failed),
        "warnings": warnings,
    }


def require_success(summary: dict[str, Any]) -> None:
    if summary.get("failed") or summary.get("pending"):
        raise IncompleteError("选定范围仍有 pending 或 failed")


def _base_results(
    project: Path,
    stage: str,
    *,
    repair_tail: bool,
) -> dict[str, dict[str, Any]]:
    translations = {
        str(key): value
        for key, value in classify_stage(
            [],
            load_stage_history(
                project, "translation", repair_tail=repair_tail
            ),
            force=False,
        ).latest_completed.items()
    }
    if stage == "proofreading":
        return translations
    applied = classify_stage(
        [],
        load_stage_history(
            project, "proofreading_applied", repair_tail=repair_tail
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
        suggested = item.get("suggested_text")
        reason = item.get("reason")
        if segment_id not in expected or counts[segment_id] != 1:
            errors.append(f"未知或重复 ID：{segment_id}")
            continue
        if review_status not in {"accepted", "suggested"}:
            errors.append(f"status 字段错误：{segment_id}")
            continue
        if review_status == "accepted" and suggested is not None:
            errors.append(f"accepted 不应包含建议文本：{segment_id}")
            continue
        if review_status == "suggested" and (
            not isinstance(suggested, str) or not suggested
        ):
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


async def run_review(
    project: Path,
    stage: str,
    scope: Scope,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    if stage not in {"proofreading", "polishing"}:
        raise ValueError(f"unsupported review stage: {stage}")
    logger = get_logger(stage)
    config, metadata, files, segments = _project_context(project, dry_run=scope.dry_run)
    prompt = _prompt(project, stage)
    library = load_terms(project)
    terms_revision = int(library["terms_revision"]) if library else None
    fingerprint = stage_fingerprint(
        config, stage, prompt, terms_revision=terms_revision
    )
    selected_segments = select_scope(segments, files, scope)
    bases = _base_results(project, stage, repair_tail=not scope.dry_run)
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
    history = load_stage_history(project, stage, repair_tail=not scope.dry_run)
    selection = classify_stage(selected_segments, history, force=scope.force)
    warnings: list[str] = []
    configured_output_warning = _configured_output_warning(config)
    if configured_output_warning:
        warnings.append(configured_output_warning)
    if missing_base:
        warnings.append(
            f"{stage} dry-run 使用源文占位估算；"
            f"实际运行仍缺少 {len(missing_base)} 条上游结果"
        )
    if selection.fingerprints and selection.fingerprints != {fingerprint}:
        warnings.append(f"{stage} 结果包含不同设置指纹；继续复用并处理 pending")
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
            )
            if context_config["enabled"]
            else []
        )
        terms_by_source: dict[str, dict[str, Any]] = {}
        for item in items:
            for term in match_terms(
                str(item["source"]),
                library,
                config["terminology"]["max_terms_per_segment"],
            ):
                terms_by_source[str(term["source"])] = term
        return {
            "target_language": config["project"]["target_language"],
            "reference_context": context,
            "terms": list(terms_by_source.values()),
            "segments": [
                {
                    "id": item["segment_id"],
                    "source": item["source"],
                    "current_text": bases[str(item["segment_id"])]["text"],
                }
                for item in items
            ],
        }

    request_segments: list[dict[str, Any]] = []
    part_original: dict[str, str] = {}
    original_parts: dict[str, list[str]] = {}
    preflight_failed: list[dict[str, Any]] = []
    for segment in selection.work:
        try:
            build_chunk_plans(
                [segment],
                all_segments=segments,
                config=config,
                prompt=prompt,
                payload_builder=payload_builder,
            )
            request_segments.append(segment)
            continue
        except ConfigError as exc:
            if "单 Segment Prompt 超过模型硬限制" not in str(exc):
                raise
            if not config["chunking"]["allow_split_oversized_segment"]:
                preflight_failed.append(segment)
                continue
        pending_parts = [
            (str(segment["source"]), str(bases[str(segment["segment_id"])]["text"]))
        ]
        accepted_parts: list[tuple[str, str]] = []
        while pending_parts:
            source_part, text_part = pending_parts.pop(0)
            probe_id = f"{segment['segment_id']}-PROBE"
            probe = {**segment, "segment_id": probe_id, "source": source_part}
            bases[probe_id] = {
                "record_id": bases[str(segment["segment_id"])]["record_id"],
                "text": text_part,
            }
            try:
                build_chunk_plans(
                    [probe],
                    all_segments=segments,
                    config=config,
                    prompt=prompt,
                    payload_builder=payload_builder,
                )
                accepted_parts.append((source_part, text_part))
            except ConfigError as exc:
                if "单 Segment Prompt 超过模型硬限制" not in str(exc):
                    raise
                left_source, right_source = _split_source_once(source_part)
                split_at = round(len(text_part) * len(left_source) / len(source_part))
                pending_parts[0:0] = [
                    (left_source, text_part[:split_at]),
                    (right_source, text_part[split_at:]),
                ]
            finally:
                bases.pop(probe_id, None)
        part_ids: list[str] = []
        for index, (source_part, text_part) in enumerate(accepted_parts, start=1):
            part_id = f"{segment['segment_id']}-P{index:03d}"
            request_segments.append(
                {**segment, "segment_id": part_id, "source": source_part}
            )
            bases[part_id] = {
                "record_id": bases[str(segment["segment_id"])]["record_id"],
                "text": text_part,
            }
            part_original[part_id] = str(segment["segment_id"])
            part_ids.append(part_id)
        original_parts[str(segment["segment_id"])] = part_ids

    plans = build_chunk_plans(
        request_segments,
        all_segments=segments,
        config=config,
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
    if scope.dry_run:
        return {
            "stage": stage,
            "dry_run": True,
            "selected": len(selection.selected),
            "requested": len(selection.work),
            "reused": len(selection.reusable),
            "chunks": len(plans),
            "estimated_input_tokens": sum(
                plan.estimated_input_tokens for plan in plans
            ),
            "warnings": warnings,
        }
    run_id, run_dir = create_run(
        project,
        stage=stage,
        fingerprint=fingerprint,
        prompt=prompt,
        selected_count=len(selection.selected),
        requested_count=len(selection.work),
        reused_count=len(selection.reusable),
        details={
            "terms_revision": terms_revision,
            "scope": _scope_record(scope),
        },
    )
    logger.info("run start run=%s", run_id)
    chunks = materialize_chunks(run_id, stage, plans)
    if config["debug"]["enabled"]:
        save_debug_chunks(
            run_dir,
            str(metadata["project_id"]),
            run_id,
            stage,
            chunks,
        )
    limiter = SlidingWindowLimiter(
        config["execution"]["requests_per_minute"],
        config["execution"]["input_tokens_per_minute"],
    )
    result_path = stage_result_path(project, stage)
    write_lock = asyncio.Lock()
    completed_ids: set[str] = set()
    failed_ids: set[str] = set()
    by_id = {str(item["segment_id"]): item for item in segments}
    by_id.update(
        {str(item["segment_id"]): item for item in request_segments}
    )
    part_results: dict[str, dict[str, tuple[dict[str, Any], str]]] = {}

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
                append_jsonl(
                    result_path,
                    record_header(
                        "stage_result",
                        str(metadata["project_id"]),
                        stage=stage,
                        segment_id=segment_id,
                        status="completed",
                        review_status=parsed["review_status"],
                        suggested_text=parsed["suggested_text"],
                        reason=parsed["reason"],
                        base_result_id=base["record_id"],
                        stage_fingerprint=fingerprint,
                        terms_revision=terms_revision,
                        run_id=run_id,
                        request_id=request_id,
                    ),
                )
                completed_ids.add(segment_id)
                logger.info(
                    "segment complete segment=%s completed=%d failed=%d",
                    segment_id,
                    len(completed_ids),
                    len(failed_ids),
                )
            else:
                append_jsonl(
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

    async def process_once(
        chunk: ChunkPlan,
        initial_parent_request_id: str | None = None,
    ) -> None:
        exhausted: list[str] = []
        tasks: list[
            tuple[
                list[dict[str, Any]],
                str | None,
                int,
                list[dict[str, Any]],
            ]
        ] = [
            (
                list(chunk.segments),
                initial_parent_request_id,
                0,
                list(chunk.segments[:1]),
            )
        ]
        while tasks:
            items, parent_request_id, format_attempt, anchor = tasks.pop(0)
            expected = [str(item["segment_id"]) for item in items]
            payload = payload_builder(items or anchor)
            if not items:
                payload["segments"] = []
            if format_attempt:
                payload["format_correction"] = (
                    "上一次响应不符合 JSONL 协议或缺少 Segment。只返回未决 "
                    "ID，每行一个紧凑 JSON 对象，最后输出 {\"type\":\"end\"}。"
                )
            messages = render_messages(prompt, payload)
            request_id = f"REQ-{uuid.uuid4().hex[:12].upper()}"
            estimated = _request_estimate(messages, config, request_id)
            try:
                content, _ = await llm.chat(
                    messages=messages,
                    temperature=config["llm"][f"temperature_{stage}"],
                    estimated_input_tokens=estimated,
                    request_id=request_id,
                    parent_request_id=parent_request_id,
                )
                valid, unresolved, parse_errors, response_complete = (
                    _parse_review_items(content, expected)
                )
            except FatalExternalError:
                raise
            except ContextLengthError:
                raise
            except ExternalError as exc:
                for segment_id in expected:
                    await save_result(segment_id, request_id, error=str(exc))
                continue
            for segment_id, parsed in valid.items():
                await accept_result(segment_id, request_id, parsed)
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
        for segment_id in dict.fromkeys(exhausted):
            await save_result(
                segment_id,
                "REQ-NONE",
                error="格式修正次数耗尽",
            )

    async def process(
        chunk: ChunkPlan,
        split_parent_request_id: str | None = None,
    ) -> None:
        try:
            await process_once(chunk, split_parent_request_id)
            return
        except ContextLengthError as exc:
            logger.warning(
                "context split parent_request=%s segments=%d",
                exc.request_id,
                len(chunk.segments),
            )
            items = list(chunk.segments)
            if len(items) > 1:
                midpoint = len(items) // 2
                groups = (items[:midpoint], items[midpoint:])
            elif (
                config["chunking"]["allow_split_oversized_segment"]
                and len(str(items[0]["source"])) > 1
            ):
                groups = tuple(
                    [part]
                    for part in _replace_with_runtime_parts(
                        items[0],
                        part_original=part_original,
                        original_parts=original_parts,
                        by_id=by_id,
                        bases=bases,
                    )
                )
            else:
                groups = ()
            if not groups:
                await save_result(
                    str(items[0]["segment_id"]),
                    "REQ-NONE",
                    error="模型报告上下文过长",
                )
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

    try:
        async with LLMClient(
            config,
            limiter,
            run_dir=run_dir,
            project_id=str(metadata["project_id"]),
            run_id=run_id,
            stage=stage,
            client=http_client,
        ) as llm:
            for segment in preflight_failed:
                await save_result(
                    str(segment["segment_id"]),
                    "REQ-NONE",
                    error="单 Segment 超过模型限制且内部拆分已关闭",
                )
            await dispatch_chunks(
                chunks,
                process,
                mode=config["execution"]["scheduling_mode"],
                max_parallel=config["execution"]["max_parallel"],
            )
        _extend_unique(warnings, llm.warnings)
    except FatalExternalError:
        finalize_run(
            run_dir,
            status="failed",
            completed=len(completed_ids),
            failed=len(selection.work) - len(completed_ids),
            warnings=warnings,
        )
        logger.error(
            "run failed run=%s completed=%d fatal_external_error=true",
            run_id,
            len(completed_ids),
        )
        raise
    finalize_run(
        run_dir,
        status="completed" if not failed_ids else "failed",
        completed=len(completed_ids),
        failed=len(failed_ids),
        warnings=warnings,
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
        "warnings": warnings,
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
    config, metadata, files, segments = _project_context(project, dry_run=scope.dry_run)
    selected = select_scope(segments, files, scope)
    suggestions = classify_stage(
        selected,
        load_stage_history(
            project, review_stage, repair_tail=not scope.dry_run
        ),
        force=False,
    ).latest_completed
    bases = _base_results(project, review_stage, repair_tail=not scope.dry_run)
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
        append_jsonl(
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


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def export_project(
    project: Path,
    export_stage: str,
    *,
    bilingual: bool,
    allow_missing: bool,
) -> dict[str, Any]:
    if export_stage not in {"translated", "proofread", "polished"}:
        raise ValueError(f"unsupported export stage: {export_stage}")
    logger = get_logger("export")
    config, _, files, segments = _project_context(project, dry_run=False)
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
    by_file: dict[str, list[dict[str, Any]]] = {}
    for segment in segments:
        by_file.setdefault(str(segment["file_id"]), []).append(segment)
    written: list[str] = []
    encoding = config["project"]["output_encoding"]
    for file_record in sorted(files, key=lambda item: int(item["file_order"])):
        lines: list[str] = []
        for segment in sorted(
            by_file[str(file_record["file_id"])],
            key=lambda item: int(item["line_index"]),
        ):
            if segment["is_empty"]:
                lines.append("")
            elif bilingual:
                lines.append(str(segment["source"]))
                lines.append(output_text[str(segment["segment_id"])])
            else:
                lines.append(output_text[str(segment["segment_id"])])
        relative = Path(str(file_record["original_name"]))
        destination = directory / relative
        try:
            payload = "\n".join(lines).encode(encoding, errors="strict")
        except (LookupError, UnicodeEncodeError) as exc:
            raise IncompleteError(
                f"输出编码 {encoding} 无法表示 {relative}: {exc}"
            ) from exc
        _atomic_write_bytes(destination, payload)
        written.append(str(destination.relative_to(project)))
        logger.info("file written path=%s", destination.relative_to(project))
    logger.info(
        "export complete stage=%s files=%d fallback_segments=%d",
        export_stage,
        len(written),
        len(fallback_records),
    )
    return {
        "stage": export_stage,
        "bilingual": bilingual,
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
) -> dict[str, Any]:
    summaries: list[dict[str, Any]] = []
    terms = load_terms(project)
    active_path = project / "terminology" / "active_task.json"
    active = read_json(active_path) if active_path.exists() else None
    if scope.force or terms is None or (active and active.get("status") == "active"):
        term_summary = await run_terminology(
            project, scope, http_client=http_client
        )
        summaries.append(term_summary)
        require_success(term_summary)
    translation = await run_translation(project, scope, http_client=http_client)
    summaries.append(translation)
    require_success(translation)
    proofreading = await run_review(
        project, "proofreading", scope, http_client=http_client
    )
    summaries.append(proofreading)
    require_success(proofreading)
    polishing = await run_review(
        project, "polishing", scope, http_client=http_client
    )
    summaries.append(polishing)
    require_success(polishing)
    return {"stage": "run-all", "steps": summaries, "failed": 0, "pending": 0}


def inspect_full(project: Path, *, dry_run: bool = False) -> dict[str, Any]:
    config, metadata, files, segments = _project_context(project, dry_run=dry_run)
    nonempty = [item for item in segments if not item["is_empty"]]
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
    }
    library = load_terms(project)
    terms_revision = int(library["terms_revision"]) if library else None
    current_term_fingerprint = stage_fingerprint(
        config,
        "terminology",
        _prompt(project, "terminology"),
    )
    summary["terminology"]["current_fingerprint"] = current_term_fingerprint
    if library:
        summary["terms_revision"] = library["terms_revision"]
    active_path = project / "terminology" / "active_task.json"
    if active_path.exists():
        active = read_json(active_path)
        if active.get("status") == "active":
            scans = [
                item
                for item in read_jsonl(
                    project / "terminology" / "scans.jsonl",
                    repair_tail=not dry_run,
                )
                if item.get("active_task_id") == active.get("active_task_id")
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
        history = load_stage_history(project, stage, repair_tail=not dry_run)
        histories[stage] = history
        completed = classify_stage([], history, force=False).latest_completed
        failed = {
            str(item["segment_id"])
            for item in history
            if item.get("status") == "failed"
            and str(item.get("segment_id")) not in completed
        }
        latest_failed_after_success = 0
        by_segment: dict[str, list[dict[str, Any]]] = {}
        for item in history:
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
                config,
                stage,
                _prompt(project, stage),
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
        bases = _base_results(project, review_stage, repair_tail=not dry_run)
        summary["outdated_suggestions"][review_stage] = sum(
            suggestion.get("base_result_id")
            != bases.get(segment_id, {}).get("record_id")
            for segment_id, suggestion in suggestions.items()
        )
    project_arg = shlex.quote(str(project))
    if summary["terminology"]["pending"] or summary["terminology"]["failed"]:
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
