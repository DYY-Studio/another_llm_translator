from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from .config import load_project_config
from .errors import ConfigError, IncompleteError, UsageError
from .execution import (
    Scope,
    choose_running_run,
    combine_usage,
    find_running_runs,
    select_scope,
    stage_fingerprint,
    unavailable_usage,
)
from .llm_client import SlidingWindowLimiter
from .llm_keys import KeyPool
from .project import load_segments, load_source_files
from .sqlite_storage import (
    latest_stage_summary,
    read_json,
    read_jsonl,
    record_exists,
)
from .stage_review import run_review
from .stage_runtime import prompt_middle_digests
from .stage_terminology import run_terminology
from .stage_translation import run_translation
from .term_decision import (
    DECISION_RULES_VERSION,
    _decision_fingerprint,
    decision_checkpoint_progress,
    decision_plan,
    decision_resume_compatibility,
    run_terminology_decision,
)
from .term_decision_drafts import (
    apply_decision_draft,
    current_decision_draft,
    manual_review_state,
)
from .term_library import load_terms

CONTINUOUS_STAGE = "continuous"
CONTINUOUS_STAGES = (
    "terminology",
    "terminology_decision",
    "translation",
    "proofreading",
    "polishing",
)
CONTINUOUS_START_STAGES = (
    "terminology",
    "translation",
    "proofreading",
)


def normalize_stages(stages: Iterable[object]) -> tuple[str, ...]:
    if isinstance(stages, (str, bytes)):
        raise UsageError("连续运行 stages 必须是非空数组")
    values = tuple(stages)
    if not values:
        raise UsageError("连续运行至少选择一个阶段")
    if any(type(value) is not str or not value for value in values):
        raise UsageError("连续运行 stages 必须是非空字符串")
    if len(set(values)) != len(values):
        raise UsageError("连续运行 stages 不能重复")
    unknown = [value for value in values if value not in CONTINUOUS_STAGES]
    if unknown:
        raise UsageError(f"连续运行包含未知阶段：{', '.join(unknown)}")
    if values[0] not in CONTINUOUS_START_STAGES:
        raise UsageError("连续运行起点只能是术语、翻译或校对")
    positions = [CONTINUOUS_STAGES.index(value) for value in values]
    if positions != list(range(positions[0], positions[-1] + 1)):
        raise UsageError("连续运行 stages 必须是 canonical 顺序中的连续阶段")
    return values


def _stable_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def stage_fingerprint_snapshot(project: Path, stage: str) -> str:
    config = load_project_config(project, stage=stage)
    library = load_terms(project)
    terms_revision = (
        int(library["terms_revision"])
        if stage != "terminology" and library is not None
        else None
    )
    return stage_fingerprint(
        config,
        stage,
        prompt_middle_digests(project, stage),
        terms_revision=terms_revision,
    )


def selection_snapshot(
    project: Path,
    scope: Scope,
    *,
    force_all: bool = False,
) -> tuple[tuple[str, str, int, int, str, str, str, str], ...]:
    files = load_source_files(project)
    segments = load_segments(project)
    selected = (
        [segment for segment in segments if not segment["is_empty"]]
        if force_all
        else select_scope(segments, files, scope)
    )
    file_order = {
        str(item["file_id"]): int(item["file_order"]) for item in files
    }
    adapter_snapshots: dict[str, str] = {}
    for file_record in files:
        state_path = file_record.get("document_adapter_state")
        state_record = (
            read_json(project, project / state_path)
            if isinstance(state_path, str)
            and record_exists(project, project / state_path)
            else None
        )
        adapter_snapshots[str(file_record["file_id"])] = _stable_digest(
            {
                "adapter_id": file_record.get("document_adapter_id"),
                "adapter_version": file_record.get("document_adapter_version"),
                "state_path": state_path,
                "state": state_record,
            }
        )
    return tuple(
        (
            str(segment["segment_id"]),
            str(segment["file_id"]),
            file_order[str(segment["file_id"])],
            int(segment["line_index"]),
            str(segment["part_id"]),
            str(segment["source"]),
            str(segment.get("model_source") or ""),
            adapter_snapshots[str(segment["file_id"])],
        )
        for segment in selected
    )


def stage_summary(
    project: Path,
    stage: str,
    config: dict[str, Any],
    *,
    active_segment_ids: set[str],
    nonempty_count: int,
    terms_revision: int | None,
) -> dict[str, Any]:
    summary = latest_stage_summary(project, stage, active_segment_ids)
    completed = {
        segment_id: item for segment_id, item in summary.items() if item["completed"]
    }
    failed = {
        segment_id for segment_id, item in summary.items() if item["failed"]
    }
    current_fingerprint = stage_fingerprint(
        config,
        stage,
        prompt_middle_digests(project, stage),
        terms_revision=terms_revision,
    )
    current_completed = sum(
        item["stage_fingerprint"] == current_fingerprint
        for item in completed.values()
    )
    return {
        "completed": len(completed),
        "failed": len(failed),
        "pending": nonempty_count - len(completed) - len(failed),
        "current_fingerprint_completed": current_completed,
        "mismatched_fingerprint_completed": max(0, len(completed) - current_completed),
    }


def terminology_summary(
    project: Path,
    config: dict[str, Any],
    *,
    active_segment_ids: set[str],
    nonempty_count: int,
) -> dict[str, Any]:
    base = {
        "completed": 0,
        "failed": 0,
        "pending": nonempty_count,
        "current_fingerprint_completed": 0,
        "mismatched_fingerprint_completed": 0,
    }
    active_path = project / "terminology" / "active_task.json"
    if not record_exists(project, active_path):
        return base
    active = read_json(project, active_path)
    if active.get("status") not in {"active", "completed"}:
        return base
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
    current_fingerprint = stage_fingerprint(
        config,
        "terminology",
        prompt_middle_digests(project, "terminology"),
    )
    current_completed = sum(
        item.get("status") == "completed"
        and item.get("stage_fingerprint") == current_fingerprint
        for item in scans
    )
    return {
        "completed": len(completed),
        "failed": len(failed),
        "pending": nonempty_count - len(completed) - len(failed),
        "current_fingerprint_completed": current_completed,
        "mismatched_fingerprint_completed": max(0, len(completed) - current_completed),
    }


def _preset_summary(config: Mapping[str, Any]) -> dict[str, str]:
    return {
        "id": str(config["_llm_preset_id"]),
        "model": str(config["llm"]["model"]),
    }


def _run_summary(run: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "run_id": str(run.get("run_id", "")),
        "status": str(run.get("status", "running")),
        "started_at": run.get("started_at"),
        "stage_fingerprint": run.get("stage_fingerprint"),
        "source_terms_revision": run.get("source_terms_revision"),
    }


def _limiter_identity(config: Mapping[str, Any]) -> tuple[str, str]:
    return str(config["_llm_preset_id"]), str(config["_llm_preset_hash"])


def _limiter_limits(config: Mapping[str, Any]) -> tuple[int, int, int, int]:
    execution = config["execution"]
    return (
        int(execution["requests_per_minute"]),
        int(execution["input_tokens_per_minute"]),
        int(execution["max_parallel"]),
        int(execution.get("max_parallel_per_key", execution["max_parallel"])),
    )


def _blocking(code: str, message: str, *, stage: str | None = None) -> dict[str, str]:
    result = {"code": code, "message": message}
    if stage is not None:
        result["stage"] = stage
    return result


def _decision_inputs_snapshot(plan: Mapping[str, Any]) -> dict[str, Any]:
    values = {
        "terms_revision": plan["library"]["terms_revision"],
        "overrides": plan["overrides_document"],
        "protected": sorted(str(value) for value in plan["protected"]),
        "eligible": plan["eligible"],
        "states": plan["states"],
        "source_conflicts": plan["source_conflicts"],
        "evidence": plan["evidence"],
        "language": plan["language"],
        "final_review": bool(plan.get("final_review", False)),
    }
    return {
        "status": "ready",
        "terms_revision": int(plan["library"]["terms_revision"]),
        "eligible": len(plan["eligible"]),
        "digest": _stable_digest(values),
    }


def _translation_complete(project: Path, segment_ids: tuple[str, ...]) -> bool:
    summary = latest_stage_summary(project, "translation", segment_ids)
    return len(summary) == len(segment_ids) and all(
        bool(item.get("completed")) and not bool(item.get("failed"))
        for item in summary.values()
    )


def inspect_continuous(
    project: Path,
    stages: Iterable[object],
    *,
    prompt_language: str | None = None,
    force: bool = False,
    reuse_mixed_fingerprints: bool = False,
    run_actions: Mapping[str, str] | None = None,
    apply_terminology_decision: bool = False,
    final_review: bool = False,
) -> dict[str, Any]:
    normalized = normalize_stages(stages)
    actions = dict(run_actions or {})
    unknown_actions = sorted(set(actions) - set(normalized))
    if unknown_actions:
        raise UsageError(
            "连续运行 run_actions 包含未选择阶段："
            + ", ".join(unknown_actions)
        )
    invalid_actions = sorted(
        stage for stage, action in actions.items() if action not in {"resume", "decline"}
    )
    if invalid_actions:
        raise UsageError("run_actions 必须是 resume 或 decline")
    if force and reuse_mixed_fingerprints:
        raise UsageError("连续运行 force 与 reuse_mixed_fingerprints 不能同时使用")
    has_decision = "terminology_decision" in normalized
    if final_review and not has_decision:
        raise UsageError("final_review 只允许连续运行中的自动术语决策")
    if apply_terminology_decision and not has_decision:
        raise UsageError("apply_terminology_decision 只允许连续运行中的自动术语决策")

    selection = selection_snapshot(project, Scope(), force_all=True)
    if not selection:
        raise UsageError("项目没有可处理的非空 Segment；请先添加源文件")
    segments = tuple(item[0] for item in selection)

    terms = load_terms(project)
    published_terms = (
        terms if terms is not None and bool(terms.get("terms")) else None
    )
    configs = {
        stage: load_project_config(project, stage=stage) for stage in normalized
    }
    limiter_identities: dict[tuple[str, str], tuple[int, int, int, int]] = {}
    for stage, config in configs.items():
        identity = _limiter_identity(config)
        limits = _limiter_limits(config)
        previous = limiter_identities.get(identity)
        if previous is not None and previous != limits:
            raise ConfigError("相同 Preset 身份的共享限流配置不一致")
        limiter_identities[identity] = limits

    decision_final_review = (
        final_review
        if has_decision and normalized[-1] == "terminology_decision"
        else has_decision
    )
    requires_decision_apply = has_decision and normalized[-1] != "terminology_decision"

    running_runs: dict[str, list[dict[str, Any]]] = {}
    blocking: list[dict[str, str]] = []
    for stage in normalized:
        runs = [_run_summary(item) for item in find_running_runs(project, stage)]
        running_runs[stage] = runs
        action = actions.get(stage)
        if runs and action is None:
            blocking.append(
                _blocking(
                    "run_action_required",
                    "存在未完成 Run，必须明确选择 resume 或 decline",
                    stage=stage,
                )
            )
        elif not runs and action is not None:
            raise UsageError(f"阶段 {stage} 没有可执行 run_action 的 running Run")
        if stage == "terminology_decision" and runs:
            source_revision = (
                int(published_terms["terms_revision"])
                if published_terms is not None
                else -1
            )
            for run in runs:
                compatible, reason = decision_resume_compatibility(
                    project,
                    str(run["run_id"]),
                    source_terms_revision=source_revision,
                    final_review=decision_final_review,
                )
                run["completed_steps"] = (
                    decision_checkpoint_progress(project, str(run["run_id"]))
                    if compatible
                    else 0
                )
                run["resume_compatible"] = compatible
                run["resume_incompatibility_reason"] = reason
            if action == "resume" and not runs[0]["resume_compatible"]:
                blocking.append(
                    _blocking(
                        "decision_resume_incompatible",
                        str(
                            runs[0]["resume_incompatibility_reason"]
                            or "自动术语决策 Run 不兼容"
                        ),
                        stage=stage,
                    )
                )
        if action == "resume" and (force or reuse_mixed_fingerprints):
            blocking.append(
                _blocking(
                    "resume_policy_conflict",
                    "连续运行续用 Run 时不能同时指定 force 或复用结果",
                    stage=stage,
                )
            )
        if stage == "terminology_decision" and action == "decline" and not force:
            blocking.append(
                _blocking(
                    "decision_decline_requires_force",
                    "结束自动决策 Run 时必须同时指定 force",
                    stage=stage,
                )
            )

    selections = {
        stage: list(selection)
        for stage in normalized
        if stage != "terminology_decision"
    }
    if "terminology_decision" in normalized:
        selections["terminology_decision"] = (
            sorted(str(item["normalized"]) for item in published_terms["terms"])
            if published_terms is not None
            else []
        )

    fingerprints: dict[str, str] = {}
    for stage in normalized:
        if stage == "terminology_decision":
            fingerprints[stage] = _stable_digest(
                {
                    "stage": stage,
                    "config_prompt_fingerprint": stage_fingerprint(
                        configs[stage],
                        stage,
                        prompt_middle_digests(project, stage),
                        terms_revision=(
                            int(terms["terms_revision"]) if terms is not None else None
                        ),
                    ),
                    "terms_revision": (
                        int(terms["terms_revision"]) if terms is not None else None
                    ),
                    "final_review": decision_final_review,
                    "decision_rules_version": DECISION_RULES_VERSION,
                }
            )
        else:
            fingerprints[stage] = stage_fingerprint(
                configs[stage],
                stage,
                prompt_middle_digests(project, stage),
                terms_revision=(
                    int(terms["terms_revision"])
                    if terms is not None and stage != "terminology"
                    else None
                ),
            )

    stage_summaries: dict[str, dict[str, Any]] = {}
    for stage in normalized:
        if stage == "terminology_decision":
            continue
        stage_summaries[stage] = (
            terminology_summary(
                project,
                configs[stage],
                active_segment_ids=set(segments),
                nonempty_count=len(segments),
            )
            if stage == "terminology"
            else stage_summary(
                project,
                stage,
                configs[stage],
                active_segment_ids=set(segments),
                nonempty_count=len(segments),
                terms_revision=(
                    int(terms["terms_revision"]) if terms is not None else None
                ),
            )
        )
        if (
            stage_summaries[stage]["mismatched_fingerprint_completed"]
            and not force
            and not reuse_mixed_fingerprints
        ):
            blocking.append(
                _blocking(
                    "mismatched_fingerprint",
                    "存在不同设置指纹的已完成结果，必须明确选择复用或 force",
                    stage=stage,
                )
            )

    decision_inputs: dict[str, Any] | None = None
    decision_plan_for_fingerprint: Mapping[str, Any] | None = None
    if has_decision:
        if requires_decision_apply and not apply_terminology_decision:
            blocking.append(
                _blocking(
                    "apply_terminology_decision_required",
                    "连续运行包含自动术语决策，必须明确授权应用决策",
                    stage="terminology_decision",
                )
            )
        if current_decision_draft(project) is not None:
            blocking.append(
                _blocking(
                    "pending_decision_draft",
                    "存在待处理术语决策草案，必须先处理后才能启动连续运行",
                    stage="terminology_decision",
                )
            )
        if manual_review_state(project)["remaining"] > 0:
            blocking.append(
                _blocking(
                    "pending_manual_review",
                    "存在未处理人工待办，必须先处理后才能启动连续运行",
                    stage="terminology_decision",
                )
            )
        if published_terms is None:
            decision_inputs = {
                "status": "skipped",
                "reason": "no_published_terms",
                "digest": _stable_digest({"status": "skipped", "reason": "no_published_terms"}),
            }
        else:
            overrides_document = read_json(
                project, project / "terminology" / "overrides.json"
            )
            protected = {
                str(item["normalized"])
                for item in overrides_document.get("overrides", [])
            }
            has_eligible = any(
                str(item["normalized"]) not in protected
                and not bool(item.get("disabled", False))
                for item in published_terms.get("terms", [])
            )
            if not has_eligible:
                decision_inputs = {
                    "status": "skipped",
                    "reason": "no_work",
                    "digest": _stable_digest(
                        {
                            "status": "skipped",
                            "reason": "no_work",
                            "terms_revision": published_terms["terms_revision"],
                            "overrides": overrides_document,
                        }
                    ),
                }
            else:
                plan = decision_plan(
                    project,
                    prompt_language,
                    final_review=decision_final_review,
                )
                decision_plan_for_fingerprint = plan
                decision_inputs = _decision_inputs_snapshot(plan)
        if (
            decision_inputs is not None
            and decision_inputs["status"] == "skipped"
            and running_runs["terminology_decision"]
            and actions.get("terminology_decision") == "resume"
        ):
            blocking.append(
                _blocking(
                    "decision_resume_skipped_requires_decline",
                    "自动术语决策当前将跳过，不能续用旧 Run；请改选 decline，并同时指定 force",
                    stage="terminology_decision",
                )
            )
        if decision_plan_for_fingerprint is not None:
            fingerprints["terminology_decision"] = _decision_fingerprint(
                decision_plan_for_fingerprint["config"],
                decision_plan_for_fingerprint["prompts"],
                decision_plan_for_fingerprint["library"],
                final_review=decision_final_review,
            )
    if normalized[0] == "proofreading" and not _translation_complete(project, segments):
        blocking.append(
            _blocking(
                "translation_incomplete",
                "校对作为连续运行起点时要求整项目翻译 100% 完成",
                stage="proofreading",
            )
        )

    options = {
        "force": bool(force),
        "reuse_mixed_fingerprints": bool(reuse_mixed_fingerprints),
        "run_actions": {stage: actions[stage] for stage in sorted(actions)},
        "apply_terminology_decision": bool(apply_terminology_decision),
        "final_review": bool(decision_final_review),
        "prompt_language": prompt_language,
        "whole_project": True,
    }
    snapshot = {
        "stages": list(normalized),
        "selections": _stable_digest(selections),
        "fingerprints": fingerprints,
        "running_runs": _stable_digest(running_runs),
        "decision_inputs": (
            decision_inputs["digest"] if decision_inputs is not None else None
        ),
        "options": _stable_digest(options),
    }
    steps: list[dict[str, Any]] = []
    for stage in normalized:
        stage_running = running_runs[stage]
        if stage == "terminology_decision" and decision_inputs is not None:
            status = str(decision_inputs["status"])
            step: dict[str, Any] = {
                "stage": stage,
                "status": status,
                "selected": len(selections[stage]),
                "preset": _preset_summary(configs[stage]),
                "running_run": stage_running[0] if stage_running else None,
            }
            if status == "skipped":
                step["reason"] = str(decision_inputs["reason"])
        else:
            step = {
                "stage": stage,
                "status": "ready",
                "selected": len(selections.get(stage, segments)),
                "preset": _preset_summary(configs[stage]),
                "running_run": stage_running[0] if stage_running else None,
            }
            if stage in stage_summaries:
                step.update(stage_summaries[stage])
        steps.append(step)
    return {
        "stage": CONTINUOUS_STAGE,
        "stages": list(normalized),
        "steps": steps,
        "presets": {stage: _preset_summary(config) for stage, config in configs.items()},
        "running_runs": running_runs,
        "blocking": blocking,
        "rules": {
            "canonical_order": list(CONTINUOUS_STAGES),
            "start_stages": list(CONTINUOUS_START_STAGES),
            "whole_project": True,
            "decision_requires_apply": requires_decision_apply,
            "decision_final_review": decision_final_review,
        },
        "selections": {stage: len(values) for stage, values in selections.items()},
        "fingerprints": fingerprints,
        "decision_inputs": decision_inputs,
        "options": options,
        "snapshot": snapshot,
    }


def require_continuous_ready(result: Mapping[str, Any]) -> None:
    blocking = result.get("blocking")
    if not isinstance(blocking, list) or not blocking:
        return
    first = blocking[0]
    if isinstance(first, Mapping):
        raise UsageError(
            str(first.get("message", "连续运行预检未通过")),
            reason=str(first.get("code", "continuous_preflight")),
        )
    raise UsageError("连续运行预检未通过", reason="continuous_preflight")


def _decision_plan_or_skip(
    project: Path,
    prompt_language: str | None,
    *,
    final_review: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    library = load_terms(project)
    if library is None or not library.get("terms"):
        return None, "no_published_terms"
    overrides_document = read_json(
        project, project / "terminology" / "overrides.json"
    )
    protected = {
        str(item["normalized"])
        for item in overrides_document.get("overrides", [])
    }
    has_eligible = any(
        str(item["normalized"]) not in protected
        and not bool(item.get("disabled", False))
        for item in library.get("terms", [])
    )
    if not has_eligible:
        return None, "no_work"
    return (
        decision_plan(project, prompt_language, final_review=final_review),
        None,
    )


def _continuous_step_summary(
    stage: str,
    summary: Mapping[str, Any],
    *,
    selected_hint: int = 0,
    run_id: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    selected_value = summary.get("selected", summary.get("eligible", selected_hint))
    selected = int(selected_value) if isinstance(selected_value, int) else selected_hint
    reused = int(summary.get("reused", 0))
    completed = int(summary.get("completed", 0)) + reused
    failed = int(summary.get("failed", 0))
    pending_value = summary.get("pending")
    pending = (
        int(pending_value)
        if isinstance(pending_value, int) and not isinstance(pending_value, bool)
        else max(0, selected - completed - failed)
    )
    if summary.get("dry_run") is True:
        pending = 0
    step_status = status or (
        "failed" if failed or pending else "completed"
    )
    step = {
        "stage": stage,
        "status": step_status,
        "selected": selected,
        "completed": completed,
        "failed": failed,
        "pending": pending,
    }
    if run_id is not None:
        step["run_id"] = run_id
    step["summary"] = dict(summary)
    return step


async def run_continuous(
    project: Path,
    scope: Scope,
    stages: Iterable[object],
    *,
    limiters: Mapping[
        tuple[str, str], SlidingWindowLimiter | KeyPool
    ],
    reuse_mixed_fingerprints: bool = False,
    run_actions: Mapping[str, str] | None = None,
    apply_terminology_decision: bool = False,
    final_review: bool = False,
    prompt_language: str | None = None,
    planned_steps: Iterable[Mapping[str, Any]] = (),
    on_stage: Callable[[str, str, Mapping[str, Any]], None] | None = None,
    on_progress: Callable[[str, int, int, int], None] | None = None,
    on_usage: Callable[[dict[str, Any] | None], None] | None = None,
) -> dict[str, Any]:
    normalized = normalize_stages(stages)
    if "polishing" in normalized:
        raise UsageError("校对自动应用将在下一节点接入")
    actions = dict(run_actions or {})
    has_decision = "terminology_decision" in normalized
    decision_final_review = (
        final_review
        if has_decision and normalized[-1] == "terminology_decision"
        else has_decision
    )
    if has_decision and normalized[-1] != "terminology_decision" and not apply_terminology_decision:
        raise UsageError("连续运行包含自动术语决策，必须明确授权应用决策")

    planned = {
        str(step["stage"]): int(step.get("selected", 0))
        for step in planned_steps
    }
    steps: list[dict[str, Any]] = []
    summaries: dict[str, dict[str, Any]] = {}
    usage_by_stage: dict[str, dict[str, Any] | None] = {}
    usage_base_by_stage: dict[str, dict[str, Any] | None] = {}
    stage_limiters: dict[str, SlidingWindowLimiter | KeyPool] = {}
    for stage in normalized:
        config = load_project_config(project, stage=stage)
        identity = _limiter_identity(config)
        try:
            stage_limiters[stage] = limiters[identity]
        except KeyError as exc:
            raise ConfigError(f"连续运行缺少阶段共享限流器：{stage}") from exc

    def report_usage(stage: str, current: dict[str, Any] | None) -> None:
        usage_by_stage[stage] = current
        if on_usage is None:
            return
        aggregate: dict[str, Any] | None = None
        for stage_name in normalized:
            base = usage_base_by_stage.get(stage_name)
            current_value = usage_by_stage.get(stage_name)
            value = (
                combine_usage(base, current_value)
                if base is not None
                else current_value
            )
            aggregate = combine_usage(aggregate, value)
        on_usage(aggregate or unavailable_usage())

    def report_progress(stage: str, completed: int, failed: int, total: int) -> None:
        for step in steps:
            if step["stage"] == stage:
                step.update(
                    completed=completed,
                    failed=failed,
                    pending=max(0, total - completed - failed),
                    selected=total,
                )
                break
        if on_progress is not None:
            on_progress(stage, completed, failed, total)

    def emit(stage: str, status: str, step: Mapping[str, Any]) -> None:
        if on_stage is not None:
            on_stage(stage, status, step)

    for stage in normalized:
        step = {
            "stage": stage,
            "status": "running",
            "selected": planned.get(stage, 0),
            "completed": 0,
            "failed": 0,
            "pending": planned.get(stage, 0),
        }
        steps.append(step)
        emit(stage, "running", step)
        try:
            resume_run_id, _ = choose_running_run(
                project,
                stage,
                action=actions.get(stage),
                dry_run=False,
                interactive=False,
            )
            usage_base_by_stage[stage] = None
            if resume_run_id is not None:
                manifest_path = (
                    project / "runs" / resume_run_id / "manifest.json"
                )
                if record_exists(project, manifest_path):
                    raw_usage = read_json(project, manifest_path).get("usage")
                    if isinstance(raw_usage, dict):
                        usage_base_by_stage[stage] = raw_usage
            if stage == "terminology_decision":
                plan, skip_reason = _decision_plan_or_skip(
                    project,
                    prompt_language,
                    final_review=decision_final_review,
                )
                if skip_reason is not None:
                    summary = {
                        "stage": stage,
                        "selected": 0,
                        "eligible": 0,
                        "completed": 0,
                        "failed": 0,
                        "pending": 0,
                        "status": "skipped",
                        "reason": skip_reason,
                    }
                    step.update(
                        _continuous_step_summary(
                            stage,
                            summary,
                            selected_hint=0,
                            run_id=resume_run_id,
                            status="skipped",
                        )
                    )
                else:
                    summary = await run_terminology_decision(
                        project,
                        replace_draft=False,
                        resume_run_id=resume_run_id,
                        prompt_language=prompt_language,
                        on_progress=lambda completed, failed, total, stage_name=stage: report_progress(
                            stage_name, completed, failed, total
                        ),
                        on_usage=lambda current, stage_name=stage: report_usage(
                            stage_name, current
                        ),
                        limiter=stage_limiters[stage],
                        plan=plan,
                        final_review=decision_final_review,
                    )
                    if (
                        normalized[-1] != "terminology_decision"
                        and int(summary.get("needs_review", 0)) > 0
                    ):
                        raise UsageError(
                            "自动术语决策仍有人工待办，无法自动应用"
                        )
                    if normalized[-1] != "terminology_decision":
                        applied = apply_decision_draft(
                            project,
                            confirm_all=True,
                        )
                        summary = {**summary, "applied": applied}
                    decision_run_id = str(summary.get("run_id", ""))
                    decision_total = summary.get("total_steps")
                    if (
                        not isinstance(decision_total, int)
                        and decision_run_id
                        and record_exists(
                            project,
                            project
                            / "runs"
                            / decision_run_id
                            / "manifest.json",
                        )
                    ):
                        decision_total = read_json(
                            project,
                            project
                            / "runs"
                            / decision_run_id
                            / "manifest.json",
                        ).get("total_steps")
                    if isinstance(decision_total, int):
                        summary = {**summary, "total_steps": decision_total}
                    step.update(
                        _continuous_step_summary(
                            stage,
                            summary,
                            selected_hint=int(
                                decision_total
                                if isinstance(decision_total, int)
                                else summary.get("eligible", 0)
                            ),
                            run_id=str(summary.get("run_id", resume_run_id or ""))
                            or None,
                        )
                    )
            elif stage == "terminology":
                summary = await run_terminology(
                    project,
                    scope,
                    resume_run_id=resume_run_id,
                    reuse_mixed_fingerprints=reuse_mixed_fingerprints,
                    prompt_language=prompt_language,
                    on_progress=lambda completed, failed, total, stage_name=stage: report_progress(
                        stage_name, completed, failed, total
                    ),
                    on_usage=lambda current, stage_name=stage: report_usage(
                        stage_name, current
                    ),
                    limiter=stage_limiters[stage],
                )
                step.update(
                    _continuous_step_summary(
                        stage,
                        summary,
                        selected_hint=planned.get(stage, 0),
                        run_id=str(summary.get("run_id", resume_run_id or "")) or None,
                    )
                )
            elif stage == "translation":
                summary = await run_translation(
                    project,
                    scope,
                    resume_run_id=resume_run_id,
                    reuse_mixed_fingerprints=reuse_mixed_fingerprints,
                    prompt_language=prompt_language,
                    on_progress=lambda completed, failed, total, stage_name=stage: report_progress(
                        stage_name, completed, failed, total
                    ),
                    on_usage=lambda current, stage_name=stage: report_usage(
                        stage_name, current
                    ),
                    limiter=stage_limiters[stage],
                )
                step.update(
                    _continuous_step_summary(
                        stage,
                        summary,
                        selected_hint=planned.get(stage, 0),
                        run_id=str(summary.get("run_id", resume_run_id or "")) or None,
                    )
                )
            else:
                summary = await run_review(
                    project,
                    stage,
                    scope,
                    resume_run_id=resume_run_id,
                    reuse_mixed_fingerprints=reuse_mixed_fingerprints,
                    prompt_language=prompt_language,
                    on_progress=lambda completed, failed, total, stage_name=stage: report_progress(
                        stage_name, completed, failed, total
                    ),
                    on_usage=lambda current, stage_name=stage: report_usage(
                        stage_name, current
                    ),
                    limiter=stage_limiters[stage],
                )
                step.update(
                    _continuous_step_summary(
                        stage,
                        summary,
                        selected_hint=planned.get(stage, 0),
                        run_id=str(summary.get("run_id", resume_run_id or "")) or None,
                    )
                )
            summaries[stage] = dict(summary)
            if step["status"] != "skipped" and (
                step["failed"] or step["pending"]
            ):
                raise IncompleteError("连续运行阶段仍有 pending 或 failed")
            if step["status"] != "skipped":
                step["status"] = "completed"
            emit(stage, str(step["status"]), step)
        except asyncio.CancelledError:
            step["status"] = "cancelled"
            emit(stage, "cancelled", step)
            raise
        except BaseException:
            step["status"] = "failed"
            emit(stage, "failed", step)
            raise

    aggregate_usage: dict[str, Any] | None = None
    for stage_name in normalized:
        base = usage_base_by_stage.get(stage_name)
        current_value = usage_by_stage.get(stage_name)
        value = (
            combine_usage(base, current_value)
            if base is not None
            else current_value
        )
        aggregate_usage = combine_usage(aggregate_usage, value)
    failure_counts: dict[str, int] = {}
    for summary in summaries.values():
        for key, value in (summary.get("failure_counts") or {}).items():
            failure_counts[str(key)] = failure_counts.get(str(key), 0) + int(value)
    return {
        "stage": CONTINUOUS_STAGE,
        "steps": steps,
        "summaries": summaries,
        "completed": sum(int(step["completed"]) for step in steps),
        "failed": sum(int(step["failed"]) for step in steps),
        "pending": sum(int(step["pending"]) for step in steps),
        "failure_counts": failure_counts,
        "usage": aggregate_usage or unavailable_usage(),
    }
