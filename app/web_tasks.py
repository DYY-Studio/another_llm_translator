from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import load_config
from .errors import UsageError
from .execution import Scope, choose_running_run, find_running_runs
from .locking import project_write_lock
from .stages import (
    inspect_full,
    run_all,
    run_review,
    run_terminology,
    run_translation,
)
from .storage import utc_now


WEB_LLM_STAGES = frozenset(
    {"terminology", "translation", "proofreading", "polishing"}
)


def _endpoint_summary(config: dict[str, Any]) -> dict[str, str]:
    return {
        "model": str(config["llm"]["model"]),
        "endpoint": (
            str(config["llm"]["base_url"]).rstrip("/")
            + "/"
            + str(config["llm"]["endpoint"]).lstrip("/")
        ),
    }


def task_options(project: Path, stage: str) -> dict[str, Any]:
    if stage not in WEB_LLM_STAGES:
        raise UsageError(f"未知 Web 阶段：{stage}")
    inspection = inspect_full(project, dry_run=True)
    stage_summary = (
        inspection["terminology"]
        if stage == "terminology"
        else inspection["stages"][stage]
    )
    completed = int(stage_summary["completed"])
    if not int(inspection["segments"]) - int(inspection["empty_segments"]):
        raise UsageError("项目没有可处理的非空 Segment；请先添加源文件")
    current_completed = int(stage_summary["current_fingerprint_completed"])
    candidates = find_running_runs(project, stage)
    running_run = None
    if candidates:
        manifest = candidates[0]
        run_id = str(manifest["run_id"])
        old_config = load_config(project / "runs" / run_id / "config.toml")
        current_config = load_config(project / "config.toml")
        running_run = {
            "run_id": run_id,
            "started_at": manifest.get("started_at"),
            "scope": manifest.get("scope"),
            "previous": _endpoint_summary(old_config),
            "current": _endpoint_summary(current_config),
        }
    return {
        "stage": stage,
        "selected": (
            completed
            + int(stage_summary["pending"])
            + int(stage_summary["failed"])
        ),
        "completed": completed,
        "pending": int(stage_summary["pending"]),
        "failed": int(stage_summary["failed"]),
        "fingerprint_count": int(stage_summary["fingerprint_count"]),
        "current_fingerprint": str(stage_summary["current_fingerprint"]),
        "current_fingerprint_completed": current_completed,
        "mismatched_fingerprint_completed": max(
            0, completed - current_completed
        ),
        "running_run": running_run,
    }


@dataclass
class WebTask:
    task_id: str
    project: Path
    stage: str
    status: str = "queued"
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    completed_at: str | None = None
    summary: dict[str, Any] | None = None
    error: str | None = None
    asyncio_task: asyncio.Task[None] | None = field(default=None, repr=False)

    def view(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "project": self.project.name,
            "stage": self.stage,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "summary": self.summary,
            "error": self.error,
        }


class WebTaskManager:
    def __init__(self) -> None:
        self.tasks: dict[str, WebTask] = {}
        self.active_by_project: dict[Path, str] = {}
        self.guard = asyncio.Lock()

    async def start(
        self,
        project: Path,
        stage: str,
        *,
        scope: Scope,
        reuse_mixed_fingerprints: bool,
        run_action: str | None,
    ) -> dict[str, Any]:
        if stage not in {
            "terminology",
            "translation",
            "proofreading",
            "polishing",
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
        if stage == "run-all":
            if run_action is not None:
                raise UsageError("run-all 不支持 run_action")
        async with self.guard:
            active_id = self.active_by_project.get(project)
            if active_id is not None:
                active = self.tasks[active_id]
                if active.status in {"queued", "running", "cancelling"}:
                    raise UsageError(
                        f"项目已有后台任务：{active.task_id}"
                    )
            if stage != "run-all":
                options = task_options(project, stage)
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
            state = WebTask(task_id=task_id, project=project, stage=stage)
            self.tasks[task_id] = state
            self.active_by_project[project] = task_id
            state.asyncio_task = asyncio.create_task(
                self._run(
                    state,
                    scope=scope,
                    reuse_mixed_fingerprints=reuse_mixed_fingerprints,
                    run_action=run_action,
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
    ) -> None:
        state.status = "running"
        state.started_at = utc_now()
        try:
            with project_write_lock(state.project):
                resume_run_id = None
                if state.stage != "run-all":
                    resume_run_id, _ = choose_running_run(
                        state.project,
                        state.stage,
                        action=run_action,
                        dry_run=False,
                        interactive=False,
                    )
                if state.stage == "terminology":
                    summary = await run_terminology(
                        state.project,
                        scope,
                        resume_run_id=resume_run_id,
                        reuse_mixed_fingerprints=reuse_mixed_fingerprints,
                    )
                elif state.stage == "translation":
                    summary = await run_translation(
                        state.project,
                        scope,
                        resume_run_id=resume_run_id,
                        reuse_mixed_fingerprints=reuse_mixed_fingerprints,
                    )
                elif state.stage in {"proofreading", "polishing"}:
                    summary = await run_review(
                        state.project,
                        state.stage,
                        scope,
                        resume_run_id=resume_run_id,
                        reuse_mixed_fingerprints=reuse_mixed_fingerprints,
                    )
                else:
                    summary = await run_all(
                        state.project,
                        scope,
                        reuse_mixed_fingerprints=reuse_mixed_fingerprints,
                    )
            state.summary = summary
            state.status = (
                "failed"
                if summary.get("failed") or summary.get("pending")
                else "completed"
            )
        except asyncio.CancelledError:
            state.status = "cancelled"
        except Exception as exc:
            state.status = "failed"
            state.error = str(exc)
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
