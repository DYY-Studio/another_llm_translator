from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import keyring

from .errors import ConfigError, ExternalError
from .user_config import user_root

SERVICE = "minimal-llm-translator"
_CREDENTIAL_ID_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]*")


def _index_path() -> Path:
    return user_root() / "credentials" / "index.json"


def _load_index() -> dict[str, dict[str, int]]:
    path = _index_path()
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        str(credential_id): {"updated_at": int(entry["updated_at"])}
        for credential_id, entry in value.items()
        if isinstance(entry, dict) and isinstance(entry.get("updated_at"), int)
    }


def _write_index(entries: dict[str, dict[str, int]]) -> None:
    path = _index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )


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


def resolve_api_key(credential: dict[str, Any]) -> str:
    """Resolve a Preset credential reference; never returns partial values."""
    kind = credential["kind"]
    name = credential["name"]
    if kind == "environment":
        value = os.getenv(name)
        if not value:
            raise ExternalError(f"缺少环境变量：{name}")
        return value
    if kind == "keychain":
        value = read_credential(name)
        if value is None:
            raise ExternalError(f"缺少钥匙串凭据：{name}")
        return value
    raise ExternalError(f"未知凭据类型：{kind}")
