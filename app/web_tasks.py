from __future__ import annotations

import asyncio
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import LLM_STAGES, load_project_config, load_run_config
from .diagnostics import Diagnostics
from .errors import (
    AppError,
    UsageError,
    app_error_payload,
    internal_error_payload,
)
from .execution import (
    Scope,
    choose_running_run,
    combine_usage,
    find_running_runs,
    stage_fingerprint,
    unavailable_usage,
)
from .llm_preset import endpoint_url
from .locking import project_write_lock
from .logging_utils import get_logger
from .project import load_segments
from .sqlite_storage import (
    latest_stage_summary,
    read_json,
    read_jsonl,
    record_exists,
    utc_now,
)
from .stages import (
    load_terms,
    prompt_middle_digests,
    run_all,
    run_review,
    run_terminology,
    run_translation,
)
from .term_decision import (
    STAGE as TERMINOLOGY_DECISION_STAGE,
)
from .term_decision import (
    current_decision_draft,
    decision_checkpoint_progress,
    decision_plan,
    decision_resume_compatibility,
    manual_review_state,
    run_terminology_decision,
)


def _endpoint_summary(config: dict[str, Any]) -> dict[str, str]:
    return {
        "model": str(config["llm"]["model"]),
        "endpoint": endpoint_url(
            config["llm"]["base_url"],
            config["llm"]["endpoint"],
            model=config["llm"]["model"],
        ),
    }


def _running_run(
    project: Path, stage: str, current_config: dict[str, Any]
) -> dict[str, Any] | None:
    candidates = find_running_runs(project, stage)
    if not candidates:
        return None
    manifest = candidates[0]
    run_id = str(manifest["run_id"])
    old_config = load_run_config(project / "runs" / run_id)
    result = {
        "run_id": run_id,
        "started_at": manifest.get("started_at"),
        "scope": manifest.get("scope"),
        "previous": _endpoint_summary(old_config),
        "current": _endpoint_summary(current_config),
    }
    if isinstance(manifest.get("last_interruption"), dict):
        result["last_interruption"] = manifest["last_interruption"]
    return result


def _stage_summary(
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
    return {
        "completed": len(completed),
        "failed": len(failed),
        "pending": nonempty_count - len(completed) - len(failed),
        "current_fingerprint_completed": sum(
            item["stage_fingerprint"] == current_fingerprint
            for item in completed.values()
        ),
    }


def _terminology_summary(
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
    return {
        "completed": len(completed),
        "failed": len(failed),
        "pending": nonempty_count - len(completed) - len(failed),
        "current_fingerprint_completed": sum(
            item.get("status") == "completed"
            and item.get("stage_fingerprint") == current_fingerprint
            for item in scans
        ),
    }


def task_options(project: Path, stage: str) -> dict[str, Any]:
    if stage == TERMINOLOGY_DECISION_STAGE:
        library = load_terms(project)
        if library is None or not library.get("terms"):
            raise UsageError("没有已发布术语库可供自动决策")
        overrides = read_json(
            project, project / "terminology" / "overrides.json"
        )
        protected = {
            str(item["normalized"])
            for item in overrides.get("overrides", [])
        }
        has_eligible = any(
            str(item["normalized"]) not in protected
            and not bool(item.get("disabled", False))
            for item in library.get("terms", [])
        )
        config = load_project_config(project, stage=stage)
        plan = decision_plan(project) if has_eligible else None
        selected = len(plan["eligible"]) if plan else 0
        running_run = _running_run(project, stage, config)
        if running_run is not None:
            compatible, reason = decision_resume_compatibility(
                project, str(running_run["run_id"])
            )
            running_run["completed_steps"] = decision_checkpoint_progress(
                project, str(running_run["run_id"])
            )
            running_run["total_steps"] = selected * 2
            running_run["resume_compatible"] = compatible
            running_run["resume_incompatibility_reason"] = reason
        return {
            "stage": stage,
            "preset": {
                "id": str(config["_llm_preset_id"]),
                "model": str(config["llm"]["model"]),
            },
            "selected": selected,
            "protected": len(plan["protected"]) if plan else len(protected),
            "overflow_policy": {
                "allow_soft_target_overflow": bool(
                    config["terminology_decision"]["allow_soft_target_overflow"]
                ),
                "anchor_overflow_mode": str(
                    config["terminology_decision"]["anchor_overflow_mode"]
                ),
            },
            "completed": 0,
            "pending": selected,
            "failed": 0,
            "current_fingerprint_completed": 0,
            "mismatched_fingerprint_completed": 0,
            "running_run": running_run,
            "has_pending_draft": current_decision_draft(project) is not None,
            "estimated_requests": int(plan["estimated_requests"]) if plan else 0,
            "estimated_input_tokens": (
                int(plan["estimated_input_tokens"]) if plan else 0
            ),
        }
    if stage not in LLM_STAGES:
        raise UsageError(f"未知 Web 阶段：{stage}")
    segments = load_segments(project)
    nonempty = [item for item in segments if not item["is_empty"]]
    if not nonempty:
        raise UsageError("项目没有可处理的非空 Segment；请先添加源文件")
    active_segment_ids = {str(item["segment_id"]) for item in nonempty}
    config = load_project_config(project, stage=stage)
    if stage == "terminology":
        summary = _terminology_summary(
            project,
            config,
            active_segment_ids=active_segment_ids,
            nonempty_count=len(nonempty),
        )
    else:
        library = load_terms(project)
        summary = _stage_summary(
            project,
            stage,
            config,
            active_segment_ids=active_segment_ids,
            nonempty_count=len(nonempty),
            terms_revision=(
                int(library["terms_revision"]) if library else None
            ),
        )
    completed = summary["completed"]
    current_completed = summary["current_fingerprint_completed"]
    return {
        "stage": stage,
        "preset": {
            "id": str(config["_llm_preset_id"]),
            "model": str(config["llm"]["model"]),
        },
        "selected": len(nonempty),
        "completed": completed,
        "pending": summary["pending"],
        "failed": summary["failed"],
        "current_fingerprint_completed": current_completed,
        "mismatched_fingerprint_completed": max(
            0, completed - current_completed
        ),
        "running_run": _running_run(project, stage, config),
    }


@dataclass
class WebTask:
    task_id: str
    project: Path
    project_id: str
    stage: str
    status: str = "queued"
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    completed_at: str | None = None
    summary: dict[str, Any] | None = None
    error: dict[str, object] | None = None
    completed_segments: int = 0
    failed_segments: int = 0
    total_segments: int = 0
    failure_counts: dict[str, int] = field(default_factory=dict)
    usage: dict[str, Any] = field(
        default_factory=lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "available": False,
            "partial": False,
        }
    )
    asyncio_task: asyncio.Task[None] | None = field(default=None, repr=False)

    def view(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "project": self.project.name,
            "project_id": self.project_id,
            "stage": self.stage,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "summary": self.summary,
            "error": self.error,
            "completed_segments": self.completed_segments,
            "failed_segments": self.failed_segments,
            "pending_segments": max(
                0,
                self.total_segments
                - self.completed_segments
                - self.failed_segments,
            ),
            "total_segments": self.total_segments,
            "failure_counts": dict(self.failure_counts),
            "usage": self.usage,
        }


class WebTaskManager:
    def __init__(self, diagnostics: Diagnostics | None = None) -> None:
        self.tasks: dict[str, WebTask] = {}
        self.active_by_project: dict[Path, str] = {}
        self.guard = asyncio.Lock()
        self.diagnostics = diagnostics

    async def start(
        self,
        project: Path,
        stage: str,
        *,
        scope: Scope,
        reuse_mixed_fingerprints: bool,
        run_action: str | None,
        prompt_language: str | None = None,
        replace_draft: bool = False,
        acknowledge_manual_review: bool = False,
    ) -> dict[str, Any]:
        if stage not in {
            "terminology",
            "translation",
            "proofreading",
            "polishing",
            TERMINOLOGY_DECISION_STAGE,
            "run-all",
        }:
            raise UsageError(f"未知后台阶段：{stage}")
        force = scope.force
        if force and reuse_mixed_fingerprints:
            raise UsageError(
                "force 与 reuse_mixed_fingerprints 不能同时使用"
            )
        if run_action not in {None, "resume", "decline"}:
            raise UsageError("run_action 必须是 resume、decline 或 null")
        if stage == "run-all" and run_action is not None:
            raise UsageError("run-all 不支持 run_action")
        if stage == TERMINOLOGY_DECISION_STAGE and reuse_mixed_fingerprints:
            raise UsageError("自动术语决策不支持复用已发布结果")
        async with self.guard:
            active_id = self.active_by_project.get(project)
            if active_id is not None:
                active = self.tasks[active_id]
                if active.status in {"queued", "running", "cancelling"}:
                    raise UsageError(
                        f"项目已有后台任务：{active.task_id}"
                    )
            selected_count = 0
            if stage == TERMINOLOGY_DECISION_STAGE:
                if (
                    run_action != "resume"
                    and not acknowledge_manual_review
                    and manual_review_state(project)["remaining"] > 0
                ):
                    raise UsageError(
                        "存在未处理人工待办；请先确认新一轮决策会在成功应用后取代旧队列"
                    )
                running = find_running_runs(project, stage)
                if run_action == "resume":
                    if not running:
                        raise UsageError(f"{stage} 没有可续用的 running Run")
                    if force:
                        raise UsageError("续用 Run 时不能同时指定 force")
                    compatible, reason = decision_resume_compatibility(
                        project, str(running[0]["run_id"])
                    )
                    if not compatible:
                        raise UsageError(
                            f"{reason}；请结束旧 Run 并强制新建"
                        )
                elif running:
                    if run_action != "decline" or not force:
                        raise UsageError(
                            "发现未完成 Run，必须选择续用或强制重做全部"
                        )
                elif run_action == "decline" and not force:
                    raise UsageError("结束自动决策 Run 时必须同时指定 force")
            elif stage != "run-all":
                options = task_options(project, stage)
                selected_count = int(options["selected"])
                running_run = options["running_run"]
                if run_action == "resume":
                    if running_run is None:
                        raise UsageError(f"{stage} 没有可续用的 running Run")
                    if force or reuse_mixed_fingerprints:
                        raise UsageError(
                            "续用 Run 时不能同时指定 force 或复用结果"
                        )
                else:
                    if running_run is not None and run_action != "decline":
                        raise UsageError(
                            "发现未完成 Run，必须选择续用或结束并新建"
                        )
                    if (
                        options["mismatched_fingerprint_completed"]
                        and not force
                        and not reuse_mixed_fingerprints
                    ):
                        raise UsageError(
                            "存在不同设置指纹的已完成结果，"
                            "必须明确选择复用或 force"
                        )
            task_id = f"TASK-{uuid.uuid4().hex[:12].upper()}"
            state = WebTask(
                task_id=task_id,
                project=project,
                project_id=str(
                    read_json(project, project / "project.json")["project_id"]
                ),
                stage=stage,
                total_segments=selected_count,
            )
            self.tasks[task_id] = state
            self.active_by_project[project] = task_id
            state.asyncio_task = asyncio.create_task(
                self._run(
                    state,
                    scope=scope,
                    reuse_mixed_fingerprints=reuse_mixed_fingerprints,
                    run_action=run_action,
                    prompt_language=prompt_language,
                    replace_draft=replace_draft,
                )
            )
            return state.view()

    async def _run(
        self,
        state: WebTask,
        *,
        scope: Scope,
        reuse_mixed_fingerprints: bool,
        run_action: str | None,
        prompt_language: str | None = None,
        replace_draft: bool = False,
    ) -> None:
        state.status = "running"
        state.started_at = utc_now()
        usage_base: dict[str, Any] | None = None
        resuming = False

        def progress(completed: int, failed: int, total: int) -> None:
            state.completed_segments = completed
            state.failed_segments = failed
            state.total_segments = total

        def usage_changed(current: dict[str, Any] | None) -> None:
            state.usage = _task_usage(usage_base, current, resuming=resuming)
            if self.diagnostics is not None:
                self.diagnostics.set_usage(state.usage)

        try:
            diagnostics_context = (
                self.diagnostics.activate(state.project.name, state.stage)
                if self.diagnostics is not None
                else nullcontext()
            )
            with diagnostics_context, project_write_lock(state.project):
                resume_run_id = None
                if state.stage != "run-all":
                    resume_run_id, _ = choose_running_run(
                        state.project,
                        state.stage,
                        action=run_action,
                        dry_run=False,
                        interactive=False,
                    )
                    if resume_run_id is not None:
                        resuming = True
                        manifest = read_json(
                            state.project,
                            state.project
                            / "runs"
                            / resume_run_id
                            / "manifest.json",
                        )
                        if type(manifest.get("usage_invocation_count")) is int:
                            raw_usage = manifest.get("usage")
                            if isinstance(raw_usage, dict):
                                usage_base = raw_usage
                if state.stage == TERMINOLOGY_DECISION_STAGE:
                    summary = await run_terminology_decision(
                        state.project,
                        replace_draft=replace_draft,
                        resume_run_id=resume_run_id,
                        prompt_language=prompt_language,
                        on_progress=progress,
                        on_usage=usage_changed,
                    )
                elif state.stage == "terminology":
                    summary = await run_terminology(
                        state.project,
                        scope,
                        resume_run_id=resume_run_id,
                        reuse_mixed_fingerprints=reuse_mixed_fingerprints,
                        prompt_language=prompt_language,
                        on_progress=progress,
                        on_usage=usage_changed,
                    )
                elif state.stage == "translation":
                    summary = await run_translation(
                        state.project,
                        scope,
                        resume_run_id=resume_run_id,
                        reuse_mixed_fingerprints=reuse_mixed_fingerprints,
                        prompt_language=prompt_language,
                        on_progress=progress,
                        on_usage=usage_changed,
                    )
                elif state.stage in {"proofreading", "polishing"}:
                    summary = await run_review(
                        state.project,
                        state.stage,
                        scope,
                        resume_run_id=resume_run_id,
                        reuse_mixed_fingerprints=reuse_mixed_fingerprints,
                        prompt_language=prompt_language,
                        on_progress=progress,
                        on_usage=usage_changed,
                    )
                else:
                    summary = await run_all(
                        state.project,
                        scope,
                        reuse_mixed_fingerprints=reuse_mixed_fingerprints,
                        prompt_language=prompt_language,
                    )
            state.summary = summary
            state.completed_segments = int(summary.get("completed", 0)) + int(
                summary.get("reused", 0)
            )
            state.failed_segments = int(summary.get("failed", 0))
            state.failure_counts = {
                str(key): int(value)
                for key, value in (summary.get("failure_counts") or {}).items()
            }
            if summary.get("selected") is not None:
                state.total_segments = int(summary["selected"])
            summary_usage = summary.get("usage")
            if isinstance(summary_usage, dict):
                state.usage = summary_usage
            state.status = (
                "failed"
                if summary.get("failed") or summary.get("pending")
                else "completed"
            )
        except asyncio.CancelledError:
            state.status = "cancelled"
        except AppError as exc:
            state.status = "failed"
            state.error = app_error_payload(exc)
        except Exception as exc:
            state.status = "failed"
            state.error = internal_error_payload()
            get_logger(state.stage).exception(
                "unexpected background task error task_id=%s project=%s",
                state.task_id,
                state.project,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        finally:
            state.completed_at = utc_now()
            async with self.guard:
                if self.active_by_project.get(state.project) == state.task_id:
                    self.active_by_project.pop(state.project, None)

    def get(self, task_id: str) -> dict[str, Any]:
        try:
            return self.tasks[task_id].view()
        except KeyError as exc:
            raise UsageError(f"未知后台任务：{task_id}") from exc

    def active_tasks(self) -> list[dict[str, Any]]:
        active = {
            "queued",
            "running",
            "cancelling",
        }
        states = [
            state.view()
            for state in self.tasks.values()
            if state.status in active
        ]
        return sorted(
            states,
            key=lambda value: (str(value["created_at"]), str(value["task_id"])),
        )

    def is_running(self, project: Path, stage: str) -> bool:
        return any(
            state.project == project
            and state.stage == stage
            and state.status in {"queued", "running", "cancelling"}
            for state in self.tasks.values()
        )

    def is_project_running(self, project: Path) -> bool:
        return any(
            state.project == project
            and state.status in {"queued", "running", "cancelling"}
            for state in self.tasks.values()
        )

    async def cancel(self, task_id: str) -> dict[str, Any]:
        try:
            state = self.tasks[task_id]
        except KeyError as exc:
            raise UsageError(f"未知后台任务：{task_id}") from exc
        if state.status not in {"queued", "running"} or state.asyncio_task is None:
            raise UsageError("后台任务当前不可取消")
        state.status = "cancelling"
        state.asyncio_task.cancel()
        return state.view()


def _task_usage(
    previous: dict[str, Any] | None,
    current: dict[str, Any] | None,
    *,
    resuming: bool,
) -> dict[str, Any]:
    if resuming:
        return combine_usage(previous, current)
    return current or unavailable_usage()
