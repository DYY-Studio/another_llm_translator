from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import UsageError
from .execution import Scope, choose_running_run
from .locking import project_write_lock
from .stages import run_all, run_review, run_terminology, run_translation
from .storage import utc_now


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
        async with self.guard:
            active_id = self.active_by_project.get(project)
            if active_id is not None:
                active = self.tasks[active_id]
                if active.status in {"queued", "running", "cancelling"}:
                    raise UsageError(
                        f"项目已有后台任务：{active.task_id}"
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
