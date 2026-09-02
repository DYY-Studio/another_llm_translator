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

def register_term_routes(*, app: FastAPI, projects_root: Path, app_root: Path) -> None:
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

    @app.get("/api/v1/projects/{name}/terms")
    async def terms(name: str) -> dict[str, Any]:
        return WebStore(project(name)).terms()

    @app.post("/api/v1/projects/{name}/terms/hits")
    async def term_hits(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        normalized = payload.get("normalized")
        if not normalized:
            raise UsageError("术语命中查询必须提供 normalized")
        if not isinstance(normalized, str):
            raise UsageError("normalized 必须是字符串")
        try:
            offset = int(payload.get("offset", 0))
            limit = int(payload.get("limit", 50))
        except (TypeError, ValueError) as exc:
            raise UsageError("术语命中窗口参数必须是整数") from exc
        return WebStore(project(name)).term_hits(
            normalized, offset=offset, limit=limit
        )

    @app.post("/api/v1/projects/{name}/terms/related")
    async def related_terms(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        normalized = payload.get("normalized")
        if not normalized:
            raise UsageError("术语推荐查询必须提供 normalized")
        if not isinstance(normalized, str):
            raise UsageError("normalized 必须是字符串")
        try:
            limit = int(payload.get("limit", 20))
        except (TypeError, ValueError) as exc:
            raise UsageError("术语推荐数量参数必须是整数") from exc
        return WebStore(project(name)).related_terms(normalized, limit=limit)

    @app.post("/api/v1/projects/{name}/terms")
    async def save_term(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return WebStore(project(name)).save_term(payload)

    @app.post("/api/v1/projects/{name}/terms/materialize")
    async def materialize_term(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return WebStore(project(name)).materialize_term(payload)

    @app.post("/api/v1/projects/{name}/terms/set-primary")
    async def set_term_primary(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return WebStore(project(name)).set_term_primary(payload)

    @app.post("/api/v1/projects/{name}/terms/leave-group")
    async def leave_term_group(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return WebStore(project(name)).leave_term_group(payload)

    @app.post("/api/v1/projects/{name}/terms/group-related")
    async def group_related_terms(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return WebStore(project(name)).group_related_terms(payload)

    @app.post("/api/v1/projects/{name}/terms/convert-to-alias")
    async def convert_related_to_alias(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return WebStore(project(name)).convert_related_to_alias(payload)

    @app.post("/api/v1/projects/{name}/terms/remove")
    async def remove_terms(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return WebStore(project(name)).remove_terms(payload)

    @app.post("/api/v1/projects/{name}/terms/clear")
    async def clear_terms(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("confirm") is not True:
            raise UsageError("必须明确确认清空术语阶段")
        root = project(name)
        if app.state.tasks.is_project_running(root):
            raise UsageError("项目存在运行中的任务，结束或取消后才能清空术语阶段")
        return WebStore(root).clear_terms()

    @app.post("/api/v1/projects/{name}/terms/delete")
    async def delete_terms(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return WebStore(project(name)).delete_terms(payload)

    @app.get("/api/v1/projects/{name}/terms/decision")
    async def get_term_decision(name: str) -> dict[str, Any]:
        return decision_review_state(project(name))

    @app.put("/api/v1/projects/{name}/terms/decision/rejections")
    async def put_term_decision_rejections(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        root = project(name)
        if app.state.tasks.is_project_running(root):
            raise UsageError("项目存在运行中的任务，不能修改术语决策草案")
        values = payload.get("rejected_proposal_ids")
        if not isinstance(values, list):
            raise UsageError("rejected_proposal_ids 必须是字符串数组")
        with project_write_lock(root):
            return {"draft": save_decision_rejections(root, values)}

    @app.put("/api/v1/projects/{name}/terms/decision/manual-review")
    async def put_term_decision_manual_review(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        root = project(name)
        run_id = payload.get("run_id")
        normalized = payload.get("normalized")
        resolved = payload.get("resolved")
        if not isinstance(run_id, str) or not run_id:
            raise UsageError("run_id 必须是非空字符串")
        if not isinstance(normalized, str) or not normalized:
            raise UsageError("normalized 必须是非空字符串")
        if not isinstance(resolved, bool):
            raise UsageError("resolved 必须是布尔值")
        with project_write_lock(root):
            return {"manual_review": set_manual_review_resolved(
                root,
                run_id=run_id,
                normalized=normalized,
                resolved=resolved,
            )}

    @app.post("/api/v1/projects/{name}/terms/decision/apply")
    async def apply_term_decision(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        root = project(name)
        if app.state.tasks.is_project_running(root):
            raise UsageError("项目存在运行中的任务，不能应用术语决策")
        with project_write_lock(root):
            result = apply_decision_draft(
                root,
                confirm_all=payload.get("confirm") is True,
                rejected_proposal_ids=[],
            )
            return {**result, "terms": WebStore(root).terms()}

    @app.post("/api/v1/projects/{name}/terms/decision/discard")
    async def discard_term_decision(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        root = project(name)
        if app.state.tasks.is_project_running(root):
            raise UsageError("项目存在运行中的任务，不能丢弃术语决策草案")
        with project_write_lock(root):
            return discard_decision_draft(
                root, confirm=payload.get("confirm") is True
            )

    @app.post("/api/v1/projects/{name}/terms/decision/rollback")
    async def rollback_term_decision(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        root = project(name)
        if app.state.tasks.is_project_running(root):
            raise UsageError("项目存在运行中的任务，不能撤销术语决策")
        with project_write_lock(root):
            result = rollback_decision(
                root, confirm=payload.get("confirm") is True
            )
            return {**result, "terms": WebStore(root).terms()}

    @app.post("/api/v1/projects/{name}/terms/publish-partial")
    async def publish_partial_term_results(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if payload.get("confirm") is not True:
            raise UsageError("必须明确确认发布当前扫描候选")
        root = project(name)
        if app.state.tasks.is_running(root, "terminology"):
            raise UsageError("术语扫描仍在运行，结束 Run 后才能发布现有结果")
        with project_write_lock(root):
            return publish_partial_terms(root)

    @app.post("/api/v1/projects/{name}/terms/import")
    async def import_term_file(
        name: str,
        file: UploadFile = File(...),
    ) -> dict[str, Any]:
        suffix = Path(file.filename or "").suffix.casefold()
        if suffix not in {".json", ".csv"}:
            raise UsageError("术语文件扩展名必须是 .json 或 .csv")
        root = project(name)
        with tempfile.NamedTemporaryFile(
            dir=root,
            prefix=".terms-import.",
            suffix=suffix,
            delete=False,
        ) as handle:
            handle.write(await file.read())
            temporary = Path(handle.name)
        try:
            with project_write_lock(root):
                return import_terms(root, temporary, dry_run=False)
        finally:
            temporary.unlink(missing_ok=True)

    @app.get("/api/v1/projects/{name}/terms/export")
    async def export_term_file(
        name: str,
        format: str = "json",
        include_disabled: bool = False,
        source: str = "published",
    ) -> Response:
        if format not in {"json", "csv"}:
            raise UsageError("术语导出格式必须是 json 或 csv")
        if source not in {"published", "scanned"}:
            raise UsageError("术语导出 source 必须是 published 或 scanned")
        root = project(name)
        with tempfile.TemporaryDirectory(
            dir=root, prefix=".terms-export."
        ) as raw:
            output = Path(raw) / f"{name}-terms.{format}"
            export_terms(
                root,
                output,
                include_disabled=include_disabled,
                source=source,
            )
            content = output.read_bytes()
        media_type = (
            "application/json"
            if format == "json"
            else "text/csv; charset=utf-8"
        )
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="terms.{format}"'
                )
            },
        )
