from __future__ import annotations

import sqlite3
from pathlib import Path

from app.execution import stage_result_path
from app.project import init_project
from app.sqlite_storage import (
    append_jsonl,
    ensure_supported,
    latest_stage_summary,
    query_segments,
    read_json,
    read_jsonl,
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
        assert version == "2"
        columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(segments)")
        }
        assert "file_order" in columns
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
