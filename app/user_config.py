from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = BUILTIN_ROOT = (
    SOURCE_ROOT
    if (SOURCE_ROOT / "config" / "config.toml").is_file()
    else Path(sys.prefix)
)

USER_ROOT_NAME = "another-llm-translator"
LEGACY_USER_ROOT_NAME = "minimal-llm-translator"
USER_ROOT_OVERRIDE_ENV = "ANOTHER_LLM_USER_ROOT"
_MIGRATION_LOGGER = logging.getLogger("another_llm_translator.migration")
_MIGRATION_STATES: dict[Path, str] = {}


def _platform_data_base() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def default_user_root(*, base: Path | None = None) -> Path:
    return (base or _platform_data_base()) / USER_ROOT_NAME


def legacy_user_root(*, base: Path | None = None) -> Path:
    return (base or _platform_data_base()) / LEGACY_USER_ROOT_NAME


def migrate_legacy_user_root(*, base: Path | None = None) -> str:
    """Move the legacy default data root once, without overwriting new data."""
    if base is None and os.environ.get(USER_ROOT_OVERRIDE_ENV):
        return "override"
    base = base or _platform_data_base()
    new_root = default_user_root(base=base)
    state = _MIGRATION_STATES.get(new_root)
    if state is not None:
        return state
    old_root = legacy_user_root(base=base)
    if new_root.exists():
        if not new_root.is_dir():
            raise RuntimeError(f"发布数据目录不是目录：{new_root}")
        if old_root.exists():
            _MIGRATION_LOGGER.warning(
                "跳过旧数据目录迁移：新目录已存在 %s；旧目录保留为 %s",
                new_root,
                old_root,
            )
        _MIGRATION_STATES[new_root] = "skipped"
        return "skipped"
    if not old_root.exists():
        _MIGRATION_STATES[new_root] = "absent"
        return "absent"
    if not old_root.is_dir():
        raise RuntimeError(f"旧数据路径不是目录，无法迁移：{old_root}")
    try:
        old_root.replace(new_root)
    except OSError as exc:
        raise RuntimeError(
            f"无法将旧数据目录迁移到 {new_root}；原目录仍保留：{old_root}: {exc}"
        ) from exc
    _MIGRATION_LOGGER.info("已将旧数据目录迁移到 %s", new_root)
    _MIGRATION_STATES[new_root] = "migrated"
    return "migrated"


def user_root() -> Path:
    """Platform user data root; ANOTHER_LLM_USER_ROOT overrides it."""
    override = os.environ.get(USER_ROOT_OVERRIDE_ENV)
    if override:
        return Path(override).expanduser()
    root = default_user_root()
    migrate_legacy_user_root()
    return root


def effective_path(relative: str | Path, *, builtin_root: Path = BUILTIN_ROOT) -> Path:
    """User-root file takes precedence; falls back to the builtin copy."""
    user = user_root() / relative
    if user.is_file():
        return user
    return builtin_root / relative


def write_user(relative: str | Path) -> Path:
    """Return the user-root write target, creating its parent directories."""
    target = user_root() / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    return target
