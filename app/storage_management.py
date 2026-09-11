from __future__ import annotations

import logging
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import AppError, ProjectError, StorageError, UsageError
from .locking import project_write_lock
from .logging_utils import LOGGER_NAME
from .sqlite_storage import (
    database_path,
    list_run_index,
    read_project_meta_read_only,
)

GLOBAL_CATEGORY_IDS = ("settings", "logs", "other")
PROJECT_CATEGORY_IDS = (
    "logs",
    "sqlite",
    "input",
    "project_config",
    "run_snapshots",
    "debug_attachments",
    "output",
    "snapshots",
    "other",
)
_LOG_NAME = re.compile(r"^app\.log(?:\.[0-9]+)?$")
_LOG_ROTATION_NAME = re.compile(r"^app\.log\.[0-9]+$")
_MARKER_NAMES = frozenset({".welcome-seen"})
_SETTINGS_DIRECTORIES = frozenset(
    {"config", "prompts", "llm_presets", "llm_adapters"}
)


@dataclass
class _CategoryStats:
    bytes: int = 0
    file_count: int = 0
    reclaimable_bytes: int = 0
    unsafe: bool = False

    def add_file(self, size: int, *, reclaimable: bool = False) -> None:
        self.bytes += size
        self.file_count += 1
        if reclaimable:
            self.reclaimable_bytes += size


@dataclass
class _RunStats:
    bytes: int = 0
    file_count: int = 0
    unsafe: bool = False


@dataclass
class _OutputFile:
    path: str
    bytes: int
    unsafe: bool = False


@dataclass
class _LogGroup:
    identifier: str
    bytes: int = 0
    file_count: int = 0
    unsafe: bool = False


@dataclass
class _ProjectScan:
    project: Path
    categories: dict[str, _CategoryStats]
    errors: list[str]
    error_categories: set[str]
    run_stats: dict[str, _RunStats]
    output_files: list[_OutputFile]
    log_groups: dict[str, _LogGroup]
    run_index: dict[str, dict[str, Any]]
    run_index_available: bool
    metadata: dict[str, Any] | None

    @property
    def complete(self) -> bool:
        return not self.errors


def _new_categories(ids: tuple[str, ...]) -> dict[str, _CategoryStats]:
    return {identifier: _CategoryStats() for identifier in ids}


def _safe_resolve(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise StorageError(f"无法解析存储路径：{path}: {exc}") from exc


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _relative_parts(path: Path, root: Path) -> tuple[str, ...]:
    return path.relative_to(root).parts


def _is_regular_file(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _output_is_eligible(relative: str) -> bool:
    path = PurePosixPath(relative)
    return ".staging" not in path.parts and path.name != ".DS_Store"


def _project_selector(project: Path, projects_root: Path, metadata: dict[str, Any] | None) -> str:
    if project.parent == projects_root:
        return project.name
    if metadata is not None and metadata.get("project_id") is not None:
        return str(metadata["project_id"])
    return project.name


def _log_identifier(path: Path) -> str:
    return "app"


def _is_log_file(path: Path) -> bool:
    return bool(_LOG_NAME.fullmatch(path.name))


def _is_rotation_file(path: Path) -> bool:
    return bool(_LOG_ROTATION_NAME.fullmatch(path.name))


def _file_size(path: Path) -> int:
    try:
        return int(path.stat(follow_symlinks=False).st_size)
    except OSError as exc:
        raise StorageError(f"无法读取文件大小：{path}: {exc}") from exc


def _format_scan_error(path: Path, exc: BaseException) -> str:
    return f"无法读取存储路径 {path}: {exc}"


def _read_run_index(project: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    index: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    offset = 0
    try:
        while True:
            rows, total = list_run_index(project, offset=offset, limit=200)
            for row in rows:
                run_id = str(row.get("run_id", ""))
                if run_id:
                    index[run_id] = row
            offset += len(rows)
            if offset >= total or not rows:
                break
    except AppError as exc:
        errors.append(str(exc))
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
    return index, errors


def _walk_project_files(
    project: Path,
    scan: _ProjectScan,
) -> None:
    root = project

    def category_for(parts: tuple[str, ...]) -> str:
        if not parts:
            return "other"
        first = parts[0]
        if first == "logs" and _is_log_file(Path(parts[-1])):
            return "logs"
        if first == "project.sqlite" or (
            len(parts) == 1 and first in {"project.sqlite-wal", "project.sqlite-shm"}
        ):
            return "sqlite"
        if first == "input":
            return "input"
        if first == "prompts" or first in {"config.toml", "project.json"}:
            return "project_config"
        if first == "runs":
            if len(parts) >= 3 and parts[2] == "payloads":
                return "debug_attachments"
            if len(parts) == 3 and parts[2] == "attempts.jsonl":
                return "debug_attachments"
            return "run_snapshots"
        if first == "output":
            return "output"
        if first == "snapshots":
            return "snapshots"
        return "other"

    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            scan.errors.append(_format_scan_error(current, exc))
            scan.error_categories.update(PROJECT_CATEGORY_IDS)
            continue
        for entry in sorted(entries, key=lambda item: item.name):
            path = Path(entry.path)
            try:
                if entry.is_symlink():
                    parts = _relative_parts(path, root)
                    category = category_for(parts)
                    scan.categories[category].unsafe = True
                    scan.error_categories.add(category)
                    if category == "debug_attachments" and len(parts) >= 2:
                        scan.run_stats.setdefault(parts[1], _RunStats()).unsafe = True
                    if category == "output":
                        scan.output_files.append(
                            _OutputFile(
                                path=PurePosixPath(*parts).as_posix(),
                                bytes=0,
                                unsafe=True,
                            )
                        )
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                parts = _relative_parts(path, root)
                category = category_for(parts)
                size = _file_size(path)
                reclaimable = category in {"logs", "debug_attachments"}
                if category == "output":
                    reclaimable = _output_is_eligible(
                        PurePosixPath(*parts).as_posix()
                    )
                scan.categories[category].add_file(
                    size, reclaimable=reclaimable
                )
                if category == "debug_attachments" and len(parts) >= 2:
                    run_id = parts[1]
                    stats = scan.run_stats.setdefault(run_id, _RunStats())
                    stats.bytes += size
                    stats.file_count += 1
                elif category == "output":
                    scan.output_files.append(
                        _OutputFile(
                            path=PurePosixPath(*parts).as_posix(),
                            bytes=size,
                        )
                    )
                elif category == "logs":
                    group = scan.log_groups.setdefault(
                        _log_identifier(path), _LogGroup(_log_identifier(path))
                    )
                    group.bytes += size
                    group.file_count += 1
            except OSError as exc:
                parts = _relative_parts(path, root)
                category = category_for(parts)
                scan.errors.append(_format_scan_error(path, exc))
                scan.error_categories.add(category)
            except (RuntimeError, ValueError) as exc:
                parts = _relative_parts(path, root)
                category = category_for(parts)
                scan.errors.append(_format_scan_error(path, exc))
                scan.error_categories.add(category)


def _scan_project(project: Path, projects_root: Path) -> _ProjectScan:
    scan = _ProjectScan(
        project=project,
        categories=_new_categories(PROJECT_CATEGORY_IDS),
        errors=[],
        error_categories=set(),
        run_stats={},
        output_files=[],
        log_groups={},
        run_index={},
        run_index_available=False,
        metadata=None,
    )
    try:
        scan.metadata = read_project_meta_read_only(project)
    except AppError as exc:
        scan.errors.append(str(exc))
        scan.error_categories.update({"sqlite", "project_config", "debug_attachments"})
    except (OSError, ValueError) as exc:
        scan.errors.append(str(exc))
        scan.error_categories.update({"sqlite", "project_config", "debug_attachments"})

    run_index, run_errors = _read_run_index(project)
    scan.run_index = run_index
    scan.run_index_available = not run_errors
    if run_errors:
        scan.errors.extend(run_errors)
        scan.error_categories.add("debug_attachments")
        scan.error_categories.add("sqlite")
    _walk_project_files(project, scan)
    return scan


def _global_category(parts: tuple[str, ...]) -> str:
    if not parts:
        return "other"
    if parts[0] in _SETTINGS_DIRECTORIES:
        return "settings"
    if parts[0] == "credentials":
        return "settings"
    if len(parts) == 1 and (parts[0] in _MARKER_NAMES or parts[0] == "server.toml"):
        return "settings"
    if parts[0] == "logs" and _is_log_file(Path(parts[-1])):
        return "logs"
    return "other"


def _walk_global_files(
    root: Path,
    app_root: Path,
    known_projects: set[Path],
    categories: dict[str, _CategoryStats],
    errors: list[str],
    error_categories: set[str],
    log_groups: dict[str, _LogGroup],
) -> None:
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            errors.append(_format_scan_error(current, exc))
            error_categories.update(GLOBAL_CATEGORY_IDS)
            continue
        for entry in sorted(entries, key=lambda item: item.name):
            path = Path(entry.path)
            try:
                resolved = _safe_resolve(path)
                if _is_relative_to(resolved, app_root):
                    continue
                if resolved in known_projects:
                    continue
                if entry.is_symlink():
                    category = _global_category(_relative_parts(path, root))
                    categories[category].unsafe = True
                    error_categories.add(category)
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                parts = _relative_parts(path, root)
                category = _global_category(parts)
                size = _file_size(path)
                categories[category].add_file(
                    size, reclaimable=category == "logs"
                )
                if category == "logs":
                    group = log_groups.setdefault(
                        _log_identifier(path), _LogGroup(_log_identifier(path))
                    )
                    group.bytes += size
                    group.file_count += 1
            except (OSError, RuntimeError, ValueError, StorageError) as exc:
                category = _global_category(_relative_parts(path, root))
                errors.append(_format_scan_error(path, exc))
                error_categories.add(category)


def _category_dict(
    identifier: str,
    stats: _CategoryStats,
    *,
    can_clear: bool = False,
    blocked_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": identifier,
        "bytes": stats.bytes,
        "file_count": stats.file_count,
        "reclaimable_bytes": stats.reclaimable_bytes,
        "can_clear": can_clear,
        "blocked_reason": blocked_reason,
    }


def _readable_reason(reason: str | None) -> str | None:
    return reason if reason else None


def _require_confirm(confirm: object) -> None:
    if confirm is not True:
        raise UsageError("必须明确确认清理操作")


def _safe_output_relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value or "\\" in value:
        raise UsageError("输出文件路径无效")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise UsageError("输出文件路径必须是项目内相对路径")
    return path.as_posix()


def _safe_run_id(value: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise UsageError("Run ID 无效")
    path = PurePosixPath(value)
    if path.is_absolute() or len(path.parts) != 1 or path.parts[0] in {".", ".."}:
        raise UsageError("Run ID 无效")
    return path.parts[0]


def _safe_child(root: Path, relative: str) -> Path:
    root_resolved = _safe_resolve(root)
    candidate = _safe_resolve(root / relative)
    if not _is_relative_to(candidate, root_resolved):
        raise UsageError("目标路径不在允许的目录内")
    return candidate


def _regular_tree_files(root: Path) -> tuple[list[tuple[Path, int]], bool]:
    files: list[tuple[Path, int]] = []
    unsafe = False
    if not root.exists():
        return files, False
    if root.is_symlink() or not root.is_dir():
        return files, True
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            raise StorageError(f"无法读取清理目录：{current}: {exc}") from exc
        for entry in entries:
            path = Path(entry.path)
            if entry.is_symlink():
                unsafe = True
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append(path)
            elif entry.is_file(follow_symlinks=False):
                files.append((path, _file_size(path)))
    return files, unsafe


def _truncate_log_file(path: Path) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    if path.is_symlink() or not path.is_file():
        raise UsageError("日志文件路径不安全")
    before = _file_size(path)
    loggers = (logging.getLogger(), logging.getLogger(LOGGER_NAME))
    target = _safe_resolve(path)
    for logger in loggers:
        for handler in list(logger.handlers):
            handler_path = getattr(handler, "baseFilename", None)
            if not handler_path:
                continue
            try:
                if _safe_resolve(Path(handler_path)) != target:
                    continue
            except StorageError:
                continue
            handler.acquire()
            try:
                stream = getattr(handler, "stream", None)
                if stream is None:
                    continue
                stream.seek(0)
                stream.truncate(0)
                stream.flush()
            finally:
                handler.release()
            return 1, before
    with path.open("r+b") as handle:
        handle.truncate(0)
    return 1, before


def _clear_log_group(root: Path) -> tuple[int, int]:
    current = root / "app.log"
    affected, reclaimed = _truncate_log_file(current)
    if root.exists():
        try:
            entries = list(os.scandir(root))
        except OSError as exc:
            raise StorageError(f"无法读取日志目录：{root}: {exc}") from exc
        for entry in entries:
            path = Path(entry.path)
            if not _is_rotation_file(path):
                continue
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                if entry.is_symlink():
                    raise UsageError("日志轮转文件路径不安全")
                continue
            size = _file_size(path)
            path.unlink()
            affected += 1
            reclaimed += size
    return affected, reclaimed


def _project_busy_reason(
    project: Path,
    is_project_running: Callable[[Path], bool],
    run_index: dict[str, dict[str, Any]],
    run_index_available: bool,
) -> str | None:
    if is_project_running(project):
        return "项目存在活动 Web 任务"
    if not run_index_available:
        return "无法确认项目 Run 状态"
    if any(str(item.get("status")) == "running" for item in run_index.values()):
        return "项目存在运行中的 Run"
    return None


class StorageManager:
    """On-demand inventory and permanent cleanup for app-managed files."""

    def __init__(
        self,
        *,
        user_data_root: Path,
        projects_root: Path,
        app_root: Path,
        project_paths: Callable[[], list[Path]],
        global_log_path: Path | None = None,
        is_project_running: Callable[[Path], bool] | None = None,
        has_active_tasks: Callable[[], bool] | None = None,
    ) -> None:
        self.user_data_root = _safe_resolve(user_data_root)
        self.projects_root = _safe_resolve(projects_root)
        self.app_root = _safe_resolve(app_root)
        self.project_paths = project_paths
        self.global_log_path = (
            _safe_resolve(global_log_path)
            if global_log_path is not None
            else self.user_data_root / "logs" / "app.log"
        )
        self.is_project_running = is_project_running or (lambda _project: False)
        self.has_active_tasks = has_active_tasks or (lambda: False)

    def _projects(self) -> list[Path]:
        values: dict[Path, Path] = {}
        for raw in self.project_paths():
            try:
                path = _safe_resolve(Path(raw))
            except StorageError:
                continue
            if path == self.app_root or _is_relative_to(path, self.app_root):
                continue
            values[path] = path
        return sorted(values.values(), key=lambda path: (path.name.casefold(), str(path)))

    def _global_scan(self, projects: list[Path]) -> tuple[dict[str, _CategoryStats], list[str], set[str], dict[str, _LogGroup]]:
        categories = _new_categories(GLOBAL_CATEGORY_IDS)
        errors: list[str] = []
        error_categories: set[str] = set()
        log_groups: dict[str, _LogGroup] = {}
        known_projects = set(projects)
        if self.user_data_root.exists():
            _walk_global_files(
                self.user_data_root,
                self.app_root,
                known_projects,
                categories,
                errors,
                error_categories,
                log_groups,
            )
        custom_log_root = self.global_log_path.parent
        if (
            custom_log_root != self.user_data_root / "logs"
            and not _is_relative_to(custom_log_root, self.user_data_root)
            and not _is_relative_to(custom_log_root, self.app_root)
        ):
            if custom_log_root.exists():
                for path in sorted(custom_log_root.iterdir(), key=lambda item: item.name):
                    if not _is_log_file(path):
                        continue
                    if path.is_symlink():
                        categories["logs"].unsafe = True
                        error_categories.add("logs")
                        continue
                    if not path.is_file():
                        continue
                    try:
                        size = _file_size(path)
                    except StorageError as exc:
                        errors.append(str(exc))
                        error_categories.add("logs")
                        continue
                    categories["logs"].add_file(size, reclaimable=True)
                    group = log_groups.setdefault("app", _LogGroup("app"))
                    group.bytes += size
                    group.file_count += 1
        return categories, errors, error_categories, log_groups

    def _busy_reason(self, project: Path, scan: _ProjectScan) -> str | None:
        return _project_busy_reason(
            project,
            self.is_project_running,
            scan.run_index,
            scan.run_index_available,
        )

    def _global_categories(
        self,
        categories: dict[str, _CategoryStats],
        errors: list[str],
        error_categories: set[str],
    ) -> list[dict[str, Any]]:
        active_reason = "存在活动 Web 任务" if self.has_active_tasks() else None
        result: list[dict[str, Any]] = []
        for identifier in GLOBAL_CATEGORY_IDS:
            stats = categories[identifier]
            blocked = None
            can_clear = False
            if identifier == "logs":
                if "logs" in error_categories or stats.unsafe:
                    blocked = "扫描未完成或目标路径不安全"
                elif active_reason is not None:
                    blocked = active_reason
                elif stats.reclaimable_bytes == 0:
                    blocked = "没有可清理日志"
                else:
                    can_clear = True
            else:
                blocked = "按策略只读"
            result.append(
                _category_dict(
                    identifier,
                    stats,
                    can_clear=can_clear,
                    blocked_reason=_readable_reason(blocked),
                )
            )
        return result

    def _project_categories(self, scan: _ProjectScan) -> list[dict[str, Any]]:
        busy_reason = self._busy_reason(scan.project, scan)
        result: list[dict[str, Any]] = []
        for identifier in PROJECT_CATEGORY_IDS:
            stats = scan.categories[identifier]
            blocked: str | None = None
            can_clear = False
            if identifier in {"logs", "debug_attachments", "output"}:
                if identifier in scan.error_categories or stats.unsafe:
                    blocked = "扫描未完成或目标路径不安全"
                elif busy_reason is not None:
                    blocked = busy_reason
                elif stats.reclaimable_bytes == 0:
                    blocked = "没有可清理文件"
                else:
                    can_clear = True
            else:
                blocked = "按策略只读"
            result.append(
                _category_dict(
                    identifier,
                    stats,
                    can_clear=can_clear,
                    blocked_reason=_readable_reason(blocked),
                )
            )
        return result

    def _project_summary(self, scan: _ProjectScan) -> dict[str, Any]:
        metadata = scan.metadata or {}
        categories = self._project_categories(scan)
        return {
            "selector": _project_selector(scan.project, self.projects_root, metadata),
            "name": str(metadata.get("name") or scan.project.name),
            "project_id": metadata.get("project_id"),
            "path": str(scan.project),
            "external": scan.project.parent != self.projects_root,
            "categories": categories,
            "total_bytes": sum(item["bytes"] for item in categories),
            "reclaimable_bytes": sum(
                item["reclaimable_bytes"] for item in categories
            ),
            "complete": scan.complete,
            "errors": list(scan.errors),
        }

    def scan(self) -> dict[str, Any]:
        projects = self._projects()
        global_categories, global_errors, global_error_categories, _ = (
            self._global_scan(projects)
        )
        project_scans = [_scan_project(path, self.projects_root) for path in projects]
        project_summaries = [self._project_summary(scan) for scan in project_scans]
        errors = [*global_errors, *(error for scan in project_scans for error in scan.errors)]
        global_values = self._global_categories(
            global_categories, global_errors, global_error_categories
        )
        total = sum(item["bytes"] for item in global_values) + sum(
            item["total_bytes"] for item in project_summaries
        )
        reclaimable = sum(item["reclaimable_bytes"] for item in global_values) + sum(
            item["reclaimable_bytes"] for item in project_summaries
        )
        return {
            "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "complete": not errors,
            "errors": errors,
            "total_bytes": total,
            "reclaimable_bytes": reclaimable,
            "global": global_values,
            "projects": project_summaries,
        }

    def scan_project(self, project: Path) -> dict[str, Any]:
        root = _safe_resolve(project)
        scan = _scan_project(root, self.projects_root)
        descriptor = self._project_summary(scan)
        busy_reason = self._busy_reason(root, scan)
        debug_runs: list[dict[str, Any]] = []
        for run_id, stats in sorted(scan.run_stats.items()):
            record = scan.run_index.get(run_id, {})
            blocked: str | None = None
            if stats.unsafe:
                blocked = "目标路径不安全"
            elif "debug_attachments" in scan.error_categories:
                blocked = "扫描未完成"
            elif busy_reason is not None:
                blocked = busy_reason
            elif str(record.get("status")) == "running":
                blocked = "Run 正在运行"
            elif stats.bytes == 0:
                blocked = "没有可清理附件"
            debug_runs.append(
                {
                    "run_id": run_id,
                    "stage": record.get("stage"),
                    "status": record.get("status"),
                    "bytes": stats.bytes,
                    "file_count": stats.file_count,
                    "reclaimable_bytes": stats.bytes,
                    "can_clear": blocked is None,
                    "blocked_reason": blocked,
                }
            )
        output_files = []
        for item in sorted(scan.output_files, key=lambda value: value.path):
            blocked: str | None = None
            if item.unsafe:
                blocked = "目标路径不安全"
            elif "output" in scan.error_categories:
                blocked = "扫描未完成"
            elif busy_reason is not None:
                blocked = busy_reason
            elif not _output_is_eligible(item.path):
                blocked = "该输出文件不可清理"
            output_files.append(
                {
                    "path": item.path,
                    "bytes": item.bytes,
                    "can_clear": blocked is None,
                    "blocked_reason": blocked,
                }
            )
        logs = []
        for identifier, group in sorted(scan.log_groups.items()):
            blocked = None
            if group.unsafe:
                blocked = "目标路径不安全"
            elif "logs" in scan.error_categories:
                blocked = "扫描未完成"
            elif busy_reason is not None:
                blocked = busy_reason
            elif group.bytes == 0:
                blocked = "没有可清理日志"
            logs.append(
                {
                    "id": identifier,
                    "bytes": group.bytes,
                    "file_count": group.file_count,
                    "reclaimable_bytes": group.bytes,
                    "can_clear": blocked is None,
                    "blocked_reason": blocked,
                }
            )
        return {
            "complete": scan.complete,
            "project": descriptor,
            "debug_runs": debug_runs,
            "output_files": output_files,
            "logs": logs,
            "errors": list(scan.errors),
        }

    def _assert_project_cleanable(self, project: Path) -> _ProjectScan:
        scan = _scan_project(project, self.projects_root)
        reason = self._busy_reason(project, scan)
        if reason is not None:
            raise UsageError(f"项目当前不可清理：{reason}")
        if not scan.complete:
            raise UsageError("项目存储扫描未完成，无法安全清理")
        return scan

    def clear_global_logs(self, *, confirm: object) -> dict[str, int]:
        _require_confirm(confirm)
        if self.has_active_tasks():
            raise UsageError("存在活动 Web 任务，暂不能清理全局日志")
        root = self.global_log_path.parent
        with _global_log_lock:
            affected, reclaimed = _clear_log_group(root)
        return {"affected_files": affected, "reclaimed_bytes": reclaimed}

    def clear_project_logs(self, project: Path, *, confirm: object) -> dict[str, int]:
        _require_confirm(confirm)
        root = _safe_resolve(project)
        with project_write_lock(root):
            self._assert_project_cleanable(root)
            affected, reclaimed = _clear_log_group(root / "logs")
        return {"affected_files": affected, "reclaimed_bytes": reclaimed}

    def clear_debug(
        self, project: Path, run_id: str, *, confirm: object
    ) -> dict[str, int]:
        _require_confirm(confirm)
        safe_run_id = _safe_run_id(run_id)
        root = _safe_resolve(project)
        with project_write_lock(root):
            scan = self._assert_project_cleanable(root)
            record = scan.run_index.get(safe_run_id)
            if record is not None and str(record.get("status")) == "running":
                raise UsageError("Run 正在运行，暂不能清理 DEBUG 附件")
            runs_root = _safe_resolve(root / "runs")
            run_dir = _safe_child(runs_root, safe_run_id)
            if not run_dir.exists() or run_dir.is_symlink() or not run_dir.is_dir():
                raise UsageError(f"Run 不存在：{safe_run_id}")
            targets: list[tuple[Path, int]] = []
            attempts = run_dir / "attempts.jsonl"
            if attempts.exists():
                if attempts.is_symlink() or not attempts.is_file():
                    raise UsageError("DEBUG 附件路径不安全")
                targets.append((attempts, _file_size(attempts)))
            payloads = run_dir / "payloads"
            payload_files, unsafe = _regular_tree_files(payloads)
            if unsafe:
                raise UsageError("DEBUG 附件路径包含符号链接，已拒绝操作")
            targets.extend(payload_files)
            if not targets:
                raise UsageError(f"Run 没有可清理 DEBUG 附件：{safe_run_id}")
            affected = 0
            reclaimed = 0
            for path, size in targets:
                path.unlink()
                affected += 1
                reclaimed += size
            if payloads.exists() and payloads.is_dir():
                for current, dirs, _ in os.walk(payloads, topdown=False, followlinks=False):
                    for name in dirs:
                        (Path(current) / name).rmdir()
                payloads.rmdir()
        return {"affected_files": affected, "reclaimed_bytes": reclaimed}

    def clear_output(
        self, project: Path, relative_path: str, *, confirm: object
    ) -> dict[str, int]:
        _require_confirm(confirm)
        relative = _safe_output_relative(relative_path)
        relative_parts = PurePosixPath(relative).parts
        if relative_parts and relative_parts[0] == "output":
            relative = PurePosixPath(*relative_parts[1:]).as_posix()
            if not relative:
                raise UsageError("输出文件路径无效")
        root = _safe_resolve(project)
        with project_write_lock(root):
            scan = self._assert_project_cleanable(root)
            output_root = _safe_resolve(root / "output")
            candidate = _safe_child(output_root, relative)
            if not candidate.exists() or candidate.is_symlink() or not candidate.is_file():
                raise UsageError("输出文件不存在或目标路径不安全")
            if not _output_is_eligible(relative):
                raise UsageError("该输出文件不可清理")
            size = _file_size(candidate)
            candidate.unlink()
        return {"affected_files": 1, "reclaimed_bytes": size}


_global_log_lock = threading.Lock()


def lightweight_project_storage(project: Path) -> dict[str, int]:
    """Return legacy WebStore totals without opening or changing SQLite."""
    root = _safe_resolve(project)
    if not root.is_dir():
        raise ProjectError(f"项目目录不存在：{project}")
    categories = _new_categories(PROJECT_CATEGORY_IDS)
    scan = _ProjectScan(
        project=root,
        categories=categories,
        errors=[],
        error_categories=set(),
        run_stats={},
        output_files=[],
        log_groups={},
        run_index={},
        run_index_available=False,
        metadata=None,
    )
    _walk_project_files(root, scan)
    if scan.errors:
        raise ProjectError(scan.errors[0])
    sqlite = database_path(root)
    sqlite_bytes = _file_size(sqlite) if sqlite.is_file() else 0
    return {
        "total_bytes": sum(item.bytes for item in categories.values()),
        "sqlite_bytes": sqlite_bytes,
    }


def project_storage_size(project: Path) -> int:
    return lightweight_project_storage(project)["total_bytes"]
