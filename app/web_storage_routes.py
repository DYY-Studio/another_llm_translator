from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from . import data_root
from .errors import UsageError
from .storage_management import StorageManager
from .web_payloads import (
    DataRootRelocationPayload,
    StorageConfirmPayload,
    StorageOutputClearPayload,
)


def register_storage_routes(
    *,
    app: FastAPI,
    projects_root: Path,
    app_root: Path,
    project: Callable[[str], Path],
    storage_manager: StorageManager,
    has_active_tasks: Callable[[], bool],
) -> None:
    del projects_root, app_root

    @app.get("/api/v1/storage/data-root")
    async def data_root_status() -> dict[str, object]:
        try:
            result = data_root.status()
        except (OSError, ValueError) as exc:
            raise UsageError(str(exc)) from exc
        return {
            "active_root": result["active_root"],
            "default_root": result["default_root"],
            "mode": result["mode"],
            "can_change": result["mode"] != "environment",
            "pending": result["pending"],
        }

    @app.post("/api/v1/storage/data-root/relocation")
    async def request_data_root_relocation(
        payload: DataRootRelocationPayload,
    ) -> dict[str, str]:
        if not payload.confirm:
            raise UsageError("必须明确确认切换数据位置")
        if has_active_tasks():
            raise UsageError("存在运行中的任务，结束或取消后才能切换数据位置")
        parent_dir = Path(payload.parent_dir)
        if not parent_dir.is_absolute():
            raise UsageError("数据位置必须是绝对路径")
        try:
            return data_root.request_relocation(parent_dir)
        except (OSError, ValueError) as exc:
            raise UsageError(str(exc)) from exc

    @app.delete("/api/v1/storage/data-root/relocation")
    async def cancel_data_root_relocation(
        payload: StorageConfirmPayload,
    ) -> dict[str, bool]:
        try:
            return data_root.cancel_relocation(confirm=payload.confirm)
        except (OSError, ValueError) as exc:
            raise UsageError(str(exc)) from exc

    @app.get("/api/v1/storage")
    async def storage_summary() -> dict[str, object]:
        return storage_manager.scan()

    @app.get("/api/v1/storage/projects/{selector}")
    async def storage_project_detail(selector: str) -> dict[str, object]:
        return await run_in_threadpool(storage_manager.scan_project, project(selector))

    @app.post("/api/v1/storage/logs/clear")
    async def clear_global_logs(payload: StorageConfirmPayload) -> dict[str, int]:
        return storage_manager.clear_global_logs(confirm=payload.confirm)

    @app.post(
        "/api/v1/projects/{name}/storage/runs/{run_id}/debug/clear"
    )
    async def clear_debug(
        name: str, run_id: str, payload: StorageConfirmPayload
    ) -> dict[str, int]:
        return storage_manager.clear_debug(
            project(name), run_id, confirm=payload.confirm
        )

    @app.post("/api/v1/projects/{name}/storage/outputs/clear")
    async def clear_output(
        name: str, payload: StorageOutputClearPayload
    ) -> dict[str, int]:
        return storage_manager.clear_output(
            project(name), payload.path, confirm=payload.confirm
        )

    @app.post("/api/v1/projects/{name}/storage/logs/clear")
    async def clear_project_logs(
        name: str, payload: StorageConfirmPayload
    ) -> dict[str, int]:
        return storage_manager.clear_project_logs(
            project(name), confirm=payload.confirm
        )

    @app.post("/api/v1/projects/{name}/storage/database/maintain")
    async def maintain_database(name: str, payload: StorageConfirmPayload) -> dict[str, int]:
        return await run_in_threadpool(storage_manager.maintain_database, project(name), confirm=payload.confirm)
