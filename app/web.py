from __future__ import annotations

import argparse
import ctypes
import hmac
import io
import ipaddress
import json
import os
import secrets
import socket
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable
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
    LLM_STAGES,
    dump_config,
    load_config,
    resolve_global_config,
    resolve_project_config,
)
from .credentials import (
    credential_summaries,
    delete_credential,
    read_credential,
    read_lan_password,
    resolve_api_key,
    save_credential,
    save_lan_password,
)
from .diagnostics import Diagnostics
from .errors import (
    AppError,
    ExternalError,
    InvalidCredentialsError,
    ProjectError,
    UsageError,
)
from .execution import Scope, full_prompt
from .llm_adapter import load_json_adapter
from .llm_preset import LLMPreset, endpoint_url, load_llm_preset, preset_path
from .locking import project_write_lock
from .logging_utils import get_logger
from .plugins import (
    document_adapter_summaries,
    get_document_adapter_for_extension,
)
from .project import (
    APP_ROOT as DEFAULT_APP_ROOT,
)
from .project import (
    PROJECTS_ROOT,
    PROMPT_LANGUAGES,
    add_project_files,
    delete_project,
    init_project,
    prompt_file,
    remove_project_files,
    resolve_project,
    resolve_project_parent,
    sync_global_templates,
)
from .server_config import load_server_config, save_server_config
from .sqlite_storage import (
    atomic_write_json,
    atomic_write_text,
    database_path,
    read_json,
)
from .stages import (
    export_project,
    export_terms,
    import_terms,
    publish_partial_terms,
    run_apply,
)
from .user_config import effective_path, user_root, write_user
from .web_store import WebStore
from .web_tasks import WebTaskManager, task_options

WEB_DIST = (
    Path(__file__).with_name("web_dist")
    if Path(__file__).with_name("web_dist").is_dir()
    else Path(sys.prefix) / "app" / "web_dist"
)
SESSION_COOKIE = "minimal_llm_session"
_SESSION_TTL_SECONDS = 30 * 24 * 3600
_WINDOWS_DRIVE_TYPES = {
    0: "unknown",
    1: "unavailable",
    2: "removable",
    3: "fixed",
    4: "network",
    5: "cdrom",
    6: "ramdisk",
}


def _windows_drive_entries() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []

    logical_drives = int(ctypes.windll.kernel32.GetLogicalDrives())
    entries: list[dict[str, Any]] = []
    for index in range(26):
        if not logical_drives & (1 << index):
            continue
        letter = chr(ord("A") + index)
        root = f"{letter}:\\"
        drive_type = int(ctypes.windll.kernel32.GetDriveTypeW(root))
        entries.append(
            {
                "name": f"{letter}:",
                "path": root,
                "type": _WINDOWS_DRIVE_TYPES.get(drive_type, "unknown"),
                "available": os.path.isdir(root),
            }
        )
    return entries


def _is_loopback(request: Request) -> bool:
    client = request.client.host if request.client else ""
    return client in {"127.0.0.1", "::1", "localhost", "testclient", "testserver"}


def lan_interfaces() -> list[dict[str, str]]:
    """IPv4 addresses and netmasks of up, non-loopback interfaces."""
    try:
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
    except OSError:
        return []
    interfaces = []
    for name, entries in addrs.items():
        if name == "lo" or name.startswith("lo"):
            continue
        if not stats.get(name) or not stats[name].isup:
            continue
        for entry in entries:
            if entry.family == socket.AF_INET and entry.netmask:
                interfaces.append(
                    {"name": name, "address": entry.address, "netmask": entry.netmask}
                )
                break
    return interfaces


def _client_allowed_on_bind(client_ip: str, bind_address: str) -> bool:
    if bind_address == "0.0.0.0":
        return True
    for item in lan_interfaces():
        if item["address"] != bind_address:
            continue
        try:
            network = ipaddress.ip_network(
                f"{item['address']}/{item['netmask']}", strict=False
            )
            return ipaddress.ip_address(client_ip) in network
        except ValueError:
            return False
    return False


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


def create_app(
    *,
    projects_root: Path = PROJECTS_ROOT,
    web_dist: Path = WEB_DIST,
    app_root: Path = DEFAULT_APP_ROOT,
    log_path: Path | None = None,
    server_config: dict[str, Any] | None = None,
) -> FastAPI:
    try:
        projects_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"无法创建项目目录：{projects_root}: {exc}") from exc
    app = FastAPI(title="Minimal LLM Translator", version="1")
    app.state.projects_root = projects_root
    app.state.app_root = app_root
    app.state.diagnostics = Diagnostics(log_path or user_root() / "logs" / "app.log")
    app.state.tasks = WebTaskManager(app.state.diagnostics)
    app.state.external_projects = set()
    app.state.server_config = server_config or load_server_config()
    app.state.sessions: dict[str, float] = {}

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
                        dirnames.sort()
                        for filename in sorted(filenames):
                            full = Path(dirpath) / filename
                            relative = full.relative_to(current).as_posix()
                            try:
                                get_document_adapter_for_extension(full.suffix)
                            except UsageError:
                                continue
                            found.append((relative, full))
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

    def valid_session(token: str | None) -> bool:
        if not token:
            return False
        expires = app.state.sessions.get(token)
        if expires is None:
            return False
        if expires <= time.time():
            app.state.sessions.pop(token, None)
            return False
        return True

    @app.middleware("http")
    async def gate_access(request: Request, call_next: Callable) -> Any:
        config = app.state.server_config
        loopback = _is_loopback(request)
        if not loopback:
            if not config["lan"]["enabled"]:
                return JSONResponse(
                    {"error": "只允许本机访问", "code": "local_only"}, status_code=403
                )
            client_ip = request.client.host if request.client else ""
            if not _client_allowed_on_bind(
                client_ip, config["lan"]["bind_address"]
            ):
                return JSONResponse(
                    {"error": "客户端不在允许的网段内", "code": "out_of_subnet"},
                    status_code=403,
                )
            path = request.url.path
            public = (
                path
                in {
                    "/api/v1/server/status",
                    "/api/v1/server/session",
                    "/api/v1/auth/login",
                    "/api/v1/auth/logout",
                }
                or not path.startswith("/api/")
            )
            if (
                config["auth"]["required"]
                and not public
                and not valid_session(request.cookies.get(SESSION_COOKIE))
            ):
                return JSONResponse(
                    {"error": "需要登录", "code": "auth_required"}, status_code=401
                )
            return await call_next(request)
        host = request.headers.get("host", "").split(":", 1)[0]
        if host not in {"127.0.0.1", "localhost", "testserver", "testclient"}:
            return JSONResponse(
                {"error": "只允许本机访问", "code": "local_only"}, status_code=403
            )
        origin = request.headers.get("origin")
        if origin:
            origin_host = origin.split("://", 1)[-1].split("/", 1)[0]
            origin_host = origin_host.split(":", 1)[0]
            if origin_host not in {"127.0.0.1", "localhost", "testserver"}:
                return JSONResponse(
                    {"error": "不允许的请求来源", "code": "invalid_origin"}, status_code=403
                )
        return await call_next(request)

    @app.exception_handler(AppError)
    async def app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            {
                "error": str(exc),
                "code": exc.code,
                "params": exc.params,
            },
            status_code=400,
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        fields: set[str] = set()
        for error in exc.errors():
            location = error.get("loc", ())
            field = ".".join(
                str(part)
                for part in location
                if part not in {"body", "query", "path", "header", "cookie"}
            )
            if field:
                fields.add(field)
        return JSONResponse(
            {
                "error": "请求参数无效",
                "code": "request_validation_error",
                "params": {"fields": sorted(fields)},
            },
            status_code=400,
        )

    def welcome_seen() -> bool:
        return (user_root() / ".welcome-seen").is_file()

    def mark_welcome_seen() -> None:
        (user_root() / ".welcome-seen").write_text("1", encoding="utf-8")

    @app.get("/api/v1/welcome")
    async def welcome() -> dict[str, Any]:
        return {"first": not welcome_seen()}

    @app.post("/api/v1/welcome/dismiss")
    async def dismiss_welcome() -> dict[str, bool]:
        mark_welcome_seen()
        return {"ok": True}

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

    @app.get("/api/v1/directories")
    async def list_directories(path: str = "") -> dict[str, Any]:
        raw_path = path.strip()
        if raw_path and "\0" in raw_path:
            raise UsageError("目录路径包含无效字符")
        try:
            candidate = (
                projects_root if not raw_path else Path(raw_path).expanduser()
            )
        except (RuntimeError, ValueError) as exc:
            raise UsageError("目录路径无效") from exc
        if raw_path and not candidate.is_absolute():
            raise UsageError("目录路径必须是绝对路径")
        if candidate.is_symlink():
            raise UsageError("目录路径不能是符号链接")
        try:
            current = candidate.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise UsageError(
                f"目录不存在或无法访问：{candidate}"
            ) from exc
        if not current.is_dir():
            raise UsageError(f"路径不是目录：{current}")
        try:
            children = sorted(
                current.iterdir(),
                key=lambda item: (item.name.casefold(), item.name),
            )
        except OSError as exc:
            raise UsageError(f"无法读取目录：{current}: {exc}") from exc
        directories = []
        for child in children:
            try:
                is_symlink = child.is_symlink()
                is_dir = child.is_dir()
            except OSError:
                continue
            if is_symlink or not is_dir:
                continue
            try:
                is_project = database_path(child).is_file()
            except OSError:
                is_project = False
            directories.append(
                {
                    "name": child.name,
                    "path": str(child),
                    "is_project": is_project,
                }
            )
        drives = _windows_drive_entries() if current.parent == current else []
        return {
            "path": str(current),
            "parent": None if current.parent == current else str(current.parent),
            "is_project": database_path(current).is_file(),
            "directories": directories,
            "drives": drives,
        }

    @app.get("/api/v1/document-adapters")
    async def document_adapters() -> dict[str, Any]:
        return {"adapters": document_adapter_summaries()}

    def validate_preset_payload(
        preset_id: str, payload: dict[str, Any]
    ) -> LLMPreset:
        if payload.get("preset_id") != preset_id:
            raise UsageError("URL 中的 Preset ID 必须与 preset_id 一致")
        presets = user_root() / "llm_presets"
        presets.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=presets,
            prefix=".preset.",
            suffix=".json",
            delete=False,
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False)
            temporary = Path(handle.name)
        try:
            preset = load_llm_preset(temporary)
            adapter = load_json_adapter(
                effective_path(
                    f"llm_adapters/{preset.adapter_id}.json",
                    builtin_root=app_root,
                )
            )
            if adapter.adapter_id != preset.adapter_id:
                raise UsageError(
                    "全局 Adapter 文件中的 adapter_id 与 Preset 不一致"
                )
            adapter.build_request(
                api_key="***",
                model=str(preset.definition["model"]),
                messages=[],
                temperature=0,
                max_output_tokens=int(preset.definition["max_output_tokens"]),
                stream=False,
                extra_body=preset.definition["extra_body"],
            )
            return preset
        finally:
            temporary.unlink(missing_ok=True)

    @app.get("/api/v1/global/config")
    async def get_global_config() -> dict[str, Any]:
        return {
            "config": load_config(
                effective_path("config/config.toml", builtin_root=app_root)
            )
        }

    @app.put("/api/v1/global/config")
    async def put_global_config(payload: dict[str, Any]) -> dict[str, bool]:
        config = payload.get("config")
        if not isinstance(config, dict):
            raise UsageError("config 必须是对象")
        content = dump_config(config)
        resolve_global_config(config, app_root)
        for stage in LLM_STAGES:
            resolve_global_config(config, app_root, stage=stage)
        atomic_write_text(write_user("config/config.toml"), content)
        return {"saved": True}

    def prompt_languages_for(root: Path) -> dict[str, list[str]]:
        """Available prompt languages per stage from an effective view."""
        return {
            stage: [
                language
                for language in PROMPT_LANGUAGES
                if (root / "prompts" / prompt_file(stage, language)).is_file()
            ]
            for stage in LLM_STAGES
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
    ) -> dict[str, Any]:
        resolved = (
            language
            if language in available and file_for(language).is_file()
            else "zh-CN"
        )
        path = file_for(resolved)
        content = path.read_text(encoding="utf-8")
        return {
            "content": content,
            "language": resolved,
            "assembled": full_prompt(stage, content, resolved),
            "languages": available,
        }

    def global_prompt_file(stage: str, language: str) -> Path:
        return effective_path(
            f"prompts/{prompt_file(stage, language)}", builtin_root=app_root
        )

    @app.get("/api/v1/global/prompts/{stage}")
    async def get_global_prompt(stage: str, language: str = "zh-CN") -> dict[str, Any]:
        if stage not in LLM_STAGES:
            raise UsageError(f"未知 Prompt 阶段：{stage}")
        validate_language(language)
        return prompt_view(
            stage,
            language,
            lambda value: global_prompt_file(stage, value),
            prompt_languages_for(app_root)[stage],
        )

    @app.put("/api/v1/global/prompts/{stage}")
    async def put_global_prompt(
        stage: str, payload: dict[str, Any]
    ) -> dict[str, bool]:
        if stage not in LLM_STAGES:
            raise UsageError(f"未知 Prompt 阶段：{stage}")
        language = validate_language(payload.get("language", "zh-CN"))
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            raise UsageError("Prompt 必须是非空字符串")
        atomic_write_text(
            write_user(f"prompts/{prompt_file(stage, language)}"), content
        )
        return {"saved": True}

    @app.get("/api/v1/global/presets")
    async def list_global_presets() -> dict[str, Any]:
        selected = str(
            load_config(
                effective_path("config/config.toml", builtin_root=app_root)
            )["llm"].get("preset", "")
        )
        values = []
        paths: dict[str, Path] = {}
        for root in (user_root(), app_root):
            for path in sorted((root / "llm_presets").glob("*.json")):
                paths.setdefault(path.stem, path)
        for path in sorted(paths.values(), key=lambda value: value.stem):
            try:
                preset = load_llm_preset(path)
                values.append(
                    {
                        "preset_id": preset.preset_id,
                        "adapter_id": preset.adapter_id,
                        "model": preset.definition["model"],
                        "selected": preset.preset_id == selected,
                        "valid": True,
                        "digest": preset.digest,
                    }
                )
            except AppError as exc:
                values.append(
                    {
                        "preset_id": path.stem,
                        "selected": path.stem == selected,
                        "valid": False,
                        "error": str(exc),
                        "error_code": exc.code,
                    }
                )
        return {"presets": values}

    def preset_file(preset_id: str) -> Path:
        preset_path(app_root, preset_id)
        return effective_path(f"llm_presets/{preset_id}.json", builtin_root=app_root)

    @app.get("/api/v1/global/presets/{preset_id}")
    async def get_global_preset(preset_id: str) -> dict[str, Any]:
        return load_llm_preset(preset_file(preset_id)).definition

    @app.put("/api/v1/global/presets/{preset_id}")
    async def put_global_preset(
        preset_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        preset = validate_preset_payload(preset_id, payload)
        atomic_write_json(write_user(f"llm_presets/{preset_id}.json"), payload)
        return {"saved": True, "digest": preset.digest}

    @app.delete("/api/v1/global/presets/{preset_id}")
    async def delete_global_preset(preset_id: str) -> dict[str, bool]:
        preset_file(preset_id)
        config = load_config(
            effective_path("config/config.toml", builtin_root=app_root)
        )
        if config["llm"].get("preset") == preset_id:
            raise UsageError("不能删除全局配置正在使用的 LLM Preset")
        for item in projects_root.iterdir() if projects_root.exists() else ():
            if not database_path(item).is_file():
                continue
            project_config = load_config(item / "config.toml")
            if project_config["llm"].get("preset") == preset_id:
                raise UsageError(f"不能删除项目 {item.name} 正在使用的 LLM Preset")
        user_file = user_root() / "llm_presets" / f"{preset_id}.json"
        builtin_file = app_root / "llm_presets" / f"{preset_id}.json"
        if user_file.is_file():
            user_file.unlink()
            return {"deleted": True}
        if builtin_file.is_file():
            raise UsageError(f"内置 LLM Preset 不能删除：{preset_id}")
        raise UsageError(f"LLM Preset 不存在：{preset_id}")

    @app.get("/api/v1/global/presets/{preset_id}/preview")
    async def preview_global_preset(preset_id: str) -> dict[str, Any]:
        preset = load_llm_preset(preset_file(preset_id))
        adapter = load_json_adapter(
            effective_path(
                f"llm_adapters/{preset.adapter_id}.json", builtin_root=app_root
            )
        )
        headers, body = adapter.build_request(
            api_key="***",
            model=str(preset.definition["model"]),
            messages=[{"role": "user", "content": "…"}],
            temperature=0.2,
            max_output_tokens=int(preset.definition["max_output_tokens"]),
            stream=False,
            extra_body=preset.definition["extra_body"],
        )
        return {
            "url": endpoint_url(
                preset.definition["base_url"],
                preset.definition["endpoint"],
                model=preset.definition["model"],
            ),
            "headers": headers,
            "body": body,
        }

    @app.post("/api/v1/global/presets/{preset_id}/models")
    async def discover_preset_models(
        preset_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        preset = validate_preset_payload(preset_id, payload)
        adapter = load_json_adapter(
            effective_path(
                f"llm_adapters/{preset.adapter_id}.json", builtin_root=app_root
            )
        )
        if adapter.models_spec is None:
            raise UsageError("该 Adapter 未声明模型发现规格")
        api_key = resolve_api_key(preset.definition["credential"])
        endpoint, headers = adapter.build_models_request(api_key=api_key)
        url = endpoint_url(
            preset.definition["base_url"], endpoint, model=preset.definition["model"]
        )
        timeout = float(preset.definition["request_timeout_seconds"])
        proxy = str(preset.definition["proxy_url"]) or None
        try:
            async with httpx.AsyncClient(timeout=timeout, proxy=proxy) as client:
                response = await client.get(url, headers=headers)
        except (httpx.HTTPError, OSError) as exc:
            raise UsageError(f"模型列表请求失败：{exc}") from exc
        if response.status_code >= 400:
            raise UsageError(f"模型列表请求失败：HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise UsageError("模型列表响应不是合法 JSON") from exc
        try:
            models = adapter.parse_models_response(data)
        except ExternalError as exc:
            raise UsageError(str(exc)) from exc
        return {"models": models, "count": len(models)}

    @app.get("/api/v1/credentials")
    async def list_credentials() -> dict[str, Any]:
        return {"credentials": credential_summaries()}

    @app.post("/api/v1/credentials")
    async def create_credential(payload: dict[str, Any]) -> dict[str, bool]:
        credential_id = payload.get("id")
        secret = payload.get("secret")
        if not isinstance(credential_id, str) or not isinstance(secret, str):
            raise UsageError("凭据 ID 和内容必须是字符串")
        save_credential(credential_id, secret)
        return {"saved": True}

    @app.put("/api/v1/credentials/{credential_id}")
    async def update_credential(
        credential_id: str, payload: dict[str, Any]
    ) -> dict[str, bool]:
        secret = payload.get("secret")
        if not isinstance(secret, str):
            raise UsageError("凭据内容必须是字符串")
        if read_credential(credential_id) is None:
            raise UsageError(f"凭据不存在：{credential_id}")
        save_credential(credential_id, secret)
        return {"saved": True}

    @app.delete("/api/v1/credentials/{credential_id}")
    async def delete_credential_route(credential_id: str) -> dict[str, bool]:
        delete_credential(credential_id)
        return {"deleted": True}

    @app.post("/api/v1/credentials/{credential_id}/test")
    async def test_credential_route(credential_id: str) -> dict[str, Any]:
        if read_credential(credential_id) is None:
            raise UsageError(f"凭据不存在：{credential_id}")
        return {"ok": True}

    @app.get("/api/v1/server/status")
    async def server_status(request: Request) -> dict[str, Any]:
        config = app.state.server_config
        loopback = _is_loopback(request)
        return {
            "lan": dict(config["lan"]),
            "auth": {
                "required": config["auth"]["required"],
                "username": config["auth"]["username"],
            },
            "authed": loopback
            or not config["auth"]["required"]
            or valid_session(request.cookies.get(SESSION_COOKIE)),
            "loopback": loopback,
        }

    @app.get("/api/v1/server/interfaces")
    async def server_interfaces() -> dict[str, Any]:
        return {"interfaces": lan_interfaces()}

    @app.put("/api/v1/server/config")
    async def put_server_config(payload: dict[str, Any]) -> dict[str, Any]:
        config = app.state.server_config
        lan = payload.get("lan")
        auth = payload.get("auth")
        if not isinstance(lan, dict) or not isinstance(auth, dict):
            raise UsageError("lan 和 auth 必须是对象")
        enabled = lan.get("enabled")
        bind_address = lan.get("bind_address")
        if not isinstance(enabled, bool) or not isinstance(bind_address, str):
            raise UsageError("lan.enabled 必须是布尔值，lan.bind_address 必须是字符串")
        if bind_address and bind_address != "0.0.0.0":
            addresses = {item["address"] for item in lan_interfaces()}
            if bind_address not in addresses:
                raise UsageError("lan.bind_address 必须是本机可用的非回环接口地址")
        required = auth.get("required")
        username = auth.get("username")
        if not isinstance(required, bool) or not isinstance(username, str):
            raise UsageError("auth.required 必须是布尔值，auth.username 必须是字符串")
        if required and not username.strip():
            raise UsageError("开启认证时必须设置用户名")
        password = auth.get("password")
        if password is not None and not isinstance(password, str):
            raise UsageError("auth.password 必须是字符串")
        if required and not password and read_lan_password() is None:
            raise UsageError("开启认证时必须设置密码")
        if password:
            save_lan_password(password)
        if not enabled:
            app.state.sessions.clear()
        config["lan"]["enabled"] = enabled
        config["lan"]["bind_address"] = bind_address
        config["auth"]["required"] = required
        config["auth"]["username"] = username.strip()
        save_server_config(config)
        warning = (
            "同网段设备拥有完整项目和 LLM 操作权限" if enabled and not required else ""
        )
        return {"saved": True, "warning": warning}

    @app.post("/api/v1/auth/login")
    async def auth_login(
        request: Request, payload: dict[str, Any]
    ) -> dict[str, Any]:
        config = app.state.server_config
        username = payload.get("username")
        password = payload.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise UsageError("用户名和密码必须是字符串")
        stored = read_lan_password()
        if not config["auth"]["required"]:
            raise UsageError("当前未开启认证")
        if username != config["auth"]["username"] or not stored:
            raise InvalidCredentialsError("用户名或密码错误")
        if not hmac.compare_digest(password.encode(), stored.encode()):
            raise InvalidCredentialsError("用户名或密码错误")
        token = secrets.token_urlsafe(32)
        app.state.sessions[token] = time.time() + _SESSION_TTL_SECONDS
        response = JSONResponse({"ok": True})
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=_SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

    @app.post("/api/v1/auth/logout")
    async def auth_logout(request: Request) -> dict[str, bool]:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            app.state.sessions.pop(token, None)
        return {"logged_out": True}

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
    async def overview(name: str, request: Request) -> dict[str, Any]:
        params = request.query_params
        try:
            offset = int(params.get("offset", "0"))
            limit = int(params.get("limit", "100"))
        except ValueError as exc:
            raise UsageError("Segment 窗口参数必须是整数") from exc
        return WebStore(project(name)).overview(
            offset=offset,
            limit=limit,
            file_id=params.get("file_id") or None,
            status=params.get("status") or None,
            search=params.get("q") or None,
            stage=params.get("stage", "translation"),
        )

    @app.get("/api/v1/projects/{name}/segments/ids")
    async def segment_index(name: str, request: Request) -> dict[str, Any]:
        params = request.query_params
        return WebStore(project(name)).segment_index(
            file_id=params.get("file_id") or None,
            status=params.get("status") or None,
            search=params.get("q") or None,
            stage=params.get("stage", "translation"),
        )

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

    @app.get("/api/v1/projects/{name}/terms")
    async def terms(name: str) -> dict[str, Any]:
        return WebStore(project(name)).terms()

    @app.get("/api/v1/projects/{name}/terms/hits")
    async def term_hits(name: str, request: Request) -> dict[str, Any]:
        params = request.query_params
        normalized = params.get("normalized")
        if not normalized:
            raise UsageError("术语命中查询必须提供 normalized")
        try:
            offset = int(params.get("offset", "0"))
            limit = int(params.get("limit", "50"))
        except ValueError as exc:
            raise UsageError("术语命中窗口参数必须是整数") from exc
        return WebStore(project(name)).term_hits(
            normalized, offset=offset, limit=limit
        )

    @app.get("/api/v1/projects/{name}/terms/related")
    async def related_terms(name: str, request: Request) -> dict[str, Any]:
        params = request.query_params
        normalized = params.get("normalized")
        if not normalized:
            raise UsageError("术语推荐查询必须提供 normalized")
        try:
            limit = int(params.get("limit", "20"))
        except ValueError as exc:
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
        for stage in LLM_STAGES:
            resolve_project_config(
                config, stage=stage, presets_root=app_root
            )
        with project_write_lock(root):
            atomic_write_text(root / "config.toml", content)
        return {"saved": True}

    @app.get("/api/v1/projects/{name}/prompts/{stage}")
    async def get_prompt(name: str, stage: str, language: str = "zh-CN") -> dict[str, Any]:
        if stage not in LLM_STAGES:
            raise UsageError(f"未知 Prompt 阶段：{stage}")
        validate_language(language)
        root = project(name)
        return prompt_view(
            stage,
            language,
            lambda value: root / "prompts" / prompt_file(stage, value),
            prompt_languages_for(root)[stage],
        )

    @app.put("/api/v1/projects/{name}/prompts/{stage}")
    async def put_prompt(
        name: str, stage: str, payload: dict[str, Any]
    ) -> dict[str, bool]:
        if stage not in LLM_STAGES:
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

    @app.get("/api/v1/global/adapters")
    async def list_global_adapters() -> dict[str, Any]:
        adapters = []
        paths: dict[str, Path] = {}
        for root in (user_root(), app_root):
            for path in sorted((root / "llm_adapters").glob("*.json")):
                paths.setdefault(path.stem, path)
        for path in sorted(paths.values(), key=lambda value: value.stem):
            try:
                adapter = load_json_adapter(path)
                adapters.append(
                    {
                        "adapter_id": adapter.adapter_id,
                        "valid": True,
                        "digest": adapter.digest,
                    }
                )
            except AppError as exc:
                adapters.append(
                    {
                        "adapter_id": path.stem,
                        "valid": False,
                        "error": str(exc),
                        "error_code": exc.code,
                    }
                )
        return {"adapters": adapters}

    @app.get("/api/v1/global/adapters/{adapter_id}")
    async def get_global_adapter(adapter_id: str) -> dict[str, Any]:
        adapter = load_json_adapter(
            effective_path(
                f"llm_adapters/{adapter_id}.json", builtin_root=app_root
            )
        )
        return adapter.definition

    @app.put("/api/v1/global/adapters/{adapter_id}")
    async def put_global_adapter(
        adapter_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if payload.get("adapter_id") != adapter_id:
            raise UsageError("URL 与 Adapter ID 不一致")
        adapters_dir = user_root() / "llm_adapters"
        adapters_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=adapters_dir,
            prefix=".adapter.",
            suffix=".json",
            delete=False,
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            temporary = Path(handle.name)
        try:
            adapter = load_json_adapter(temporary)
            atomic_write_json(write_user(f"llm_adapters/{adapter_id}.json"), payload)
        finally:
            temporary.unlink(missing_ok=True)
        return {"saved": True, "digest": adapter.digest}

    @app.get("/api/v1/global/adapters/{adapter_id}/preview")
    async def adapter_preview(adapter_id: str) -> dict[str, Any]:
        adapter = load_json_adapter(
            effective_path(
                f"llm_adapters/{adapter_id}.json", builtin_root=app_root
            )
        )
        headers, body = adapter.build_request(
            api_key="***",
            model="model",
            messages=[{"role": "user", "content": "…"}],
            temperature=0.2,
            max_output_tokens=4096,
            stream=False,
        )
        return {"headers": headers, "body": body}

    @app.post("/api/v1/projects/{name}/tasks")
    async def start_task(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        stage = str(payload.get("stage", ""))

        def boolean_option(key: str) -> bool:
            value = payload.get(key, False)
            if not isinstance(value, bool):
                raise UsageError(f"{key} 必须是布尔值")
            return value

        force = boolean_option("force")
        reuse_mixed_fingerprints = boolean_option(
            "reuse_mixed_fingerprints"
        )
        run_action = payload.get("run_action")
        if run_action is not None and not isinstance(run_action, str):
            raise UsageError("run_action 必须是字符串或 null")
        scope = Scope(
            from_file=payload.get("from_file"),
            only_file=payload.get("only_file"),
            only_segment=payload.get("only_segment"),
            force=force,
            dry_run=False,
        )
        scope.validate()
        return await app.state.tasks.start(
            project(name),
            stage,
            scope=scope,
            reuse_mixed_fingerprints=reuse_mixed_fingerprints,
            run_action=run_action,
            prompt_language=(
                validate_language(payload.get("language"))
                if "language" in payload
                else None
            ),
        )

    @app.get("/api/v1/projects/{name}/task-options/{stage}")
    async def get_task_options(name: str, stage: str) -> dict[str, Any]:
        return task_options(project(name), stage)

    @app.get("/api/v1/tasks/{task_id}")
    async def task(task_id: str) -> dict[str, Any]:
        return app.state.tasks.get(task_id)

    @app.post("/api/v1/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str) -> dict[str, Any]:
        return await app.state.tasks.cancel(task_id)

    @app.get("/api/v1/diagnostics")
    async def diagnostics(
        level: str | None = None,
        project: str | None = None,
        stage: str | None = None,
        q: str | None = None,
        request_session: str | None = None,
        request_after: int | None = None,
    ) -> dict[str, Any]:
        try:
            return app.state.diagnostics.snapshot(
                level=level or None,
                project=project or None,
                stage=stage or None,
                query=q or None,
                request_session=request_session or None,
                request_after=request_after,
            )
        except ValueError as exc:
            raise UsageError(str(exc)) from exc

    @app.get("/api/v1/diagnostics/requests/{request_id}")
    async def diagnostic_request(request_id: str) -> dict[str, Any]:
        try:
            return app.state.diagnostics.request_detail(request_id)
        except ValueError as exc:
            raise UsageError(str(exc)) from exc

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

    @app.get("/api/v1/projects/{name}/exports/download")
    async def download_export(name: str, file: str) -> Response:
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

    if web_dist.is_dir():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")
    else:
        get_logger().warning(
            "未找到前端构建产物 %s；请先执行 npm run build --prefix web", web_dist
        )
    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.web",
        description="启动本地 Web 翻译工作台",
    )
    parser.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    uvicorn.run(
        "app.web:create_app",
        factory=True,
        host="0.0.0.0",
        port=args.port,
    )


if __name__ == "__main__":
    main()
