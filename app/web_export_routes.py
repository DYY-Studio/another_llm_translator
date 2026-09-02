from __future__ import annotations
import argparse
import ctypes
import hmac
import io
import ipaddress
import json
import os
import secrets
import shutil
import socket
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote
import httpx
import psutil
import uvicorn
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from .config import (
    LLM_MODEL_STAGES,
    dump_config,
    load_config,
    resolve_global_config,
    resolve_project_config,
)
from .credentials import (
    credential_summaries,
    delete_credential,
    parse_api_keys,
    read_credential,
    read_lan_password,
    resolve_api_keys,
    save_credential,
    save_lan_password,
)
from .diagnostics import DiagnosticsHub
from .errors import (
    AppError,
    ExternalError,
    InvalidCredentialsError,
    ProjectError,
    UsageError,
    app_error_payload,
    internal_error_payload,
)
from .execution import Scope, full_prompt
from .llm_adapter import load_json_adapter
from .llm_migration import migrate_llm_resources
from .llm_preset import LLMPreset, endpoint_url, load_llm_preset, preset_path
from .locking import project_write_lock
from .logging_utils import get_logger
from .plugins import (
    document_adapter_replacement_options,
    document_adapter_summaries,
    get_document_adapter,
    get_document_adapter_for_extension,
    resolve_translation_validators,
)
from .project import (
    APP_ROOT as DEFAULT_APP_ROOT,
)
from .project import (
    PROJECTS_ROOT,
    PROMPT_LANGUAGES,
    FileReplacementPlan,
    add_project_files,
    apply_file_replacement,
    delete_project,
    init_project,
    natural_path_key,
    prepare_file_replacement,
    prompt_file,
    remove_project_files,
    reorder_project_files,
    resolve_project,
    resolve_project_parent,
    sync_global_templates,
    load_source_files,
)
from .prompt_library import (
    delete_prompt_library,
    list_prompt_library,
    read_prompt_library,
    save_prompt_library,
)
from .server_config import load_server_config, save_server_config
from .sqlite_storage import (
    atomic_write_json,
    atomic_write_text,
    compact_project_database,
    database_path,
    read_json,
    read_adapter_state,
)
from .project_export import export_project
from .stage_review import run_apply
from .term_exchange import export_terms, import_terms
from .term_library import publish_partial_terms
from .term_decision import (
    apply_decision_draft,
    decision_review_state,
    discard_decision_draft,
    rollback_decision,
    save_decision_rejections,
    set_manual_review_resolved,
)
from .user_config import effective_path, user_root, write_user
from .web_store import WebStore
from .web_tasks import WebTaskManager, task_options

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

def register_export_routes(*, app: FastAPI, projects_root: Path, app_root: Path) -> None:
    SESSION_COOKIE = "another_llm_session"
    _SESSION_TTL_SECONDS = 30 * 24 * 3600
    _WINDOWS_DRIVE_TYPES = {0: "unknown", 1: "unavailable", 2: "removable", 3: "fixed", 4: "network", 5: "cdrom", 6: "ramdisk"}
    def project_paths() -> list[Path]:
        paths: list[Path] = []
        if projects_root.exists():
            paths.extend(
                item
                for item in sorted(
                    projects_root.iterdir(), key=lambda value: value.name
                )
                if database_path(item).is_file()
            )
        paths.extend(
            path
            for path in sorted(app.state.external_projects)
            if database_path(path).is_file()
        )
        unique: dict[Path, Path] = {}
        for path in paths:
            unique[path.resolve()] = path.resolve()
        return list(unique.values())

    def project(name: str) -> Path:
        if not name or "/" in name or "\\" in name or name in {".", ".."}:
            raise UsageError("项目名无效")
        try:
            return resolve_project(name, projects_root)
        except ProjectError:
            matches = [
                path
                for path in project_paths()
                if str(read_json(path, path / "project.json")["project_id"]) == name
            ]
            if len(matches) != 1:
                raise ProjectError(f"项目不存在或标识冲突：{name}")
            return matches[0]

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
