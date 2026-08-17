from __future__ import annotations

import json
import os
from pathlib import Path

from .errors import ConfigError
from .llm_adapter import load_json_adapter
from .llm_preset import load_llm_preset
from .sqlite_storage import atomic_write_json
from .user_config import USER_ROOT_OVERRIDE_ENV, default_user_root, user_root


def migrate_llm_resources(*, base: Path | None = None) -> int:
    """Upgrade user-owned LLM resources without touching immutable Run snapshots."""
    if base is None and os.environ.get(USER_ROOT_OVERRIDE_ENV):
        root = user_root()
    else:
        root = default_user_root(base=base) if base is not None else user_root()
    migrated = 0
    migrated += _migrate_directory(
        root / "llm_presets", kind="preset", legacy_version=2
    )
    migrated += _migrate_directory(
        root / "llm_adapters", kind="adapter", legacy_version=1
    )
    return migrated


def _migrate_directory(path: Path, *, kind: str, legacy_version: int) -> int:
    if not path.is_dir():
        return 0
    migrated = 0
    for item in sorted(path.glob("*.json")):
        try:
            raw = json.loads(item.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"无法读取 LLM {kind} 迁移文件：{item}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"LLM {kind} 迁移文件顶层必须是对象：{item}")
        if raw.get("schema_version") != legacy_version:
            continue
        if kind == "preset":
            load_llm_preset(item)
            raw["schema_version"] = 3
            raw.setdefault("stream", False)
            raw.setdefault("stream_endpoint", "")
        else:
            load_json_adapter(item)
            raw["schema_version"] = 2
        try:
            atomic_write_json(item, raw)
        except OSError as exc:
            raise ConfigError(f"无法写入 LLM {kind} 迁移文件：{item}: {exc}") from exc
        migrated += 1
    return migrated
