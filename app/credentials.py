from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import keyring

from .errors import ConfigError, ExternalError
from .sqlite_storage import atomic_write_json
from .user_config import USER_ROOT_OVERRIDE_ENV, legacy_user_root, user_root

SERVICE = "another-llm-translator"
LEGACY_SERVICE = "minimal-llm-translator"
LAN_ACCOUNT = "lan-auth"
_CREDENTIAL_ID_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]*")
_MIGRATION_LOGGER = logging.getLogger("another_llm_translator.migration")


def _index_path() -> Path:
    return user_root() / "credentials" / "index.json"


def _indexed_ids(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"无法读取凭据迁移索引 {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"凭据迁移索引格式无效：{path}")
    return {
        credential_id
        for credential_id, entry in value.items()
        if isinstance(credential_id, str)
        and _CREDENTIAL_ID_RE.fullmatch(credential_id)
        and isinstance(entry, dict)
    }


def migrate_legacy_credentials() -> int:
    """Copy legacy keyring entries without overwriting release entries."""
    if os.environ.get(USER_ROOT_OVERRIDE_ENV):
        return 0
    current_index = _index_path()
    old_index = legacy_user_root() / "credentials" / "index.json"
    accounts = _indexed_ids(current_index) | _indexed_ids(old_index) | {LAN_ACCOUNT}
    copied = 0
    for account in sorted(accounts):
        try:
            if keyring.get_password(SERVICE, account) is not None:
                continue
            secret = keyring.get_password(LEGACY_SERVICE, account)
            if secret is None:
                continue
            keyring.set_password(SERVICE, account, secret)
        except keyring.errors.KeyringError as exc:
            raise ConfigError(f"无法迁移钥匙串凭据 {account}：{exc}") from exc
        copied += 1
    if copied:
        _MIGRATION_LOGGER.info("已迁移 %d 个旧钥匙串条目", copied)
    return copied


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
