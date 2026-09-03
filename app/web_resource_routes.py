from __future__ import annotations
import ctypes
import hmac
import ipaddress
import json
import os
import secrets
import socket
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
import httpx
import psutil
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from .config import (
    LLM_MODEL_STAGES,
    dump_config,
    load_config,
    resolve_project_config,
    resolve_global_config,
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
from .errors import (
    AppError,
    ConfigError,
    ExternalError,
    InvalidCredentialsError,
    UsageError,
)
from .locking import project_write_lock
from .execution import full_prompt
from .llm_adapter import load_json_adapter
from .llm_preset import LLMPreset, endpoint_url, load_llm_preset, preset_path
from .plugins import (
    document_adapter_summaries,
    resolve_translation_validators,
)
from .project import (
    PROMPT_LANGUAGES,
    prompt_file,
)
from .prompt_library import (
    delete_prompt_library,
    list_prompt_library,
    read_prompt_library,
    save_prompt_library,
)
from .server_config import save_server_config
from .sqlite_storage import (
    atomic_write_json,
    atomic_write_text,
    database_path,
)
from .user_config import effective_path, user_root, write_user

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
    try:
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
    except OSError:
        return []
    interfaces = []
    for name, entries in addrs.items():
        if name == "lo" or name.startswith("lo") or not stats.get(name) or not stats[name].isup:
            continue
        for entry in entries:
            if entry.family == socket.AF_INET and entry.netmask:
                interfaces.append({"name": name, "address": entry.address, "netmask": entry.netmask})
                break
    return interfaces

def _client_allowed_on_bind(client_ip: str, bind_address: str) -> bool:
    if bind_address == "0.0.0.0":
        return True
    for item in lan_interfaces():
        if item["address"] != bind_address:
            continue
        try:
            network = ipaddress.ip_network(f"{item['address']}/{item['netmask']}", strict=False)
            return ipaddress.ip_address(client_ip) in network
        except ValueError:
            return False
    return False




def register_resource_routes(
    *,
    app: FastAPI,
    projects_root: Path,
    app_root: Path,
    project: Callable[[str], Path],
) -> None:
    SESSION_COOKIE = "another_llm_session"
    _SESSION_TTL_SECONDS = 30 * 24 * 3600
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

    def welcome_seen() -> bool:
        return (user_root() / ".welcome-seen").is_file()

    def mark_welcome_seen() -> None:
        (user_root() / ".welcome-seen").write_text("1", encoding="utf-8")

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
                extra_headers=preset.definition["extra_headers"],
                session_id="RUN-VALIDATION",
                request_id="REQ-VALIDATION",
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

    @app.get("/api/v1/welcome")
    async def welcome() -> dict[str, Any]:
        return {"first": not welcome_seen()}

    @app.post("/api/v1/welcome/dismiss")
    async def dismiss_welcome() -> dict[str, bool]:
        mark_welcome_seen()
        return {"ok": True}

    @app.post("/api/v1/directories")
    async def list_directories(payload: dict[str, Any]) -> dict[str, Any]:
        path = payload.get("path", "")
        if not isinstance(path, str):
            raise UsageError("目录路径必须是字符串")
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
        try:
            if candidate.is_symlink():
                raise UsageError("目录路径不能是符号链接")
        except OSError as exc:
            raise UsageError("目录路径无效") from exc
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

    @app.get("/api/v1/translation-validators")
    async def translation_validators() -> dict[str, Any]:
        bindings = resolve_translation_validators()
        return {"validators": [summary for _, summary in bindings]}

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
        for stage in LLM_MODEL_STAGES:
            resolve_global_config(config, app_root, stage=stage)
        atomic_write_text(write_user("config/config.toml"), content)
        return {"saved": True}

    @app.get("/api/v1/global/prompts/{stage}")
    async def get_global_prompt(stage: str, language: str = "zh-CN") -> dict[str, Any]:
        if stage not in LLM_MODEL_STAGES:
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
        if stage not in LLM_MODEL_STAGES:
            raise UsageError(f"未知 Prompt 阶段：{stage}")
        language = validate_language(payload.get("language", "zh-CN"))
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            raise UsageError("Prompt 必须是非空字符串")
        atomic_write_text(
            write_user(f"prompts/{prompt_file(stage, language)}"), content
        )
        return {"saved": True}

    @app.get("/api/v1/projects/{name}/config")
    async def get_project_config(name: str) -> dict[str, Any]:
        return {"config": load_config(project(name) / "config.toml")}

    @app.put("/api/v1/projects/{name}/config")
    async def put_project_config(
        name: str, payload: dict[str, Any]
    ) -> dict[str, bool]:
        config = payload.get("config")
        if not isinstance(config, dict):
            raise UsageError("config 必须是对象")
        content = dump_config(config)
        root = project(name)
        resolve_project_config(config, presets_root=app_root)
        for stage in LLM_MODEL_STAGES:
            resolve_project_config(config, stage=stage, presets_root=app_root)
        with project_write_lock(root):
            atomic_write_text(root / "config.toml", content)
        return {"saved": True}

    @app.get("/api/v1/projects/{name}/prompts/{stage}")
    async def get_project_prompt(
        name: str, stage: str, language: str = "zh-CN"
    ) -> dict[str, Any]:
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
    async def put_project_prompt(
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

    @app.get("/api/v1/prompt-library/{stage}/{language}")
    async def get_prompt_library(stage: str, language: str) -> dict[str, Any]:
        return {
            "stage": stage,
            "language": language,
            "entries": list_prompt_library(stage, language),
        }

    @app.get("/api/v1/prompt-library/{stage}/{language}/{prompt_id:path}")
    async def get_prompt_library_entry(
        stage: str, language: str, prompt_id: str
    ) -> dict[str, Any]:
        content, digest = read_prompt_library(stage, language, prompt_id)
        result: dict[str, Any] = {
            "id": prompt_id,
            "stage": stage,
            "language": language,
            "content": content,
            "digest": digest,
            "assembled": full_prompt(stage, content, language),
        }
        if stage == "terminology_decision":
            assembled_phases = {
                phase: full_prompt(stage, content, language, phase=phase)
                for phase in ("adjudication", "consistency")
            }
            result["assembled_phases"] = assembled_phases
            result["assembled"] = assembled_phases["adjudication"]
        return result

    @app.put("/api/v1/prompt-library/{stage}/{language}/{prompt_id:path}")
    async def put_prompt_library_entry(
        stage: str, language: str, prompt_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        digest = save_prompt_library(
            stage,
            language,
            prompt_id,
            payload.get("content"),
        )
        return {"saved": True, "id": prompt_id, "digest": digest}

    @app.delete("/api/v1/prompt-library/{stage}/{language}/{prompt_id:path}")
    async def delete_prompt_library_entry(
        stage: str, language: str, prompt_id: str
    ) -> dict[str, Any]:
        delete_prompt_library(stage, language, prompt_id)
        return {"deleted": True, "id": prompt_id}

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
                        "stream": bool(preset.definition["stream"]),
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

    @app.get("/api/v1/global/presets/{preset_id}")
    async def get_global_preset(preset_id: str) -> dict[str, Any]:
        return load_llm_preset(preset_file(preset_id)).definition

    @app.put("/api/v1/global/presets/{preset_id}")
    async def put_global_preset(
        preset_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        preset = validate_preset_payload(preset_id, payload)
        path = write_user(f"llm_presets/{preset_id}.json")
        atomic_write_json(path, preset.definition)
        saved = load_llm_preset(path)
        return {"saved": True, "digest": saved.digest}

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
            stream=bool(preset.definition["stream"]),
            extra_body=preset.definition["extra_body"],
            extra_headers=preset.definition["extra_headers"],
            session_id="RUN-PREVIEW",
            request_id="REQ-PREVIEW",
        )
        endpoint = (
            preset.definition["stream_endpoint"]
            if preset.definition["stream"] and preset.definition["stream_endpoint"]
            else preset.definition["endpoint"]
        )
        return {
            "url": endpoint_url(
                preset.definition["base_url"],
                endpoint,
                model=preset.definition["model"],
            ),
            "headers": headers,
            "body": body,
            "transport": "sse"
            if preset.definition["stream"]
            else "non_streaming",
        }

    @app.post("/api/v1/global/presets/{preset_id}/models")
    async def discover_preset_models(
        preset_id: str, payload: dict[str, Any], key_index: int
    ) -> dict[str, Any]:
        if key_index < 1:
            raise UsageError("key_index 必须从 1 开始")
        preset = validate_preset_payload(preset_id, payload)
        adapter = load_json_adapter(
            effective_path(
                f"llm_adapters/{preset.adapter_id}.json", builtin_root=app_root
            )
        )
        if adapter.models_spec is None:
            raise UsageError("该 Adapter 未声明模型发现规格")
        api_keys = resolve_api_keys(preset.definition["credential"])
        if key_index > len(api_keys):
            raise UsageError("key_index 超出 API Key 范围")
        api_key = api_keys[key_index - 1]
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
        secret = read_credential(credential_id)
        if secret is None:
            raise UsageError(f"凭据不存在：{credential_id}")
        try:
            parse_api_keys(secret)
        except ConfigError as exc:
            raise UsageError(str(exc)) from exc
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
            "tasks": {
                "max_active_projects": app.state.tasks.max_active_projects,
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
        tasks = payload.get("tasks")
        if not isinstance(tasks, dict):
            raise UsageError("tasks 必须是对象")
        max_active_projects = tasks.get("max_active_projects")
        if (
            not isinstance(max_active_projects, int)
            or isinstance(max_active_projects, bool)
            or max_active_projects < 1
        ):
            raise UsageError("tasks.max_active_projects 必须是正整数")
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
        next_config = {
            **config,
            "lan": {
                **config["lan"],
                "enabled": enabled,
                "bind_address": bind_address,
            },
            "auth": {
                **config["auth"],
                "required": required,
                "username": username.strip(),
            },
            "tasks": {
                **config.get("tasks", {}),
                "max_active_projects": max_active_projects,
            },
        }
        save_server_config(next_config)
        app.state.server_config = next_config
        await app.state.tasks.set_max_active_projects(max_active_projects)
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
                        "streaming_supported": adapter.streaming_supported,
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
