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
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        stage = str(payload.get("stage", ""))

        def boolean_option(key: str) -> bool:
            value = payload.get(key, False)
            if not isinstance(value, bool):
                raise UsageError(f"{key} 必须是布尔值")
            return value

        force = boolean_option("force")
        replace_draft = boolean_option("replace_draft")
        acknowledge_manual_review = boolean_option("acknowledge_manual_review")
        reuse_mixed_fingerprints = boolean_option(
            "reuse_mixed_fingerprints"
        )
        run_action = payload.get("run_action")
        if run_action is not None and not isinstance(run_action, str):
            raise UsageError("run_action 必须是字符串或 null")
        scope = Scope(
            from_file=payload.get("from_file"),
            only_file=payload.get("only_file"),
            only_segment=payload.get("only_segment"),
            force=force,
            dry_run=False,
        )
        scope.validate()
        return await app.state.tasks.start(
            project(name),
            stage,
            scope=scope,
            reuse_mixed_fingerprints=reuse_mixed_fingerprints,
            run_action=run_action,
            prompt_language=(
                validate_language(payload.get("language"))
                if "language" in payload
                else None
            ),
            replace_draft=replace_draft,
            acknowledge_manual_review=acknowledge_manual_review,
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
