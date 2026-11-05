from __future__ import annotations

import asyncio
import json
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
)
from .locking import project_write_lock
from .plugins import normalize_model_text
from .project import load_segments, load_source_files
from .repetition_consistency import (
    current_draft as current_repetition_draft,
    draft_path as repetition_draft_path,
    parse_comparison,
    read_draft as read_repetition_draft,
)
from .sqlite_storage import (
    append_jsonl,
    append_stage_results,
    atomic_write_text,
    latest_stage_states,
    query_segment_neighbors,
    read_json,
    read_repetition_run,
    record_exists,
    record_header,
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


def _parse_scope(payload: dict[str, Any]) -> Scope:
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
    return Scope(
        from_file=raw.get("from_file"),
        only_file=raw.get("only_file"),
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
    force = bool((manifest.get("scope") or {}).get("force", False))
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


def register_agent_routes(
    *,
    app: FastAPI,
    projects_root: Path,
    app_root: Path,
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
            with project_write_lock(root):
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
        scope = _parse_scope(payload)
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
        task_view = await app.state.tasks.register_agent_task(
            root,
            prepared["project_id"],
            stage,
            prepared["run_id"],
            prepared["selected_count"],
            prepared["completed_count"],
        )
        return {**prepared, "task_id": task_view["task_id"]}

    @app.post("/api/v1/projects/{name}/agent/runs/{run_id}/checkout")
    async def checkout_agent_batch(
        name: str, run_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        root = project(name)
        safe_id = _safe_run_id(run_id)
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
            raw_scope = manifest.get("scope") or {}
            scope = Scope(
                from_file=raw_scope.get("from_file"),
                only_file=raw_scope.get("only_file"),
                only_segment=raw_scope.get("only_segment"),
                segment_ids=(
                    tuple(str(v) for v in raw_scope["segment_ids"])
                    if isinstance(raw_scope.get("segment_ids"), list)
                    else None
                ),
                force=bool(raw_scope.get("force", False)),
            )
            selected = select_scope(segments, files, scope)
            pending, completed_count, remaining_count = _agent_run_progress(
                root, manifest, selected
            )
            if not pending:
                return {
                    "run_id": safe_id,
                    "stage": stage,
                    "batch": [],
                    "terms": [],
                    "reference_context": [],
                    "completed_count": completed_count,
                    "remaining_pending_count": 0,
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
            neighbor_states = (
                latest_stage_states(root, "translation", neighbor_ids)
                if neighbor_ids
                else {}
            )
            reference_context = [
                {
                    "segment_id": str(item["segment_id"]),
                    "source": str(item["source"]),
                    "translation": (
                        neighbor_states.get(str(item["segment_id"]), {})
                        .get("completed", {})
                        or {}
                    ).get("text"),
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
                entry: dict[str, Any] = {
                    "segment_id": sid,
                    "file_id": boundary[0],
                    "part_id": boundary[1],
                    "line_index": int(item["line_index"]),
                    "source": str(item["source"]),
                    "model_source": segment_model_source(item),
                }
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
                "remaining_pending_count": remaining_count,
                "warnings": warnings,
            }

        return await run_blocking(_checkout)

    @app.post("/api/v1/projects/{name}/agent/runs/{run_id}/submit")
    async def submit_agent_batch(
        name: str, run_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        root = project(name)
        safe_id = _safe_run_id(run_id)
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            raise UsageError("items 必须是非空数组")
        allow_warnings = payload.get("allow_warnings", False)
        if not isinstance(allow_warnings, bool):
            raise UsageError("allow_warnings 必须是布尔值")

        def _submit() -> dict[str, Any]:
            with project_write_lock(root):
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
                raw_scope = manifest.get("scope") or {}
                scope = Scope(
                    from_file=raw_scope.get("from_file"),
                    only_file=raw_scope.get("only_file"),
                    only_segment=raw_scope.get("only_segment"),
                    segment_ids=(
                        tuple(str(v) for v in raw_scope["segment_ids"])
                        if isinstance(raw_scope.get("segment_ids"), list)
                        else None
                    ),
                    force=bool(raw_scope.get("force", False)),
                )
                selected = select_scope(segments, files, scope)
                selected_by_id = {str(item["segment_id"]): item for item in selected}
                language = str(manifest.get("prompt_language") or _prompt_language(root, stage, None))
                validators = config["_translation_validator_instances"]
                fingerprint = str(manifest.get("stage_fingerprint") or store._fingerprint(stage))
                terms_revision = manifest.get("terms_revision")
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
                        raw_text = entry.get("text")
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
                        context = store._translation_validation_context(segment, text)
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
                            context = store._translation_validation_context(segment, suggested_text)
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
                _, completed_count, remaining_count = _agent_run_progress(
                    root, manifest, selected
                )
                return {
                    "run_id": safe_id,
                    "stage": stage,
                    "committed_ids": committed_ids,
                    "rejected_items": rejected_items,
                    "completed_count": completed_count,
                    "remaining_pending_count": remaining_count,
                }

        result = await run_blocking(_submit)
        app.state.tasks.update_agent_task(
            root,
            safe_id,
            completed_segments=result["completed_count"],
            failed_segments=len(result["rejected_items"]),
        )
        return result

    @app.post("/api/v1/projects/{name}/agent/runs/{run_id}/finish")
    async def finish_agent_run(
        name: str, run_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        root = project(name)
        safe_id = _safe_run_id(run_id)
        action = payload.get("action", "complete")
        if action not in {"complete", "interrupt"}:
            raise UsageError("action 必须是 complete 或 interrupt")
        reason = payload.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise UsageError("reason 必须是字符串")

        def _finish() -> dict[str, Any]:
            with project_write_lock(root):
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
                _, _, files, segments = _project_context(root, stage=stage)
                raw_scope = manifest.get("scope") or {}
                scope = Scope(
                    from_file=raw_scope.get("from_file"),
                    only_file=raw_scope.get("only_file"),
                    only_segment=raw_scope.get("only_segment"),
                    segment_ids=(
                        tuple(str(v) for v in raw_scope["segment_ids"])
                        if isinstance(raw_scope.get("segment_ids"), list)
                        else None
                    ),
                    force=bool(raw_scope.get("force", False)),
                )
                selected = select_scope(segments, files, scope)
                _, completed_count, remaining_count = _agent_run_progress(
                    root, manifest, selected
                )
                reused_count = int(manifest.get("reused_segment_count", 0))
                newly_completed = max(0, completed_count - reused_count)
                if action == "complete":
                    if remaining_count > 0:
                        raise IncompleteError(
                            f"Run {safe_id} 仍有 {remaining_count} 个待处理 Segment 未完成；请继续处理或使用 action='interrupt'"
                        )
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
        if not isinstance(window, int) or isinstance(window, bool) or not 1 <= window <= 30:
            raise UsageError("window 必须是 1 到 30 之间的整数")

        def _inspect() -> dict[str, Any]:
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
            non_empty = [s for s in segments if not s["is_empty"]]
            if target_group is None:
                if not isinstance(source_text, str) or not source_text:
                    raise UsageError(f"未在当前重复译文草案中找到组：{group_id}")
                matched_segments = [s for s in non_empty if s["source"] == source_text]
                if len(matched_segments) < 2:
                    raise UsageError("指定 source 未找到重复出现的非空 Segment")
                occ_ids = [str(s["segment_id"]) for s in matched_segments]
            else:
                source_text_val = str(target_group["source"])
                occ_ids = [str(o["segment_id"]) for o in target_group["occurrences"]]
                source_text = source_text_val

            states = latest_stage_states(root, "translation", occ_ids)
            occurrences = []
            for idx, sid in enumerate(occ_ids, 1):
                seg = by_id[sid]
                before, after = query_segment_neighbors(
                    root,
                    file_id=str(seg["file_id"]),
                    part_id=str(seg["part_id"]),
                    line_index=int(seg["line_index"]),
                    before_limit=window,
                    after_limit=window,
                )
                ctx_ids = [str(item["segment_id"]) for item in (*before, *after)]
                ctx_states = latest_stage_states(root, "translation", ctx_ids) if ctx_ids else {}
                occurrences.append(
                    {
                        "occurrence_id": str(idx),
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
                                    ctx_states.get(str(item["segment_id"]), {}).get("completed")
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
                                    ctx_states.get(str(item["segment_id"]), {}).get("completed")
                                    or {}
                                ).get("text"),
                            }
                            for item in after
                        ],
                    }
                )
            return {
                "group_id": target_group["id"] if target_group else None,
                "source": source_text,
                "draft_group": target_group,
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
                    if not isinstance(entry, dict) or not isinstance(entry.get("group_id"), str):
                        raise UsageError("每个 group_proposals 项必须包含字符串 group_id")
                    gid = entry["group_id"]
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
