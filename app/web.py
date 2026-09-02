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

WEB_DIST = (
    Path(__file__).with_name("web_dist")
    if Path(__file__).with_name("web_dist").is_dir()
    else Path(sys.prefix) / "app" / "web_dist"
)
SESSION_COOKIE = "another_llm_session"
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


@dataclass
class ReplacementPreviewSession:
    preview_id: str
    plan: FileReplacementPlan

    @property
    def temporary_root(self) -> Path:
        return self.plan.temporary_root

    def cleanup(self) -> None:
        self.plan.cleanup()


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
    migrate_llm_resources()
    try:
        projects_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"无法创建项目目录：{projects_root}: {exc}") from exc
    app = FastAPI(title="Another LLM Translator", version="1")
    app.state.projects_root = projects_root
    app.state.app_root = app_root
    app.state.diagnostics = DiagnosticsHub(
        log_path or user_root() / "logs" / "app.log"
    )
    app.state.external_projects = set()
    app.state.server_config = server_config or load_server_config()
    tasks_config = app.state.server_config.get("tasks", {})
    app.state.tasks = WebTaskManager(
        app.state.diagnostics,
        max_active_projects=tasks_config.get("max_active_projects", 2),
    )
    app.state.sessions: dict[str, float] = {}
    app.state.replacement_previews: dict[tuple[Path, str], ReplacementPreviewSession] = {}

    async def cleanup_replacement_previews() -> None:
        for session in app.state.replacement_previews.values():
            session.cleanup()
        app.state.replacement_previews.clear()

    app.router.add_event_handler("shutdown", cleanup_replacement_previews)
    app.router.add_event_handler("shutdown", app.state.tasks.shutdown)

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
                    {"error": "只允许本机访问", "code": "local_only", "params": {}},
                    status_code=403,
                )
            client_ip = request.client.host if request.client else ""
            if not _client_allowed_on_bind(
                client_ip, config["lan"]["bind_address"]
            ):
                return JSONResponse(
                    {
                        "error": "客户端不在允许的网段内",
                        "code": "out_of_subnet",
                        "params": {},
                    },
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
                    {"error": "需要登录", "code": "auth_required", "params": {}},
                    status_code=401,
                )
            return await call_next(request)
        host = request.headers.get("host", "").split(":", 1)[0]
        if host not in {"127.0.0.1", "localhost", "testserver", "testclient"}:
            return JSONResponse(
                {"error": "只允许本机访问", "code": "local_only", "params": {}},
                status_code=403,
            )
        origin = request.headers.get("origin")
        if origin:
            origin_host = origin.split("://", 1)[-1].split("/", 1)[0]
            origin_host = origin_host.split(":", 1)[0]
            if origin_host not in {"127.0.0.1", "localhost", "testserver"}:
                return JSONResponse(
                    {
                        "error": "不允许的请求来源",
                        "code": "invalid_origin",
                        "params": {},
                    },
                    status_code=403,
                )
        return await call_next(request)

    @app.exception_handler(AppError)
    async def app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(app_error_payload(exc), status_code=400)

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

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        get_logger("web").exception(
            "unexpected request error method=%s path=%s",
            request.method,
            request.url.path,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return JSONResponse(internal_error_payload(), status_code=500)

    def welcome_seen() -> bool:
        return (user_root() / ".welcome-seen").is_file()

    def mark_welcome_seen() -> None:
        (user_root() / ".welcome-seen").write_text("1", encoding="utf-8")



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
                stream=bool(preset.definition["stream"]),
                extra_body=preset.definition["extra_body"],
            )
            return preset
        finally:
            temporary.unlink(missing_ok=True)



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








    def preset_file(preset_id: str) -> Path:
        preset_path(app_root, preset_id)
        return effective_path(f"llm_presets/{preset_id}.json", builtin_root=app_root)












































































    from .web_term_routes import register_term_routes
    register_term_routes(app=app, projects_root=projects_root, app_root=app_root)
    from .web_resource_routes import register_resource_routes
    register_resource_routes(app=app, projects_root=projects_root, app_root=app_root)
    from .web_segment_routes import register_segment_routes
    register_segment_routes(app=app, projects_root=projects_root, app_root=app_root)
    from .web_export_routes import register_export_routes
    register_export_routes(app=app, projects_root=projects_root, app_root=app_root)
    from .web_task_routes import register_task_routes
    register_task_routes(app=app, projects_root=projects_root, app_root=app_root)
    from .web_project_routes import register_project_routes
    register_project_routes(app=app, projects_root=projects_root, app_root=app_root)

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
