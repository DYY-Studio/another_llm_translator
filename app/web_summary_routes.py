from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from .errors import UsageError
from .execution import Scope
from .i18n import SUPPORTED_LANGUAGES
from .locking import project_write_lock
from .project import load_segments
from .sqlite_storage import (
    read_content_summaries,
    read_summary_participation,
    write_summary_participation,
)
from .stage_runtime import prompt_preflight
from .summary_aggregation import (
    aggregation_preflight,
    export_summary_markdown,
)
from .summary_provenance import assess_full_summary
from .web_payloads import (
    BoundaryPayload,
    SummaryExportPayload,
    SummaryParticipationPayload,
    SummarySelectionPayload,
)


def _selection(values: list[BoundaryPayload]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        file_id = value.file_id
        part_id = value.part_id
        boundary = (file_id, part_id)
        if boundary in seen:
            raise UsageError(f"boundary 不能重复：{file_id}/{part_id}")
        seen.add(boundary)
        result.append({"file_id": file_id, "part_id": part_id})
    return result


def _prompt_language(language: str | None) -> str | None:
    if language is None:
        return None
    if language not in SUPPORTED_LANGUAGES:
        raise UsageError("language 必须是 zh-CN 或 en")
    return str(language)


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
        current_by_boundary: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for segment in load_segments(root):
            if segment.get("is_empty"):
                continue
            boundary = (str(segment["file_id"]), str(segment["part_id"]))
            current_by_boundary.setdefault(boundary, []).append(segment)
        artifacts_by_boundary: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for artifact in artifacts:
            boundary = (str(artifact["file_id"]), str(artifact["part_id"]))
            artifacts_by_boundary.setdefault(boundary, []).append(artifact)
        visible_artifacts = []
        for artifact in artifacts:
            boundary = (str(artifact["file_id"]), str(artifact["part_id"]))
            assessment = (
                assess_full_summary(
                    artifact,
                    current_by_boundary.get(boundary, []),
                    artifacts_by_boundary.get(boundary, []),
                )
                if artifact.get("kind") == "full"
                else None
            )
            visible_artifacts.append(
                {
                    **artifact,
                    "expired": assessment.expired if assessment is not None else False,
                    "expiry_reason": (
                        assessment.expiry_reason if assessment is not None else None
                    ),
                }
            )
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
            "artifacts": visible_artifacts,
        }

    @app.put("/api/v1/projects/{name}/summaries/participation")
    async def save_participation(
        name: str, payload: SummaryParticipationPayload
    ) -> dict[str, Any]:
        values = _selection(payload.boundaries)
        selected = payload.selected
        with project_write_lock(project(name)):
            write_summary_participation(
                project(name),
                [dict(value, selected=selected) for value in values],
            )
        return {"participation": read_summary_participation(project(name))}

    @app.post("/api/v1/projects/{name}/summaries/aggregation-preflight")
    async def preflight(
        name: str, payload: SummarySelectionPayload
    ) -> dict[str, Any]:
        language = _prompt_language(payload.language)
        prompt = prompt_preflight(project(name), language, ("content_summary",))
        if not bool(prompt["ok"]):
            missing = ", ".join(str(item) for item in prompt["missing"])
            raise UsageError(
                f"缺少 {prompt['language']} Prompt：{missing}",
                reason="prompt_language_missing",
            )
        return aggregation_preflight(project(name), _selection(payload.boundaries))

    @app.post("/api/v1/projects/{name}/summaries/aggregate")
    async def aggregate(
        name: str, payload: SummarySelectionPayload
    ) -> dict[str, Any]:
        root = project(name)
        values = _selection(payload.boundaries)
        return await app.state.tasks.start(
            root,
            "content_summary",
            scope=Scope(),
            reuse_mixed_fingerprints=False,
            run_action=None,
            prompt_language=_prompt_language(payload.language),
            summary_selection=values,
        )

    @app.post("/api/v1/projects/{name}/summaries/export")
    async def export(name: str, payload: SummaryExportPayload) -> dict[str, Any]:
        root = project(name)
        with project_write_lock(root):
            output = export_summary_markdown(
                root,
                _selection(payload.boundaries),
                payload.path,
            )
        return {"path": output.relative_to(root / "output").as_posix()}
