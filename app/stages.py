from __future__ import annotations

import asyncio
import json
import re
import unicodedata
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

from .config import load_config
from .errors import (
    ConfigError,
    ExternalError,
    FatalExternalError,
    IncompleteError,
    StorageError,
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
    previous_context,
    render_messages,
    save_debug_chunks,
    select_scope,
    stage_fingerprint,
    stage_result_path,
)
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


def _log(project: Path, level: str, message: str) -> None:
    path = project / "logs" / "app.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{level} {message}\n")


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


def _validate_term_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or not isinstance(value.get("terms"), list):
        raise ValueError("术语响应必须包含 terms 数组")
    terms: list[dict[str, Any]] = []
    for item in value["terms"]:
        if not isinstance(item, dict):
            raise ValueError("术语项必须是对象")
        for key in ("source", "category", "description"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError(f"术语项缺少有效 {key}")
        preferred = item.get("preferred_translation")
        if preferred is not None and not isinstance(preferred, str):
            raise ValueError("preferred_translation 必须是字符串或 null")
        aliases = item.get("aliases", [])
        if not isinstance(aliases, list) or not all(
            isinstance(alias, str) for alias in aliases
        ):
            raise ValueError("aliases 必须是字符串数组")
        terms.append(
            {
                "source": item["source"].strip(),
                "category": item["category"].strip(),
                "description": item["description"].strip(),
                "preferred_translation": preferred.strip() if preferred else None,
                "aliases": [alias.strip() for alias in aliases if alias.strip()],
            }
        )
    return terms


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
    if existing_fingerprints and existing_fingerprints != {fingerprint}:
        warnings.append("活动术语任务包含不同设置指纹；继续复用并处理 pending")

    context_config = config["context"]["terminology"]

    def payload_builder(items: list[dict[str, Any]]) -> dict[str, Any]:
        context = (
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
            "reference_context": context,
            "segments": [
                {"id": item["segment_id"], "source": item["source"]} for item in items
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
        config=config,
        prompt=prompt,
        payload_builder=payload_builder,
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
    )
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

    async def process(chunk: ChunkPlan) -> tuple[int, int]:
        unresolved = list(chunk.segments)
        parent_request_id: str | None = None
        for format_attempt in range(config["retry"]["format_max_attempts"] + 1):
            payload = payload_builder(unresolved)
            if format_attempt:
                payload["format_correction"] = (
                    "上一次响应格式错误。只返回合法 JSON，不要解释。"
                )
            messages = render_messages(prompt, payload)
            estimated = estimate_messages(
                messages, config["execution"]["token_safety_factor"]
            )
            request_id = f"REQ-{uuid.uuid4().hex[:12].upper()}"
            try:
                content, _ = await llm.chat(
                    messages=messages,
                    temperature=config["llm"]["temperature_terminology"],
                    estimated_input_tokens=estimated,
                    request_id=request_id,
                    parent_request_id=parent_request_id,
                )
                terms = _validate_term_items(json.loads(content))
            except FatalExternalError:
                raise
            except (ExternalError, ValueError, json.JSONDecodeError) as exc:
                if format_attempt < config["retry"]["format_max_attempts"] and not isinstance(
                    exc, ExternalError
                ):
                    parent_request_id = request_id
                    continue
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
                                error_class=type(exc).__name__,
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
            return len(completed_originals), 0
        return 0, len(unresolved)

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
    except FatalExternalError:
        finalize_run(run_dir, status="failed", completed=0, failed=len(work))
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
    _log(project, "INFO", f"terminology run={run_id} completed={completed} failed={failed}")
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
    matched: list[tuple[int, int, dict[str, Any]]] = []
    for term in library.get("terms", []):
        names = [term.get("source", ""), *term.get("aliases", [])]
        normalized_names = [normalize_term(str(name)) for name in names if name]
        hits = [name for name in normalized_names if name and name in normalized_source]
        if not hits:
            continue
        matched.append(
            (
                max(len(name) for name in hits),
                1 if term.get("preferred_translation") else 0,
                term,
            )
        )
    matched.sort(key=lambda item: (-item[0], -item[1], item[2].get("source", "")))
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
        for _, _, term in matched[:limit]
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


def _parse_translation_items(
    content: str, expected_ids: list[str]
) -> tuple[dict[str, str], list[str], list[str]]:
    value = json.loads(content)
    if not isinstance(value, dict) or not isinstance(value.get("segments"), list):
        raise ValueError("翻译响应必须包含 segments 数组")
    counts = Counter(
        item.get("id")
        for item in value["segments"]
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    )
    expected = set(expected_ids)
    valid: dict[str, str] = {}
    errors: list[str] = []
    for item in value["segments"]:
        if not isinstance(item, dict):
            errors.append("非对象 Segment")
            continue
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
    return valid, unresolved, errors


async def run_translation(
    project: Path,
    scope: Scope,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
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
        config=config,
        prompt=prompt,
        payload_builder=payload_builder,
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
    )
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
    ) -> tuple[dict[str, tuple[str, str]], list[str]]:
        expected = [str(item["segment_id"]) for item in group]
        unresolved = expected
        valid_total: dict[str, tuple[str, str]] = {}
        parent_request_id: str | None = None
        for format_attempt in range(config["retry"]["format_max_attempts"] + 1):
            unresolved_items = [by_id[segment_id] for segment_id in unresolved]
            payload = payload_builder(unresolved_items)
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
                    for item in unresolved_items
                ]
                payload["validation_repair"] = (
                    "返回不含所列残留字符的完整修正版译文。"
                )
            if format_attempt:
                payload["format_correction"] = (
                    "上一次响应格式错误或缺少 Segment。只返回全部未决 ID。"
                )
            messages = render_messages(prompt, payload)
            estimated = estimate_messages(
                messages, config["execution"]["token_safety_factor"]
            )
            request_id = f"REQ-{uuid.uuid4().hex[:12].upper()}"
            try:
                content, _ = await llm.chat(
                    messages=messages,
                    temperature=config["llm"]["temperature_translation"],
                    estimated_input_tokens=estimated,
                    request_id=request_id,
                    parent_request_id=parent_request_id,
                )
                valid, unresolved, _ = _parse_translation_items(content, unresolved)
            except FatalExternalError:
                raise
            except ExternalError as exc:
                for segment_id in unresolved:
                    await save_failed(
                        segment_id, request_id, type(exc).__name__, str(exc)
                    )
                return valid_total, []
            except (ValueError, json.JSONDecodeError):
                valid = {}
            for segment_id, text in valid.items():
                valid_total[segment_id] = (text, request_id)
            if not unresolved:
                return valid_total, []
            parent_request_id = request_id
        return valid_total, unresolved

    async def process(chunk: ChunkPlan) -> None:
        group = list(chunk.segments)
        valid, unresolved = await request_translations(group)
        for segment_id, (text, request_id) in valid.items():
            await accept_candidate(segment_id, text, request_id)
        for segment_id in unresolved:
            await save_failed(
                segment_id,
                f"REQ-{uuid.uuid4().hex[:12].upper()}",
                "format_error",
                "格式修正次数耗尽",
            )

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
            for _ in range(max_repairs):
                if not validation_pending:
                    break
                current_pending = dict(validation_pending)
                validation_pending.clear()
                groups = contiguous_groups(
                    item["segment"] for item in current_pending.values()
                )
                for group in groups:
                    subset = {
                        str(item["segment_id"]): current_pending[
                            str(item["segment_id"])
                        ]
                        for item in group
                    }
                    valid, unresolved = await request_translations(
                        group, repair_candidates=subset
                    )
                    for segment_id, (text, request_id) in valid.items():
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
                    for segment_id in unresolved:
                        validation_pending[segment_id] = subset[segment_id]
    except FatalExternalError:
        finalize_run(
            run_dir,
            status="failed",
            completed=len(completed_ids),
            failed=len(selection.work) - len(completed_ids),
            warnings=warnings,
        )
        raise

    exhausted_mode = config["validation"]["translation"]["exhausted_mode"]
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
    _log(
        project,
        "INFO",
        f"translation run={run_id} completed={len(completed_ids)} failed={failed_count}",
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
