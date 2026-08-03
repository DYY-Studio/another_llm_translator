from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .errors import ProjectError, StorageError

SCHEMA_VERSION = 1


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
                    payload_json TEXT NOT NULL,
                    UNIQUE(file_id, line_index)
                );
                CREATE INDEX IF NOT EXISTS segments_file_order
                    ON segments(file_id, line_index);
                CREATE INDEX IF NOT EXISTS segments_source_search
                    ON segments(source);
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
                    created_at TEXT,
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
                    created_at TEXT,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS terminology_scans_task_segment
                    ON terminology_scans(active_task_id, segment_id, sequence);
                CREATE TABLE IF NOT EXISTS terminology_candidates (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id TEXT NOT NULL UNIQUE,
                    active_task_id TEXT NOT NULL,
                    created_at TEXT,
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
                    created_at TEXT,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS run_chunks_run
                    ON run_chunks(run_id, sequence);
                INSERT INTO schema_meta(key, value)
                    VALUES ('schema_version', '1')
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value;
                """
            )
    except sqlite3.Error as exc:
        raise StorageError(f"无法初始化项目 SQLite：{path}: {exc}") from exc
    finally:
        try:
            connection.close()
        except UnboundLocalError:
            pass


def ensure_supported(project: Path) -> None:
    path = database_path(project)
    if not path.is_file():
        raise ProjectError(
            f"项目缺少 project.sqlite 或仍使用旧 JSONL 格式：{project}；请重新创建项目"
        )
    try:
        connection = _connect(path)
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.Error as exc:
        raise StorageError(f"无法读取项目 schema：{path}: {exc}") from exc
    finally:
        try:
            connection.close()
        except UnboundLocalError:
            pass
    if row is None or int(row[0]) != SCHEMA_VERSION:
        value = row[0] if row is not None else "缺失"
        raise ProjectError(f"不支持的项目 SQLite schema_version：{value}；请重新创建项目")


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


def write_project_meta(project: Path, value: dict[str, Any]) -> None:
    connection = _with_db(project)
    try:
        with connection:
            connection.execute("DELETE FROM project_meta")
            connection.executemany(
                "INSERT INTO project_meta(key, value_json) VALUES (?, ?)",
                [(key, _json(item)) for key, item in value.items()],
            )
    except sqlite3.Error as exc:
        raise StorageError(f"无法写入项目元数据：{project}: {exc}") from exc
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
                    (str(item["file_id"]), int(item["file_order"]), _json(item))
                    for item in file_values
                ],
            )
            connection.executemany(
                """
                INSERT INTO segments(
                    segment_id, file_id, line_index, part_id, source,
                    is_empty, model_source, payload_json
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
                        _json(item),
                    )
                    for item in segment_values
                ],
            )
            connection.executemany(
                "INSERT INTO adapter_states(file_id, payload_json) VALUES (?, ?)",
                [(str(item["file_id"]), _json(item)) for item in state_values],
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
        rows = connection.execute(
            "SELECT payload_json FROM files ORDER BY file_order"
        ).fetchall()
        return [_load(str(row[0])) for row in rows]
    except sqlite3.Error as exc:
        raise StorageError(f"无法读取项目 File：{project}: {exc}") from exc
    finally:
        connection.close()


def read_segments(project: Path) -> list[dict[str, Any]]:
    connection = _with_db(project)
    try:
        rows = connection.execute(
            "SELECT payload_json FROM segments ORDER BY file_id, line_index"
        ).fetchall()
        return [_load(str(row[0])) for row in rows]
    except sqlite3.Error as exc:
        raise StorageError(f"无法读取项目 Segment：{project}: {exc}") from exc
    finally:
        connection.close()


def read_adapter_state(project: Path, file_id: str) -> dict[str, Any] | None:
    connection = _with_db(project)
    try:
        row = connection.execute(
            "SELECT payload_json FROM adapter_states WHERE file_id = ?", (file_id,)
        ).fetchone()
        return _load(str(row[0])) if row is not None else None
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
    if relative == "source/files.jsonl":
        return "files", None
    if relative == "source/segments.jsonl":
        return "segments", None
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


def recognizes(path: Path) -> bool:
    for parent in (path.resolve(), *path.resolve().parents):
        if (parent / "project.sqlite").is_file():
            try:
                return _kind(path, parent)[0] != "file"
            except ValueError:
                return False
    return False


def read_json(project: Path, path: Path) -> dict[str, Any] | None:
    ensure_supported(project)
    kind, key = _kind(path, project)
    if kind == "project":
        return read_project_meta(project)
    if kind == "adapter_state":
        return read_adapter_state(project, str(key))
    if kind in {"terms", "overrides", "active_task"}:
        connection = _with_db(project)
        try:
            row = connection.execute(
                "SELECT payload_json FROM terms_state WHERE key = ?", (kind,)
            ).fetchone()
            return _load(str(row[0])) if row is not None else None
        finally:
            connection.close()
    if kind == "run_manifest":
        connection = _with_db(project)
        try:
            row = connection.execute(
                "SELECT payload_json FROM runs WHERE run_id = ?", (key,)
            ).fetchone()
            return _load(str(row[0])) if row is not None else None
        finally:
            connection.close()
    raise StorageError(f"SQLite 不支持读取 JSON 路径：{path}")


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
                    (str(value.get("file_id") or key), _json(value)),
                )
            elif kind in {"terms", "overrides", "active_task"}:
                connection.execute(
                    "INSERT INTO terms_state(key, payload_json) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET payload_json=excluded.payload_json",
                    (kind, _json(value)),
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
                        str(value.get("run_id") or key),
                        str(value.get("stage") or "unknown"),
                        str(value.get("status") or "unknown"),
                        value.get("started_at"),
                        _json(value),
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


def _records(project: Path, kind: str, key: str | None = None) -> list[dict[str, Any]]:
    connection = _with_db(project)
    try:
        if kind == "stage":
            rows = connection.execute(
                "SELECT payload_json FROM stage_results WHERE stage = ? ORDER BY sequence",
                (key,),
            ).fetchall()
        elif kind == "scans":
            rows = connection.execute(
                "SELECT payload_json FROM terminology_scans ORDER BY sequence"
            ).fetchall()
        elif kind == "candidates":
            rows = connection.execute(
                "SELECT payload_json FROM terminology_candidates ORDER BY sequence"
            ).fetchall()
        elif kind == "chunks":
            rows = connection.execute(
                "SELECT payload_json FROM run_chunks WHERE run_id = ? ORDER BY sequence",
                (key,),
            ).fetchall()
        else:
            raise StorageError(f"SQLite 不支持读取记录类型：{kind}")
        return [_load(str(row[0])) for row in rows]
    finally:
        connection.close()


def read_jsonl(project: Path, path: Path, *, repair_tail: bool = True) -> list[dict[str, Any]]:
    del repair_tail
    kind, key = _kind(path, project)
    if kind == "files":
        return read_files(project)
    if kind == "segments":
        return read_segments(project)
    return _records(project, kind, key)


def write_jsonl(project: Path, path: Path, values: Iterable[dict[str, Any]]) -> None:
    kind, key = _kind(path, project)
    records = [dict(value) for value in values]
    connection = _with_db(project)
    try:
        with connection:
            if kind == "files":
                connection.execute("DELETE FROM segments")
                connection.execute("DELETE FROM files")
                connection.executemany(
                    "INSERT INTO files(file_id, file_order, payload_json) VALUES (?, ?, ?)",
                    [(str(item["file_id"]), int(item["file_order"]), _json(item)) for item in records],
                )
            elif kind == "segments":
                connection.execute("DELETE FROM segments")
                connection.executemany(
                    """
                    INSERT INTO segments(segment_id,file_id,line_index,part_id,source,is_empty,model_source,payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            str(item["segment_id"]), str(item["file_id"]), int(item["line_index"]),
                            str(item["part_id"]), str(item["source"]), int(bool(item["is_empty"])),
                            item.get("model_source"), _json(item),
                        )
                        for item in records
                    ],
                )
            elif kind == "stage":
                connection.execute("DELETE FROM stage_results WHERE stage = ?", (key,))
                _insert_stage(connection, records)
            elif kind == "scans":
                connection.execute("DELETE FROM terminology_scans")
                _insert_scans(connection, records)
            elif kind == "candidates":
                connection.execute("DELETE FROM terminology_candidates")
                _insert_candidates(connection, records)
            elif kind == "chunks":
                connection.execute("DELETE FROM run_chunks WHERE run_id = ?", (key,))
                _insert_chunks(connection, records, str(key))
            else:
                raise StorageError(f"SQLite 不支持写入记录类型：{kind}")
    except sqlite3.Error as exc:
        raise StorageError(f"无法写入 SQLite 记录：{path}: {exc}") from exc
    finally:
        connection.close()


def _insert_stage(connection: sqlite3.Connection, records: Iterable[dict[str, Any]]) -> None:
    connection.executemany(
        "INSERT INTO stage_results(record_id,stage,segment_id,status,created_at,payload_json) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (str(item["record_id"]), str(item.get("stage")), item.get("segment_id"), item.get("status"), item.get("created_at"), _json(item))
            for item in records
        ],
    )


def _insert_scans(connection: sqlite3.Connection, records: Iterable[dict[str, Any]]) -> None:
    connection.executemany(
        "INSERT INTO terminology_scans(record_id,active_task_id,segment_id,status,created_at,payload_json) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (str(item["record_id"]), str(item["active_task_id"]), item.get("segment_id"), item.get("status"), item.get("created_at"), _json(item))
            for item in records
        ],
    )


def _insert_candidates(connection: sqlite3.Connection, records: Iterable[dict[str, Any]]) -> None:
    connection.executemany(
        "INSERT INTO terminology_candidates(record_id,active_task_id,created_at,payload_json) VALUES (?, ?, ?, ?)",
        [
            (str(item["record_id"]), str(item["active_task_id"]), item.get("created_at"), _json(item))
            for item in records
        ],
    )


def _insert_chunks(connection: sqlite3.Connection, records: Iterable[dict[str, Any]], run_id: str) -> None:
    connection.executemany(
        "INSERT INTO run_chunks(record_id,run_id,created_at,payload_json) VALUES (?, ?, ?, ?)",
        [
            (str(item["record_id"]), run_id, item.get("created_at"), _json(item))
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
        query = "SELECT payload_json FROM runs WHERE 1=1"
        params: list[Any] = []
        if stage is not None:
            query += " AND stage = ?"
            params.append(stage)
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY started_at DESC, run_id DESC"
        return [_load(str(row[0])) for row in connection.execute(query, params).fetchall()]
    finally:
        connection.close()


def _stage_cte(stage: str | None) -> tuple[str, list[Any]]:
    if not stage:
        return "", []
    return (
        """
        LEFT JOIN (
            SELECT segment_id, status, payload_json,
                   ROW_NUMBER() OVER (
                       PARTITION BY segment_id ORDER BY sequence DESC
                   ) AS rank
            FROM stage_results
            WHERE stage = ?
        ) AS latest_stage
          ON latest_stage.segment_id = segments.segment_id
         AND latest_stage.rank = 1
        """,
        [stage],
    )


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
        join, params = _stage_cte(stage)
        clauses = ["segments.is_empty = 0"]
        if file_id:
            clauses.append("segments.file_id = ?")
            params.append(file_id)
        if search:
            clauses.append(
                "(instr(lower(segments.source), lower(?)) > 0 OR "
                "instr(lower(COALESCE(latest_stage.payload_json, '')), lower(?)) > 0)"
            )
            params.extend([search, search])
        if status:
            _append_stage_status_filter(clauses, params, status)
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
        join, params = _stage_cte(stage)
        clauses = ["segments.is_empty = 0"]
        if file_id:
            clauses.append("segments.file_id = ?")
            params.append(file_id)
        if search:
            clauses.append(
                "(instr(lower(segments.source), lower(?)) > 0 OR "
                "instr(lower(COALESCE(latest_stage.payload_json, '')), lower(?)) > 0)"
            )
            params.extend([search, search])
        if status:
            _append_stage_status_filter(clauses, params, status)
        params.extend([limit, offset])
        query = f"""
            SELECT segments.payload_json
            FROM segments
            {join}
            WHERE {' AND '.join(clauses)}
            ORDER BY segments.file_id, segments.line_index
            LIMIT ? OFFSET ?
        """
        return [_load(str(row[0])) for row in connection.execute(query, params).fetchall()]
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
        join, params = _stage_cte(stage)
        clauses = ["segments.is_empty = 0"]
        if file_id:
            clauses.append("segments.file_id = ?")
            params.append(file_id)
        if search:
            clauses.append(
                "(instr(lower(segments.source), lower(?)) > 0 OR "
                "instr(lower(COALESCE(latest_stage.payload_json, '')), lower(?)) > 0)"
            )
            params.extend([search, search])
        if status:
            _append_stage_status_filter(clauses, params, status)
        query = f"""
            SELECT segments.segment_id
            FROM segments {join}
            WHERE {' AND '.join(clauses)}
            ORDER BY segments.file_id, segments.line_index
        """
        return [str(row[0]) for row in connection.execute(query, params).fetchall()]
    finally:
        connection.close()


def get_segment(project: Path, segment_id: str) -> dict[str, Any] | None:
    connection = _with_db(project)
    try:
        row = connection.execute(
            "SELECT payload_json FROM segments WHERE segment_id = ?", (segment_id,)
        ).fetchone()
        return _load(str(row[0])) if row is not None else None
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
            SELECT payload_json FROM (
                SELECT payload_json, segment_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY segment_id ORDER BY sequence DESC
                       ) AS rank
                FROM stage_results
                WHERE stage = ?{filter_sql}
            ) WHERE rank = 1
            """,
            params,
        ).fetchall()
        values_by_id = [_load(str(row[0])) for row in rows]
        return {str(item["segment_id"]): item for item in values_by_id}
    finally:
        connection.close()
