from __future__ import annotations
from collections.abc import Callable
import io
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import quote
from fastapi import FastAPI
from fastapi.responses import Response
from .errors import (
    UsageError,
)
from .locking import project_write_lock
from .project import (
    sync_global_templates,
)
from .project_export import export_project




def _resolve_export_file(root: Path, raw: str) -> Path:
    """Resolve a project-relative output file, rejecting escape and symlinks."""
    if "\0" in raw:
        raise UsageError("导出文件路径无效")
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise UsageError("导出文件路径必须位于项目 output 目录内")
    resolved = (root / "output" / relative).resolve()
    output_root = (root / "output").resolve()
    if not resolved.is_relative_to(output_root) or not resolved.is_file():
        raise UsageError("导出文件路径必须位于项目 output 目录内")
    return resolved

def _export_files(root: Path) -> list[Path]:
    """Return sorted regular files that are eligible for export downloads."""
    output_root = (root / "output").resolve()
    if not output_root.is_dir():
        return []
    files: list[Path] = []
    for path in sorted(output_root.rglob("*")):
        try:
            is_symlink = path.is_symlink()
            is_file = path.is_file()
        except OSError:
            continue
        if is_symlink or not is_file:
            continue
        relative = path.relative_to(output_root)
        if any(part == ".staging" for part in relative.parts):
            continue
        if path.name == ".DS_Store":
            continue
        files.append(path)
    return files

def _attachment_header(filename: str) -> str:
    encoded = quote(filename)
    if encoded == filename:
        return f'attachment; filename="{filename}"'
    return f"attachment; filename*=utf-8''{encoded}"

def register_export_routes(*, app: FastAPI, projects_root: Path, app_root: Path, project: Callable[[str], Path]) -> None:
    SESSION_COOKIE = "another_llm_session"
    _SESSION_TTL_SECONDS = 30 * 24 * 3600
    _WINDOWS_DRIVE_TYPES = {0: "unknown", 1: "unavailable", 2: "removable", 3: "fixed", 4: "network", 5: "cdrom", 6: "ramdisk"}


    @app.post("/api/v1/projects/{name}/export")
    async def export(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        root = project(name)
        file_ids = payload.get("file_ids")
        if file_ids is not None and (
            not isinstance(file_ids, list)
            or not all(isinstance(value, str) and value for value in file_ids)
        ):
            raise UsageError("file_ids 必须是字符串数组或 null")
        with project_write_lock(root):
            return export_project(
                root,
                str(payload.get("stage", "")),
                bilingual=bool(payload.get("bilingual", False)),
                allow_missing=bool(payload.get("allow_missing", False)),
                output_format=str(payload.get("format", "original")),
                file_ids=file_ids,
            )

    @app.get("/api/v1/projects/{name}/exports")
    async def exports(name: str) -> dict[str, Any]:
        root = project(name)
        output_root = (root / "output").resolve()
        files: list[dict[str, Any]] = []
        for path in _export_files(root):
            relative = path.relative_to(output_root)
            stat = path.stat()
            files.append(
                {
                    "path": relative.as_posix(),
                    "size": stat.st_size,
                    "mtime": int(stat.st_mtime),
                }
            )
        return {"files": files}

    @app.post("/api/v1/projects/{name}/exports/download")
    async def download_export(
        name: str, payload: dict[str, Any]
    ) -> Response:
        file = payload.get("file")
        if not isinstance(file, str) or not file:
            raise UsageError("导出文件路径必须是非空字符串")
        root = project(name)
        path = _resolve_export_file(root, file)
        return Response(
            content=path.read_bytes(),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": _attachment_header(path.name)
            },
        )

    @app.get("/api/v1/projects/{name}/exports/download-all")
    async def download_export_all(name: str) -> Response:
        root = project(name)
        paths = _export_files(root)
        if not paths:
            raise UsageError("至少需要一个导出文件")
        output_root = (root / "output").resolve()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in paths:
                archive.write(path, path.relative_to(output_root).as_posix())
        return Response(
            content=buffer.getvalue(),
            media_type="application/zip",
            headers={
                "Content-Disposition": _attachment_header(
                    f"{name}-exports.zip"
                )
            },
        )

    @app.post("/api/v1/projects/{name}/exports/remove")
    async def remove_exports(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        files = payload.get("files")
        if (
            not isinstance(files, list)
            or not files
            or not all(isinstance(value, str) and value for value in files)
        ):
            raise UsageError("files 必须是非空字符串数组")
        root = project(name)
        paths = [_resolve_export_file(root, raw) for raw in files]
        with project_write_lock(root):
            for path in paths:
                path.unlink()
        return {"removed": files}

    @app.post("/api/v1/projects/{name}/sync-templates")
    async def sync_templates(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        root = project(name)
        choice = payload.get("choice")
        if choice not in {"update", "keep"}:
            raise UsageError("模板选择必须是 update 或 keep")
        with project_write_lock(root):
            warnings = sync_global_templates(
                root,
                app_root=app_root,
                interactive=True,
                choice=choice,
            )
        return {"warnings": warnings}
