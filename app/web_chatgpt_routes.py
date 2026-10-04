from __future__ import annotations

from fastapi import FastAPI, Request

from .chatgpt_oauth import ChatGPTConnection
from .errors import UsageError


def register_chatgpt_routes(app: FastAPI) -> None:
    connection = ChatGPTConnection()
    app.state.chatgpt = connection
    app.router.add_event_handler("shutdown", connection.cancel)

    def local(request: Request) -> None:
        if not request.client or request.client.host not in {"127.0.0.1", "::1", "testclient", "testserver"}:
            raise UsageError("ChatGPT 连接管理只能在主机本地进行")

    @app.get("/api/v1/chatgpt/connection")
    async def status(request: Request) -> dict:
        value = connection.summary()
        value["local"] = bool(request.client and request.client.host in {"127.0.0.1", "::1", "testclient", "testserver"})
        return value

    @app.put("/api/v1/chatgpt/connection")
    async def configure(request: Request, payload: dict) -> dict:
        local(request)
        if set(payload) != {"proxy_url"}:
            raise UsageError("ChatGPT 连接设置只接受 proxy_url")
        await connection.configure(payload["proxy_url"])
        return connection.summary()

    @app.post("/api/v1/chatgpt/connection/{action}")
    async def operate(action: str, request: Request) -> dict:
        local(request)
        operations = {"login": connection.login, "cancel": connection.cancel, "logout": connection.logout, "welcome-dismiss": connection.dismiss_welcome}
        if action == "login-new":
            await connection.login(new_account=True)
            return connection.summary()
        if action not in operations:
            raise UsageError("未知 ChatGPT 连接操作")
        await operations[action]()
        return connection.summary()
