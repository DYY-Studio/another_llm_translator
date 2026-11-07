from __future__ import annotations

import asyncio
from collections import defaultdict
from copy import deepcopy
import json
import unicodedata
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from .async_utils import run_blocking
from .errors import AppError, IncompleteError, UsageError
from .execution import (
    Scope,
    _interrupt_run,
    classify_stage_states,
    continue_run,
    create_run,
    finalize_run,
    find_running_runs,
    scope_from_run,
    segment_model_source,
    select_scope,
    stage_fingerprint,
    stage_result_path,
    unavailable_usage,
)
from .locking import project_write_lock
from .plugins import normalize_model_text
from .project import load_segments, load_source_files
from .repetition_consistency import (
    current_draft as current_repetition_draft,
    draft_path as repetition_draft_path,
    parse_comparison,
    read_draft as read_repetition_draft,
    repetition_plan,
)
from .sqlite_storage import (
    append_jsonl,
    append_jsonl_records,
    append_stage_results,
    atomic_write_text,
    latest_stage_states,
    query_segment_neighbors,
    read_json,
    read_repetition_run,
    record_exists,
    record_header,
    segment_ids as query_segment_ids,
    terminology_scan_state,
    utc_now,
    write_json,
    write_repetition_state,
)
from .stage_runtime import (
    _assemble_warnings,
    _base_results,
    _document_prompt_requirement_helpers,
    _frozen_run_options,
    _project_context,
    _prompt_factory,
    _prompt_language,
    _require_nonempty_segments,
    _scope_record,
    document_prompt_context,
    document_prompt_requirements,
    prompt_middle_digests,
)
from .term_library import load_terms, term_normalization
from .term_matching import _TermMatchCache
from .translation_validation import validate_translation_text
from .web_store import REVIEW_STAGES, WebStore
from .web_task_routes import _safe_run_id

AGENT_STAGES = ("translation", "proofreading", "polishing")


def _repair_guidance(
    findings: list[dict[str, Any]],
    validators: tuple[Any, ...],
    language: str,
) -> list[str]:
    by_id = {v.validator_id: v for v in validators}
    instructions: list[str] = []
    for finding in findings:
        validator = by_id.get(str(finding.get("validator", "")))
        if validator is None:
            continue
        text = validator.retry_instruction(str(finding.get("match_type", "")), language)
        if text and text not in instructions:
            instructions.append(text)
    return instructions


def _parse_scope(root: Path, payload: dict[str, Any]) -> Scope:
    raw = payload.get("scope") or {}
    if not isinstance(raw, dict):
        raise UsageError("scope 必须是对象")
    segment_ids = raw.get("segment_ids")
    if segment_ids is not None and (
        not isinstance(segment_ids, list)
        or not segment_ids
        or not all(isinstance(item, str) and item for item in segment_ids)
    ):
        raise UsageError("scope.segment_ids 必须是非空字符串数组")
    force = raw.get("force", False)
    if not isinstance(force, bool):
        raise UsageError("scope.force 必须是布尔值")
    only_file = raw.get("only_file")
    only_part = raw.get("only_part")
    if only_part is not None:
        if not isinstance(only_part, str) or not only_part:
            raise UsageError("scope.only_part 必须是非空字符串")
        if not isinstance(only_file, str) or not only_file:
            raise UsageError("scope.only_part 必须与 scope.only_file 同时提供")
        if segment_ids is not None or raw.get("from_file") or raw.get("only_segment"):
            raise UsageError("scope.only_part 不能与 from_file、only_segment 或 segment_ids 同时使用")
        resolved_ids = query_segment_ids(root, file_id=only_file, part_id=only_part)
        if not resolved_ids:
            raise UsageError(f"指定章节范围为空或不存在：{only_file} / {only_part}")
        return Scope(segment_ids=tuple(resolved_ids), force=force)
    return Scope(
        from_file=raw.get("from_file"),
        only_file=only_file,
        only_segment=raw.get("only_segment"),
        segment_ids=tuple(dict.fromkeys(segment_ids)) if segment_ids else None,
        force=force,
    )


def _agent_run_progress(
    project: Path,
    manifest: dict[str, Any],
    selected: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    run_id = str(manifest["run_id"])
    stage = str(manifest["stage"])
    force = bool((manifest.get("scope") or {}).get("force", False)) and not manifest.get(
        "continuations"
    )
    selected_ids = [str(item["segment_id"]) for item in selected]
    states = latest_stage_states(project, stage, selected_ids)
    pending: list[dict[str, Any]] = []
    completed_count = 0
    for item in selected:
        sid = str(item["segment_id"])
        completed = states.get(sid, {}).get("completed")
        if completed is not None and (not force or completed.get("run_id") == run_id):
            completed_count += 1
        else:
            pending.append(item)
    return pending, completed_count, len(pending)


def _parse_slice_spec(
    run_id: str, payload: dict[str, Any]
) -> tuple[str, str | None, int | None, int | None]:
    part_spec = payload.get("part_id")
    line_start = payload.get("line_start")
    line_end = payload.get("line_end")
    if part_spec is not None and (not isinstance(part_spec, str) or not part_spec):
        raise UsageError("part_id 必须是非空字符串")
    if line_start is not None and (
        not isinstance(line_start, int) or isinstance(line_start, bool) or line_start < 0
    ):
        raise UsageError("line_start 必须是非负整数")
    if line_end is not None and (
        not isinstance(line_end, int) or isinstance(line_end, bool) or line_end < 0
    ):
        raise UsageError("line_end 必须是非负整数")
    if "@" in run_id:
        run_id, spec = run_id.split("@", 1)
        if ":" in spec:
            spec_part, rng = spec.rsplit(":", 1)
            if "-" in rng:
                s_str, e_str = rng.split("-", 1)
                if s_str.isdigit() and e_str.isdigit():
                    part_spec = spec_part
                    line_start = int(s_str)
                    line_end = int(e_str)
        if not part_spec:
            part_spec = spec
    return run_id, part_spec, line_start, line_end


def _filter_slice(
    items: list[dict[str, Any]],
    part_spec: str | None,
    line_start: int | None,
    line_end: int | None,
) -> list[dict[str, Any]]:
    if isinstance(part_spec, str) and part_spec:
        items = [
            item
            for item in items
            if str(item["part_id"]) == part_spec
            or str(item["part_id"]).endswith("/" + part_spec)
        ]
    if isinstance(line_start, int) and not isinstance(line_start, bool):
        items = [item for item in items if int(item["line_index"]) >= line_start]
    if isinstance(line_end, int) and not isinstance(line_end, bool):
        items = [item for item in items if int(item["line_index"]) < line_end]
    return items


def register_agent_routes(
    *,
    app: FastAPI,
    project: Callable[[str], Path],
) -> None:
    @app.post("/api/v1/projects/{name}/agent/segments/context")
    async def segment_context(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        root = project(name)
        segment_id = payload.get("segment_id")
        if not isinstance(segment_id, str) or not segment_id:
            raise UsageError("segment_id 必须是非空字符串")
        window = payload.get("window", 5)
        if not isinstance(window, int) or isinstance(window, bool) or not 0 <= window <= 50:
            raise UsageError("window 必须是 0 到 50 之间的整数")

        def _load() -> dict[str, Any]:
            store = WebStore(root)
            files = load_source_files(root)
            segment = store._require_segment(segment_id, files=files, with_adapter_state=True)
            before_segments, after_segments = query_segment_neighbors(
                root,
                file_id=str(segment["file_id"]),
                part_id=str(segment["part_id"]),
                line_index=int(segment["line_index"]),
                before_limit=window,
                after_limit=window,
            )
            context_segments = [*before_segments, *after_segments]
            format_counts = store._format_counts([segment, *context_segments])
            segment_filter = [
                segment_id,
                *(str(item["segment_id"]) for item in context_segments),
            ]
            histories = {
                stage_name: store._history(stage_name, segment_filter)
                for stage_name in (
                    "translation",
                    "proofreading",
                    "proofreading_applied",
                    "polishing",
                    "polishing_applied",
                )
            }
            detail = store._segment_detail_view(
                segment,
                histories,
                format_count=format_counts[segment_id],
            )
            file_by_id = {str(f["file_id"]): f for f in files}
            adapter, state, run_options = document_prompt_context(
                root, file_by_id[str(segment["file_id"])]
            )
            detail["model_source"] = adapter.render_model_source(
                segment=segment, opaque_state=state, run_options=run_options
            )
            all_segments = load_segments(root)
            library = load_terms(root, resolve_knowledge=False)
            warnings: list[str] = []
            cache = _TermMatchCache(
                library,
                term_normalization(store.config),
                int(store.config["terminology"]["max_terms_per_segment"]),
                segments=all_segments,
                warnings=warnings,
                sourced_policy=store.config["terminology"]["sourced_description_injection"],
                unsourced_policy=store.config["terminology"]["unsourced_description_injection"],
                project=root,
            )
            detail["matched_terms"] = cache.for_items([segment])
            detail["term_warnings"] = warnings
            detail["document_adapter_requirements"] = {
                stage_name: document_prompt_requirements(adapter, state, run_options, stage_name)
                for stage_name in AGENT_STAGES
            }
            detail["context"] = {
                "before": [
                    store._segment_detail_view(
                        item,
                        histories,
                        format_count=format_counts[str(item["segment_id"])],
                    )
                    for item in before_segments
                ],
                "after": [
                    store._segment_detail_view(
                        item,
                        histories,
                        format_count=format_counts[str(item["segment_id"])],
                    )
                    for item in after_segments
                ],
            }
            return detail

        return await run_blocking(_load)

    @app.post("/api/v1/projects/{name}/agent/segments/fix")
    async def segment_fix(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        root = project(name)
        if app.state.tasks.is_project_running(root):
            raise UsageError("项目存在运行中的任务，不能直接执行单段修正")
        stage = payload.get("stage", "translation")
        if stage not in AGENT_STAGES:
            raise UsageError(f"不支持的修正阶段：{stage}")
        segment_id = payload.get("segment_id")
        if not isinstance(segment_id, str) or not segment_id:
            raise UsageError("segment_id 必须是非空字符串")
        allow_warnings = payload.get("allow_warnings", False)
        if not isinstance(allow_warnings, bool):
            raise UsageError("allow_warnings 必须是布尔值")

        def _execute() -> dict[str, Any]:
            with project_write_lock(root, wait_seconds=15.0):
                store = WebStore(root)
                files = load_source_files(root)
                segment = store._require_segment(segment_id, files=files, with_adapter_state=True)
                language = _prompt_language(root, stage, None)
                validators = store.config["_translation_validator_instances"]

                if stage == "translation":
                    raw_text = payload.get("text")
                    if not isinstance(raw_text, str) or not raw_text.strip():
                        raise UsageError("译文必须是非空字符串")
                    try:
                        text = normalize_model_text(files, segment, raw_text, "translation")
                    except AppError as exc:
                        return {
                            "status": "rejected",
                            "segment_id": segment_id,
                            "stage": stage,
                            "error_layer": "document_adapter",
                            "error": str(exc),
                            "findings": [],
                            "repair_guidance": [str(exc)],
                        }
                    context = store._translation_validation_context(segment, text)
                    findings = asyncio.run(validate_translation_text(context, validators))
                    guidance = _repair_guidance(findings, validators, language)
                    if findings and not allow_warnings:
                        return {
                            "status": "rejected",
                            "segment_id": segment_id,
                            "stage": stage,
                            "error_layer": "translation_validation",
                            "findings": findings,
                            "repair_guidance": guidance,
                        }
                    record = record_header(
                        "stage_result",
                        store.project_id,
                        stage="translation",
                        segment_id=segment_id,
                        status="completed",
                        text=text,
                        validation_status="warning" if findings else "passed",
                        validation_findings=findings,
                        stage_fingerprint=store._fingerprint("translation"),
                        terms_revision=store._terms_revision(),
                        run_id=None,
                        request_id=None,
                        origin="agent_mcp",
                        decision_validation=context.decision.records if context.decision else [],
                    )
                    append_jsonl(root, stage_result_path(root, "translation"), record)
                    return {
                        "status": "committed",
                        "segment_id": segment_id,
                        "stage": stage,
                        "result": store._result_view(record),
                        "findings": findings,
                    }

                base = store._base_results(stage, [segment_id]).get(segment_id)
                if base is None:
                    raise UsageError("当前 Segment 缺少可用基准结果")
                review_status = payload.get("review_status")
                if review_status not in {"accepted", "suggested"}:
                    raise UsageError("review_status 必须是 accepted 或 suggested")
                suggested_text = payload.get("suggested_text")
                findings: list[dict[str, Any]] = []
                if review_status == "suggested":
                    if not isinstance(suggested_text, str) or not suggested_text.strip():
                        raise UsageError("suggested 状态需要非空 suggested_text")
                    try:
                        suggested_text = normalize_model_text(
                            files, segment, suggested_text, stage
                        )
                    except AppError as exc:
                        return {
                            "status": "rejected",
                            "segment_id": segment_id,
                            "stage": stage,
                            "error_layer": "document_adapter",
                            "error": str(exc),
                            "findings": [],
                            "repair_guidance": [str(exc)],
                        }
                    context = store._translation_validation_context(segment, suggested_text)
                    findings = asyncio.run(validate_translation_text(context, validators))
                    guidance = _repair_guidance(findings, validators, language)
                    if findings and not allow_warnings:
                        return {
                            "status": "rejected",
                            "segment_id": segment_id,
                            "stage": stage,
                            "error_layer": "translation_validation",
                            "findings": findings,
                            "repair_guidance": guidance,
                        }
                else:
                    suggested_text = None
                reason = payload.get("reason")
                if reason is not None and not isinstance(reason, str):
                    raise UsageError("reason 必须是字符串或 null")
                suggestion = record_header(
                    "stage_result",
                    store.project_id,
                    stage=stage,
                    segment_id=segment_id,
                    status="completed",
                    review_status=review_status,
                    suggested_text=suggested_text,
                    reason=reason or None,
                    base_result_id=base["record_id"],
                    stage_fingerprint=store._fingerprint(stage),
                    terms_revision=store._terms_revision(),
                    run_id=None,
                    request_id=None,
                    origin="agent_mcp",
                )
                append_jsonl(root, stage_result_path(root, stage), suggestion)
                applied = None
                if payload.get("apply") is True:
                    text = str(suggested_text) if review_status == "suggested" else str(base["text"])
                    applied_stage = f"{stage}_applied"
                    applied = record_header(
                        "stage_result",
                        store.project_id,
                        stage=applied_stage,
                        segment_id=segment_id,
                        status="completed",
                        suggestion_result_id=suggestion["record_id"],
                        base_result_id=base["record_id"],
                        allowed_outdated_base=False,
                        stage_fingerprint=store._fingerprint(applied_stage),
                        run_id=None,
                        request_id=None,
                        origin="agent_mcp",
                    )
                    append_jsonl(root, stage_result_path(root, applied_stage), applied)
                    applied = {**applied, "text": text}
                return {
                    "status": "committed",
                    "segment_id": segment_id,
                    "stage": stage,
                    "suggestion": store._result_view(suggestion),
                    "applied": store._result_view(applied) if applied else None,
                    "findings": findings,
                }

        return await run_blocking(_execute)

    @app.post("/api/v1/projects/{name}/agent/runs/start")
    async def start_agent_run(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        root = project(name)
        if app.state.tasks.is_project_running(root):
            raise UsageError("项目已有后台任务，请等待完成或先取消")
        stage = payload.get("stage")
        if stage not in AGENT_STAGES:
            raise UsageError(f"Agent 批次执行仅支持 translation、proofreading 或 polishing：{stage}")
        scope = _parse_scope(root, payload)
        run_action = payload.get("run_action")
        if run_action not in {None, "resume", "decline"}:
            raise UsageError("run_action 必须是 resume、decline 或 null")
        reuse_mixed = payload.get("reuse_mixed_fingerprints", False)
        if not isinstance(reuse_mixed, bool):
            raise UsageError("reuse_mixed_fingerprints 必须是布尔值")
        if scope.force and reuse_mixed:
            raise UsageError("force 与 reuse_mixed_fingerprints 不能同时使用")
        prompt_language = payload.get("prompt_language")
        agent_model_hint = payload.get("agent_model_hint")
        if agent_model_hint is not None and not isinstance(agent_model_hint, str):
            raise UsageError("agent_model_hint 必须是字符串")

        def _prepare() -> dict[str, Any]:
            with project_write_lock(root):
                candidates = find_running_runs(root, stage)
                resume_run_id: str | None = None
                run_warnings: list[str] = []
                if candidates:
                    latest = candidates[0]
                    latest_id = str(latest["run_id"])
                    for older in candidates[1:]:
                        _interrupt_run(
                            root,
                            older,
                            reason="superseded",
                            superseded_by_run_id=latest_id,
                        )
                    if run_action is None:
                        raise UsageError(
                            f"发现未完成 Run：{latest_id}；必须指定 run_action='resume' 或 'decline'"
                        )
                    if run_action == "decline":
                        _interrupt_run(root, latest, reason="resume_declined")
                        run_warnings.append(f"已拒绝续用 Run：{latest_id}")
                    else:
                        if latest.get("executor") != "agent_mcp":
                            raise UsageError(
                                f"未完成 Run {latest_id} 由宿主模型创建，不能作为 Agent MCP Run 续作；请指定 run_action='decline' 或通过宿主任务续作"
                            )
                        resume_run_id = latest_id
                        run_warnings.append(f"将续用 Agent MCP Run：{latest_id}")
                elif run_action == "resume":
                    raise UsageError(f"{stage} 没有可续用的 running Run")

                frozen_options = _frozen_run_options(root, resume_run_id)
                config, metadata, files, segments = _project_context(
                    root, stage=stage, frozen_run_options=frozen_options
                )
                _require_nonempty_segments(segments)
                language = _prompt_language(root, stage, prompt_language)
                effective_scope = (
                    scope_from_run(root, resume_run_id, dry_run=False)
                    if resume_run_id
                    else scope
                )
                selected = select_scope(segments, files, effective_scope)
                selected_ids = [str(item["segment_id"]) for item in selected]
                if stage in REVIEW_STAGES:
                    bases = _base_results(root, stage, segment_ids=selected_ids)
                    missing = [sid for sid in selected_ids if sid not in bases]
                    if missing:
                        raise IncompleteError(
                            f"{stage} 缺少上游基准译文，无法启动：{', '.join(missing[:10])}"
                        )
                states = latest_stage_states(root, stage, selected_ids)
                selection = classify_stage_states(
                    selected, states, force=effective_scope.force
                )
                library = load_terms(root, resolve_knowledge=False)
                terms_revision = int(library["terms_revision"]) if library else None
                requirements_for, _ = _document_prompt_requirement_helpers(config, language)
                prompt_fn = _prompt_factory(root, stage, language)
                prompt = prompt_fn(requirements_for(selected))
                fingerprint = stage_fingerprint(
                    config,
                    stage,
                    prompt_middle_digests(root, stage),
                    terms_revision=terms_revision,
                )
                mixed = bool(selection.fingerprints - {fingerprint})
                if mixed and not effective_scope.force and not reuse_mixed:
                    raise UsageError(
                        "所选范围存在不同设置生成的已完成结果；请显式指定 reuse_mixed_fingerprints=True 复用，或 scope.force=True 重做"
                    )
                term_warnings: list[str] = []
                cache = _TermMatchCache(
                    library,
                    term_normalization(config),
                    int(config["terminology"]["max_terms_per_segment"]),
                    segments=segments,
                    warnings=term_warnings,
                    sourced_policy=config["terminology"]["sourced_description_injection"],
                    unsourced_policy=config["terminology"]["unsourced_description_injection"],
                    project=root,
                )
                if selection.work:
                    cache.for_items(list(selection.work))
                warnings = _assemble_warnings(
                    stage=stage,
                    resume_run_id=resume_run_id,
                    resume_arguments_ignored=False,
                    resume_message=f"将续用 Agent MCP Run：{resume_run_id}" if resume_run_id else "",
                    config=config,
                    fingerprint=fingerprint,
                    existing_fingerprints=selection.fingerprints,
                    reusable_count=len(selection.reusable),
                    force=effective_scope.force,
                    reuse_allowed=reuse_mixed,
                    dry_run=False,
                    extra=[*(w for w in run_warnings if not resume_run_id), *term_warnings],
                )
                if not selection.work and not resume_run_id:
                    raise UsageError("所选范围内没有待处理的 Segment")

                if resume_run_id:
                    run_id, _ = continue_run(
                        root,
                        resume_run_id,
                        config=config,
                        stage=stage,
                        fingerprint=fingerprint,
                        prompt=prompt,
                        scope=effective_scope,
                        selected_count=len(selected),
                        requested_count=len(selection.work),
                        reused_count=len(selection.reusable),
                    )[:2]
                else:
                    run_id, _ = create_run(
                        root,
                        config=config,
                        stage=stage,
                        fingerprint=fingerprint,
                        prompt=prompt,
                        selected_count=len(selected),
                        requested_count=len(selection.work),
                        reused_count=len(selection.reusable),
                        details={
                            "executor": "agent_mcp",
                            "agent_model_hint": (agent_model_hint or "agent_mcp").strip() or "agent_mcp",
                            "prompt_language": language,
                            **({"terms_revision": terms_revision} if terms_revision is not None else {}),
                            "scope": _scope_record(effective_scope),
                            "warnings": warnings,
                        },
                    )
                return {
                    "run_id": run_id,
                    "project_id": str(metadata["project_id"]),
                    "stage": stage,
                    "selected_count": len(selected),
                    "reused_count": len(selection.reusable),
                    "pending_count": len(selection.work),
                    "completed_count": len(selected) - len(selection.work),
                    "prompt": prompt,
                    "document_adapter_requirements": config.get(
                        "_document_adapter_prompt_requirements", {}
                    ),
                    "warnings": warnings,
                }

        prepared = await run_blocking(_prepare)
        try:
            task_view = await app.state.tasks.register_agent_task(
                root,
                prepared["project_id"],
                stage,
                prepared["run_id"],
                prepared["selected_count"],
                prepared["completed_count"],
            )
        except BaseException:
            def _rollback_run() -> None:
                with project_write_lock(root):
                    manifest_path = root / "runs" / prepared["run_id"] / "manifest.json"
                    if record_exists(root, manifest_path):
                        manifest = read_json(root, manifest_path)
                        if manifest.get("status") == "running":
                            _interrupt_run(root, manifest, reason="task_registration_failed")

            await run_blocking(_rollback_run)
            raise
        return {**prepared, "task_id": task_view["task_id"]}

    @app.post("/api/v1/projects/{name}/agent/runs/{run_id}/checkout")
    async def checkout_agent_batch(
        name: str, run_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        root = project(name)
        run_id, part_spec, line_start, line_end = _parse_slice_spec(run_id, payload)
        safe_id = _safe_run_id(run_id)
        if app.state.tasks.has_conflicting_task(root, safe_id):
            raise UsageError("项目存在其他运行中的后台任务")
        max_segments = payload.get("max_segments", 15)
        if (
            not isinstance(max_segments, int)
            or isinstance(max_segments, bool)
            or not 1 <= max_segments <= 100
        ):
            raise UsageError("max_segments 必须是 1 到 100 之间的整数")
        context_before = payload.get("context_before", 3)
        if (
            not isinstance(context_before, int)
            or isinstance(context_before, bool)
            or not 0 <= context_before <= 20
        ):
            raise UsageError("context_before 必须是 0 到 20 之间的整数")

        def _checkout() -> dict[str, Any]:
            manifest_path = root / "runs" / safe_id / "manifest.json"
            if not record_exists(root, manifest_path):
                raise UsageError(f"Run 不存在：{safe_id}")
            manifest = read_json(root, manifest_path)
            if manifest.get("executor") != "agent_mcp":
                raise UsageError(f"Run {safe_id} 不是 Agent MCP Run")
            if manifest.get("status") != "running":
                raise UsageError(f"Run {safe_id} 当前状态为 {manifest.get('status')}，不可拉取批次")
            stage = str(manifest["stage"])
            frozen_options = _frozen_run_options(root, safe_id)
            config, _, files, segments = _project_context(
                root, stage=stage, frozen_run_options=frozen_options
            )
            scope = scope_from_run(root, safe_id, dry_run=False)
            selected = select_scope(segments, files, scope)
            pending, completed_count, remaining_count = _agent_run_progress(
                root, manifest, selected
            )
            pending = _filter_slice(pending, part_spec, line_start, line_end)
            slice_remaining = len(pending)
            if not pending:
                return {
                    "run_id": safe_id,
                    "stage": stage,
                    "batch": [],
                    "terms": [],
                    "reference_context": [],
                    "completed_count": completed_count,
                    "remaining_pending_count": 0,
                    "total_remaining_pending_count": remaining_count,
                }
            first = pending[0]
            boundary = (str(first["file_id"]), str(first["part_id"]))
            batch: list[dict[str, Any]] = []
            for item in pending:
                if (str(item["file_id"]), str(item["part_id"])) != boundary:
                    break
                batch.append(item)
                if len(batch) >= max_segments:
                    break

            bases = (
                _base_results(
                    root, stage, segment_ids=[str(item["segment_id"]) for item in batch]
                )
                if stage in REVIEW_STAGES
                else {}
            )
            library = load_terms(root, resolve_knowledge=False)
            warnings: list[str] = []
            cache = _TermMatchCache(
                library,
                term_normalization(config),
                int(config["terminology"]["max_terms_per_segment"]),
                segments=segments,
                warnings=warnings,
                sourced_policy=config["terminology"]["sourced_description_injection"],
                unsourced_policy=config["terminology"]["unsourced_description_injection"],
                project=root,
            )
            terms = cache.for_items(batch)

            before_neighbors, _ = (
                query_segment_neighbors(
                    root,
                    file_id=boundary[0],
                    part_id=boundary[1],
                    line_index=int(batch[0]["line_index"]),
                    before_limit=context_before,
                    after_limit=0,
                )
                if context_before > 0
                else ([], [])
            )
            neighbor_ids = [str(item["segment_id"]) for item in before_neighbors]
            neighbor_bases = (
                _base_results(
                    root,
                    "polishing" if stage == "polishing" else "proofreading",
                    segment_ids=neighbor_ids,
                )
                if neighbor_ids
                else {}
            )
            reference_context = [
                {
                    "segment_id": str(item["segment_id"]),
                    "source": str(item["source"]),
                    "translation": (neighbor_bases.get(str(item["segment_id"])) or {}).get("text"),
                }
                for item in before_neighbors
            ]
            language = str(manifest.get("prompt_language") or _prompt_language(root, stage, None))
            req_for_file = (
                config.get("_document_adapter_prompt_requirements", {})
                .get(boundary[0], {})
                .get(language)
            )
            batch_items = []
            for item in batch:
                sid = str(item["segment_id"])
                src = str(item["source"])
                ms = segment_model_source(item)
                entry: dict[str, Any] = {
                    "segment_id": sid,
                    "line_index": int(item["line_index"]),
                    "source": src,
                }
                if ms is not None and ms != src:
                    entry["model_source"] = ms
                if stage in REVIEW_STAGES and sid in bases:
                    entry["current_text"] = bases[sid]["text"]
                batch_items.append(entry)

            return {
                "run_id": safe_id,
                "stage": stage,
                "file_id": boundary[0],
                "part_id": boundary[1],
                "document_adapter_requirement": req_for_file,
                "batch": batch_items,
                "terms": terms,
                "reference_context": reference_context,
                "completed_count": completed_count,
                "remaining_pending_count": slice_remaining,
                "total_remaining_pending_count": remaining_count,
                "warnings": warnings,
            }

        result = await run_blocking(_checkout)
        checked_out = app.state.tasks.record_agent_checkout(
            root,
            safe_id,
            segment_ids=[item["segment_id"] for item in result.get("batch", [])],
            part_id=result.get("part_id"),
        )
        result["checked_out_count"] = checked_out
        return result

    @app.post("/api/v1/projects/{name}/agent/runs/{run_id}/submit")
    async def submit_agent_batch(
        name: str, run_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        root = project(name)
        run_id, part_spec, line_start, line_end = _parse_slice_spec(run_id, payload)
        safe_id = _safe_run_id(run_id)
        if app.state.tasks.has_conflicting_task(root, safe_id):
            raise UsageError("项目存在其他运行中的后台任务")
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            raise UsageError("items 必须是非空数组")
        allow_warnings = payload.get("allow_warnings", False)
        if not isinstance(allow_warnings, bool):
            raise UsageError("allow_warnings 必须是布尔值")

        def _submit() -> dict[str, Any]:
            with project_write_lock(root, wait_seconds=15.0):
                manifest_path = root / "runs" / safe_id / "manifest.json"
                if not record_exists(root, manifest_path):
                    raise UsageError(f"Run 不存在：{safe_id}")
                manifest = read_json(root, manifest_path)
                if manifest.get("executor") != "agent_mcp":
                    raise UsageError(f"Run {safe_id} 不是 Agent MCP Run")
                if manifest.get("status") != "running":
                    raise UsageError(f"Run {safe_id} 当前状态为 {manifest.get('status')}，不可提交结果")
                stage = str(manifest["stage"])
                store = WebStore(root)
                frozen_options = _frozen_run_options(root, safe_id)
                config, metadata, files, segments = _project_context(
                    root, stage=stage, frozen_run_options=frozen_options
                )
                scope = scope_from_run(root, safe_id, dry_run=False)
                selected = select_scope(segments, files, scope)
                selected_by_id = {str(item["segment_id"]): item for item in selected}
                language = str(manifest.get("prompt_language") or _prompt_language(root, stage, None))
                validators = config["_translation_validator_instances"]
                continuations = manifest.get("continuations") or []
                fingerprint = str(
                    (continuations[-1].get("stage_fingerprint") if continuations else None)
                    or manifest.get("stage_fingerprint")
                    or store._fingerprint(stage)
                )
                val_library = load_terms(root, resolve_knowledge=False)
                terms_revision = int(val_library["terms_revision"]) if val_library else None
                bases = (
                    _base_results(root, stage, segment_ids=list(selected_by_id))
                    if stage in REVIEW_STAGES
                    else {}
                )

                records: list[dict[str, Any]] = []
                committed_ids: list[str] = []
                rejected_items: list[dict[str, Any]] = []
                seen_ids: set[str] = set()

                for entry in items:
                    if not isinstance(entry, dict):
                        raise UsageError("items 中的每个元素必须是对象")
                    sid = entry.get("segment_id")
                    if not isinstance(sid, str) or sid not in selected_by_id:
                        raise UsageError(f"未知或不在当前 Run 范围内的 segment_id：{sid}")
                    if sid in seen_ids:
                        raise UsageError(f"同一批次包含重复的 segment_id：{sid}")
                    seen_ids.add(sid)
                    segment = selected_by_id[sid]

                    if stage == "translation":
                        raw_text = (
                            entry.get("text")
                            if "text" in entry
                            else entry.get("translation")
                        )
                        if not isinstance(raw_text, str) or not raw_text.strip():
                            rejected_items.append(
                                {
                                    "segment_id": sid,
                                    "error_layer": "format",
                                    "error": "译文必须是非空字符串",
                                    "findings": [],
                                    "repair_guidance": ["提供非空目标译文"],
                                }
                            )
                            continue
                        try:
                            text = normalize_model_text(files, segment, raw_text, "translation")
                        except AppError as exc:
                            rejected_items.append(
                                {
                                    "segment_id": sid,
                                    "error_layer": "document_adapter",
                                    "error": str(exc),
                                    "findings": [],
                                    "repair_guidance": [str(exc)],
                                }
                            )
                            continue
                        context = store._translation_validation_context(
                            segment, text, library=val_library, load_library=False
                        )
                        findings = asyncio.run(validate_translation_text(context, validators))
                        guidance = _repair_guidance(findings, validators, language)
                        if findings and not allow_warnings:
                            rejected_items.append(
                                {
                                    "segment_id": sid,
                                    "error_layer": "translation_validation",
                                    "findings": findings,
                                    "repair_guidance": guidance,
                                }
                            )
                            continue
                        records.append(
                            record_header(
                                "stage_result",
                                str(metadata["project_id"]),
                                stage="translation",
                                segment_id=sid,
                                status="completed",
                                text=text,
                                validation_status="warning" if findings else "passed",
                                validation_findings=findings,
                                stage_fingerprint=fingerprint,
                                terms_revision=terms_revision,
                                run_id=safe_id,
                                request_id=None,
                                origin="agent_mcp",
                                decision_validation=context.decision.records if context.decision else [],
                            )
                        )
                        committed_ids.append(sid)
                    else:
                        base = bases.get(sid)
                        if base is None:
                            raise UsageError(f"Segment 缺少基准译文：{sid}")
                        review_status = entry.get("review_status") or entry.get("status")
                        if review_status not in {"accepted", "suggested"}:
                            rejected_items.append(
                                {
                                    "segment_id": sid,
                                    "error_layer": "format",
                                    "error": "review_status 必须是 accepted 或 suggested",
                                    "findings": [],
                                    "repair_guidance": ["指定 review_status 为 accepted 或 suggested"],
                                }
                            )
                            continue
                        suggested_text = entry.get("suggested_text")
                        if review_status == "suggested":
                            if not isinstance(suggested_text, str) or not suggested_text.strip():
                                rejected_items.append(
                                    {
                                        "segment_id": sid,
                                        "error_layer": "format",
                                        "error": "suggested 状态必须提供非空 suggested_text",
                                        "findings": [],
                                        "repair_guidance": ["提供非空 suggested_text"],
                                    }
                                )
                                continue
                            try:
                                suggested_text = normalize_model_text(
                                    files, segment, suggested_text, stage
                                )
                            except AppError as exc:
                                rejected_items.append(
                                    {
                                        "segment_id": sid,
                                        "error_layer": "document_adapter",
                                        "error": str(exc),
                                        "findings": [],
                                        "repair_guidance": [str(exc)],
                                    }
                                )
                                continue
                            context = store._translation_validation_context(
                                segment, suggested_text, library=val_library, load_library=False
                            )
                            findings = asyncio.run(validate_translation_text(context, validators))
                            guidance = _repair_guidance(findings, validators, language)
                            if findings and not allow_warnings:
                                rejected_items.append(
                                    {
                                        "segment_id": sid,
                                        "error_layer": "translation_validation",
                                        "findings": findings,
                                        "repair_guidance": guidance,
                                    }
                                )
                                continue
                        else:
                            suggested_text = None
                        reason = entry.get("reason")
                        if reason is not None and not isinstance(reason, str):
                            raise UsageError(f"reason 必须是字符串或 null：{sid}")
                        records.append(
                            record_header(
                                "stage_result",
                                str(metadata["project_id"]),
                                stage=stage,
                                segment_id=sid,
                                status="completed",
                                review_status=review_status,
                                suggested_text=suggested_text,
                                reason=reason or None,
                                base_result_id=base["record_id"],
                                stage_fingerprint=fingerprint,
                                terms_revision=terms_revision,
                                run_id=safe_id,
                                request_id=None,
                                origin="agent_mcp",
                            )
                        )
                        committed_ids.append(sid)

                if records:
                    append_stage_results(root, records)
                pending, completed_count, remaining_count = _agent_run_progress(
                    root, manifest, selected
                )
                slice_pending = _filter_slice(pending, part_spec, line_start, line_end)
                return {
                    "run_id": safe_id,
                    "stage": stage,
                    "committed_ids": committed_ids,
                    "rejected_items": rejected_items,
                    "completed_count": completed_count,
                    "remaining_pending_count": len(slice_pending),
                    "total_remaining_pending_count": remaining_count,
                }

        result = await run_blocking(_submit)
        app.state.tasks.update_agent_task(
            root,
            safe_id,
            completed_segments=result["completed_count"],
            failed_segments=len(result["rejected_items"]),
            committed_ids=result["committed_ids"],
        )
        return result

    @app.post("/api/v1/projects/{name}/agent/runs/{run_id}/finish")
    async def finish_agent_run(
        name: str, run_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        root = project(name)
        safe_id = _safe_run_id(run_id)
        if app.state.tasks.has_conflicting_task(root, safe_id):
            raise UsageError("项目存在其他运行中的后台任务")
        action = payload.get("action", "complete")
        if action not in {"complete", "interrupt"}:
            raise UsageError("action 必须是 complete 或 interrupt")
        reason = payload.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise UsageError("reason 必须是字符串")
        accept_remaining = payload.get("accept_remaining", False)
        if not isinstance(accept_remaining, bool):
            raise UsageError("accept_remaining 必须是布尔值")
        if accept_remaining and action != "complete":
            raise UsageError("accept_remaining 仅在 action='complete' 时可用")

        def _finish() -> dict[str, Any]:
            with project_write_lock(root, wait_seconds=15.0):
                run_dir = root / "runs" / safe_id
                manifest_path = run_dir / "manifest.json"
                if not record_exists(root, manifest_path):
                    raise UsageError(f"Run 不存在：{safe_id}")
                manifest = read_json(root, manifest_path)
                if manifest.get("executor") != "agent_mcp":
                    raise UsageError(f"Run {safe_id} 不是 Agent MCP Run")
                if manifest.get("status") != "running":
                    raise UsageError(f"Run {safe_id} 已处于 {manifest.get('status')} 状态")
                stage = str(manifest["stage"])
                if accept_remaining and stage not in REVIEW_STAGES:
                    raise UsageError("accept_remaining 仅支持 proofreading 或 polishing 阶段")
                _, metadata, files, segments = _project_context(root, stage=stage)
                scope = scope_from_run(root, safe_id, dry_run=False)
                selected = select_scope(segments, files, scope)
                pending, completed_count, remaining_count = _agent_run_progress(
                    root, manifest, selected
                )
                accepted_remaining_count = 0
                if action == "complete":
                    if accept_remaining and pending:
                        store = WebStore(root)
                        continuations = manifest.get("continuations") or []
                        fingerprint = str(
                            (continuations[-1].get("stage_fingerprint") if continuations else None)
                            or manifest.get("stage_fingerprint")
                            or store._fingerprint(stage)
                        )
                        val_library = load_terms(root, resolve_knowledge=False)
                        terms_revision = (
                            int(val_library["terms_revision"]) if val_library else None
                        )
                        pending_ids = [str(item["segment_id"]) for item in pending]
                        bases = _base_results(root, stage, segment_ids=pending_ids)
                        missing_bases = [sid for sid in pending_ids if sid not in bases]
                        if missing_bases:
                            raise UsageError(
                                f"部分待确认 Segment 缺少可用基准结果：{', '.join(missing_bases[:10])}"
                            )
                        records = [
                            record_header(
                                "stage_result",
                                str(metadata["project_id"]),
                                stage=stage,
                                segment_id=sid,
                                status="completed",
                                review_status="accepted",
                                suggested_text=None,
                                reason=None,
                                base_result_id=bases[sid]["record_id"],
                                stage_fingerprint=fingerprint,
                                terms_revision=terms_revision,
                                run_id=safe_id,
                                request_id=None,
                                origin="agent_mcp",
                            )
                            for sid in pending_ids
                        ]
                        append_stage_results(root, records)
                        accepted_remaining_count = len(records)
                        completed_count += accepted_remaining_count
                        remaining_count = 0
                    if remaining_count > 0:
                        raise IncompleteError(
                            f"Run {safe_id} 仍有 {remaining_count} 个待处理 Segment 未完成；请继续处理或使用 action='interrupt'"
                        )
                    reused_count = int(manifest.get("reused_segment_count", 0))
                    newly_completed = max(0, completed_count - reused_count)
                    finalize_run(
                        root,
                        run_dir,
                        status="completed",
                        completed=newly_completed,
                        failed=0,
                        warnings=list(manifest.get("warnings", [])),
                        usage_invoked=False,
                    )
                    final_status = "completed"
                else:
                    reused_count = int(manifest.get("reused_segment_count", 0))
                    newly_completed = max(0, completed_count - reused_count)
                    _interrupt_run(
                        root,
                        manifest,
                        reason=(reason or "agent_interrupted").strip() or "agent_interrupted",
                    )
                    final_status = "interrupted"
                return {
                    "run_id": safe_id,
                    "stage": stage,
                    "status": final_status,
                    "selected": len(selected),
                    "completed": newly_completed,
                    "reused": reused_count,
                    "pending": remaining_count,
                    "accepted_remaining": accepted_remaining_count,
                }

        summary = await run_blocking(_finish)
        await app.state.tasks.finish_agent_task(
            root,
            safe_id,
            status="completed" if summary["status"] == "completed" else "cancelled",
            summary=summary,
        )
        return summary

    @app.post("/api/v1/projects/{name}/agent/repetition/inspect")
    async def inspect_repetition_group(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        root = project(name)
        group_id = payload.get("group_id")
        source_text = payload.get("source")
        if not group_id and not source_text:
            raise UsageError("必须提供 group_id 或 source")
        window = payload.get("window", 5)
        if not isinstance(window, int) or isinstance(window, bool) or not 0 <= window <= 30:
            raise UsageError("window 必须是 0 到 30 之间的整数")
        offset = payload.get("offset", 0)
        limit = payload.get("limit", 20)
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise UsageError("offset 必须是非负整数")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise UsageError("limit 必须是 1 到 100 之间的整数")

        def _inspect() -> dict[str, Any]:
            nonlocal source_text
            draft = current_repetition_draft(root)
            target_group: dict[str, Any] | None = None
            if draft is not None:
                for group in draft.get("groups", []):
                    if (group_id and group.get("id") == group_id) or (
                        source_text and group.get("source") == source_text
                    ):
                        target_group = group
                        break
            segments = load_segments(root)
            by_id = {str(s["segment_id"]): s for s in segments}
            if target_group is None:
                if not isinstance(source_text, str) or not source_text:
                    raise UsageError(f"未在当前重复译文草案中找到组：{group_id}")
                matched_segments = [
                    s for s in segments if not s["is_empty"] and s["source"] == source_text
                ]
                if len(matched_segments) < 2:
                    raise UsageError("指定 source 未找到重复出现的非空 Segment")
                occ_pairs = [
                    (str(idx), str(s["segment_id"]))
                    for idx, s in enumerate(matched_segments, 1)
                ]
            else:
                source_text_val = str(target_group["source"])
                occ_pairs = [
                    (str(o.get("id") or idx), str(o["segment_id"]))
                    for idx, o in enumerate(target_group["occurrences"], 1)
                ]
                source_text = source_text_val

            paged_pairs = occ_pairs[offset : offset + limit]
            page_windows = []
            needed_ids = [sid for _, sid in paged_pairs]
            for occ_id, sid in paged_pairs:
                seg = by_id[sid]
                if window == 0:
                    before, after = [], []
                else:
                    before, after = query_segment_neighbors(
                        root,
                        file_id=str(seg["file_id"]),
                        part_id=str(seg["part_id"]),
                        line_index=int(seg["line_index"]),
                        before_limit=window,
                        after_limit=window,
                    )
                needed_ids.extend(str(item["segment_id"]) for item in (*before, *after))
                page_windows.append((occ_id, sid, seg, before, after))
            states = (
                latest_stage_states(root, "translation", list(dict.fromkeys(needed_ids)))
                if needed_ids
                else {}
            )
            occurrences = [
                {
                    "occurrence_id": occ_id,
                    "segment_id": sid,
                    "file_id": seg["file_id"],
                    "part_id": seg["part_id"],
                    "line_index": seg["line_index"],
                    "translation": (states.get(sid, {}).get("completed") or {}).get("text"),
                    "before": [
                        {
                            "segment_id": str(item["segment_id"]),
                            "source": str(item["source"]),
                            "translation": (
                                states.get(str(item["segment_id"]), {}).get("completed")
                                or {}
                            ).get("text"),
                        }
                        for item in before
                    ],
                    "after": [
                        {
                            "segment_id": str(item["segment_id"]),
                            "source": str(item["source"]),
                            "translation": (
                                states.get(str(item["segment_id"]), {}).get("completed")
                                or {}
                            ).get("text"),
                        }
                        for item in after
                    ],
                }
                for occ_id, sid, seg, before, after in page_windows
            ]
            return {
                "group_id": target_group["id"] if target_group else None,
                "source": source_text,
                "draft_group": target_group,
                "total_occurrences": len(occ_pairs),
                "offset": offset,
                "limit": limit,
                "occurrences": occurrences,
            }

        return await run_blocking(_inspect)

    @app.post("/api/v1/projects/{name}/agent/repetition/proposals")
    async def update_repetition_proposals(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        root = project(name)
        if app.state.tasks.is_project_running(root):
            raise UsageError("项目存在后台任务，结束后才能修改重复译文草案")
        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise UsageError("必须提供草案 run_id")
        updates = payload.get("group_proposals")
        if not isinstance(updates, list) or not updates:
            raise UsageError("group_proposals 必须是非空数组")

        def _update() -> dict[str, Any]:
            with project_write_lock(root):
                manifest = read_repetition_run(root, ("pending",))
                if manifest is None or manifest["run_id"] != run_id:
                    raise UsageError("没有匹配的待审核重复译文草案")
                draft = read_repetition_draft(root, manifest)
                groups_by_id = {str(g["id"]): g for g in draft["groups"]}
                updated_ids: list[str] = []
                for entry in updates:
                    gid = (
                        entry.get("group_id") or entry.get("id")
                        if isinstance(entry, dict)
                        else None
                    )
                    if not isinstance(gid, str) or not gid:
                        raise UsageError("每个 group_proposals 项必须包含字符串 group_id")
                    group = groups_by_id.get(gid)
                    if group is None:
                        raise UsageError(f"草案中不存在重复组：{gid}")
                    comparison_input = {
                        "groups": entry.get("groups", []),
                        "keep": entry.get("keep", []),
                        "uncertain": entry.get("uncertain", []),
                    }
                    parsed = parse_comparison(comparison_input, group)
                    group.update(parsed)
                    updated_ids.append(gid)
                valid_proposal_ids = {
                    p["id"] for g in draft["groups"] for p in g["proposals"]
                }
                manifest["choices"] = {
                    k: v
                    for k, v in manifest.get("choices", {}).items()
                    if k in valid_proposal_ids
                }
                atomic_write_text(
                    repetition_draft_path(root, run_id),
                    json.dumps(draft, ensure_ascii=False, separators=(",", ":")) + "\n",
                )
                warnings = write_repetition_state(root, records=[], manifests=[manifest])
                return {
                    "run_id": run_id,
                    "updated_group_ids": updated_ids,
                    "warnings": warnings,
                }

        return await run_blocking(_update)

    @app.post("/api/v1/projects/{name}/agent/repetition/scan")
    async def scan_repetition_live(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        root = project(name)
        only_file = payload.get("only_file")
        if only_file is not None and (not isinstance(only_file, str) or not only_file):
            raise UsageError("only_file 必须是非空字符串")
        only_part = payload.get("only_part")
        if only_part is not None:
            if not isinstance(only_part, str) or not only_part:
                raise UsageError("only_part 必须是非空字符串")
            if not only_file:
                raise UsageError("only_part 必须与 only_file 同时提供")
        exclude_punctuation_only = payload.get("exclude_punctuation_only", True)
        if not isinstance(exclude_punctuation_only, bool):
            raise UsageError("exclude_punctuation_only 必须是布尔值")
        create_draft = payload.get("create_draft", False)
        if not isinstance(create_draft, bool):
            raise UsageError("create_draft 必须是布尔值")
        replace_draft = payload.get("replace_draft", False)
        if not isinstance(replace_draft, bool):
            raise UsageError("replace_draft 必须是布尔值")
        include_occurrences = payload.get("include_occurrences", False)
        if not isinstance(include_occurrences, bool):
            raise UsageError("include_occurrences 必须是布尔值")
        sort_by = payload.get("sort_by", "occurrence_count")
        if sort_by not in {"group_index", "occurrence_count"}:
            raise UsageError("sort_by 必须是 group_index 或 occurrence_count")
        offset = payload.get("offset", 0)
        limit = payload.get("limit", 50)
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 200
        ):
            raise UsageError("offset/limit 分页参数无效")

        def _scan() -> dict[str, Any]:
            if create_draft:
                if only_part:
                    raise UsageError(
                        "正式重复译文草案仅支持整个项目或单个 File（only_file），章节级范围请使用实时扫描（create_draft=False）"
                    )
                if app.state.tasks.is_project_running(root):
                    raise UsageError("项目存在后台任务，结束后才能创建重复译文草案")
                with project_write_lock(root):
                    old = read_repetition_run(root, ("pending",))
                    if old is not None and not replace_draft:
                        raise UsageError(
                            "存在未处理的重复译文草案，请先应用、丢弃或指定 replace_draft=True"
                        )
                    for running in find_running_runs(root, "repetition_consistency"):
                        _interrupt_run(root, running, reason="resume_declined")
                    plan = repetition_plan(
                        root,
                        Scope(only_file=only_file),
                        exclude_punctuation_only=exclude_punctuation_only,
                        screening_enabled=False,
                        final_review=False,
                        estimate_usage=False,
                    )
                    if not plan["groups"]:
                        raise UsageError("所选范围内没有重复原文译文差异组")
                    run_id, run_dir = create_run(
                        root,
                        config=plan["config"],
                        stage="repetition_consistency",
                        fingerprint=plan["fingerprint"],
                        prompt=plan["prompt"],
                        selected_count=plan["selected"],
                        requested_count=plan["selected"],
                        reused_count=0,
                        details={
                            "executor": "agent_mcp",
                            "final_review": False,
                            "screening_enabled": False,
                            "exclude_punctuation_only": exclude_punctuation_only,
                            "excluded_punctuation_groups": plan[
                                "excluded_punctuation_groups"
                            ],
                            "prompt_language": plan["language"],
                            "terms_revision": plan["terms_revision"],
                            "repetition_checkpoint": 2,
                            "repetition_protocol": 3,
                            "repetition_input_fingerprint": plan[
                                "input_fingerprint"
                            ],
                            "usage_invocation_count": 0,
                            "draft_status": "generating",
                            "scope": {"only_file": only_file},
                            "choices": {},
                        },
                    )
                    atomic_write_text(
                        run_dir / "repetition_terms.json",
                        json.dumps(
                            {
                                "terms_revision": plan["terms_revision"],
                                "terms": plan["terms"],
                                "groups": {
                                    g["id"]: {
                                        o["id"]: o["terms"]
                                        for o in g["occurrences"]
                                    }
                                    for g in plan["groups"]
                                },
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n",
                    )
                    finalize_run(
                        root,
                        run_dir,
                        status="completed",
                        completed=plan["selected"],
                        failed=0,
                        usage=unavailable_usage(),
                        usage_invoked=False,
                        warnings=plan["warnings"],
                    )
                    draft_groups = deepcopy(plan["groups"])
                    for group in draft_groups:
                        for key in (
                            "screening_execution_index",
                            "screening_input_tokens",
                            "comparison_input_tokens",
                        ):
                            group.pop(key, None)
                        for occurrence in group["occurrences"]:
                            occurrence.pop("terms", None)
                        members = [o["id"] for o in group["occurrences"]]
                        group.update(
                            screening="disabled",
                            confidence=None,
                            over_budget=False,
                            proposals=[],
                            keep=[],
                            uncertain=members,
                        )
                    draft = {
                        "run_id": run_id,
                        "groups": draft_groups,
                        "evidence": plan["evidence"],
                        "sources": plan["sources"],
                        "context_segments": 3,
                        "scope": {"only_file": only_file},
                        "created_at": utc_now(),
                    }
                    atomic_write_text(
                        repetition_draft_path(root, run_id),
                        json.dumps(draft, ensure_ascii=False, separators=(",", ":")) + "\n",
                    )
                    manifest = read_json(root, run_dir / "manifest.json")
                    manifest.update(draft_status="pending", choices={})
                    manifests = [manifest]
                    if old and old["run_id"] != run_id:
                        old["draft_status"] = "replaced"
                        manifests.append(old)
                    warnings = write_repetition_state(
                        root, records=[], manifests=manifests
                    )
                    if warnings:
                        manifest["warnings"] = list(
                            dict.fromkeys([*manifest.get("warnings", []), *warnings])
                        )
                        write_json(root, run_dir / "manifest.json", manifest)
                    return {
                        "created_draft_run_id": run_id,
                        "total_groups": len(draft_groups),
                        "excluded_punctuation_groups": plan["excluded_punctuation_groups"],
                        "warnings": warnings,
                    }

            segments = load_segments(root)
            selected = [
                s
                for s in segments
                if not s["is_empty"]
                and (only_file is None or s["file_id"] == only_file)
                and (only_part is None or s["part_id"] == only_part)
            ]
            if not selected:
                raise UsageError("指定扫描范围为空或不存在")
            states = latest_stage_states(
                root, "translation", [str(s["segment_id"]) for s in selected]
            )
            bases = {
                sid: state["completed"]
                for sid, state in states.items()
                if state.get("completed") is not None
            }
            by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for s in selected:
                sid = str(s["segment_id"])
                if sid in bases:
                    by_source[str(s["source"])].append(s)
            candidates = [
                items
                for items in by_source.values()
                if len(items) > 1
                and len({bases[str(s["segment_id"])]["text"] for s in items}) > 1
            ]
            excluded_punctuation_groups = 0
            if exclude_punctuation_only:
                retained = []
                for items in candidates:
                    variants = {bases[str(s["segment_id"])]["text"] for s in items}
                    bodies = {
                        "".join(
                            c for c in text if not unicodedata.category(c).startswith("P")
                        )
                        for text in variants
                    }
                    if len(bodies) == 1:
                        excluded_punctuation_groups += 1
                    else:
                        retained.append(items)
                candidates = retained

            indexed_candidates = list(enumerate(candidates, 1))
            if sort_by == "occurrence_count":
                indexed_candidates.sort(key=lambda pair: (-len(pair[1]), pair[0]))
            paged_candidates = indexed_candidates[offset : offset + limit]
            groups = []
            for idx, items in paged_candidates:
                variants = list(
                    dict.fromkeys(bases[str(s["segment_id"])]["text"] for s in items)
                )
                translation_counts: dict[str, int] = {}
                for s in items:
                    t_text = str(bases[str(s["segment_id"])]["text"])
                    translation_counts[t_text] = translation_counts.get(t_text, 0) + 1
                group_entry: dict[str, Any] = {
                    "group_index": idx,
                    "source": str(items[0]["source"]),
                    "occurrence_count": len(items),
                    "translations": variants,
                    "translation_counts": translation_counts,
                }
                if include_occurrences:
                    group_entry["occurrences"] = [
                        {
                            "occurrence_id": str(occ_idx),
                            "segment_id": str(s["segment_id"]),
                            "file_id": s["file_id"],
                            "part_id": s["part_id"],
                            "line_index": s["line_index"],
                            "translation": bases[str(s["segment_id"])]["text"],
                        }
                        for occ_idx, s in enumerate(items[:20], 1)
                    ]
                    if len(items) > 20:
                        group_entry["occurrences_truncated"] = True
                groups.append(group_entry)
            return {
                "total_segments_in_scope": len(selected),
                "translated_segments_in_scope": len(bases),
                "excluded_punctuation_groups": excluded_punctuation_groups,
                "total_groups": len(indexed_candidates),
                "sort_by": sort_by,
                "offset": offset,
                "limit": limit,
                "groups": groups,
            }

        return await run_blocking(_scan)

    @app.post("/api/v1/projects/{name}/agent/terms/complete-scan")
    async def complete_term_scan(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        root = project(name)
        if app.state.tasks.is_project_running(root):
            raise UsageError("项目存在运行中的任务，不能标记术语扫描状态")
        scope_payload = (
            payload.get("scope")
            if isinstance(payload.get("scope"), dict)
            else {
                k: payload[k]
                for k in ("from_file", "only_file", "only_part", "only_segment", "segment_ids")
                if k in payload and payload[k] is not None
            }
        )
        scope = _parse_scope(root, {"scope": scope_payload})

        def _mark() -> dict[str, Any]:
            with project_write_lock(root, wait_seconds=15.0):
                store = WebStore(root)
                files = load_source_files(root)
                segments = load_segments(root)
                all_nonempty = [s for s in segments if not s["is_empty"]]
                selected = select_scope(segments, files, scope)
                active_path = root / "terminology" / "active_task.json"
                active = (
                    read_json(root, active_path)
                    if record_exists(root, active_path)
                    else None
                )
                reset_task = payload.get("reset_task") is True
                if active is None or reset_task or not active.get("active_task_id"):
                    task_id = f"TERM-TASK-{uuid.uuid4().hex[:10].upper()}"
                else:
                    task_id = str(active["active_task_id"])
                all_ids = [str(s["segment_id"]) for s in all_nonempty]
                done_ids, _ = terminology_scan_state(root, task_id, all_ids)
                fingerprint = store._fingerprint("terminology")
                new_records = [
                    record_header(
                        "terminology_scan",
                        store.project_id,
                        stage="terminology",
                        segment_id=str(s["segment_id"]),
                        status="completed",
                        run_id=None,
                        request_id=None,
                        active_task_id=task_id,
                        stage_fingerprint=fingerprint,
                        origin="agent_mcp",
                    )
                    for s in selected
                    if str(s["segment_id"]) not in done_ids
                ]
                if new_records:
                    append_jsonl_records(
                        root, root / "terminology" / "scans.jsonl", new_records
                    )
                    done_ids.update(r["segment_id"] for r in new_records)
                all_done = len(done_ids) >= len(all_nonempty)
                library = load_terms(root, resolve_knowledge=False)
                revision = int(library["terms_revision"]) if library else 0
                active_record = record_header(
                    "terminology_task",
                    store.project_id,
                    record_id=task_id,
                    active_task_id=task_id,
                    status="completed" if all_done else "active",
                    initial_stage_fingerprint=(active or {}).get("initial_stage_fingerprint")
                    or fingerprint,
                    terms_revision=revision,
                )
                write_json(root, active_path, active_record)
                return {
                    "active_task_id": task_id,
                    "status": active_record["status"],
                    "newly_marked": len(new_records),
                    "completed_segments": len(done_ids),
                    "total_segments": len(all_nonempty),
                    "stage_completion": store.stage_completion()["terminology"],
                }

        return await run_blocking(_mark)
