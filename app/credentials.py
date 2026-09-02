from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import keyring

from .errors import ConfigError, ExternalError
from .sqlite_storage import atomic_write_json
from .user_config import user_root

SERVICE = "another-llm-translator"
LAN_ACCOUNT = "lan-auth"
_CREDENTIAL_ID_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]*")


def _index_path() -> Path:
    return user_root() / "credentials" / "index.json"


def _load_index() -> dict[str, dict[str, int]]:
    path = _index_path()
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"无法读取凭据索引 {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"凭据索引格式无效：{path}")
    return {
        str(credential_id): {"updated_at": int(entry["updated_at"])}
        for credential_id, entry in value.items()
        if isinstance(entry, dict) and isinstance(entry.get("updated_at"), int)
    }


def _write_index(entries: dict[str, dict[str, int]]) -> None:
    atomic_write_json(_index_path(), entries)


def credential_summaries() -> list[dict[str, Any]]:
    return [
        {"id": credential_id, "updated_at": entry["updated_at"]}
        for credential_id, entry in sorted(_load_index().items())
    ]


def _validate_id(credential_id: str) -> str:
    if not isinstance(credential_id, str) or not _CREDENTIAL_ID_RE.fullmatch(
        credential_id
    ):
        raise ConfigError("凭据 ID 格式无效")
    return credential_id


def read_credential(credential_id: str) -> str | None:
    account = _validate_id(credential_id)
    try:
        return keyring.get_password(SERVICE, account)
    except keyring.errors.KeyringError as exc:
        raise ConfigError(f"无法读取系统钥匙串：{exc}") from exc


def save_credential(credential_id: str, secret: str) -> None:
    account = _validate_id(credential_id)
    if not isinstance(secret, str) or not secret:
        raise ConfigError("凭据内容必须是非空字符串")
    try:
        keyring.set_password(SERVICE, account, secret)
    except keyring.errors.KeyringError as exc:
        raise ConfigError(f"无法写入系统钥匙串：{exc}") from exc
    entries = _load_index()
    entries[account] = {"updated_at": int(time.time())}
    _write_index(entries)


def delete_credential(credential_id: str) -> None:
    account = _validate_id(credential_id)
    entries = _load_index()
    if account not in entries:
        raise ConfigError(f"凭据不存在：{account}")
    try:
        keyring.delete_password(SERVICE, account)
    except keyring.errors.PasswordDeleteError:
        pass
    except keyring.errors.KeyringError as exc:
        raise ConfigError(f"无法删除系统钥匙串凭据：{exc}") from exc
    del entries[account]
    _write_index(entries)


def parse_api_keys(secret: str) -> tuple[str, ...]:
    """Parse a newline-delimited API key value without normalizing keys."""
    if not isinstance(secret, str) or not secret:
        raise ConfigError("API Key 内容必须是非空字符串")
    normalized = secret.replace("\r\n", "\n")
    if normalized.endswith("\n"):
        normalized = normalized[:-1]
    lines = normalized.split("\n")
    if not lines or any(not line for line in lines):
        raise ConfigError("API Key 列表不能包含空行")
    for index, line in enumerate(lines, start=1):
        if any(character.isspace() for character in line):
            raise ConfigError(f"API Key 第 {index} 行不能包含空白字符")
    if len(lines) != len(set(lines)):
        raise ConfigError("API Key 列表不能包含重复 Key")
    return tuple(lines)


def resolve_api_keys(credential: dict[str, Any]) -> tuple[str, ...]:
    """Resolve a Preset credential reference into newline-delimited keys."""
    kind = credential["kind"]
    name = credential["name"]
    if kind == "environment":
        value = os.getenv(name)
        if not value:
            raise ExternalError(f"缺少环境变量：{name}")
        try:
            return parse_api_keys(value)
        except ConfigError as exc:
            raise ExternalError(f"环境变量 {name} 中的 API Key 无效：{exc}") from exc
    if kind == "keychain":
        value = read_credential(name)
        if value is None:
            raise ExternalError(f"缺少钥匙串凭据：{name}")
        try:
            return parse_api_keys(value)
        except ConfigError as exc:
            raise ExternalError(f"钥匙串凭据 {name} 中的 API Key 无效：{exc}") from exc
    raise ExternalError(f"未知凭据类型：{kind}")


def save_lan_password(secret: str) -> None:
    if not isinstance(secret, str) or not secret:
        raise ConfigError("LAN 密码必须是非空字符串")
    try:
        keyring.set_password(SERVICE, LAN_ACCOUNT, secret)
    except keyring.errors.KeyringError as exc:
        raise ConfigError(f"无法写入系统钥匙串：{exc}") from exc


def read_lan_password() -> str | None:
    try:
        return keyring.get_password(SERVICE, LAN_ACCOUNT)
    except keyring.errors.KeyringError as exc:
        raise ConfigError(f"无法读取系统钥匙串：{exc}") from exc
