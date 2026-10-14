from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from .errors import UsageError
from .execution import Scope
from .repetition_consistency import repetition_plan
from .repetition_drafts import apply_draft, discard_draft, review_state, rollback_draft, save_choices


def register_repetition_routes(*, app: FastAPI, project: Callable[[str], Path]) -> None:
    def idle(name: str) -> Path:
        root = project(name)
        if app.state.tasks.is_project_running(root):
            raise UsageError("项目存在后台任务，结束后才能修改重复译文草案")
        return root

    def run_id(payload: dict[str, Any]) -> str:
        value = payload.get("run_id")
        if not isinstance(value, str) or not value:
            raise UsageError("必须提供草案 Run ID")
        return value

    @app.get("/api/v1/projects/{name}/repetition/options")
    def options(name: str, only_file: str | None = None, language: str | None = None) -> dict[str, Any]:
        plan = repetition_plan(project(name), Scope(only_file=only_file), prompt_language=language)
        return {"candidate_groups": len(plan["groups"]), "selected": plan["selected"],
                "over_budget_groups": sum(g["over_budget"] for g in plan["groups"]),
                "estimated_requests": plan["estimated_requests"], "estimated_input_tokens": plan["estimated_input_tokens"],
                "decision_model": plan["decision_preset"]["model"], "model": plan["config"]["llm"]["model"]}

    @app.get("/api/v1/projects/{name}/repetition")
    def state(name: str) -> dict[str, Any]:
        return review_state(project(name))

    @app.put("/api/v1/projects/{name}/repetition/choices")
    def choices(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return save_choices(idle(name), payload.get("choices"), run_id=run_id(payload))

    @app.post("/api/v1/projects/{name}/repetition/apply")
    def apply(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        import asyncio
        if not isinstance(payload.get("choices"), dict):
            raise UsageError("必须提供确认时的草案审核选择")
        return asyncio.run(apply_draft(idle(name), confirm=payload.get("confirm") is True,
            run_id=run_id(payload), expected_choices=payload["choices"]))

    @app.post("/api/v1/projects/{name}/repetition/discard")
    def discard(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return discard_draft(idle(name), confirm=payload.get("confirm") is True, run_id=run_id(payload))

    @app.post("/api/v1/projects/{name}/repetition/rollback")
    def rollback(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return rollback_draft(idle(name), confirm=payload.get("confirm") is True, run_id=run_id(payload))
