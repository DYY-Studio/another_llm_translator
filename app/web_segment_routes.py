from __future__ import annotations
from collections.abc import Callable
from pathlib import Path
from typing import Any
from fastapi import FastAPI
from .errors import (
    UsageError,
)
from .execution import Scope
from .locking import project_write_lock
from .stage_review import run_apply
from .web_payloads import SegmentFilterPayload, SegmentQueryPayload
from .web_store import WebStore




def register_segment_routes(*, app: FastAPI, projects_root: Path, app_root: Path, project: Callable[[str], Path]) -> None:
    SESSION_COOKIE = "another_llm_session"
    _SESSION_TTL_SECONDS = 30 * 24 * 3600
    _WINDOWS_DRIVE_TYPES = {0: "unknown", 1: "unavailable", 2: "removable", 3: "fixed", 4: "network", 5: "cdrom", 6: "ramdisk"}


    @app.post("/api/v1/projects/{name}/segments/query")
    async def overview_query(
        name: str, payload: SegmentQueryPayload
    ) -> dict[str, Any]:
        if bool(payload.file_id) != bool(payload.part_id):
            raise UsageError("file_id 与 part_id 必须同时提供")
        return WebStore(project(name)).segment_query(
            offset=payload.offset,
            limit=payload.limit,
            file_id=payload.file_id or None,
            part_id=payload.part_id or None,
            status=payload.status or None,
            search=payload.q or None,
            stage=payload.stage,
        )

    @app.post("/api/v1/projects/{name}/segments/ids")
    async def segment_index(
        name: str, payload: SegmentFilterPayload
    ) -> dict[str, Any]:
        if bool(payload.file_id) != bool(payload.part_id):
            raise UsageError("file_id 与 part_id 必须同时提供")
        return WebStore(project(name)).segment_index(
            file_id=payload.file_id or None,
            part_id=payload.part_id or None,
            status=payload.status or None,
            search=payload.q or None,
            stage=payload.stage,
        )

    @app.get("/api/v1/projects/{name}/segments/{segment_id}")
    async def segment(name: str, segment_id: str) -> dict[str, Any]:
        return WebStore(project(name)).segment_detail(segment_id)

    @app.post("/api/v1/projects/{name}/translations")
    async def save_translation(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return WebStore(project(name)).save_translation(payload)

    @app.post("/api/v1/projects/{name}/reviews")
    async def save_review(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return WebStore(project(name)).save_review(payload)

    @app.post("/api/v1/projects/{name}/apply")
    async def apply_results(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        root = project(name)
        stage = str(payload.get("stage", ""))
        segment_ids = payload.get("segment_ids")
        if segment_ids is not None and (
            not isinstance(segment_ids, list)
            or not segment_ids
            or not all(isinstance(value, str) for value in segment_ids)
        ):
            raise UsageError("segment_ids 必须是非空字符串数组")
        allow_outdated = payload.get("allow_outdated_base", False)
        confirmed_all = payload.get("all", False)
        if not isinstance(allow_outdated, bool):
            raise UsageError("allow_outdated_base 必须是布尔值")
        if not isinstance(confirmed_all, bool):
            raise UsageError("all 必须是布尔值")
        scope = Scope(
            from_file=payload.get("from_file"),
            only_file=payload.get("only_file"),
            only_segment=payload.get("only_segment"),
            segment_ids=(
                tuple(dict.fromkeys(segment_ids))
                if isinstance(segment_ids, list)
                else None
            ),
        )
        with project_write_lock(root):
            return run_apply(
                root,
                stage,
                scope,
                allow_outdated_base=allow_outdated,
                confirmed_all=confirmed_all,
            )
