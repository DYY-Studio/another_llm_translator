from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .user_config import user_root


def default_server_config() -> dict[str, Any]:
    return {
        "lan": {"enabled": False, "bind_address": ""},
        "auth": {"required": False, "username": ""},
    }


def server_config_path() -> Path:
    return user_root() / "server.toml"


def load_server_config() -> dict[str, Any]:
    path = server_config_path()
    if not path.is_file():
        return default_server_config()
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"无法读取服务器配置 {path}: {exc}") from exc
    config = default_server_config()
    lan = value.get("lan", {})
    auth = value.get("auth", {})
    for key in ("enabled", "bind_address"):
        if key in lan:
            config["lan"][key] = lan[key]
    for key in ("required", "username"):
        if key in auth:
            config["auth"][key] = auth[key]
    _validate(config)
    return config


def _validate(config: dict[str, Any]) -> None:
    for key in ("lan.enabled", "auth.required"):
        section, name = key.split(".")
        if not isinstance(config[section][name], bool):
            raise ConfigError(f"server.toml {key} 必须是布尔值")
    for key in ("lan.bind_address", "auth.username"):
        section, name = key.split(".")
        if not isinstance(config[section][name], str):
            raise ConfigError(f"server.toml {key} 必须是字符串")


def save_server_config(config: dict[str, Any]) -> None:
    _validate(config)
    lan = config["lan"]
    auth = config["auth"]
    lines = [
        "[lan]",
        f"enabled = {json.dumps(lan['enabled'])}",
        f"bind_address = {json.dumps(lan['bind_address'])}",
        "",
        "[auth]",
        f"required = {json.dumps(auth['required'])}",
        f"username = {json.dumps(auth['username'])}",
        "",
    ]
    path = server_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
