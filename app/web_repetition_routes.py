from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request

from .errors import UsageError
from .execution import Scope, find_running_runs
from .repetition_consistency import STAGE, repetition_plan, repetition_resume
from .repetition_drafts import apply_draft, discard_draft, review_state, rollback_draft, save_choices
from .web_tasks import _running_run


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
    def options(request: Request, name: str, estimate_usage: bool = False, only_file: str | None = None, language: str | None = None, exclude_punctuation_only: bool = True, screening_enabled: bool = False, run_action: str | None = None) -> dict[str, Any]:
        if run_action not in {None, "resume", "decline"}:
            raise UsageError("Run 选择必须是 resume 或 decline")
        root = project(name)
        runs = find_running_runs(root, STAGE)
        if runs and run_action != "decline" and runs[0].get("repetition_checkpoint") == 2:
            original = runs[0]
            only_file = original["scope"]["only_file"]
            exclude_punctuation_only = original["exclude_punctuation_only"]
            screening_enabled = original["screening_enabled"]
        estimate_usage = estimate_usage and app.state.server_config.get("tasks", {}).get("estimate_usage_before_run", False)
        plan = repetition_plan(root, Scope(only_file=only_file), prompt_language=language, exclude_punctuation_only=exclude_punctuation_only, screening_enabled=screening_enabled, preview_only=not estimate_usage and not runs, estimate_usage=estimate_usage)
        running = repetition_resume(root, plan) if runs else None
        if running:
            running = {**_running_run(root, STAGE, plan["config"]), **running}
        action = (run_action or ("resume" if running["resume_compatible"] else "decline")) if running else None
        resuming = bool(running and action == "resume" and running["resume_compatible"])
        return {"excluded_punctuation_groups": plan["excluded_punctuation_groups"], "candidate_groups": len(plan["groups"]), "selected": plan["selected"],
                "running_run": running, "run_action": action, "only_file": only_file, "screening_enabled": screening_enabled, "exclude_punctuation_only": exclude_punctuation_only,
                "completed": running["completed"] if resuming else 0,
                "over_budget_groups": running["over_budget_groups"] if resuming else sum(g["over_budget"] for g in plan["groups"]),
                "estimated_requests": (running["estimated_requests"] if resuming else plan["estimated_requests"]) if estimate_usage else None, "estimated_input_tokens": (running["estimated_input_tokens"] if resuming else plan["estimated_input_tokens"]) if estimate_usage else None, "estimate_error": None,
                "preset": {"id": plan["config"]["_llm_preset_id"], "model": plan["config"]["llm"]["model"]},
                "decision_model": (plan["decision_preset"]["_resources"]["preset"]["model"] if plan["decision_preset"]["protocol"] == "llm" else plan["decision_preset"]["model"]) if plan["decision_preset"] else None, "model": plan["config"]["llm"]["model"]}

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
