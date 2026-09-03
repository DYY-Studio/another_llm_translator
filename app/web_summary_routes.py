from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from .errors import UsageError
from .execution import Scope
from .locking import project_write_lock
from .project import load_segments
from .sqlite_storage import (
    read_content_summaries,
    read_summary_participation,
    write_summary_participation,
)
from .summary_aggregation import (
    aggregation_preflight,
    export_summary_markdown,
)


def _selection(payload: dict[str, Any]) -> list[dict[str, str]]:
    values = payload.get("boundaries", payload.get("selection"))
    if not isinstance(values, list):
        raise UsageError("boundaries 必须是 file_id/part_id 对象数组")
    result: list[dict[str, str]] = []
    for value in values:
        if not isinstance(value, dict):
            raise UsageError("boundaries 必须是 file_id/part_id 对象数组")
        file_id = value.get("file_id")
        part_id = value.get("part_id")
        if not isinstance(file_id, str) or not file_id:
            raise UsageError("boundary 缺少 file_id")
        if not isinstance(part_id, str) or not part_id:
            raise UsageError("boundary 缺少 part_id")
        result.append({"file_id": file_id, "part_id": part_id})
    return result


def register_summary_routes(
    *,
    app: FastAPI,
    projects_root: Path,
    app_root: Path,
    project: Callable[[str], Path],
) -> None:
    del projects_root, app_root

    @app.get("/api/v1/projects/{name}/summaries")
    async def summaries(name: str) -> dict[str, Any]:
        root = project(name)
        selected = {
            (str(item["file_id"]), str(item["part_id"])): bool(item["selected"])
            for item in read_summary_participation(root)
        }
        boundaries: dict[tuple[str, str], int] = {}
        for segment in load_segments(root):
            if segment.get("is_empty"):
                continue
            key = (str(segment["file_id"]), str(segment["part_id"]))
            boundaries[key] = boundaries.get(key, 0) + 1
        artifacts = read_content_summaries(root)
        return {
            "participation": [
                {
                    "file_id": file_id,
                    "part_id": part_id,
                    "selected": selected.get((file_id, part_id), False),
                }
                for file_id, part_id in sorted(boundaries)
            ],
            "boundaries": [
                {
                    "file_id": file_id,
                    "part_id": part_id,
                    "segment_count": count,
                }
                for (file_id, part_id), count in sorted(boundaries.items())
            ],
            "artifacts": artifacts,
        }

    @app.put("/api/v1/projects/{name}/summaries/participation")
    async def save_participation(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        values = _selection(payload)
        selected = payload.get("selected", True)
        if not isinstance(selected, bool):
            raise UsageError("selected 必须是布尔值")
        with project_write_lock(project(name)):
            write_summary_participation(
                project(name),
                [dict(value, selected=selected) for value in values],
            )
        return {"participation": read_summary_participation(project(name))}

    @app.post("/api/v1/projects/{name}/summaries/aggregation-preflight")
    async def preflight(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return aggregation_preflight(project(name), _selection(payload))

    @app.post("/api/v1/projects/{name}/summaries/aggregate")
    async def aggregate(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        root = project(name)
        values = _selection(payload)
        return await app.state.tasks.start(
            root,
            "content_summary",
            scope=Scope(),
            reuse_mixed_fingerprints=False,
            run_action=None,
            summary_selection=values,
        )

    @app.post("/api/v1/projects/{name}/summaries/export")
    async def export(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        root = project(name)
        relative_path = payload.get("path", "summary.md")
        if not isinstance(relative_path, str):
            raise UsageError("path 必须是字符串")
        with project_write_lock(root):
            output = export_summary_markdown(
                root,
                _selection(payload),
                relative_path,
            )
        return {"path": output.relative_to(root / "output").as_posix()}
