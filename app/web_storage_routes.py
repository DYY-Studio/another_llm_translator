from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI

from .storage_management import StorageManager
from .web_payloads import StorageConfirmPayload, StorageOutputClearPayload


def register_storage_routes(
    *,
    app: FastAPI,
    projects_root: Path,
    app_root: Path,
    project: Callable[[str], Path],
    storage_manager: StorageManager,
) -> None:
    del projects_root, app_root

    @app.get("/api/v1/storage")
    async def storage_summary() -> dict[str, object]:
        return storage_manager.scan()

    @app.get("/api/v1/storage/projects/{selector}")
    async def storage_project_detail(selector: str) -> dict[str, object]:
        return storage_manager.scan_project(project(selector))

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
