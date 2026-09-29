from __future__ import annotations

import json
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
USER_ROOT_OVERRIDE_ENV = "ANOTHER_LLM_USER_ROOT"
USER_ROOT_LOCATOR_NAME = f"{USER_ROOT_NAME}-location.json"


def _platform_data_base() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def default_user_root(*, base: Path | None = None) -> Path:
    return (base or _platform_data_base()) / USER_ROOT_NAME


def user_root_locator_path() -> Path:
    return _platform_data_base() / USER_ROOT_LOCATOR_NAME


def _located_user_root() -> Path | None:
    locator = user_root_locator_path()
    if not locator.exists() and not locator.is_symlink():
        return None
    try:
        data = json.loads(locator.read_text(encoding="utf-8"))
        root_value = data["active_root"]
        root = Path(root_value)
        if (
            type(data.get("version")) is not int
            or data["version"] != 1
            or not isinstance(root_value, str)
            or not root.is_absolute()
        ):
            raise ValueError("invalid version or non-absolute root")
        if root.name != USER_ROOT_NAME:
            raise ValueError(f"custom user root must end in {USER_ROOT_NAME}")
        if root.is_symlink():
            raise ValueError("custom user root must not be a symbolic link")
        if not root.is_dir() or not os.access(root, os.R_OK | os.W_OK | os.X_OK):
            raise ValueError("custom user root is unavailable")
        return root
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise ValueError(f"invalid user data locator {locator}: {exc}") from exc


def user_root() -> Path:
    """Resolve the user data root from environment, locator, then platform default."""
    override = os.environ.get(USER_ROOT_OVERRIDE_ENV)
    if override:
        return Path(override).expanduser()
    located = _located_user_root()
    if located is not None:
        return located
    return default_user_root()


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
