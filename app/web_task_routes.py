from __future__ import annotations
from collections.abc import Callable
from pathlib import Path
from typing import Any
from fastapi import FastAPI
from .errors import (
    UsageError,
)
from .execution import Scope
from .project import (
    PROMPT_LANGUAGES,
)
from .web_payloads import TaskStartPayload
from .web_tasks import task_options




def register_task_routes(*, app: FastAPI, projects_root: Path, app_root: Path, project: Callable[[str], Path]) -> None:
    SESSION_COOKIE = "another_llm_session"
    _SESSION_TTL_SECONDS = 30 * 24 * 3600
    _WINDOWS_DRIVE_TYPES = {0: "unknown", 1: "unavailable", 2: "removable", 3: "fixed", 4: "network", 5: "cdrom", 6: "ramdisk"}


    def validate_language(value: object) -> str:
        if value not in PROMPT_LANGUAGES:
            raise UsageError("language 必须是 zh-CN 或 en")
        return str(value)

    @app.post("/api/v1/projects/{name}/tasks")
    async def start_task(
        name: str, payload: TaskStartPayload
    ) -> dict[str, Any]:
        stage = payload.stage
        scope = Scope(
            from_file=payload.from_file,
            only_file=payload.only_file,
            only_segment=payload.only_segment,
            force=payload.force,
            dry_run=False,
        )
        scope.validate()
        summary_selection_provided = "summary_selection" in payload.model_fields_set
        summary_selection = [
            value.model_dump() for value in payload.summary_selection
        ]
        if summary_selection_provided and stage != "content_summary":
            raise UsageError("summary_selection 只允许内容概括阶段")
        return await app.state.tasks.start(
            project(name),
            stage,
            scope=scope,
            reuse_mixed_fingerprints=payload.reuse_mixed_fingerprints,
            run_action=payload.run_action,
            prompt_language=(
                validate_language(payload.language)
                if "language" in payload.model_fields_set
                else None
            ),
            replace_draft=payload.replace_draft,
            acknowledge_manual_review=payload.acknowledge_manual_review,
            include_summaries=payload.include_summaries,
            summary_selection=summary_selection,
        )

    @app.get("/api/v1/projects/{name}/task-options/{stage}")
    async def get_task_options(
        name: str,
        stage: str,
        include_summaries: bool = False,
        language: str | None = None,
    ) -> dict[str, Any]:
        return task_options(
            project(name),
            stage,
            include_summaries=include_summaries,
            prompt_language=(validate_language(language) if language is not None else None),
        )

    @app.get("/api/v1/tasks/active")
    async def active_tasks() -> dict[str, Any]:
        return {"tasks": app.state.tasks.active_tasks()}

    @app.get("/api/v1/tasks/{task_id}")
    async def task(task_id: str) -> dict[str, Any]:
        return app.state.tasks.get(task_id)

    @app.post("/api/v1/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str) -> dict[str, Any]:
        return await app.state.tasks.cancel(task_id)

    @app.post("/api/v1/diagnostics")
    async def diagnostics(payload: dict[str, Any]) -> dict[str, Any]:
        values = {
            key: payload.get(key)
            for key in (
                "level",
                "project",
                "stage",
                "q",
                "request_session",
                "request_after",
            )
        }
        for key in ("level", "project", "stage", "q", "request_session"):
            if values[key] is not None and not isinstance(values[key], str):
                raise UsageError(f"{key} 必须是字符串")
        request_after = values["request_after"]
        if request_after is not None:
            try:
                request_after = int(request_after)
            except (TypeError, ValueError) as exc:
                raise UsageError("request_after 必须是整数") from exc
        try:
            return app.state.diagnostics.snapshot(
                level=values["level"] or None,
                project=values["project"] or None,
                stage=values["stage"] or None,
                query=values["q"] or None,
                request_session=values["request_session"] or None,
                request_after=request_after,
            )
        except ValueError as exc:
            raise UsageError(str(exc)) from exc

    @app.get("/api/v1/diagnostics/requests/{request_id}")
    async def diagnostic_request(request_id: str) -> dict[str, Any]:
        try:
            return app.state.diagnostics.request_detail(request_id)
        except ValueError as exc:
            raise UsageError(str(exc)) from exc
