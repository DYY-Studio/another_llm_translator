from __future__ import annotations

import argparse
import ipaddress
import socket
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psutil
import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .diagnostics import DiagnosticsHub
from .errors import AppError, ProjectError, UsageError, app_error_payload, internal_error_payload
from .llm_migration import migrate_llm_resources
from .logging_utils import get_logger
from .project import APP_ROOT as DEFAULT_APP_ROOT, PROJECTS_ROOT, resolve_project
from .sqlite_storage import database_path, read_json
from .server_config import load_server_config
from .user_config import user_root
from .web_tasks import WebTaskManager
from .web_project_routes import ReplacementPreviewSession

WEB_DIST = (
    Path(__file__).with_name("web_dist")
    if Path(__file__).with_name("web_dist").is_dir()
    else Path(sys.prefix) / "app" / "web_dist"
)
SESSION_COOKIE = "another_llm_session"
_SESSION_TTL_SECONDS = 30 * 24 * 3600
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

    def remember_project(path: Path) -> None:
        normalized = path.resolve()
        if normalized.parent != projects_root.resolve():
            app.state.external_projects.add(normalized)

    def project_paths() -> list[Path]:
        paths: list[Path] = []
        if projects_root.exists():
            paths.extend(
                item
                for item in sorted(projects_root.iterdir(), key=lambda value: value.name)
                if database_path(item).is_file()
            )
        paths.extend(
            path
            for path in sorted(app.state.external_projects)
            if database_path(path).is_file()
        )
        return list({path.resolve() for path in paths})

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

    async def cleanup_replacement_previews() -> None:
        for session in app.state.replacement_previews.values():
            session.cleanup()
        app.state.replacement_previews.clear()

    app.router.add_event_handler("shutdown", cleanup_replacement_previews)
    app.router.add_event_handler("shutdown", app.state.tasks.shutdown)



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









































































































    from .web_term_routes import register_term_routes
    register_term_routes(app=app, projects_root=projects_root, app_root=app_root, project=project)
    from .web_resource_routes import register_resource_routes
    register_resource_routes(app=app, projects_root=projects_root, app_root=app_root)
    from .web_segment_routes import register_segment_routes
    register_segment_routes(app=app, projects_root=projects_root, app_root=app_root, project=project)
    from .web_export_routes import register_export_routes
    register_export_routes(app=app, projects_root=projects_root, app_root=app_root, project=project)
    from .web_task_routes import register_task_routes
    register_task_routes(app=app, projects_root=projects_root, app_root=app_root, project=project)
    from .web_project_routes import register_project_routes
    register_project_routes(app=app, projects_root=projects_root, app_root=app_root, project=project, project_paths=project_paths, remember_project=remember_project)

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
