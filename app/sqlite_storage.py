from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .errors import ProjectError, StorageError

SCHEMA_VERSION = 3

STAGES = frozenset(
    {
        "terminology",
        "terminology_decision",
        "translation",
        "proofreading",
        "proofreading_applied",
        "polishing",
        "polishing_applied",
    }
)
RECORD_STATUSES = frozenset(
    {
        "active",
        "running",
        "completed",
        "failed",
        "interrupted",
        "reset",
        "partial_published",
    }
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


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def append_jsonl_file(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def database_path(project: Path) -> Path:
    return project / "project.sqlite"


def _connect(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection
    except sqlite3.Error as exc:
        raise StorageError(f"无法打开项目 SQLite：{path}: {exc}") from exc


def initialize(project: Path) -> None:
    path = database_path(project)
    try:
        connection = _connect(path)
        with connection:
            _ensure_schema(connection)
    except sqlite3.Error as exc:
        raise StorageError(f"无法初始化项目 SQLite：{path}: {exc}") from exc
    finally:
        try:
            connection.close()
        except UnboundLocalError:
            pass


def _create_tables(connection: sqlite3.Connection) -> None:
    """Create the current schema objects without changing existing tables."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS project_meta (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS files (
            file_id TEXT PRIMARY KEY,
            file_order INTEGER NOT NULL UNIQUE,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS segments (
            segment_id TEXT PRIMARY KEY,
            file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
            line_index INTEGER NOT NULL,
            part_id TEXT NOT NULL,
            source TEXT NOT NULL,
            is_empty INTEGER NOT NULL,
            model_source TEXT,
            created_at TEXT,
            UNIQUE(file_id, line_index)
        );
        CREATE TABLE IF NOT EXISTS adapter_states (
            file_id TEXT PRIMARY KEY REFERENCES files(file_id) ON DELETE CASCADE,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stage_results (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id TEXT NOT NULL UNIQUE,
            stage TEXT NOT NULL,
            segment_id TEXT,
            status TEXT,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS stage_results_stage_segment
            ON stage_results(stage, segment_id, sequence);
        CREATE TABLE IF NOT EXISTS terminology_scans (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id TEXT NOT NULL UNIQUE,
            active_task_id TEXT NOT NULL,
            segment_id TEXT,
            status TEXT,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS terminology_scans_task_segment
            ON terminology_scans(active_task_id, segment_id, sequence);
        CREATE TABLE IF NOT EXISTS terminology_candidates (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id TEXT NOT NULL UNIQUE,
            active_task_id TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS terminology_candidates_task
            ON terminology_candidates(active_task_id, sequence);
        CREATE TABLE IF NOT EXISTS terms_state (
            key TEXT PRIMARY KEY,
            payload_json TEXT
        );
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            stage TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS runs_stage_status
            ON runs(stage, status, started_at);
        CREATE TABLE IF NOT EXISTS run_chunks (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id TEXT NOT NULL UNIQUE,
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS run_chunks_run
            ON run_chunks(run_id, sequence);
        """
    )


def _project_id(connection: sqlite3.Connection) -> str | None:
    row = connection.execute(
        "SELECT value_json FROM project_meta WHERE key = 'project_id'"
    ).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(str(row[0]))
    except json.JSONDecodeError as exc:
        raise StorageError(f"项目 project_id 损坏：{exc}") from exc
    return str(value) if value is not None else None


def _residual(value: dict[str, Any], keys: Iterable[str]) -> str:
    residual = dict(value)
    for key in keys:
        residual.pop(key, None)
    return _json(residual)


_FILE_RESIDUAL_FIELDS = (
    "schema_version",
    "record_type",
    "record_id",
    "project_id",
    "file_id",
    "file_order",
)
_ADAPTER_RESIDUAL_FIELDS = (
    "schema_version",
    "record_type",
    "record_id",
    "project_id",
    "file_id",
)
_STAGE_RESIDUAL_FIELDS = (
    "schema_version",
    "record_type",
    "record_id",
    "project_id",
    "stage",
    "segment_id",
    "status",
)
_SCAN_RESIDUAL_FIELDS = (
    "schema_version",
    "record_type",
    "record_id",
    "project_id",
    "stage",
    "active_task_id",
    "segment_id",
    "status",
)
_CANDIDATE_RESIDUAL_FIELDS = (
    "schema_version",
    "record_type",
    "record_id",
    "project_id",
    "stage",
    "status",
    "active_task_id",
)
_RUN_RESIDUAL_FIELDS = (
    "schema_version",
    "record_type",
    "record_id",
    "project_id",
    "run_id",
    "stage",
    "status",
    "started_at",
    "created_at",
)
_CHUNK_RESIDUAL_FIELDS = (
    "schema_version",
    "record_type",
    "record_id",
    "project_id",
    "run_id",
)
_TERMS_RESIDUAL_FIELDS = ("schema_version", "record_type", "project_id")


def _check_mirrors(
    value: dict[str, Any], expected: dict[str, Any], location: str
) -> None:
    for key, actual in expected.items():
        if key not in value:
            continue
        expected_value = bool(actual) if key == "is_empty" else actual
        if value[key] != expected_value:
            raise StorageError(
                f"SQLite SQL/Payload 字段不一致：{location}.{key}: "
                f"SQL={expected_value!r}, payload={value[key]!r}"
            )


def _common_expected(
    project_id: str | None,
    *,
    record_type: str | None = None,
    record_id: str | None = None,
) -> dict[str, Any]:
    expected: dict[str, Any] = {"schema_version": 1}
    if project_id is not None:
        expected["project_id"] = project_id
    if record_type is not None:
        expected["record_type"] = record_type
    if record_id is not None:
        expected["record_id"] = record_id
    return expected


def _stage_record_type(status: Any) -> str | None:
    if status == "reset":
        return "stage_reset"
    if status is None:
        return None
    return "stage_result"


def _terms_record_type(key: str) -> str:
    return {
        "terms": "terminology_library",
        "overrides": "terminology_overrides",
        "active_task": "terminology_task",
    }[key]


def _migrate_legacy_to_v3(connection: sqlite3.Connection) -> None:
    """Validate and rewrite v1/v2 rows into the relation-first v3 layout."""
    project_id = _project_id(connection)
    files = connection.execute(
        "SELECT file_id, file_order, payload_json FROM files"
    ).fetchall()
    segment_columns = {
        str(item["name"])
        for item in connection.execute("PRAGMA table_info(segments)")
    }
    segment_file_order = (
        "segments.file_order"
        if "file_order" in segment_columns
        else "files.file_order"
    )
    segments = connection.execute(
        f"""SELECT segments.segment_id, segments.file_id,
                  {segment_file_order} AS file_order, segments.line_index,
                  segments.part_id, segments.source, segments.is_empty,
                  segments.model_source, segments.payload_json
           FROM segments
           JOIN files ON files.file_id = segments.file_id"""
    ).fetchall()
    adapter_states = connection.execute(
        "SELECT file_id, payload_json FROM adapter_states"
    ).fetchall()
    stage_results = connection.execute(
        """SELECT sequence, record_id, stage, segment_id, status, payload_json
           FROM stage_results ORDER BY sequence"""
    ).fetchall()
    scans = connection.execute(
        """SELECT sequence, record_id, active_task_id, segment_id, status,
                  payload_json FROM terminology_scans ORDER BY sequence"""
    ).fetchall()
    candidates = connection.execute(
        """SELECT sequence, record_id, active_task_id, payload_json
           FROM terminology_candidates ORDER BY sequence"""
    ).fetchall()
    runs = connection.execute(
        "SELECT run_id, stage, status, started_at, payload_json FROM runs"
    ).fetchall()
    chunks = connection.execute(
        """SELECT sequence, record_id, run_id, payload_json
           FROM run_chunks ORDER BY sequence"""
    ).fetchall()
    terms = connection.execute(
        "SELECT key, payload_json FROM terms_state WHERE payload_json IS NOT NULL"
    ).fetchall()

    file_payloads: list[tuple[str, str]] = []
    for row in files:
        value = _load(str(row["payload_json"]))
        file_id = str(row["file_id"])
        _check_mirrors(
            value,
            {
                **_common_expected(
                    project_id,
                    record_type="source_file",
                    record_id=f"FILE-{file_id}",
                ),
                "file_id": file_id,
                "file_order": int(row["file_order"]),
            },
            f"files/{file_id}",
        )
        file_payloads.append(
            (file_id, _residual(value, _FILE_RESIDUAL_FIELDS))
        )

    segment_rows: list[tuple[Any, ...]] = []
    for row in segments:
        value = _load(str(row["payload_json"]))
        segment_id = str(row["segment_id"])
        expected = {
            **_common_expected(
                project_id,
                record_type="source_segment",
                record_id=segment_id,
            ),
            "segment_id": segment_id,
            "file_id": str(row["file_id"]),
            "file_order": int(row["file_order"]),
            "line_index": int(row["line_index"]),
            "part_id": str(row["part_id"]),
            "source": str(row["source"]),
            "is_empty": bool(row["is_empty"]),
            "model_source": row["model_source"],
        }
        _check_mirrors(value, expected, f"segments/{segment_id}")
        segment_rows.append(
            (
                segment_id,
                str(row["file_id"]),
                int(row["line_index"]),
                str(row["part_id"]),
                str(row["source"]),
                int(bool(row["is_empty"])),
                row["model_source"],
                value.get("created_at"),
            )
        )

    adapter_payloads: list[tuple[str, str]] = []
    for row in adapter_states:
        file_id = str(row["file_id"])
        value = _load(str(row["payload_json"]))
        _check_mirrors(
            value,
            {
                **_common_expected(
                    project_id,
                    record_type="document_adapter_state",
                    record_id=f"DOCUMENT-{file_id}",
                ),
                "file_id": file_id,
            },
            f"adapter_states/{file_id}",
        )
        adapter_payloads.append(
            (file_id, _residual(value, _ADAPTER_RESIDUAL_FIELDS))
        )

    stage_payloads: list[tuple[Any, ...]] = []
    for row in stage_results:
        value = _load(str(row["payload_json"]))
        record_id = str(row["record_id"])
        stage = str(row["stage"])
        status = row["status"]
        _check_mirrors(
            value,
            {
                **_common_expected(
                    project_id,
                    record_type=_stage_record_type(status),
                    record_id=record_id,
                ),
                "stage": stage,
                "segment_id": row["segment_id"],
                "status": status,
            },
            f"stage_results/{record_id}",
        )
        stage_payloads.append(
            (
                int(row["sequence"]),
                record_id,
                stage,
                row["segment_id"],
                status,
                _residual(value, _STAGE_RESIDUAL_FIELDS),
            )
        )

    scan_payloads: list[tuple[Any, ...]] = []
    for row in scans:
        value = _load(str(row["payload_json"]))
        record_id = str(row["record_id"])
        _check_mirrors(
            value,
            {
                **_common_expected(
                    project_id,
                    record_type="terminology_scan",
                    record_id=record_id,
                ),
                "stage": "terminology",
                "active_task_id": str(row["active_task_id"]),
                "segment_id": row["segment_id"],
                "status": row["status"],
            },
            f"terminology_scans/{record_id}",
        )
        scan_payloads.append(
            (
                int(row["sequence"]),
                record_id,
                str(row["active_task_id"]),
                row["segment_id"],
                row["status"],
                _residual(value, _SCAN_RESIDUAL_FIELDS),
            )
        )

    candidate_payloads: list[tuple[Any, ...]] = []
    for row in candidates:
        value = _load(str(row["payload_json"]))
        record_id = str(row["record_id"])
        _check_mirrors(
            value,
            {
                **_common_expected(
                    project_id,
                    record_type="terminology_candidates",
                    record_id=record_id,
                ),
                "stage": "terminology",
                "status": "completed",
                "active_task_id": str(row["active_task_id"]),
            },
            f"terminology_candidates/{record_id}",
        )
        candidate_payloads.append(
            (
                int(row["sequence"]),
                record_id,
                str(row["active_task_id"]),
                _residual(value, _CANDIDATE_RESIDUAL_FIELDS),
            )
        )

    run_payloads: list[tuple[str, str, str, str | None, str]] = []
    for row in runs:
        value = _load(str(row["payload_json"]))
        run_id = str(row["run_id"])
        started_at = row["started_at"]
        _check_mirrors(
            value,
            {
                **_common_expected(
                    project_id,
                    record_type="run",
                    record_id=run_id,
                ),
                "run_id": run_id,
                "stage": str(row["stage"]),
                "status": str(row["status"]),
                "created_at": started_at,
            },
            f"runs/{run_id}",
        )
        run_payloads.append(
            (
                run_id,
                str(row["stage"]),
                str(row["status"]),
                started_at,
                _residual(value, _RUN_RESIDUAL_FIELDS),
            )
        )

    chunk_payloads: list[tuple[Any, ...]] = []
    for row in chunks:
        value = _load(str(row["payload_json"]))
        record_id = str(row["record_id"])
        _check_mirrors(
            value,
            {
                **_common_expected(
                    project_id,
                    record_type="chunk_manifest",
                    record_id=record_id,
                ),
                "run_id": str(row["run_id"]),
            },
            f"run_chunks/{record_id}",
        )
        chunk_payloads.append(
            (
                int(row["sequence"]),
                record_id,
                str(row["run_id"]),
                _residual(value, _CHUNK_RESIDUAL_FIELDS),
            )
        )

    term_payloads: list[tuple[str, str]] = []
    for row in terms:
        key = str(row["key"])
        value = _load(str(row["payload_json"]))
        _check_mirrors(
            value,
            _common_expected(project_id, record_type=_terms_record_type(key)),
            f"terms_state/{key}",
        )
        term_payloads.append(
            (key, _residual(value, _TERMS_RESIDUAL_FIELDS))
        )

    connection.execute(
        """CREATE TABLE segments_v3 (
            segment_id TEXT PRIMARY KEY,
            file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
            line_index INTEGER NOT NULL,
            part_id TEXT NOT NULL,
            source TEXT NOT NULL,
            is_empty INTEGER NOT NULL,
            model_source TEXT,
            created_at TEXT,
            UNIQUE(file_id, line_index)
        )"""
    )
    connection.executemany(
        """INSERT INTO segments_v3(
            segment_id,file_id,line_index,part_id,source,is_empty,model_source,created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        segment_rows,
    )
    connection.execute("DROP TABLE segments")
    connection.execute("ALTER TABLE segments_v3 RENAME TO segments")

    connection.execute(
        """CREATE TABLE stage_results_v3 (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id TEXT NOT NULL UNIQUE,
            stage TEXT NOT NULL,
            segment_id TEXT,
            status TEXT,
            payload_json TEXT NOT NULL
        )"""
    )
    connection.executemany(
        """INSERT INTO stage_results_v3(
            sequence,record_id,stage,segment_id,status,payload_json
        ) VALUES (?, ?, ?, ?, ?, ?)""",
        stage_payloads,
    )
    connection.execute("DROP TABLE stage_results")
    connection.execute("ALTER TABLE stage_results_v3 RENAME TO stage_results")
    connection.execute(
        """CREATE TABLE terminology_scans_v3 (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id TEXT NOT NULL UNIQUE,
            active_task_id TEXT NOT NULL,
            segment_id TEXT,
            status TEXT,
            payload_json TEXT NOT NULL
        )"""
    )
    connection.executemany(
        """INSERT INTO terminology_scans_v3(
            sequence,record_id,active_task_id,segment_id,status,payload_json
        ) VALUES (?, ?, ?, ?, ?, ?)""",
        scan_payloads,
    )
    connection.execute("DROP TABLE terminology_scans")
    connection.execute("ALTER TABLE terminology_scans_v3 RENAME TO terminology_scans")
    connection.execute(
        """CREATE TABLE terminology_candidates_v3 (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id TEXT NOT NULL UNIQUE,
            active_task_id TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )"""
    )
    connection.executemany(
        """INSERT INTO terminology_candidates_v3(
            sequence,record_id,active_task_id,payload_json
        ) VALUES (?, ?, ?, ?)""",
        candidate_payloads,
    )
    connection.execute("DROP TABLE terminology_candidates")
    connection.execute(
        "ALTER TABLE terminology_candidates_v3 RENAME TO terminology_candidates"
    )
    connection.execute(
        """CREATE TABLE run_chunks_v3 (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id TEXT NOT NULL UNIQUE,
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            payload_json TEXT NOT NULL
        )"""
    )
    connection.executemany(
        """INSERT INTO run_chunks_v3(
            sequence,record_id,run_id,payload_json
        ) VALUES (?, ?, ?, ?)""",
        chunk_payloads,
    )
    connection.execute("DROP TABLE run_chunks")
    connection.execute("ALTER TABLE run_chunks_v3 RENAME TO run_chunks")

    connection.executemany(
        "UPDATE files SET payload_json = ? WHERE file_id = ?",
        [(payload, file_id) for file_id, payload in file_payloads],
    )
    connection.executemany(
        "UPDATE adapter_states SET payload_json = ? WHERE file_id = ?",
        [(payload, file_id) for file_id, payload in adapter_payloads],
    )
    connection.executemany(
        "UPDATE runs SET payload_json = ? WHERE run_id = ?",
        [(payload, run_id) for run_id, _stage, _status, _started, payload in run_payloads],
    )
    connection.executemany(
        "UPDATE terms_state SET payload_json = ? WHERE key = ?",
        [(payload, key) for key, payload in term_payloads],
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS stage_results_stage_segment "
        "ON stage_results(stage, segment_id, sequence)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS terminology_scans_task_segment "
        "ON terminology_scans(active_task_id, segment_id, sequence)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS terminology_candidates_task "
        "ON terminology_candidates(active_task_id, sequence)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS run_chunks_run ON run_chunks(run_id, sequence)"
    )


def _ensure_schema(connection: sqlite3.Connection) -> None:
    """Ensure the project database matches SCHEMA_VERSION, migrating v1/v2."""
    _create_tables(connection)
    row = connection.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        version = SCHEMA_VERSION
    else:
        version = int(row[0])
        if version in {1, 2}:
            _migrate_legacy_to_v3(connection)
            version = 3
        elif version != SCHEMA_VERSION:
            raise ProjectError(
                f"不支持的项目 SQLite schema_version：{row[0]}；请重新创建项目"
            )
    connection.execute(
        "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(version),),
    )


_SUPPORTED_CACHE: set[Path] = set()


def ensure_supported(project: Path) -> None:
    path = database_path(project)
    if path in _SUPPORTED_CACHE:
        return
    if not path.is_file():
        raise ProjectError(
            f"项目缺少 project.sqlite 或仍使用旧 JSONL 格式：{project}；请重新创建项目"
        )
    try:
        connection = _connect(path)
        with connection:
            if connection.execute(
                "SELECT 1 FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone() is None:
                raise ProjectError(
                    "不支持的项目 SQLite schema_version：缺失；请重新创建项目"
                )
            _ensure_schema(connection)
    except sqlite3.Error as exc:
        raise StorageError(f"无法读取项目 schema：{path}: {exc}") from exc
    finally:
        try:
            connection.close()
        except UnboundLocalError:
            pass
    _SUPPORTED_CACHE.add(path)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load(value: str) -> dict[str, Any]:
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise StorageError(f"SQLite JSON 字段损坏：{exc}") from exc
    if not isinstance(loaded, dict):
        raise StorageError("SQLite JSON 字段必须是对象")
    return loaded


def _with_common_header(
    value: dict[str, Any],
    *,
    project_id: str | None,
    record_type: str | None = None,
    record_id: str | None = None,
    fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = dict(value)
    result.setdefault("schema_version", 1)
    if record_type is not None:
        result.setdefault("record_type", record_type)
    if record_id is not None:
        result.setdefault("record_id", record_id)
    if project_id is not None:
        result.setdefault("project_id", project_id)
    for key, item in (fields or {}).items():
        if item is not None:
            result.setdefault(key, item)
    return result


def _hydrate_file(row: sqlite3.Row, project_id: str | None) -> dict[str, Any]:
    value = _load(str(row["payload_json"]))
    return _with_common_header(
        value,
        project_id=project_id,
        record_type="source_file",
        record_id=f"FILE-{row['file_id']}",
        fields={
            "file_id": str(row["file_id"]),
            "file_order": int(row["file_order"]),
        },
    )


def _hydrate_segment(row: sqlite3.Row, project_id: str | None) -> dict[str, Any]:
    return _with_common_header(
        {},
        project_id=project_id,
        record_type="source_segment",
        record_id=str(row["segment_id"]),
        fields={
            "segment_id": str(row["segment_id"]),
            "file_id": str(row["file_id"]),
            "line_index": int(row["line_index"]),
            "part_id": str(row["part_id"]),
            "source": str(row["source"]),
            "is_empty": bool(row["is_empty"]),
            "model_source": row["model_source"],
            "created_at": row["created_at"],
        },
    )


def _hydrate_adapter_state(
    row: sqlite3.Row, project_id: str | None
) -> dict[str, Any]:
    value = _load(str(row["payload_json"]))
    file_id = str(row["file_id"])
    return _with_common_header(
        value,
        project_id=project_id,
        record_type="document_adapter_state",
        record_id=f"DOCUMENT-{file_id}",
        fields={"file_id": file_id},
    )


def _hydrate_stage(row: sqlite3.Row, project_id: str | None) -> dict[str, Any]:
    status = row["status"]
    return _with_common_header(
        _load(str(row["payload_json"])),
        project_id=project_id,
        record_type=_stage_record_type(status),
        record_id=str(row["record_id"]),
        fields={
            "stage": str(row["stage"]),
            "segment_id": row["segment_id"],
            "status": status,
        },
    )


def _hydrate_scan(row: sqlite3.Row, project_id: str | None) -> dict[str, Any]:
    return _with_common_header(
        _load(str(row["payload_json"])),
        project_id=project_id,
        record_type="terminology_scan",
        record_id=str(row["record_id"]),
        fields={
            "stage": "terminology",
            "active_task_id": str(row["active_task_id"]),
            "segment_id": row["segment_id"],
            "status": row["status"],
        },
    )


def _hydrate_candidate(row: sqlite3.Row, project_id: str | None) -> dict[str, Any]:
    return _with_common_header(
        _load(str(row["payload_json"])),
        project_id=project_id,
        record_type="terminology_candidates",
        record_id=str(row["record_id"]),
        fields={
            "stage": "terminology",
            "status": "completed",
            "active_task_id": str(row["active_task_id"]),
        },
    )


def _hydrate_run(row: sqlite3.Row, project_id: str | None) -> dict[str, Any]:
    return _with_common_header(
        _load(str(row["payload_json"])),
        project_id=project_id,
        record_type="run",
        record_id=str(row["run_id"]),
        fields={
            "run_id": str(row["run_id"]),
            "stage": str(row["stage"]),
            "status": str(row["status"]),
            "started_at": row["started_at"],
            "created_at": row["started_at"],
        },
    )


def _hydrate_chunk(row: sqlite3.Row, project_id: str | None) -> dict[str, Any]:
    return _with_common_header(
        _load(str(row["payload_json"])),
        project_id=project_id,
        record_type="chunk_manifest",
        record_id=str(row["record_id"]),
        fields={"run_id": str(row["run_id"])},
    )


def _hydrate_terms(
    row: sqlite3.Row, project_id: str | None
) -> dict[str, Any]:
    key = str(row["key"])
    return _with_common_header(
        _load(str(row["payload_json"])),
        project_id=project_id,
        record_type=_terms_record_type(key),
    )


def _with_db(project: Path) -> sqlite3.Connection:
    ensure_supported(project)
    return _connect(database_path(project))


def read_project_meta(project: Path) -> dict[str, Any]:
    connection = _with_db(project)
    try:
        rows = connection.execute("SELECT key, value_json FROM project_meta").fetchall()
        return {str(row["key"]): json.loads(row["value_json"]) for row in rows}
    except (sqlite3.Error, json.JSONDecodeError) as exc:
        raise StorageError(f"无法读取项目元数据：{project}: {exc}") from exc
    finally:
        connection.close()


def replace_source(
    project: Path,
    files: Iterable[dict[str, Any]],
    segments: Iterable[dict[str, Any]],
    metadata: dict[str, Any],
    adapter_states: Iterable[dict[str, Any]] = (),
) -> None:
    connection = _with_db(project)
    file_values = [dict(item) for item in files]
    segment_values = [dict(item) for item in segments]
    state_values = [dict(item) for item in adapter_states]
    try:
        with connection:
            connection.execute("DELETE FROM segments")
            connection.execute("DELETE FROM files")
            connection.execute("DELETE FROM adapter_states")
            connection.executemany(
                "INSERT INTO files(file_id, file_order, payload_json) VALUES (?, ?, ?)",
                [
                    (
                        str(item["file_id"]),
                        int(item["file_order"]),
                        _residual(item, _FILE_RESIDUAL_FIELDS),
                    )
                    for item in file_values
                ],
            )
            connection.executemany(
                """
                INSERT INTO segments(
                    segment_id, file_id, line_index, part_id, source,
                    is_empty, model_source, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(item["segment_id"]),
                        str(item["file_id"]),
                        int(item["line_index"]),
                        str(item["part_id"]),
                        str(item["source"]),
                        int(bool(item["is_empty"])),
                        item.get("model_source"),
                        item.get("created_at"),
                    )
                    for item in segment_values
                ],
            )
            connection.executemany(
                "INSERT INTO adapter_states(file_id, payload_json) VALUES (?, ?)",
                [
                    (
                        str(item["file_id"]),
                        _residual(item, _ADAPTER_RESIDUAL_FIELDS),
                    )
                    for item in state_values
                ],
            )
            connection.execute("DELETE FROM project_meta")
            connection.executemany(
                "INSERT INTO project_meta(key, value_json) VALUES (?, ?)",
                [(key, _json(item)) for key, item in metadata.items()],
            )
    except sqlite3.Error as exc:
        raise StorageError(f"无法写入项目源数据：{project}: {exc}") from exc
    finally:
        connection.close()


def read_files(project: Path) -> list[dict[str, Any]]:
    connection = _with_db(project)
    try:
        project_id = _project_id(connection)
        rows = connection.execute(
            "SELECT file_id, file_order, payload_json FROM files ORDER BY file_order"
        ).fetchall()
        return [_hydrate_file(row, project_id) for row in rows]
    except sqlite3.Error as exc:
        raise StorageError(f"无法读取项目 File：{project}: {exc}") from exc
    finally:
        connection.close()


def read_segments(project: Path) -> list[dict[str, Any]]:
    connection = _with_db(project)
    try:
        project_id = _project_id(connection)
        rows = connection.execute(
            """
            SELECT segments.segment_id, segments.file_id, segments.line_index,
                   segments.part_id, segments.source, segments.is_empty,
                   segments.model_source, segments.created_at
            FROM files CROSS JOIN segments ON segments.file_id = files.file_id
            ORDER BY files.file_order, segments.line_index
            """
        ).fetchall()
        return [_hydrate_segment(row, project_id) for row in rows]
    except sqlite3.Error as exc:
        raise StorageError(f"无法读取项目 Segment：{project}: {exc}") from exc
    finally:
        connection.close()


def read_segment_sources(project: Path) -> list[dict[str, Any]]:
    connection = _with_db(project)
    try:
        rows = connection.execute(
            """
            SELECT segments.segment_id, segments.file_id, segments.line_index,
                   segments.source
            FROM files CROSS JOIN segments ON segments.file_id = files.file_id
            WHERE segments.is_empty = 0
            ORDER BY files.file_order, segments.line_index
            """
        ).fetchall()
        return [
            {
                "segment_id": str(row[0]),
                "file_id": str(row[1]),
                "line_index": int(row[2]),
                "source": str(row[3]),
            }
            for row in rows
        ]
    except sqlite3.Error as exc:
        raise StorageError(f"无法读取 Segment 源文本：{project}: {exc}") from exc
    finally:
        connection.close()


def read_adapter_state(project: Path, file_id: str) -> dict[str, Any] | None:
    connection = _with_db(project)
    try:
        row = connection.execute(
            "SELECT file_id, payload_json FROM adapter_states WHERE file_id = ?",
            (file_id,),
        ).fetchone()
        return (
            _hydrate_adapter_state(row, _project_id(connection))
            if row is not None
            else None
        )
    except sqlite3.Error as exc:
        raise StorageError(f"无法读取 Adapter 状态：{project}: {exc}") from exc
    finally:
        connection.close()


def _relative(path: Path, project: Path) -> str:
    return path.resolve().relative_to(project.resolve()).as_posix()


def _kind(path: Path, project: Path) -> tuple[str, str | None]:
    relative = _relative(path, project)
    if relative == "project.json":
        return "project", None
    if relative.startswith("source/adapters/") and relative.endswith(".json"):
        return "adapter_state", Path(relative).stem
    if relative.startswith("stages/") and relative.endswith(".jsonl"):
        return "stage", Path(relative).stem
    if relative == "terminology/terms.json":
        return "terms", None
    if relative == "terminology/overrides.json":
        return "overrides", None
    if relative == "terminology/active_task.json":
        return "active_task", None
    if relative == "terminology/scans.jsonl":
        return "scans", None
    if relative == "terminology/candidates.jsonl":
        return "candidates", None
    parts = Path(relative).parts
    if len(parts) == 3 and parts[0] == "runs" and parts[2] == "manifest.json":
        return "run_manifest", parts[1]
    if len(parts) == 3 and parts[0] == "runs" and parts[2] == "chunks.jsonl":
        return "chunks", parts[1]
    return "file", None


def read_json(project: Path, path: Path) -> dict[str, Any]:
    ensure_supported(project)
    kind, key = _kind(path, project)
    if kind == "project":
        value = read_project_meta(project)
    elif kind == "adapter_state":
        value = read_adapter_state(project, str(key))
    elif kind in {"terms", "overrides", "active_task"}:
        connection = _with_db(project)
        try:
            row = connection.execute(
                "SELECT key, payload_json FROM terms_state WHERE key = ?", (kind,)
            ).fetchone()
            value = (
                _hydrate_terms(row, _project_id(connection))
                if row is not None
                else None
            )
        finally:
            connection.close()
    elif kind == "run_manifest":
        connection = _with_db(project)
        try:
            row = connection.execute(
                "SELECT run_id, stage, status, started_at, payload_json "
                "FROM runs WHERE run_id = ?",
                (key,),
            ).fetchone()
            value = (
                _hydrate_run(row, _project_id(connection))
                if row is not None
                else None
            )
        finally:
            connection.close()
    else:
        raise StorageError(f"SQLite 不支持读取 JSON 路径：{path}")
    if value is None:
        raise StorageError(f"SQLite 记录不存在：{path}")
    return _validate_record(value, str(path))


def write_json(project: Path, path: Path, value: dict[str, Any]) -> None:
    ensure_supported(project)
    kind, key = _kind(path, project)
    connection = _with_db(project)
    try:
        with connection:
            if kind == "project":
                connection.execute("DELETE FROM project_meta")
                connection.executemany(
                    "INSERT INTO project_meta(key, value_json) VALUES (?, ?)",
                    [(name, _json(item)) for name, item in value.items()],
                )
            elif kind == "adapter_state":
                connection.execute(
                    "INSERT INTO adapter_states(file_id, payload_json) VALUES (?, ?) "
                    "ON CONFLICT(file_id) DO UPDATE SET payload_json=excluded.payload_json",
                    (
                        str(value.get("file_id") or key),
                        _residual(value, _ADAPTER_RESIDUAL_FIELDS),
                    ),
                )
            elif kind in {"terms", "overrides", "active_task"}:
                connection.execute(
                    "INSERT INTO terms_state(key, payload_json) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET payload_json=excluded.payload_json",
                    (
                        kind,
                        _residual(value, _TERMS_RESIDUAL_FIELDS),
                    ),
                )
            elif kind == "run_manifest":
                connection.execute(
                    """
                    INSERT INTO runs(run_id, stage, status, started_at, payload_json)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        stage=excluded.stage, status=excluded.status,
                        started_at=excluded.started_at, payload_json=excluded.payload_json
                    """,
                    (
                        str(key),
                        str(value["stage"]),
                        str(value["status"]),
                        value.get("started_at"),
                        _residual(value, _RUN_RESIDUAL_FIELDS),
                    ),
                )
            else:
                raise StorageError(f"SQLite 不支持写入 JSON 路径：{path}")
    except sqlite3.Error as exc:
        raise StorageError(f"无法写入 SQLite JSON：{path}: {exc}") from exc
    finally:
        connection.close()
    if kind == "run_manifest":
        # The manifest is an intentionally human-readable run snapshot.  The
        # SQLite row remains authoritative for indexing and activity checks.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def clear_terminology_state(project: Path, overrides: dict[str, Any]) -> None:
    """Atomically remove published and in-progress terminology state."""
    ensure_supported(project)
    connection = _with_db(project)
    try:
        with connection:
            connection.execute(
                "DELETE FROM terms_state WHERE key IN ('terms', 'active_task')"
            )
            connection.execute("DELETE FROM terminology_scans")
            connection.execute("DELETE FROM terminology_candidates")
            connection.execute(
                "INSERT INTO terms_state(key, payload_json) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET payload_json=excluded.payload_json",
                (
                    "overrides",
                    _residual(overrides, _TERMS_RESIDUAL_FIELDS),
                ),
            )
    except sqlite3.Error as exc:
        raise StorageError(f"无法清空术语阶段状态：{project}: {exc}") from exc
    finally:
        connection.close()


def write_terminology_decision_state(
    project: Path,
    *,
    terms: dict[str, Any],
    overrides: dict[str, Any],
    run_manifest: dict[str, Any],
) -> None:
    """Commit a terminology decision and its Run state in one transaction."""
    ensure_supported(project)
    connection = _with_db(project)
    try:
        with connection:
            connection.executemany(
                "INSERT INTO terms_state(key, payload_json) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET payload_json=excluded.payload_json",
                [
                    ("terms", _residual(terms, _TERMS_RESIDUAL_FIELDS)),
                    ("overrides", _residual(overrides, _TERMS_RESIDUAL_FIELDS)),
                ],
            )
            connection.execute(
                """
                INSERT INTO runs(run_id, stage, status, started_at, payload_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    stage=excluded.stage, status=excluded.status,
                    started_at=excluded.started_at, payload_json=excluded.payload_json
                """,
                (
                    str(run_manifest["run_id"]),
                    str(run_manifest["stage"]),
                    str(run_manifest["status"]),
                    run_manifest.get("started_at"),
                    _residual(run_manifest, _RUN_RESIDUAL_FIELDS),
                ),
            )
    except sqlite3.Error as exc:
        raise StorageError(f"无法原子应用术语决策：{project}: {exc}") from exc
    finally:
        connection.close()
    try:
        atomic_write_json(
            project / "runs" / str(run_manifest["run_id"]) / "manifest.json",
            run_manifest,
        )
    except OSError:
        # SQLite is authoritative; the human-readable Run mirror can be
        # reconstructed by a later manifest update.
        pass


def _records(
    project: Path, kind: str, key: str | None = None, *, task_id: str | None = None
) -> list[dict[str, Any]]:
    connection = _with_db(project)
    try:
        project_id = _project_id(connection)
        if kind == "stage":
            rows = connection.execute(
                """SELECT sequence, record_id, stage, segment_id, status,
                          payload_json
                   FROM stage_results WHERE stage = ? ORDER BY sequence""",
                (key,),
            ).fetchall()
            return [_hydrate_stage(row, project_id) for row in rows]
        elif kind == "scans":
            if task_id is not None:
                rows = connection.execute(
                    "SELECT sequence, record_id, active_task_id, segment_id, status, "
                    "payload_json FROM terminology_scans "
                    "WHERE active_task_id = ? ORDER BY sequence",
                    (task_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT sequence, record_id, active_task_id, segment_id, status, "
                    "payload_json FROM terminology_scans ORDER BY sequence"
                ).fetchall()
            return [_hydrate_scan(row, project_id) for row in rows]
        elif kind == "candidates":
            if task_id is not None:
                rows = connection.execute(
                    "SELECT sequence, record_id, active_task_id, payload_json "
                    "FROM terminology_candidates "
                    "WHERE active_task_id = ? ORDER BY sequence",
                    (task_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT sequence, record_id, active_task_id, payload_json "
                    "FROM terminology_candidates ORDER BY sequence"
                ).fetchall()
            return [_hydrate_candidate(row, project_id) for row in rows]
        elif kind == "chunks":
            rows = connection.execute(
                """SELECT sequence, record_id, run_id, payload_json
                   FROM run_chunks WHERE run_id = ? ORDER BY sequence""",
                (key,),
            ).fetchall()
            return [_hydrate_chunk(row, project_id) for row in rows]
        else:
            raise StorageError(f"SQLite 不支持读取记录类型：{kind}")
    finally:
        connection.close()


def read_jsonl(
    project: Path, path: Path, *, task_id: str | None = None
) -> list[dict[str, Any]]:
    kind, key = _kind(path, project)
    return [
        _validate_record(item, str(path))
        for item in _records(project, kind, key, task_id=task_id)
    ]


def _insert_stage(connection: sqlite3.Connection, records: Iterable[dict[str, Any]]) -> None:
    connection.executemany(
        "INSERT INTO stage_results(record_id,stage,segment_id,status,payload_json) VALUES (?, ?, ?, ?, ?)",
        [
            (
                str(item["record_id"]),
                str(item.get("stage")),
                item.get("segment_id"),
                item.get("status"),
                _residual(item, _STAGE_RESIDUAL_FIELDS),
            )
            for item in records
        ],
    )


def _insert_scans(connection: sqlite3.Connection, records: Iterable[dict[str, Any]]) -> None:
    connection.executemany(
        "INSERT INTO terminology_scans(record_id,active_task_id,segment_id,status,payload_json) VALUES (?, ?, ?, ?, ?)",
        [
            (
                str(item["record_id"]),
                str(item["active_task_id"]),
                item.get("segment_id"),
                item.get("status"),
                _residual(item, _SCAN_RESIDUAL_FIELDS),
            )
            for item in records
        ],
    )


def _insert_candidates(connection: sqlite3.Connection, records: Iterable[dict[str, Any]]) -> None:
    connection.executemany(
        "INSERT INTO terminology_candidates(record_id,active_task_id,payload_json) VALUES (?, ?, ?)",
        [
            (
                str(item["record_id"]),
                str(item["active_task_id"]),
                _residual(item, _CANDIDATE_RESIDUAL_FIELDS),
            )
            for item in records
        ],
    )


def _insert_chunks(connection: sqlite3.Connection, records: Iterable[dict[str, Any]], run_id: str) -> None:
    connection.executemany(
        "INSERT INTO run_chunks(record_id,run_id,payload_json) VALUES (?, ?, ?)",
        [
            (
                str(item["record_id"]),
                str(item.get("run_id") or run_id),
                _residual(item, _CHUNK_RESIDUAL_FIELDS),
            )
            for item in records
        ],
    )


def append_jsonl(project: Path, path: Path, value: dict[str, Any]) -> None:
    kind, key = _kind(path, project)
    connection = _with_db(project)
    try:
        with connection:
            if kind == "stage":
                _insert_stage(connection, [value])
            elif kind == "scans":
                _insert_scans(connection, [value])
            elif kind == "candidates":
                _insert_candidates(connection, [value])
            elif kind == "chunks":
                _insert_chunks(connection, [value], str(key))
            else:
                raise StorageError(f"SQLite 不支持追加记录类型：{kind}")
    except sqlite3.Error as exc:
        raise StorageError(f"无法追加 SQLite 记录：{path}: {exc}") from exc
    finally:
        connection.close()


def append_stage_results(
    project: Path, records: Iterable[dict[str, Any]]
) -> None:
    """Append stage-result records in one SQLite transaction."""
    values = list(records)
    if not values:
        return
    connection = _with_db(project)
    try:
        with connection:
            _insert_stage(connection, values)
    except sqlite3.Error as exc:
        raise StorageError(f"无法批量追加 SQLite 阶段记录：{project}: {exc}") from exc
    finally:
        connection.close()


def record_exists(project: Path, path: Path) -> bool:
    kind, key = _kind(path, project)
    connection = _with_db(project)
    try:
        if kind in {"terms", "overrides", "active_task"}:
            row = connection.execute("SELECT 1 FROM terms_state WHERE key = ?", (kind,)).fetchone()
        elif kind == "run_manifest":
            row = connection.execute("SELECT 1 FROM runs WHERE run_id = ?", (key,)).fetchone()
        elif kind == "adapter_state":
            row = connection.execute("SELECT 1 FROM adapter_states WHERE file_id = ?", (key,)).fetchone()
        else:
            raise StorageError(f"SQLite 不支持检查记录类型：{kind}")
        return row is not None
    finally:
        connection.close()


def list_runs(project: Path, stage: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    connection = _with_db(project)
    try:
        query = (
            "SELECT run_id, stage, status, started_at, payload_json "
            "FROM runs WHERE 1=1"
        )
        params: list[Any] = []
        if stage is not None:
            query += " AND stage = ?"
            params.append(stage)
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY started_at DESC, run_id DESC"
        project_id = _project_id(connection)
        return [
            _hydrate_run(row, project_id)
            for row in connection.execute(query, params).fetchall()
        ]
    finally:
        connection.close()


def _stage_cte(stage: str | None) -> tuple[str, list[Any]]:
    if not stage:
        return "", []
    return (
        """
        LEFT JOIN (
            SELECT sr2.segment_id, sr2.status, sr2.payload_json
            FROM stage_results sr2
            JOIN (
                SELECT segment_id, MAX(sequence) AS seq
                FROM stage_results
                WHERE stage = ?
                GROUP BY segment_id
            ) AS latest ON latest.seq = sr2.sequence
        ) AS latest_stage
          ON latest_stage.segment_id = segments.segment_id
        """,
        [stage],
    )


def _stage_filters(
    *, status: str | None, search: str | None, stage: str | None
) -> tuple[str, list[Any], list[str]]:
    """Build the stage-result join and clauses, or nothing when unfiltered."""
    if not status and not search:
        return "", [], []
    join, params = _stage_cte(stage)
    clauses = []
    if search:
        clauses.append(
            "(instr(lower(segments.source), lower(?)) > 0 OR "
            "instr(lower(COALESCE(latest_stage.payload_json, '')), lower(?)) > 0)"
        )
        params.extend([search, search])
    if status:
        _append_stage_status_filter(clauses, params, status)
    return join, params, clauses


def _append_stage_status_filter(
    clauses: list[str], params: list[Any], status: str
) -> None:
    if status == "pending":
        clauses.append("(latest_stage.status IS NULL OR latest_stage.status = 'reset')")
    elif status == "warning":
        clauses.append(
            "latest_stage.status = 'completed' AND ("
            "instr(lower(COALESCE(latest_stage.payload_json, '')), "
            "'\"validation_status\":\"warning\"') > 0 OR "
            "instr(lower(COALESCE(latest_stage.payload_json, '')), "
            "'\"validation_status\": \"warning\"') > 0)"
        )
    else:
        clauses.append("latest_stage.status = ?")
        params.append(status)


def segment_count(
    project: Path,
    *,
    file_id: str | None = None,
    status: str | None = None,
    search: str | None = None,
    stage: str | None = None,
) -> int:
    connection = _with_db(project)
    try:
        join, params, stage_clauses = _stage_filters(
            status=status, search=search, stage=stage
        )
        clauses = ["segments.is_empty = 0", *stage_clauses]
        if file_id:
            clauses.append("segments.file_id = ?")
            params.append(file_id)
        query = f"SELECT COUNT(*) FROM segments {join} WHERE {' AND '.join(clauses)}"
        return int(connection.execute(query, params).fetchone()[0])
    finally:
        connection.close()


def query_segments(
    project: Path,
    *,
    offset: int = 0,
    limit: int = 100,
    file_id: str | None = None,
    status: str | None = None,
    search: str | None = None,
    stage: str | None = None,
) -> list[dict[str, Any]]:
    if offset < 0 or limit < 1 or limit > 500:
        raise ProjectError("Segment 窗口参数无效")
    connection = _with_db(project)
    try:
        join, params, stage_clauses = _stage_filters(
            status=status, search=search, stage=stage
        )
        clauses = ["segments.is_empty = 0", *stage_clauses]
        if file_id:
            clauses.append("segments.file_id = ?")
            params.append(file_id)
        params.extend([limit, offset])
        query = f"""
            SELECT segments.segment_id, segments.file_id, segments.line_index,
                   segments.part_id, segments.source, segments.is_empty,
                   segments.model_source, segments.created_at
            FROM files CROSS JOIN segments ON segments.file_id = files.file_id
            {join}
            WHERE {' AND '.join(clauses)}
            ORDER BY files.file_order, segments.line_index
            LIMIT ? OFFSET ?
        """
        project_id = _project_id(connection)
        return [
            _hydrate_segment(row, project_id)
            for row in connection.execute(query, params).fetchall()
        ]
    finally:
        connection.close()


def query_segment_neighbors(
    project: Path,
    *,
    file_id: str,
    part_id: str,
    line_index: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    connection = _with_db(project)
    try:
        before_rows = connection.execute(
            """
            SELECT segment_id, file_id, line_index, part_id, source,
                   is_empty, model_source, created_at
            FROM segments
            WHERE file_id = ?
              AND part_id = ?
              AND is_empty = 0
              AND line_index < ?
            ORDER BY line_index DESC
            LIMIT 2
            """,
            (file_id, part_id, line_index),
        ).fetchall()
        after_rows = connection.execute(
            """
            SELECT segment_id, file_id, line_index, part_id, source,
                   is_empty, model_source, created_at
            FROM segments
            WHERE file_id = ?
              AND part_id = ?
              AND is_empty = 0
              AND line_index > ?
            ORDER BY line_index
            LIMIT 2
            """,
            (file_id, part_id, line_index),
        ).fetchall()
        project_id = _project_id(connection)
        before = [_hydrate_segment(row, project_id) for row in reversed(before_rows)]
        after = [_hydrate_segment(row, project_id) for row in after_rows]
        return before, after
    finally:
        connection.close()


def segment_ids(
    project: Path,
    *,
    file_id: str | None = None,
    status: str | None = None,
    search: str | None = None,
    stage: str | None = None,
) -> list[str]:
    connection = _with_db(project)
    try:
        join, params, stage_clauses = _stage_filters(
            status=status, search=search, stage=stage
        )
        clauses = ["segments.is_empty = 0", *stage_clauses]
        if file_id:
            clauses.append("segments.file_id = ?")
            params.append(file_id)
        query = f"""
            SELECT segments.segment_id
            FROM files CROSS JOIN segments ON segments.file_id = files.file_id
            {join}
            WHERE {' AND '.join(clauses)}
            ORDER BY files.file_order, segments.line_index
        """
        return [str(row[0]) for row in connection.execute(query, params).fetchall()]
    finally:
        connection.close()


def get_segment(project: Path, segment_id: str) -> dict[str, Any] | None:
    connection = _with_db(project)
    try:
        row = connection.execute(
            """SELECT segment_id, file_id, line_index, part_id, source,
                      is_empty, model_source, created_at
               FROM segments WHERE segment_id = ?""",
            (segment_id,),
        ).fetchone()
        return (
            _hydrate_segment(row, _project_id(connection))
            if row is not None
            else None
        )
    finally:
        connection.close()


def latest_stage_results(
    project: Path,
    stage: str,
    segment_ids: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    connection = _with_db(project)
    try:
        params: list[Any] = [stage]
        filter_sql = ""
        values = list(segment_ids) if segment_ids is not None else None
        if values == []:
            return {}
        if values:
            placeholders = ",".join("?" for _ in values)
            filter_sql = f" AND segment_id IN ({placeholders})"
            params.extend(values)
        rows = connection.execute(
            f"""
            SELECT record_id, stage, segment_id, status, payload_json FROM (
                SELECT record_id, stage, status, payload_json, segment_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY segment_id ORDER BY sequence DESC
                       ) AS rank
                FROM stage_results
                WHERE stage = ?{filter_sql}
            ) WHERE rank = 1
            """,
            params,
        ).fetchall()
        project_id = _project_id(connection)
        values_by_id = [_hydrate_stage(row, project_id) for row in rows]
        return {str(item["segment_id"]): item for item in values_by_id}
    finally:
        connection.close()


def latest_stage_summary(
    project: Path,
    stage: str,
    segment_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Per-segment completed/failed classification with the latest completed
    fingerprint. A reset voids earlier results; failed records do not."""
    values = list(segment_ids)
    if not values:
        return {}
    placeholders = ",".join("?" for _ in values)
    params: list[Any] = [stage, *values]
    connection = _with_db(project)
    try:
        rows = connection.execute(
            f"""
            SELECT agg.segment_id,
                   agg.last_completed > COALESCE(agg.last_reset, 0) AS completed,
                   agg.last_failed IS NOT NULL
                       AND NOT (agg.last_completed > COALESCE(agg.last_reset, 0)) AS failed,
                   CASE
                       WHEN agg.last_completed > COALESCE(agg.last_reset, 0)
                       THEN json_extract(completed.payload_json, '$.stage_fingerprint')
                   END AS fingerprint
            FROM (
                SELECT segment_id,
                       MAX(CASE WHEN status = 'completed' THEN sequence END) AS last_completed,
                       MAX(CASE WHEN status = 'reset' THEN sequence END) AS last_reset,
                       MAX(CASE WHEN status = 'failed' THEN sequence END) AS last_failed
                FROM stage_results
                WHERE stage = ? AND segment_id IN ({placeholders})
                GROUP BY segment_id
            ) AS agg
            LEFT JOIN stage_results AS completed
              ON completed.sequence = agg.last_completed
            """,
            params,
        ).fetchall()
        return {
            str(row["segment_id"]): {
                "completed": bool(row["completed"]),
                "failed": bool(row["failed"]),
                "stage_fingerprint": row["fingerprint"],
            }
            for row in rows
        }
    finally:
        connection.close()


def latest_stage_states(
    project: Path,
    stage: str,
    segment_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Read only the latest completed payload and attempt status per Segment."""
    values = [str(value) for value in segment_ids]
    if not values:
        return {}
    placeholders = ",".join("?" for _ in values)
    connection = _with_db(project)
    try:
        rows = connection.execute(
            f"""
            WITH aggregate AS (
                SELECT segment_id,
                       MAX(CASE WHEN status = 'completed' THEN sequence END)
                           AS completed_sequence,
                       MAX(CASE WHEN status = 'reset' THEN sequence END)
                           AS reset_sequence,
                       MAX(sequence) AS latest_sequence
                FROM stage_results
                WHERE stage = ? AND segment_id IN ({placeholders})
                GROUP BY segment_id
            )
            SELECT aggregate.segment_id,
                   CASE
                       WHEN aggregate.completed_sequence IS NOT NULL
                        AND aggregate.completed_sequence
                            > COALESCE(aggregate.reset_sequence, 0)
                       THEN completed.payload_json
                   END AS completed_payload,
                   completed.record_id AS completed_record_id,
                   completed.stage AS completed_stage,
                   completed.segment_id AS completed_segment_id,
                   completed.status AS completed_status,
                   latest.status AS latest_status
            FROM aggregate
            LEFT JOIN stage_results AS completed
              ON completed.sequence = aggregate.completed_sequence
            LEFT JOIN stage_results AS latest
              ON latest.sequence = aggregate.latest_sequence
            """,
            [stage, *values],
        ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            completed_payload = row["completed_payload"]
            completed = None
            if completed_payload is not None:
                completed = _validate_record(
                    _hydrate_stage(
                        {
                            "record_id": row["completed_record_id"],
                            "stage": row["completed_stage"],
                            "segment_id": row["completed_segment_id"],
                            "status": row["completed_status"],
                            "payload_json": completed_payload,
                        },
                        _project_id(connection),
                    ),
                    f"stage={stage} segment={row['segment_id']}",
                )
            result[str(row["segment_id"])] = {
                "completed": completed,
                "latest_status": row["latest_status"],
            }
        return result
    finally:
        connection.close()


def terminology_scan_state(
    project: Path,
    task_id: str,
    segment_ids: Iterable[str],
) -> tuple[set[str], set[str]]:
    """Return completed Segment IDs and fingerprints for one terminology task."""
    values = [str(value) for value in segment_ids]
    if not values:
        return set(), set()
    placeholders = ",".join("?" for _ in values)
    connection = _with_db(project)
    try:
        rows = connection.execute(
            f"""
            SELECT DISTINCT segment_id,
                   json_extract(payload_json, '$.stage_fingerprint')
                       AS stage_fingerprint
            FROM terminology_scans
            WHERE active_task_id = ?
              AND status = 'completed'
              AND segment_id IN ({placeholders})
            """,
            [task_id, *values],
        ).fetchall()
        completed = {str(row["segment_id"]) for row in rows}
        fingerprints = {
            str(row["stage_fingerprint"])
            for row in rows
            if row["stage_fingerprint"] is not None
        }
        return completed, fingerprints
    finally:
        connection.close()


def compact_project_database(project: Path) -> dict[str, int]:
    """Checkpoint and compact one project database under its caller's lock."""
    ensure_supported(project)
    path = database_path(project)
    connection = _connect(path)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.commit()
        before_bytes = path.stat().st_size
        if shutil.disk_usage(path.parent).free < before_bytes:
            raise StorageError(
                "磁盘空间不足，无法压缩项目 SQLite "
                f"（至少需要约 {before_bytes} 字节可用空间）"
            )
        connection.execute("VACUUM")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.commit()
        check = connection.execute("PRAGMA integrity_check").fetchone()
        if check is None or str(check[0]).lower() != "ok":
            raise StorageError(f"SQLite 压缩后完整性检查失败：{check[0] if check else 'unknown'}")
    except sqlite3.Error as exc:
        raise StorageError(f"无法压缩项目 SQLite：{project}: {exc}") from exc
    finally:
        connection.close()
    after_bytes = path.stat().st_size
    return {
        "before_bytes": before_bytes,
        "after_bytes": after_bytes,
        "reclaimed_bytes": max(0, before_bytes - after_bytes),
    }
