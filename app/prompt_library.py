from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .config import LLM_STAGES
from .errors import UsageError
from .project import PROMPT_LANGUAGES
from .sqlite_storage import atomic_write_text
from .user_config import user_root

_PROMPT_ID_RE = re.compile(r"[a-z][a-z0-9-]*")
_LIBRARY_SUFFIX = ".middle.txt"


def validate_prompt_library_scope(stage: str, language: str) -> None:
    if stage not in LLM_STAGES:
        raise UsageError(f"未知 Prompt 阶段：{stage}")
    if language not in PROMPT_LANGUAGES:
        raise UsageError("language 必须是 zh-CN 或 en")


def validate_prompt_library_id(prompt_id: str) -> str:
    if not isinstance(prompt_id, str) or not _PROMPT_ID_RE.fullmatch(prompt_id):
        raise UsageError(
            "Prompt 仓库 ID 必须以小写字母开头，只能包含小写字母、数字和连字符"
        )
    return prompt_id


def _library_directory(stage: str, language: str, *, create: bool = False) -> Path:
    validate_prompt_library_scope(stage, language)
    directory = user_root()
    if create:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise UsageError("无法创建用户数据目录") from exc
    for component in ("prompt_library", stage, language):
        directory /= component
        if directory.is_symlink():
            raise UsageError("Prompt 仓库目录不能是符号链接")
        if create:
            try:
                directory.mkdir(exist_ok=True)
            except OSError as exc:
                raise UsageError("无法创建 Prompt 仓库目录") from exc
        elif directory.exists() and not directory.is_dir():
            raise UsageError("Prompt 仓库路径不是目录")
    return directory


def prompt_library_path(stage: str, language: str, prompt_id: str) -> Path:
    prompt_id = validate_prompt_library_id(prompt_id)
    return _library_directory(stage, language) / f"{prompt_id}{_LIBRARY_SUFFIX}"


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def list_prompt_library(stage: str, language: str) -> list[dict[str, str]]:
    directory = _library_directory(stage, language)
    if not directory.is_dir():
        return []
    values: list[dict[str, str]] = []
    for path in sorted(
        directory.glob(f"*{_LIBRARY_SUFFIX}"), key=lambda item: item.name
    ):
        if path.is_symlink() or not path.is_file():
            continue
        prompt_id = path.name[: -len(_LIBRARY_SUFFIX)]
        if not _PROMPT_ID_RE.fullmatch(prompt_id):
            continue
        try:
            values.append({"id": prompt_id, "digest": _digest(path.read_bytes())})
        except OSError as exc:
            raise UsageError(f"无法读取 Prompt 仓库条目：{prompt_id}") from exc
    return values


def read_prompt_library(stage: str, language: str, prompt_id: str) -> tuple[str, str]:
    path = prompt_library_path(stage, language, prompt_id)
    if path.is_symlink() or not path.is_file():
        raise UsageError(f"Prompt 仓库条目不存在：{prompt_id}")
    try:
        raw = path.read_bytes()
        content = raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise UsageError(f"无法读取 Prompt 仓库条目：{prompt_id}") from exc
    return content, _digest(raw)


def save_prompt_library(
    stage: str,
    language: str,
    prompt_id: str,
    content: str,
) -> str:
    prompt_id = validate_prompt_library_id(prompt_id)
    if not isinstance(content, str) or not content.strip():
        raise UsageError("Prompt 必须是非空字符串")
    path = (
        _library_directory(stage, language, create=True)
        / f"{prompt_id}{_LIBRARY_SUFFIX}"
    )
    if path.is_symlink():
        raise UsageError(f"Prompt 仓库条目不是安全的普通文件：{prompt_id}")
    try:
        atomic_write_text(path, content)
    except (OSError, UnicodeError) as exc:
        raise UsageError(f"无法保存 Prompt 仓库条目：{prompt_id}") from exc
    return _digest(content.encode("utf-8"))


def delete_prompt_library(stage: str, language: str, prompt_id: str) -> None:
    path = prompt_library_path(stage, language, prompt_id)
    if path.is_symlink() or not path.is_file():
        raise UsageError(f"Prompt 仓库条目不存在：{prompt_id}")
    try:
        path.unlink()
    except OSError as exc:
        raise UsageError(f"无法删除 Prompt 仓库条目：{prompt_id}") from exc
