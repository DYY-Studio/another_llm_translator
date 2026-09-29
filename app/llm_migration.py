from __future__ import annotations

import json
from pathlib import Path

from .errors import ConfigError
from .llm_preset import load_llm_preset
from .sqlite_storage import atomic_write_json
from .user_config import default_user_root, user_root


def migrate_llm_resources(*, base: Path | None = None) -> int:
    """Upgrade user-owned LLM resources without touching immutable Run snapshots."""
    root = default_user_root(base=base) if base is not None else user_root()
    migrated = 0
    migrated += _migrate_presets(root / "llm_presets")
    return migrated


def _migrate_presets(path: Path) -> int:
    if not path.is_dir():
        return 0
    migrated = 0
    for item in sorted(path.glob("*.json")):
        try:
            raw = json.loads(item.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"无法读取 LLM preset 迁移文件：{item}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"LLM preset 迁移文件顶层必须是对象：{item}")
        if raw.get("schema_version") not in {2, 3, 4, 5, 6}:
            continue
        raw = load_llm_preset(item).definition
        try:
            atomic_write_json(item, raw)
        except OSError as exc:
            raise ConfigError(f"无法写入 LLM preset 迁移文件：{item}: {exc}") from exc
        migrated += 1
    return migrated
