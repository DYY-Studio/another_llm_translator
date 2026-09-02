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

@dataclass
class ReplacementPreviewSession:
    preview_id: str
    plan: FileReplacementPlan

    @property
    def temporary_root(self) -> Path:
        return self.plan.temporary_root

    def cleanup(self) -> None:
        self.plan.cleanup()

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

def register_project_routes(*, app: FastAPI, projects_root: Path, app_root: Path) -> None:
    SESSION_COOKIE = "another_llm_session"
    _SESSION_TTL_SECONDS = 30 * 24 * 3600
    _WINDOWS_DRIVE_TYPES = {0: "unknown", 1: "unavailable", 2: "removable", 3: "fixed", 4: "network", 5: "cdrom", 6: "ramdisk"}
    async def stage_uploads(
        upload_root: Path,
        uploads: list[UploadFile],
        relative_paths: list[str] | None,
        input_kinds: list[str] | None,
        server_paths: list[str] | None = None,
        server_input_kinds: list[str] | None = None,
    ) -> tuple[list[str], list[str], list[str]]:
        paths = (
            relative_paths
            if relative_paths is not None
            else [Path(upload.filename or "input").name for upload in uploads]
        )
        kinds = input_kinds if input_kinds is not None else ["file"] * len(uploads)
        if len(paths) != len(uploads) or len(kinds) != len(uploads):
            raise UsageError("上传文件、相对路径与来源类型数量不一致")
        normalized: list[str] = []
        seen: set[str] = set()
        for value in paths:
            path = PurePosixPath(value)
            if (
                not value
                or "\\" in value
                or path.is_absolute()
                or path.name in {"", ".", ".."}
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                raise UsageError(f"上传相对路径无效：{value}")
            name = path.as_posix()
            key = name.casefold()
            if key in seen:
                raise UsageError(f"上传文件存在重复相对路径：{name}")
            seen.add(key)
            normalized.append(name)
        if any(kind not in {"file", "folder"} for kind in kinds):
            raise UsageError("输入来源类型必须是 file 或 folder")

        server_entries: list[tuple[str, Path]] = []
        if server_paths is not None:
            server_kinds = (
                server_input_kinds
                if server_input_kinds is not None
                else ["file"] * len(server_paths)
            )
            if len(server_paths) != len(server_kinds):
                raise UsageError("服务端路径与来源类型数量不一致")
            if any(kind not in {"file", "folder"} for kind in server_kinds):
                raise UsageError("输入来源类型必须是 file 或 folder")
            for raw, kind in zip(server_paths, server_kinds, strict=True):
                if not isinstance(raw, str) or not raw.strip():
                    raise UsageError("服务端路径必须是非空字符串")
                try:
                    current = Path(raw).resolve(strict=True)
                except (OSError, RuntimeError, ValueError) as exc:
                    raise UsageError(f"路径不存在或无法访问：{raw}: {exc}") from exc
                if kind == "folder":
                    if not current.is_dir():
                        raise UsageError(f"路径不是目录：{raw}")
                    found: list[tuple[str, Path]] = []
                    for dirpath, dirnames, filenames in os.walk(
                        current, followlinks=False
                    ):
                        dirnames.sort(key=natural_path_key)
                        for filename in filenames:
                            full = Path(dirpath) / filename
                            relative = full.relative_to(current).as_posix()
                            try:
                                get_document_adapter_for_extension(full.suffix)
                            except UsageError:
                                continue
                            found.append((relative, full))
                    found.sort(key=lambda item: natural_path_key(item[0]))
                    if not found:
                        raise UsageError(f"目录中没有受支持的输入文件：{raw}")
                    server_entries.extend(found)
                else:
                    if not current.is_file():
                        raise UsageError(f"路径不是文件：{raw}")
                    try:
                        get_document_adapter_for_extension(current.suffix)
                    except UsageError:
                        raise UsageError(f"不支持的输入文件：{raw}") from None
                    server_entries.append((current.name, current))

        for relative, source in server_entries:
            key = relative.casefold()
            if key in seen:
                raise UsageError(f"输入文件存在重复相对路径：{relative}")
            seen.add(key)

        inputs: list[str] = []
        original_names: list[str] = []
        ignored: list[str] = []
        for upload, name, kind in zip(
            uploads, normalized, kinds, strict=True
        ):
            try:
                get_document_adapter_for_extension(PurePosixPath(name).suffix)
            except UsageError:
                if kind == "folder":
                    ignored.append(name)
                    continue
                raise UsageError(f"不支持的输入文件：{name}") from None
            target = upload_root.joinpath(*PurePosixPath(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(await upload.read())
            inputs.append(str(target))
            original_names.append(name)
        for relative, source in server_entries:
            target = upload_root.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            inputs.append(str(target))
            original_names.append(relative)
        warnings = []
        if ignored:
            examples = "、".join(ignored[:5])
            suffix = "…" if len(ignored) > 5 else ""
            warnings.append(
                f"已忽略 {len(ignored)} 个不支持的文件：{examples}{suffix}"
            )
        return inputs, original_names, warnings

    def parse_adapter_options(raw: str) -> dict[str, dict[str, str]]:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise UsageError("Document Adapter 导入选项不是合法 JSON") from exc
        if not isinstance(value, dict):
            raise UsageError("Document Adapter 导入选项必须是对象")
        for adapter_id, options in value.items():
            if (
                not isinstance(adapter_id, str)
                or not isinstance(options, dict)
                or any(
                    not isinstance(option_id, str)
                    or not isinstance(option_value, str)
                    for option_id, option_value in options.items()
                )
            ):
                raise UsageError("Document Adapter 导入选项格式无效")
        return value

    def remember_project(path: Path) -> None:
        normalized = path.resolve()
        if normalized.parent == projects_root.resolve():
            return
        app.state.external_projects.add(normalized)

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

    def project_selector(path: Path, metadata: dict[str, Any]) -> str:
        return (
            path.name
            if path.parent == projects_root.resolve()
            else str(metadata["project_id"])
        )

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

    def replacement_key(root: Path, file_id: str) -> tuple[Path, str]:
        return root.resolve(), file_id

    def prompt_languages_for(root: Path) -> dict[str, list[str]]:
        """Available prompt languages per stage from an effective view."""
        return {
            stage: [
                language
                for language in PROMPT_LANGUAGES
                if (root / "prompts" / prompt_file(stage, language)).is_file()
            ]
            for stage in LLM_MODEL_STAGES
        }

    def validate_language(value: object) -> str:
        if value not in PROMPT_LANGUAGES:
            raise UsageError("language 必须是 zh-CN 或 en")
        return str(value)

    def prompt_view(
        stage: str,
        language: str,
        file_for: Callable[[str], Path],
        available: list[str],
        global_file_for: Callable[[str], Path] | None = None,
    ) -> dict[str, Any]:
        resolved = (
            language
            if language in available and file_for(language).is_file()
            else "zh-CN"
        )
        path = file_for(resolved)
        content = path.read_text(encoding="utf-8")
        result: dict[str, Any] = {
            "content": content,
            "language": resolved,
            "assembled": full_prompt(stage, content, resolved),
            "languages": available,
        }
        if stage == "terminology_decision":
            assembled_phases = {
                phase: full_prompt(stage, content, resolved, phase=phase)
                for phase in ("adjudication", "consistency")
            }
            result["assembled_phases"] = assembled_phases
            result["assembled"] = assembled_phases["adjudication"]
        if global_file_for is not None:
            global_path = global_file_for(resolved)
            if global_path.is_file():
                result["global_sync"] = {
                    "available": True,
                    "same": path.read_bytes() == global_path.read_bytes(),
                    "language": resolved,
                }
            else:
                result["global_sync"] = {
                    "available": False,
                    "same": False,
                    "language": resolved,
                }
        return result

    def global_prompt_file(stage: str, language: str) -> Path:
        return effective_path(
            f"prompts/{prompt_file(stage, language)}", builtin_root=app_root
        )

    @app.get("/api/v1/projects")
    async def list_projects() -> dict[str, Any]:
        values = []
        selectors: set[str] = set()
        for item in project_paths():
            metadata = read_json(item, item / "project.json")
            selector = project_selector(item, metadata)
            if selector in selectors:
                raise UsageError(f"项目标识冲突：{selector}")
            selectors.add(selector)
            values.append(
                {
                    "selector": selector,
                    "name": metadata["name"],
                    "project_id": metadata["project_id"],
                    "path": str(item),
                    "external": item.parent != projects_root.resolve(),
                    "file_count": metadata["file_count"],
                    "segment_count": metadata["segment_count"],
                }
            )
        return {
            "projects": values,
            "default_projects_path": str(projects_root.resolve()),
        }

    @app.get(
        "/api/v1/projects/{name}/files/{file_id}/replacement-options"
    )
    async def replacement_options(name: str, file_id: str) -> dict[str, Any]:
        root = project(name)
        file_record = next(
            (
                item
                for item in load_source_files(root)
                if str(item.get("file_id")) == file_id
            ),
            None,
        )
        if file_record is None:
            raise UsageError(f"未知文件 ID：{file_id}")
        adapter_id = str(file_record["document_adapter_id"])
        adapter = get_document_adapter(adapter_id)
        state = read_adapter_state(root, file_id)
        opaque_state = (
            state.get("state")
            if isinstance(state, dict) and isinstance(state.get("state"), dict)
            else state
        )
        values = document_adapter_replacement_options(
            adapter,
            opaque_state=opaque_state,
        )
        summary = next(
            item
            for item in document_adapter_summaries(
                replacement_values=values
            )
            if item["adapter_id"] == adapter_id
        )
        return {"adapter": summary, "values": values}

    @app.post("/api/v1/projects")
    async def create_project(
        name: str = Form(...),
        empty: bool = Form(False),
        parent_dir: str = Form(""),
        files: list[UploadFile] | None = File(None),
        relative_paths: list[str] | None = Form(None),
        input_kinds: list[str] | None = Form(None),
        server_paths: list[str] | None = Form(None),
        server_input_kinds: list[str] | None = Form(None),
        adapter_options: str = Form("{}"),
    ) -> dict[str, Any]:
        uploads = files or []
        with tempfile.TemporaryDirectory(prefix="translator-upload-") as raw:
            upload_root = Path(raw)
            inputs, original_names, upload_warnings = await stage_uploads(
                upload_root,
                uploads,
                relative_paths,
                input_kinds,
                server_paths,
                server_input_kinds,
            )
            if empty == bool(inputs):
                raise UsageError("必须上传输入文件，或显式选择创建空项目")
            selected_root = (
                resolve_project_parent(parent_dir, require_absolute=True)
                if parent_dir
                else projects_root
            )
            path, summary = init_project(
                inputs,
                name=name,
                document_adapter_id=None,
                original_names=original_names,
                adapter_options=parse_adapter_options(adapter_options),
                empty=empty,
                app_root=app_root,
                projects_root=selected_root,
            )
            summary["warnings"] = [
                *upload_warnings,
                *list(summary["warnings"]),
            ]
        assert path is not None
        remember_project(path)
        metadata = read_json(path, path / "project.json")
        summary["project_path"] = str(path)
        summary["project_selector"] = project_selector(path, metadata)
        summary["external"] = path.parent != projects_root.resolve()
        return summary

    @app.post("/api/v1/projects/open")
    async def open_project(payload: dict[str, Any]) -> dict[str, Any]:
        value = payload.get("path")
        if not isinstance(value, str) or not value.strip():
            raise UsageError("项目路径必须是非空字符串")
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            raise UsageError("项目路径必须是绝对路径")
        root = resolve_project(str(candidate))
        metadata = read_json(root, root / "project.json")
        remember_project(root)
        return {
            "selector": project_selector(root, metadata),
            "name": metadata["name"],
            "path": str(root),
            "external": root.parent != projects_root.resolve(),
        }

    @app.delete("/api/v1/projects/{name}")
    async def delete_project_route(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if payload.get("confirm") is not True:
            raise UsageError("必须明确确认删除整个项目")
        root = project(name)
        if app.state.tasks.is_project_running(root):
            raise UsageError("项目存在运行中的任务，结束或取消后才能删除")
        with project_write_lock(root):
            result = delete_project(
                root,
                protected_roots=(projects_root, app_root, user_root()),
            )
        app.state.external_projects.discard(root.resolve())
        return result

    @app.get("/api/v1/projects/{name}")
    async def overview(
        name: str,
        request: Request,
    ) -> dict[str, Any]:
        params = request.query_params
        if set(params) - {"offset", "limit", "stage"}:
            raise UsageError(
                "Segment 搜索和筛选必须通过 POST /segments/query 提交"
            )
        try:
            offset = int(params.get("offset", "0"))
            limit = int(params.get("limit", "100"))
        except ValueError as exc:
            raise UsageError("Segment 窗口参数必须是整数") from exc
        if offset < 0 or limit < 1:
            raise UsageError("Segment 窗口参数无效")
        try:
            return WebStore(project(name)).overview(
                offset=offset,
                limit=limit,
                stage=params.get("stage", "translation"),
            )
        except ValueError as exc:
            raise UsageError(str(exc)) from exc

    @app.post("/api/v1/projects/{name}/storage/compact")
    async def compact_project_route(name: str) -> dict[str, int]:
        root = project(name)
        if app.state.tasks.is_project_running(root):
            raise UsageError("项目存在运行中的任务，结束或取消后才能压缩存储")
        with project_write_lock(root):
            return compact_project_database(root)

    @app.post("/api/v1/projects/{name}/files")
    async def add_files(
        name: str,
        files: list[UploadFile] | None = File(None),
        relative_paths: list[str] | None = Form(None),
        input_kinds: list[str] | None = Form(None),
        server_paths: list[str] | None = Form(None),
        server_input_kinds: list[str] | None = Form(None),
        adapter_options: str = Form("{}"),
    ) -> dict[str, Any]:
        uploads = files or []
        if not uploads and not server_paths:
            raise UsageError("至少提供一个输入文件")
        root = project(name)
        with tempfile.TemporaryDirectory(prefix="translator-upload-") as raw:
            upload_root = Path(raw)
            inputs, original_names, upload_warnings = await stage_uploads(
                upload_root,
                uploads,
                relative_paths,
                input_kinds,
                server_paths,
                server_input_kinds,
            )
            if not inputs:
                raise UsageError("没有受支持的输入文件")
            with project_write_lock(root):
                summary = add_project_files(
                    root,
                    inputs,
                    original_names=original_names,
                    adapter_options=parse_adapter_options(adapter_options),
                )
            summary["warnings"] = [
                *upload_warnings,
                *list(summary["warnings"]),
            ]
            return summary

    @app.post("/api/v1/projects/{name}/files/{file_id}/replacement-preview")
    async def replacement_preview(
        name: str,
        file_id: str,
        file: UploadFile | None = File(None),
        server_path: str | None = Form(None),
        adapter_options: str = Form("{}"),
    ) -> dict[str, Any]:
        root = project(name)
        normalized_server_path = server_path.strip() if server_path else ""
        if file is None and not normalized_server_path:
            raise UsageError("必须上传一个替换文件或提供服务端文件路径")
        if file is not None and normalized_server_path:
            raise UsageError("替换只能提供一个文件来源")
        upload_root = Path(tempfile.mkdtemp(prefix="translator-replacement-upload-"))
        plan: FileReplacementPlan | None = None
        try:
            inputs, _, upload_warnings = await stage_uploads(
                upload_root,
                [file] if file is not None else [],
                None,
                None,
                [normalized_server_path] if normalized_server_path else None,
                ["file"] if normalized_server_path else None,
            )
            if len(inputs) != 1:
                raise UsageError("替换只能接受一个文件")
            with project_write_lock(root):
                plan = prepare_file_replacement(
                    root,
                    file_id,
                    Path(inputs[0]),
                    adapter_options=parse_adapter_options(adapter_options),
                )
            shutil.rmtree(upload_root, ignore_errors=True)
            preview_id = secrets.token_urlsafe(24)
            session = ReplacementPreviewSession(
                preview_id=preview_id,
                plan=plan,
            )
            key = replacement_key(root, file_id)
            previous = app.state.replacement_previews.pop(key, None)
            if previous is not None:
                previous.cleanup()
            app.state.replacement_previews[key] = session
            result = dict(plan.impact)
            result["warnings"] = [
                *upload_warnings,
                *plan.impact["warnings"],
            ]
            result["preview_id"] = preview_id
            return result
        except BaseException:
            if plan is not None:
                plan.cleanup()
            shutil.rmtree(upload_root, ignore_errors=True)
            raise

    @app.post("/api/v1/projects/{name}/files/{file_id}/replacement-confirm")
    async def replacement_confirm(
        name: str, file_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        preview_id = payload.get("preview_id")
        if not isinstance(preview_id, str) or not preview_id:
            raise UsageError("preview_id 必须是非空字符串")
        root = project(name)
        key = replacement_key(root, file_id)
        session = app.state.replacement_previews.get(key)
        if session is None or session.preview_id != preview_id:
            raise UsageError("替换预览不存在或已失效")
        with project_write_lock(root):
            result = apply_file_replacement(root, session.plan)
        app.state.replacement_previews.pop(key, None)
        session.cleanup()
        return result

    @app.delete(
        "/api/v1/projects/{name}/files/{file_id}/replacement-preview/{preview_id}"
    )
    async def replacement_cancel(
        name: str, file_id: str, preview_id: str
    ) -> dict[str, Any]:
        root = project(name)
        key = replacement_key(root, file_id)
        session = app.state.replacement_previews.get(key)
        if session is None or session.preview_id != preview_id:
            raise UsageError("替换预览不存在或已失效")
        app.state.replacement_previews.pop(key, None)
        session.cleanup()
        return {"cancelled": True}

    @app.post("/api/v1/projects/{name}/files/remove")
    async def remove_files(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        file_ids = payload.get("file_ids")
        if (
            not isinstance(file_ids, list)
            or not file_ids
            or not all(isinstance(value, str) and value for value in file_ids)
        ):
            raise UsageError("file_ids 必须是非空字符串数组")
        root = project(name)
        with project_write_lock(root):
            return remove_project_files(root, file_ids)

    @app.post("/api/v1/projects/{name}/files/reorder")
    async def reorder_files(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        file_ids = payload.get("file_ids")
        if (
            not isinstance(file_ids, list)
            or not file_ids
            or not all(isinstance(value, str) and value for value in file_ids)
        ):
            raise UsageError("file_ids 必须是非空字符串数组")
        root = project(name)
        with project_write_lock(root):
            return reorder_project_files(root, file_ids)

    @app.post("/api/v1/projects/{name}/results/reset")
    async def reset_results(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return WebStore(project(name)).reset_results(payload)

    @app.get("/api/v1/projects/{name}/config")
    async def get_config(name: str) -> dict[str, Any]:
        return {"config": load_config(project(name) / "config.toml")}

    @app.put("/api/v1/projects/{name}/config")
    async def put_config(name: str, payload: dict[str, Any]) -> dict[str, bool]:
        config = payload.get("config")
        if not isinstance(config, dict):
            raise UsageError("config 必须是对象")
        content = dump_config(config)
        root = project(name)
        resolve_project_config(config, presets_root=app_root)
        for stage in LLM_MODEL_STAGES:
            resolve_project_config(
                config, stage=stage, presets_root=app_root
            )
        with project_write_lock(root):
            atomic_write_text(root / "config.toml", content)
        return {"saved": True}

    @app.get("/api/v1/projects/{name}/prompts/{stage}")
    async def get_prompt(name: str, stage: str, language: str = "zh-CN") -> dict[str, Any]:
        if stage not in LLM_MODEL_STAGES:
            raise UsageError(f"未知 Prompt 阶段：{stage}")
        validate_language(language)
        root = project(name)
        return prompt_view(
            stage,
            language,
            lambda value: root / "prompts" / prompt_file(stage, value),
            prompt_languages_for(root)[stage],
            global_file_for=lambda value: global_prompt_file(stage, value),
        )

    @app.put("/api/v1/projects/{name}/prompts/{stage}")
    async def put_prompt(
        name: str, stage: str, payload: dict[str, Any]
    ) -> dict[str, bool]:
        if stage not in LLM_MODEL_STAGES:
            raise UsageError(f"未知 Prompt 阶段：{stage}")
        language = validate_language(payload.get("language", "zh-CN"))
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            raise UsageError("Prompt 不能为空")
        root = project(name)
        with project_write_lock(root):
            atomic_write_text(
                root / "prompts" / prompt_file(stage, language), content
            )
        return {"saved": True}

    @app.get("/api/v1/projects/{name}/task-options/{stage}")
    async def get_task_options(name: str, stage: str) -> dict[str, Any]:
        return task_options(project(name), stage)
