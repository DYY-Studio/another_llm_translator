from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .config import normalize_settings
from .user_config import user_root


def default_server_config() -> dict[str, Any]:
    return {
        "lan": {"enabled": False, "bind_address": ""},
        "auth": {"required": False, "username": ""},
        "tasks": {"max_active_projects": 2, "estimate_usage_before_run": False},
    }


def server_config_path() -> Path:
    return user_root() / "server.toml"


def load_server_config(*, warnings: list[str] | None = None) -> dict[str, Any]:
    path = server_config_path()
    if not path.is_file():
        return default_server_config()
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"无法读取服务器配置 {path}: {exc}") from exc
    config = normalize_settings(value, default_server_config(), warnings=warnings, path="server")
    _validate(config)
    return config


def _validate(config: dict[str, Any]) -> None:
    for key in ("lan.enabled", "auth.required", "tasks.estimate_usage_before_run"):
        section, name = key.split(".")
        if not isinstance(config[section][name], bool):
            raise ConfigError(f"server.toml {key} 必须是布尔值")
    for key in ("lan.bind_address", "auth.username"):
        section, name = key.split(".")
        if not isinstance(config[section][name], str):
            raise ConfigError(f"server.toml {key} 必须是字符串")
    max_active_projects = config["tasks"]["max_active_projects"]
    if (
        not isinstance(max_active_projects, int)
        or isinstance(max_active_projects, bool)
        or max_active_projects < 1
    ):
        raise ConfigError("server.toml tasks.max_active_projects 必须是正整数")


def save_server_config(config: dict[str, Any], *, warnings: list[str] | None = None) -> None:
    config = normalize_settings(config, default_server_config(), warnings=warnings, path="server")
    _validate(config)
    lan = config["lan"]
    auth = config["auth"]
    tasks = config["tasks"]
    lines = [
        "[lan]",
        f"enabled = {json.dumps(lan['enabled'])}",
        f"bind_address = {json.dumps(lan['bind_address'])}",
        "",
        "[auth]",
        f"required = {json.dumps(auth['required'])}",
        f"username = {json.dumps(auth['username'])}",
        "",
        "[tasks]",
        f"max_active_projects = {tasks['max_active_projects']}",
        f"estimate_usage_before_run = {json.dumps(tasks['estimate_usage_before_run'])}",
        "",
    ]
    path = server_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
