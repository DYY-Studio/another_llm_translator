from __future__ import annotations

import os
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = BUILTIN_ROOT = (
    SOURCE_ROOT
    if (SOURCE_ROOT / "config" / "config.toml").is_file()
    else Path(sys.prefix)
)


def user_root() -> Path:
    """Platform user data root; MINIMAL_LLM_USER_ROOT overrides it."""
    override = os.environ.get("MINIMAL_LLM_USER_ROOT")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "minimal-llm-translator"


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
