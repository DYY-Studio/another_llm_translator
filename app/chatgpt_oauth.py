"""Local ChatGPT plan authorization and renewable sessions."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import re
import secrets
import time
import uuid
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
import keyring

from .errors import ConfigError, ExternalError, FatalExternalError, UsageError
from .locking import project_write_lock
from .sqlite_storage import atomic_write_json
from .user_config import user_root

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
USAGE_URL = "https://chatgpt.com/#settings/Usage"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke " + PLAN_SCOPE
_TERMINAL_REFRESH = {"invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired", "refresh_token_invalidated", "refresh_token_reused"}
_UNSUPPORTED_BODY = {"background", "conversation", "max_output_tokens", "max_tool_calls", "metadata", "moderation", "multi_agent", "prompt", "prompt_cache_retention", "safety_identifier", "temperature", "top_logprobs", "top_p", "truncation", "user", "previous_response_id"}


def validate_plan_body(body: dict[str, Any]) -> None:
    unsupported = set(body) & _UNSUPPORTED_BODY
    if unsupported:
        raise ConfigError("ChatGPT Plan 不支持请求字段：" + ", ".join(sorted(unsupported)))
    if body.get("store") is not False or body.get("stream") is not True:
        raise ConfigError("ChatGPT Plan 要求 store=false、stream=true")
    if not isinstance(body.get("input"), list) or any(isinstance(item, dict) and item.get("role") == "system" for item in body["input"]):
        raise ConfigError("ChatGPT Plan 要求数组 input 和 developer 指令")


def plan_error(data: Any, status: int, request_id: str) -> FatalExternalError | None:
    if not isinstance(data, dict):
        return None
    detail = data.get("error", data)
    if isinstance(data.get("response"), dict):
        detail = data["response"].get("error", detail)
    if not isinstance(detail, dict):
        return None
    code = detail.get("code")
    messages = {
        "subscription_sharing_usage_limit_exceeded": "ChatGPT Plan 用量已达限制，请打开 ChatGPT 设置中的管理用量",
        "subscription_sharing_user_not_eligible": "当前 ChatGPT 账户不符合 Plan 使用条件",
        "subscription_sharing_unsupported_capability": "ChatGPT Plan 不支持当前请求能力，请检查参数",
        "subscription_sharing_route_not_supported": "ChatGPT Plan 不支持当前请求端点",
        "subscription_sharing_invalid_user": "ChatGPT 授权无效，请在本机设置页检查连接并重新登录",
        "chatpass_v2_scope_not_authorized": "ChatGPT Plan 权限不足，请重新授权",
        "chatpass_v2_invalid_authorization_context": "ChatGPT Plan 授权上下文无效，请检查连接",
    }
    if not isinstance(code, str) or code not in messages:
        return None
    error = FatalExternalError(messages[code])
    error.params = {"provider_code": code, "http_status": status, "request_id": request_id, "usage_url": USAGE_URL}
    return error


def parse_usage_headers(headers: httpx.Headers) -> dict[str, Any]:
    """Read server-provided metered windows; never retain arbitrary headers."""
    buckets: dict[str, Any] = {}
    errors: list[str] = []
    for name, raw in headers.items():
        match = re.fullmatch(r"x-([a-z0-9-]+)-(primary|secondary)-used-percent", name)
        if not match:
            continue
        limit, window = match.groups()
        prefix = f"x-{limit}-{window}"
        try:
            used = float(raw)
            if not math.isfinite(used) or not 0 <= used <= 100:
                raise ValueError()
            minutes = headers.get(prefix + "-window-minutes")
            reset = headers.get(prefix + "-reset-at")
            value = {"used_percent": used, "window_minutes": int(minutes) if minutes is not None else None,
                     "reset_at": int(reset) if reset is not None else None}
            if value["window_minutes"] is not None and value["window_minutes"] <= 0:
                raise ValueError()
            if value["reset_at"] is not None and value["reset_at"] < 0:
                raise ValueError()
            buckets.setdefault(limit, {})[window] = value
        except ValueError:
            errors.append(f"Invalid usage header: {prefix}")
    return {"buckets": buckets, "errors": errors}


class ChatGPTConnection:
    def __init__(self) -> None:
        self.root = user_root() / "chatgpt"
        self.path = self.root / "connection.json"
        self.service = "another-llm-translator-chatgpt-" + hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()[:16]
        self.task: asyncio.Task[None] | None = None
        self._login_lock = asyncio.Lock()
        self.error = ""
        self._metadata: dict[str, Any] | None = None

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"host_id": "", "proxy_url": "", "active": None, "registrations": {}, "welcome_seen": False}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError("无法读取 ChatGPT 连接设置") from exc
        if not isinstance(value, dict) or not isinstance(value.get("registrations"), dict):
            raise ConfigError("ChatGPT 连接设置格式无效")
        return value

    @asynccontextmanager
    async def locked(self) -> AsyncIterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 35
        while True:
            lock = project_write_lock(self.root)
            try:
                lock.__enter__()
                break
            except UsageError:
                if time.monotonic() >= deadline:
                    raise UsageError("ChatGPT 连接正在被另一进程使用，请稍后重试") from None
                await asyncio.sleep(0.05)
        try:
            yield
        finally:
            lock.__exit__(None, None, None)

    def tokens(self, client_id: str) -> dict[str, Any] | None:
        try:
            raw = keyring.get_password(self.service, client_id)
        except keyring.errors.KeyringError as exc:
            raise ConfigError("无法读取 ChatGPT 系统钥匙串凭据") from exc
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise ConfigError("ChatGPT 系统钥匙串凭据格式无效") from exc
        if not isinstance(value, dict):
            raise ConfigError("ChatGPT 系统钥匙串凭据格式无效")
        return value

    def save_tokens(self, client_id: str, tokens: dict[str, Any]) -> None:
        try:
            keyring.set_password(self.service, client_id, json.dumps(tokens))
        except keyring.errors.KeyringError as exc:
            raise ConfigError("无法保存 ChatGPT 系统钥匙串凭据") from exc

    def clear_tokens(self, client_id: str) -> None:
        if self.tokens(client_id) is None:
            return
        try:
            keyring.delete_password(self.service, client_id)
        except keyring.errors.KeyringError as exc:
            raise ConfigError("无法删除 ChatGPT 系统钥匙串凭据") from exc

    def summary(self) -> dict[str, Any]:
        state = self.read()
        active = state["active"]
        registration = state["registrations"].get(active, {})
        tokens = self.tokens(active) if active else None
        return {
            "email": registration.get("email", ""),
            "connected": tokens is not None,
            "can_switch_account": not active and bool(state["registrations"]),
            "plan_enabled": bool(tokens and PLAN_SCOPE in tokens["scopes"] and "resource.invoke" in tokens["scopes"]),
            "proxy_url": state["proxy_url"],
            "pending": self.task is not None and not self.task.done(),
            "error": self.error,
            "welcome_required": bool(tokens and PLAN_SCOPE in tokens["scopes"] and not state["welcome_seen"]),
            "usage_url": USAGE_URL,
        }

    def identity(self) -> tuple[str, str]:
        state = self.read()
        active = state["active"]
        if not active:
            raise FatalExternalError("请在本机设置页登录 ChatGPT 并授权使用 Plan")
        return active, state["registrations"][active]["session"]

    async def configure(self, proxy_url: str) -> None:
        if not isinstance(proxy_url, str):
            raise UsageError("ChatGPT 代理地址必须是字符串")
        if proxy_url:
            try:
                parsed = urlsplit(proxy_url)
                parsed.port
            except ValueError:
                raise UsageError("ChatGPT 代理地址无效") from None
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise UsageError("ChatGPT 代理必须是不含凭据的 HTTP/HTTPS 地址")
        if self.summary()["pending"]:
            raise UsageError("请先结束当前 ChatGPT 登录")
        async with self.locked():
            state = self.read()
            state["proxy_url"] = proxy_url
            atomic_write_json(self.path, state)
            self._metadata = None

    async def dismiss_welcome(self) -> None:
        async with self.locked():
            state = self.read()
            state["welcome_seen"] = True
            atomic_write_json(self.path, state)

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            async with httpx.AsyncClient(proxy=self.read()["proxy_url"] or None, timeout=30, trust_env=False) as client:
                return await client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            # Exception strings can contain URLs or credential-bearing headers.
            error = ExternalError("ChatGPT 网络请求失败，请检查连接代理")
            error.params = {"retryable": True}
            raise error from exc

    def usage_snapshot(self) -> dict[str, Any]:
        path = self.root / "usage-experiment.json"
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError("无法读取实验性 ChatGPT 用量快照") from exc
        state = self.read()
        active = state["active"]
        identity = [active, state["registrations"].get(active, {}).get("session")]
        return value.get("sources", {}) if value.get("identity") == identity else {}

    async def record_usage(self, identity: tuple[str, str], source: str, value: dict[str, Any]) -> None:
        async with self.locked():
            state = self.read()
            active = state["active"]
            if (active, state["registrations"].get(active, {}).get("session")) != identity:
                return
            sources = self.usage_snapshot()
            sources[source] = {"observed_at": time.time(), **value}
            atomic_write_json(self.root / "usage-experiment.json", {"identity": list(identity), "sources": sources})

    async def query_usage(self) -> dict[str, Any]:
        identity = self.identity()
        token = await self.access_token(identity)
        response = await self.request("GET", "https://chatgpt.com/backend-api/wham/usage",
                                      headers={"Authorization": "Bearer " + token})
        # Do not expose response bodies, account IDs or credentials in diagnostics.
        value: dict[str, Any] = {"http_status": response.status_code,
                                 "request_id": response.headers.get("x-request-id", ""),
                                 "buckets": {}, "errors": []}
        if response.status_code != 200:
            value["errors"] = [f"Usage query failed: HTTP {response.status_code}"]
        else:
            try:
                data = response.json()
                limits = [("codex", data.get("rate_limit"))]
                limits.extend((item["metered_feature"], item.get("rate_limit")) for item in data.get("additional_rate_limits", []) or [])
                for name, details in limits:
                    if not details:
                        continue
                    windows = {}
                    for key in ("primary", "secondary"):
                        window = details.get(key + "_window")
                        if window is None:
                            continue
                        used = float(window["used_percent"])
                        seconds = int(window["limit_window_seconds"])
                        reset = int(window["reset_at"])
                        if not math.isfinite(used) or not 0 <= used <= 100 or seconds <= 0 or reset < 0:
                            raise ValueError()
                        windows[key] = {"used_percent": used, "window_minutes": seconds / 60, "reset_at": reset}
                    if windows:
                        value["buckets"][name] = windows
                if not value["buckets"]:
                    value["errors"] = ["Usage response contains no supported windows"]
            except (ValueError, KeyError, TypeError, AttributeError):
                value["buckets"] = {}
                value["errors"] = ["Usage response format is not supported"]
        await self.record_usage(identity, "endpoint", value)
        return self.usage_snapshot()

    async def metadata(self) -> dict[str, Any]:
        if self._metadata is None:
            response = await self.request("GET", ISSUER + "/.well-known/openid-configuration")
            value = self.response_json(response)
            if value.get("issuer") != ISSUER:
                raise ExternalError("ChatGPT OIDC issuer 无效")
            for field in ("jwks_uri", "revocation_endpoint"):
                parsed = urlsplit(str(value.get(field, "")))
                if parsed.scheme != "https" or parsed.hostname != "auth.openai.com" or parsed.username or parsed.password:
                    raise ExternalError("ChatGPT OIDC endpoint 无效")
            self._metadata = value
        return self._metadata

    @staticmethod
    def response_json(response: httpx.Response) -> dict[str, Any]:
        try:
            value = response.json()
        except ValueError as exc:
            raise ExternalError(f"ChatGPT 响应无效：HTTP {response.status_code}") from exc
        if not isinstance(value, dict):
            raise ExternalError("ChatGPT 响应不是对象")
        if response.status_code != 200:
            # Only a short protocol error identifier is safe to include.
            code = value.get("error")
            code = code if isinstance(code, str) and code.isascii() and all(c.isalnum() or c == "_" for c in code) and len(code) < 100 else "oauth_error"
            error = ExternalError(f"ChatGPT OAuth 请求失败：HTTP {response.status_code} ({code})")
            error.params = {"oauth_code": code, "retryable": response.status_code == 429 or response.status_code >= 500}
            raise error
        return value

    @staticmethod
    def token_record(value: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
        for field in ("access_token", "refresh_token"):
            if not isinstance(value.get(field), str) or not value[field]:
                raise ExternalError(f"ChatGPT 响应缺少 {field}")
        if str(value.get("token_type", "")).lower() != "bearer" or not isinstance(value.get("expires_in"), int) or value["expires_in"] <= 0:
            raise ExternalError("ChatGPT 令牌类型或有效期无效")
        scope = value.get("scope")
        if scope is not None and not isinstance(scope, str):
            raise ExternalError("ChatGPT 授权 scope 无效")
        if scope is None and previous is None:
            raise ExternalError("ChatGPT 响应缺少授权 scope")
        return {
            "access_token": value["access_token"], "refresh_token": value["refresh_token"],
            "id_token": value.get("id_token") or (previous or {}).get("id_token"),
            "scopes": scope.split() if scope is not None else previous["scopes"],
            "expires_at": time.time() + value["expires_in"],
        }

    async def complete_login(self, code: str, client_id: str, nonce: str, verifier: str, redirect_uri: str, expected_client: str | None) -> None:
        if not client_id or client_id == "dynamic_agent_client" or (expected_client and client_id != expected_client):
            raise ExternalError("ChatGPT 登录返回的 client ID 不匹配")
        async with self.locked():
            state = self.read()
            if state["active"] != expected_client:
                raise ExternalError("ChatGPT 连接已变更，请重新登录")
            value = self.response_json(await self.request("POST", ISSUER + "/api/accounts/oauth/token", data={"grant_type": "authorization_code", "client_id": client_id, "code": code, "code_verifier": verifier, "redirect_uri": redirect_uri, "resource": RESOURCE}))
            metadata = await self.metadata()
            jwks = self.response_json(await self.request("GET", metadata["jwks_uri"]))
            try:
                header = jwt.get_unverified_header(value["id_token"])
                keys = [key for key in jwks["keys"] if key.get("kid") == header.get("kid")]
                if len(keys) != 1:
                    raise ValueError("unknown signing key")
                key = jwt.PyJWK.from_dict(keys[0])
                if header.get("alg") not in {"RS256", "ES256"} or key.algorithm_name != header["alg"]:
                    raise ValueError("invalid signing algorithm")
                identity = jwt.decode(value["id_token"], key.key, algorithms=[header["alg"]], audience=client_id, issuer=ISSUER, options={"require": ["exp", "iss", "aud", "sub", "nonce"]})
                if not isinstance(identity["sub"], str) or not identity["sub"] or not hmac.compare_digest(identity["nonce"], nonce):
                    raise ValueError("invalid identity")
                registration = state["registrations"].get(client_id)
                if registration and registration["subject"] != identity["sub"]:
                    raise ValueError("account changed")
            except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
                raise ExternalError("ChatGPT 身份验证失败，未替换现有连接") from exc
            tokens = self.token_record(value)
            if not isinstance(tokens["id_token"], str):
                raise ExternalError("ChatGPT 响应缺少 ID Token")
            self.save_tokens(client_id, tokens)
            state["registrations"][client_id] = {"subject": identity["sub"], "email": identity.get("email", ""), "session": uuid.uuid4().hex}
            state["active"] = client_id
            atomic_write_json(self.path, state)

    async def access_token(self, expected: tuple[str, str]) -> str:
        async with self.locked():
            if self.identity() != expected:
                raise FatalExternalError("ChatGPT 连接已变更，请重新启动任务")
            tokens = self.tokens(expected[0])
            if not tokens:
                raise FatalExternalError("请在本机设置页重新登录 ChatGPT")
            if not {PLAN_SCOPE, "resource.invoke"}.issubset(tokens["scopes"]):
                raise FatalExternalError("ChatGPT 尚未授权使用 Plan，请在本机设置页重新授权")
            if tokens["expires_at"] <= time.time() + 60:
                try:
                    value = self.response_json(await self.request("POST", ISSUER + "/api/accounts/oauth/token", data={"grant_type": "refresh_token", "client_id": expected[0], "refresh_token": tokens["refresh_token"], "resource": RESOURCE}))
                except ExternalError as exc:
                    if exc.params.get("oauth_code") in _TERMINAL_REFRESH:
                        self.clear_tokens(expected[0])
                        raise FatalExternalError("ChatGPT 会话已失效，请在本机设置页重新登录") from exc
                    raise
                tokens = self.token_record(value, tokens)
                self.save_tokens(expected[0], tokens)
                if not {PLAN_SCOPE, "resource.invoke"}.issubset(tokens["scopes"]):
                    raise FatalExternalError("ChatGPT Plan 授权已失效，请重新授权")
            return tokens["access_token"]

    async def logout(self) -> str:
        await self.cancel()
        warning = ""
        async with self.locked():
            state = self.read()
            active = state["active"]
            tokens = self.tokens(active) if active else None
            if tokens:
                try:
                    endpoint = (await self.metadata())["revocation_endpoint"]
                    for attempt in range(2):
                        response = await self.request("POST", endpoint, data={"token": tokens["refresh_token"], "token_type_hint": "refresh_token", "client_id": active})
                        if response.status_code < 500:
                            break
                        await asyncio.sleep(0.2)
                    if response.status_code != 200:
                        raise ExternalError("revocation failed")
                except ExternalError:
                    warning = "已在本机退出；未确认远端撤销，请到 ChatGPT 设置中断开连接"
                self.clear_tokens(active)
            if active:
                state["last_client"] = active
            state["active"] = None
            atomic_write_json(self.path, state)
        self.error = warning
        return warning

    async def login(self, *, new_account: bool = False) -> None:
        if self._login_lock.locked() or self.summary()["pending"]:
            raise UsageError("ChatGPT 登录正在进行")
        async with self._login_lock:
            await self._login(new_account=new_account)

    async def _login(self, *, new_account: bool) -> None:
        self.error = ""
        async with self.locked():
            state = self.read()
            if new_account and state["active"]:
                raise UsageError("请先退出当前 ChatGPT 连接，再更换账户")
            if not new_account and not state["active"] and state.get("last_client"):
                state["active"] = state["last_client"]
                atomic_write_json(self.path, state)
            if not state["host_id"]:
                state["host_id"] = "urn:uuid:" + str(uuid.uuid4())
                atomic_write_json(self.path, state)
        future: asyncio.Future[dict[str, str]] = asyncio.get_running_loop().create_future()
        verifier, nonce, oauth_state = (secrets.token_urlsafe(32) for _ in range(3))
        server = await asyncio.start_server(lambda r, w: self.callback(r, w, future, oauth_state), "127.0.0.1", 0, limit=8192)
        redirect = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/auth/callback"
        params = {"client_id": state["active"] or "dynamic_agent_client", "ext_agent_host_id": state["host_id"], "redirect_uri": redirect, "response_type": "code", "scope": SCOPES, "resource": RESOURCE, "state": oauth_state, "nonce": nonce, "code_challenge_method": "S256", "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")}
        if state["active"]:
            registration = state["registrations"][state["active"]]
            params["login_hint"] = registration["email"]
            if not self.summary()["plan_enabled"]:
                params["prompt"] = "consent"
        else:
            params["agent_name_hint"] = "Another LLM Translator"
        self.task = asyncio.create_task(self.finish_login(server, future, state["active"], nonce, verifier, redirect))
        try:
            opened = await asyncio.to_thread(webbrowser.open, ISSUER + "/api/accounts/authorize?" + urlencode(params))
        except Exception:
            opened = False
        if not opened:
            await self.cancel()
            raise UsageError("无法打开系统浏览器，请检查本机浏览器配置")

    async def finish_login(self, server: asyncio.Server, future: asyncio.Future[dict[str, str]], expected: str | None, nonce: str, verifier: str, redirect: str) -> None:
        try:
            result = await asyncio.wait_for(future, timeout=300)
            if "error" in result:
                raise ExternalError("ChatGPT 登录被拒绝或取消")
            await self.complete_login(result["code"], result.get("client_id") or expected or "", nonce, verifier, redirect, expected)
        except TimeoutError:
            self.error = "ChatGPT 登录超时，请重新登录"
        except (ExternalError, ConfigError, UsageError) as exc:
            self.error = str(exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.error = "ChatGPT 登录失败，请重新登录"
        finally:
            server.close()
            await server.wait_closed()

    async def callback(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, future: asyncio.Future[dict[str, str]], oauth_state: str) -> None:
        status = "400 Bad Request"
        try:
            headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
            method, target, _ = headers.split(b"\r\n", 1)[0].decode("ascii").split(" ")
            url = urlsplit(target)
            query = parse_qs(url.query, strict_parsing=True)
            values = {key: value[0] for key, value in query.items() if len(value) == 1}
            if method != "GET" or url.path != "/auth/callback" or future.done() or not hmac.compare_digest(values.get("state", ""), oauth_state):
                raise ValueError("invalid callback")
            if not values.get("code") and not values.get("error"):
                raise ValueError("missing result")
            future.set_result(values)
            status = "200 OK"
        except (ValueError, UnicodeError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        body = "授权回调已收到，请返回应用查看结果。" if status == "200 OK" else "授权回调无效，请返回应用重新登录。"
        data = body.encode("utf-8")
        writer.write(f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\nCache-Control: no-store\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data)
        try:
            await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()

    async def cancel(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None
