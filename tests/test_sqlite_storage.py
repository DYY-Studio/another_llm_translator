from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.execution import stage_result_path
from app.errors import StorageError
from app.project import init_project
from app.sqlite_storage import (
    append_jsonl,
    compact_project_database,
    ensure_supported,
    latest_stage_summary,
    query_segments,
    read_json,
    read_files,
    read_jsonl,
    read_segments,
    read_segment_sources,
    record_header,
)
from tests.test_foundation import make_app_root


def create_project(tmp_path: Path, text: str = "one\n\ntwo") -> Path:
    source = tmp_path / "source.txt"
    source.write_text(text, encoding="utf-8-sig")
    project, _ = init_project(
        [str(source)],
        name="demo",
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    return project


def create_v2_project(tmp_path: Path, *, conflict: bool = False) -> tuple[Path, dict, dict, dict]:
    project = tmp_path / "v2"
    project.mkdir()
    database = sqlite3.connect(project / "project.sqlite")
    project_id = "PRJ-V2"
    file_record = record_header(
        "source_file",
        project_id,
        record_id="FILE-F0001",
        file_id="F0001",
        file_order=1,
        original_name="source.txt",
        stored_name="input/F0001__source.txt",
        segment_count=1,
    )
    segment_record = record_header(
        "source_segment",
        project_id,
        record_id="F0001-S000001",
        segment_id="F0001-S000001",
        file_id="F0001",
        line_index=0,
        part_id="document",
        source="source",
        is_empty=False,
    )
    stage_record = record_header(
        "stage_result",
        project_id,
        stage="translation",
        segment_id="F0001-S000001",
        status="completed",
        text="translated",
        stage_fingerprint="sha256:test",
        run_id="RUN-TEST",
        extra_payload="kept",
    )
    if conflict:
        segment_record["source"] = "different"
    try:
        database.executescript(
            """
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE project_meta (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
            CREATE TABLE files (
                file_id TEXT PRIMARY KEY,
                file_order INTEGER NOT NULL UNIQUE,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE segments (
                segment_id TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                file_order INTEGER NOT NULL,
                line_index INTEGER NOT NULL,
                part_id TEXT NOT NULL,
                source TEXT NOT NULL,
                is_empty INTEGER NOT NULL,
                model_source TEXT,
                payload_json TEXT NOT NULL,
                UNIQUE(file_id, line_index)
            );
            CREATE TABLE adapter_states (file_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
            CREATE TABLE stage_results (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id TEXT NOT NULL UNIQUE,
                stage TEXT NOT NULL,
                segment_id TEXT,
                status TEXT,
                created_at TEXT,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX stage_results_stage_segment ON stage_results(stage, segment_id, sequence);
            CREATE TABLE terminology_scans (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id TEXT NOT NULL UNIQUE,
                active_task_id TEXT NOT NULL,
                segment_id TEXT,
                status TEXT,
                created_at TEXT,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX terminology_scans_task_segment ON terminology_scans(active_task_id, segment_id, sequence);
            CREATE TABLE terminology_candidates (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id TEXT NOT NULL UNIQUE,
                active_task_id TEXT NOT NULL,
                created_at TEXT,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX terminology_candidates_task ON terminology_candidates(active_task_id, sequence);
            CREATE TABLE terms_state (key TEXT PRIMARY KEY, payload_json TEXT);
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                stage TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX runs_stage_status ON runs(stage, status, started_at);
            CREATE TABLE run_chunks (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL,
                created_at TEXT,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX run_chunks_run ON run_chunks(run_id, sequence);
            """
        )
        database.execute("INSERT INTO schema_meta(key,value) VALUES ('schema_version','2')")
        database.execute(
            "INSERT INTO project_meta(key,value_json) VALUES (?,?)",
            ("project_id", json.dumps(project_id)),
        )
        database.execute(
            "INSERT INTO files(file_id,file_order,payload_json) VALUES (?,?,?)",
            ("F0001", 1, json.dumps(file_record, ensure_ascii=False)),
        )
        database.execute(
            """INSERT INTO segments(
                segment_id,file_id,file_order,line_index,part_id,source,is_empty,model_source,payload_json
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                "F0001-S000001",
                "F0001",
                1,
                0,
                "document",
                "source",
                0,
                None,
                json.dumps(segment_record, ensure_ascii=False),
            ),
        )
        database.execute(
            """INSERT INTO stage_results(
                record_id,stage,segment_id,status,created_at,payload_json
            ) VALUES (?,?,?,?,?,?)""",
            (
                stage_record["record_id"],
                "translation",
                "F0001-S000001",
                "completed",
                stage_record["created_at"],
                json.dumps(stage_record, ensure_ascii=False),
            ),
        )
        database.commit()
    finally:
        database.close()
    return project, file_record, segment_record, stage_record


def test_v2_migrates_payloads_and_preserves_public_records(tmp_path: Path) -> None:
    project, file_record, segment_record, stage_record = create_v2_project(tmp_path)

    ensure_supported(project)

    with sqlite3.connect(project / "project.sqlite") as database:
        assert database.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "3"
        assert "payload_json" not in {
            row[1] for row in database.execute("PRAGMA table_info(segments)")
        }
        assert database.execute(
            "SELECT payload_json FROM stage_results"
        ).fetchone()[0] == json.dumps(
            {
                "text": "translated",
                "stage_fingerprint": "sha256:test",
                "run_id": "RUN-TEST",
                "extra_payload": "kept",
                "created_at": stage_record["created_at"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    assert read_files(project) == [file_record]
    assert read_segments(project) == [segment_record]
    assert read_jsonl(project, stage_result_path(project, "translation")) == [stage_record]


def test_v2_migration_rejects_sql_payload_conflict_without_changes(tmp_path: Path) -> None:
    project, _file_record, _segment_record, _stage_record = create_v2_project(
        tmp_path, conflict=True
    )

    with pytest.raises(StorageError, match="segments/F0001-S000001.source"):
        ensure_supported(project)

    with sqlite3.connect(project / "project.sqlite") as database:
        assert database.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "2"
        assert database.execute(
            "SELECT source FROM segments"
        ).fetchone()[0] == "source"


def test_compact_project_database_reclaims_deleted_pages(tmp_path: Path) -> None:
    project = create_project(tmp_path)
    with sqlite3.connect(project / "project.sqlite") as database:
        database.execute("CREATE TABLE compact_probe(value TEXT NOT NULL)")
        database.executemany(
            "INSERT INTO compact_probe(value) VALUES (?)",
            [("x" * 4096,) for _ in range(256)],
        )
        database.execute("DROP TABLE compact_probe")
        database.commit()

    summary = compact_project_database(project)

    assert summary["before_bytes"] > summary["after_bytes"]
    assert summary["reclaimed_bytes"] == (
        summary["before_bytes"] - summary["after_bytes"]
    )
    with sqlite3.connect(project / "project.sqlite") as database:
        assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_v1_project_migrates_file_order_and_drops_dead_indexes(
    tmp_path: Path,
) -> None:
    project = tmp_path / "legacy"
    project.mkdir()
    database = sqlite3.connect(project / "project.sqlite")
    try:
        database.executescript(
            """
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE files (
                file_id TEXT PRIMARY KEY,
                file_order INTEGER NOT NULL UNIQUE,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE segments (
                segment_id TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                line_index INTEGER NOT NULL,
                part_id TEXT NOT NULL,
                source TEXT NOT NULL,
                is_empty INTEGER NOT NULL,
                model_source TEXT,
                payload_json TEXT NOT NULL,
                UNIQUE(file_id, line_index)
            );
            CREATE INDEX segments_file_order ON segments(file_id, line_index);
            CREATE INDEX segments_source_search ON segments(source);
            INSERT INTO schema_meta(key, value) VALUES ('schema_version', '1');
            """
        )
        database.executemany(
            "INSERT INTO files(file_id, file_order, payload_json) VALUES (?, ?, ?)",
            [
                ("F0001", 2, '{"file_id":"F0001"}'),
                ("F0002", 1, '{"file_id":"F0002"}'),
            ],
        )
        database.executemany(
            """
            INSERT INTO segments(
                segment_id, file_id, line_index, part_id, source, is_empty,
                model_source, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("F0001-S000001", "F0001", 0, "document", "first", 0, None, '{"segment_id":"F0001-S000001"}'),
                ("F0002-S000001", "F0002", 0, "document", "second", 0, None, '{"segment_id":"F0002-S000001"}'),
            ],
        )
        database.commit()
    finally:
        database.close()

    ensure_supported(project)

    connection = sqlite3.connect(project / "project.sqlite")
    try:
        connection.row_factory = sqlite3.Row
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()["value"]
        assert version == "3"
        columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(segments)")
        }
        assert "file_order" not in columns
        assert "payload_json" not in columns
        indexes = {
            str(row["name"]) for row in connection.execute("PRAGMA index_list(segments)")
        }
        assert "segments_source_search" not in indexes
    finally:
        connection.close()

    assert [item["segment_id"] for item in query_segments(project)] == [
        "F0002-S000001",
        "F0001-S000001",
    ]
    assert [
        item["segment_id"] for item in query_segments(project, file_id="F0002")
    ] == ["F0002-S000001"]


def test_latest_stage_summary_preserves_completed_after_failed_and_reset_voids(
    tmp_path: Path,
) -> None:
    project = create_project(tmp_path)
    project_id = str(read_json(project, project / "project.json")["project_id"])
    path = stage_result_path(project, "translation")

    def record(segment_id: str, status: str, **fields: object) -> None:
        append_jsonl(
            project,
            path,
            record_header(
                "stage_result",
                project_id,
                stage="translation",
                segment_id=segment_id,
                status=status,
                **fields,
            ),
        )

    record("F0001-S000001", "completed", text="a1", stage_fingerprint="fp1")
    record("F0001-S000001", "failed", error_class="external_error", error_message="boom")
    record("F0001-S000002", "completed", text="b1", stage_fingerprint="fp2")
    record("F0001-S000002", "reset", reset_batch_id="R")

    summary = latest_stage_summary(
        project, "translation", ["F0001-S000001", "F0001-S000002"]
    )
    assert summary["F0001-S000001"] == {
        "completed": True,
        "failed": False,
        "stage_fingerprint": "fp1",
    }
    assert summary["F0001-S000002"] == {
        "completed": False,
        "failed": False,
        "stage_fingerprint": None,
    }

    record("F0001-S000002", "failed", error_class="external_error", error_message="boom")
    summary = latest_stage_summary(
        project, "translation", ["F0001-S000001", "F0001-S000002"]
    )
    assert summary["F0001-S000002"] == {
        "completed": False,
        "failed": True,
        "stage_fingerprint": None,
    }
    assert latest_stage_summary(project, "translation", []) == {}


def test_read_segment_sources_excludes_empty_and_preserves_order(
    tmp_path: Path,
) -> None:
    project = create_project(tmp_path, "first\n\u3000\nthird")
    sources = read_segment_sources(project)
    assert [item["source"] for item in sources] == ["first", "third"]
    assert [item["segment_id"] for item in sources] == [
        "F0001-S000001",
        "F0001-S000003",
    ]
    assert sources[0]["line_index"] == 0
    assert sources[1]["file_id"] == "F0001"


def test_read_jsonl_filters_terminology_records_by_task(tmp_path: Path) -> None:
    project = create_project(tmp_path, "one")
    project_id = str(read_json(project, project / "project.json")["project_id"])
    scans_path = project / "terminology" / "scans.jsonl"
    for task_id in ("TASK-A", "TASK-B"):
        for status in ("completed", "failed"):
            append_jsonl(
                project,
                scans_path,
                record_header(
                    "terminology_scan",
                    project_id,
                    stage="terminology",
                    segment_id="F0001-S000001",
                    status=status,
                    active_task_id=task_id,
                ),
            )

    scans_a = read_jsonl(project, scans_path, task_id="TASK-A")
    assert len(scans_a) == 2
    assert {item["active_task_id"] for item in scans_a} == {"TASK-A"}
    assert len(read_jsonl(project, scans_path)) == 4
