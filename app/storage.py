from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import StorageError

STAGES = frozenset(
    {
        "terminology",
        "translation",
        "proofreading",
        "proofreading_applied",
        "polishing",
        "polishing_applied",
    }
)
RECORD_STATUSES = frozenset(
    {"active", "running", "completed", "failed", "interrupted"}
)
REVIEW_STATUSES = frozenset({"accepted", "suggested"})
VALIDATION_STATUSES = frozenset({"passed", "warning"})
ERROR_CATEGORIES = frozenset(
    {
        "context_error",
        "external_error",
        "format_error",
        "validation_error",
        "stage_error",
    }
)


def _validate_record(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise StorageError(f"不支持或缺少 schema_version：{location}")
    for key, allowed in (
        ("stage", STAGES),
        ("status", RECORD_STATUSES),
        ("review_status", REVIEW_STATUSES),
        ("validation_status", VALIDATION_STATUSES),
        ("error_class", ERROR_CATEGORIES),
    ):
        if key in value and value[key] is not None and value[key] not in allowed:
            raise StorageError(f"不支持的 {key}：{location}: {value[key]}")
    return value


def utc_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_record_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


def record_header(
    record_type: str,
    project_id: str,
    *,
    record_id: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "record_type": record_type,
        "record_id": record_id or new_record_id("REC"),
        "project_id": project_id,
        **fields,
        "created_at": utc_now(),
    }


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            for value in values:
                handle.write(
                    json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                )
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageError(f"无法读取 JSON：{path}: {exc}") from exc
    return _validate_record(value, str(path))


def read_jsonl(path: Path, *, repair_tail: bool = True) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise StorageError(f"无法读取 JSONL：{path}: {exc}") from exc

    lines = data.splitlines(keepends=True)
    records: list[dict[str, Any]] = []
    offset = 0
    for index, raw_line in enumerate(lines):
        if not raw_line.strip():
            offset += len(raw_line)
            continue
        try:
            record = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            is_last = index == len(lines) - 1
            if not is_last:
                raise StorageError(
                    f"JSONL 中间行损坏：{path}:{index + 1}: {exc}"
                ) from exc
            if not repair_tail:
                raise StorageError(f"JSONL 尾行损坏（dry-run 不修复）：{path}") from exc
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = path.with_name(f"{path.name}.{timestamp}.corrupt-tail")
            backup.write_bytes(data[offset:])
            with path.open("r+b") as handle:
                handle.truncate(offset)
                handle.flush()
                os.fsync(handle.fileno())
            break
        records.append(_validate_record(record, f"{path}:{index + 1}"))
        offset += len(raw_line)
    return records
