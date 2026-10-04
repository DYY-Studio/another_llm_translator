from __future__ import annotations

import time
from pathlib import Path
from typing import Iterable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .chatgpt_oauth import ChatGPTConnection
from .errors import UsageError, app_error_payload


class ChatGPTSessionRequired(UsageError):
    code = "auth_required"


def require_plan_session(request: Request) -> None:
    if request.client and request.client.host in {"127.0.0.1", "::1", "testclient", "testserver"}:
        return
    token = request.cookies.get("another_llm_session")
    if not token or request.app.state.sessions.get(token, 0) <= time.time():
        raise ChatGPTSessionRequired("使用 ChatGPT Plan 需要局域网登录；请在主机服务器设置中配置认证")


def require_project_plan_session(request: Request, root: Path, stages: Iterable[str]) -> None:
    if request.client and request.client.host in {"127.0.0.1", "::1", "testclient", "testserver"}:
        return
    from .config import LLM_MODEL_STAGES, load_project_config
    for stage in stages:
        if stage in LLM_MODEL_STAGES and load_project_config(root, stage=stage)["llm"]["credential"]["kind"] == "chatgpt":
            require_plan_session(request)
            return


def register_chatgpt_routes(app: FastAPI) -> None:
    @app.exception_handler(ChatGPTSessionRequired)
    async def session_required(_: Request, exc: ChatGPTSessionRequired) -> JSONResponse:
        return JSONResponse(app_error_payload(exc), status_code=401)

    connection = ChatGPTConnection()
    app.state.chatgpt = connection
    app.router.add_event_handler("shutdown", connection.cancel)

    def local(request: Request) -> None:
        if not request.client or request.client.host not in {"127.0.0.1", "::1", "testclient", "testserver"}:
            raise UsageError("ChatGPT 连接管理只能在主机本地进行")

    @app.get("/api/v1/chatgpt/connection")
    async def status(request: Request) -> dict:
        require_plan_session(request)
        value = connection.summary()
        value["local"] = bool(request.client and request.client.host in {"127.0.0.1", "::1", "testclient", "testserver"})
        return value

    @app.get("/api/v1/chatgpt/usage")
    async def usage(request: Request) -> dict:
        require_plan_session(request)
        return connection.usage_snapshot()

    @app.post("/api/v1/chatgpt/usage")
    async def query_usage(request: Request) -> dict:
        local(request)
        return await connection.query_usage()

    @app.put("/api/v1/chatgpt/connection")
    async def configure(request: Request, payload: dict) -> dict:
        local(request)
        if set(payload) != {"proxy_url"}:
            raise UsageError("ChatGPT 连接设置只接受 proxy_url")
        await connection.configure(payload["proxy_url"])
        return {**connection.summary(), "local": True}

    @app.post("/api/v1/chatgpt/connection/{action}")
    async def operate(action: str, request: Request) -> dict:
        local(request)
        operations = {"login": connection.login, "cancel": connection.cancel, "logout": connection.logout, "welcome-dismiss": connection.dismiss_welcome}
        if action == "login-new":
            await connection.login(new_account=True)
            return {**connection.summary(), "local": True}
        if action not in operations:
            raise UsageError("未知 ChatGPT 连接操作")
        await operations[action]()
        return {**connection.summary(), "local": True}
