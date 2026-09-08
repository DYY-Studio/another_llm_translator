from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.errors import ProjectError, StorageError
from app.execution import stage_result_path
from app.project import init_project
from app.sqlite_storage import (
    append_jsonl,
    compact_project_database,
    ensure_supported,
    latest_stage_summary,
    mark_content_summaries_source_changed,
    publish_content_summary_fulls,
    query_segments,
    read_content_summaries,
    read_files,
    read_json,
    read_jsonl,
    read_segment_sources,
    read_segments,
    read_summary_participation,
    read_summary_runs,
    record_header,
    replace_source,
    segment_count,
    segment_ids,
    write_content_summary,
    write_json,
    write_summary_participation,
    write_summary_run,
)
from app.summary_provenance import build_provenance, digest
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


def _summary_artifact(
    project_id: str,
    summary_id: str,
    *,
    kind: str,
    status: str = "completed",
    text: str | None = None,
    file_id: str = "F0001",
    part_id: str = "document",
    source_changed: bool = False,
    created_at: str = "2024-01-01T00:00:00+00:00",
    updated_at: str | None = None,
) -> dict[str, object]:
    record = record_header(
        "content_summary",
        project_id,
        record_id=summary_id,
        kind=kind,
        file_id=file_id,
        part_id=part_id,
        status=status,
        text=text if text is not None else summary_id,
        source_range={
            "file_id": file_id,
            "part_id": part_id,
            "segment_ids": [f"{summary_id}-SEGMENT"],
            "segments": [],
        },
        source_digest=f"sha256:{summary_id}-source",
        input_digest=f"sha256:{summary_id}-input",
        prompt_digest="sha256:prompt",
        model="test-model",
        source_changed=source_changed,
    )
    record["created_at"] = created_at
    record["updated_at"] = updated_at or created_at
    return record


def _attach_provenance(
    artifact: dict[str, object],
    origin: str,
    children: list[dict[str, object]],
) -> dict[str, object]:
    provenance, input_digest = build_provenance(origin, children)
    artifact["provenance"] = provenance
    artifact["input_digest"] = input_digest
    return artifact


def _summary_child(artifact: dict[str, object]) -> dict[str, object]:
    return {
        "record_id": artifact["record_id"],
        "kind": artifact["kind"],
        "text": artifact["text"],
        "source_digest": artifact["source_digest"],
        "source_range": artifact["source_range"],
    }


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


def test_summary_participation_and_artifacts_persist_provenance(
    tmp_path: Path,
) -> None:
    project = create_project(tmp_path)
    project_id = str(read_json(project, project / "project.json")["project_id"])

    write_summary_participation(
        project,
        [
            {"file_id": "F0001", "part_id": "document", "selected": True},
            {"file_id": "F0001", "part_id": "appendix", "selected": False},
        ],
    )
    summary = record_header(
        "content_summary",
        project_id,
        record_id="SUMMARY-F0001-DOCUMENT-FRAGMENT",
        kind="fragment",
        file_id="F0001",
        part_id="document",
        status="completed",
        text="A short summary.",
        source_range={"segment_ids": ["F0001-S000001"]},
        source_digest="sha256:source",
        input_digest="sha256:input",
        prompt_digest="sha256:prompt",
        model="test-model",
        run_id="SUMMARY-RUN-1",
        refs=["F0001-S000001"],
    )
    write_content_summary(project, summary)

    assert read_summary_participation(project) == [
        {"file_id": "F0001", "part_id": "appendix", "selected": False},
        {"file_id": "F0001", "part_id": "document", "selected": True},
    ]
    stored = read_content_summaries(project)
    assert len(stored) == 1
    assert stored[0]["record_id"] == summary["record_id"]
    assert stored[0]["kind"] == "fragment"
    assert stored[0]["source_range"] == summary["source_range"]
    assert stored[0]["source_digest"] == summary["source_digest"]
    assert stored[0]["input_digest"] == summary["input_digest"]
    assert stored[0]["prompt_digest"] == summary["prompt_digest"]
    assert stored[0]["model"] == summary["model"]
    assert stored[0]["refs"] == summary["refs"]
    assert stored[0]["source_changed"] is False


def test_publish_fulls_prunes_unreachable_summary_history_and_keeps_active_states(
    tmp_path: Path,
) -> None:
    project = create_project(tmp_path)
    project_id = str(read_json(project, project / "project.json")["project_id"])

    reachable_fragment_a = _summary_artifact(
        project_id, "FRAGMENT-REACHABLE-A", kind="fragment"
    )
    reachable_fragment_b = _summary_artifact(
        project_id, "FRAGMENT-REACHABLE-B", kind="fragment"
    )
    reachable_leaf = _attach_provenance(
        _summary_artifact(project_id, "REDUCTION-REACHABLE-LEAF", kind="reduction"),
        "llm",
        [_summary_child(reachable_fragment_a), _summary_child(reachable_fragment_b)],
    )
    reachable_root = _attach_provenance(
        _summary_artifact(
            project_id,
            "REDUCTION-REACHABLE-ROOT",
            kind="reduction",
            status="stale",
        ),
        "llm",
        [_summary_child(reachable_leaf)],
    )
    completed_unreachable_fragment = _summary_artifact(
        project_id, "FRAGMENT-COMPLETED-UNREACHABLE", kind="fragment"
    )
    stale_fragment = _summary_artifact(
        project_id, "FRAGMENT-STALE-UNREACHABLE", kind="fragment", status="stale"
    )
    changed_fragment = _summary_artifact(
        project_id,
        "FRAGMENT-SOURCE-CHANGED-UNREACHABLE",
        kind="fragment",
        source_changed=True,
    )
    old_reduction = _attach_provenance(
        _summary_artifact(project_id, "REDUCTION-COMPLETED-UNREACHABLE", kind="reduction"),
        "llm",
        [_summary_child(completed_unreachable_fragment)],
    )
    old_stale_reduction = _attach_provenance(
        _summary_artifact(
            project_id,
            "REDUCTION-STALE-UNREACHABLE",
            kind="reduction",
            status="stale",
        ),
        "llm",
        [_summary_child(completed_unreachable_fragment)],
    )
    full_one = _attach_provenance(
        _summary_artifact(
            project_id,
            "FULL-ONE",
            kind="full",
            created_at="2024-01-01T00:00:00+00:00",
        ),
        "llm",
        [_summary_child(old_reduction)],
    )
    full_two = _attach_provenance(
        _summary_artifact(
            project_id,
            "FULL-TWO",
            kind="full",
            created_at="2024-01-02T00:00:00+00:00",
        ),
        "llm",
        [_summary_child(reachable_root)],
    )
    full_three = _attach_provenance(
        _summary_artifact(
            project_id,
            "FULL-THREE",
            kind="full",
            created_at="2024-01-03T00:00:00+00:00",
        ),
        "llm",
        [_summary_child(reachable_fragment_a)],
    )
    full_four = _attach_provenance(
        _summary_artifact(
            project_id,
            "FULL-FOUR",
            kind="full",
            created_at="2024-01-04T00:00:00+00:00",
        ),
        "llm",
        [_summary_child(reachable_root)],
    )
    nonterminal_records = [
        _summary_artifact(
            project_id, "FRAGMENT-DRAFT", kind="fragment", status="draft"
        ),
        _summary_artifact(
            project_id, "FRAGMENT-RUNNING", kind="fragment", status="running"
        ),
        _summary_artifact(
            project_id, "FRAGMENT-FAILED", kind="fragment", status="failed"
        ),
        _summary_artifact(
            project_id, "REDUCTION-DRAFT", kind="reduction", status="draft"
        ),
        _summary_artifact(
            project_id, "REDUCTION-RUNNING", kind="reduction", status="running"
        ),
        _summary_artifact(
            project_id, "REDUCTION-FAILED", kind="reduction", status="failed"
        ),
        _summary_artifact(project_id, "FULL-DRAFT", kind="full", status="draft"),
        _summary_artifact(project_id, "FULL-RUNNING", kind="full", status="running"),
        _summary_artifact(project_id, "FULL-FAILED", kind="full", status="failed"),
    ]
    for artifact in [
        reachable_fragment_a,
        reachable_fragment_b,
        reachable_leaf,
        reachable_root,
        completed_unreachable_fragment,
        stale_fragment,
        changed_fragment,
        old_reduction,
        old_stale_reduction,
        full_one,
        full_two,
        full_three,
        *nonterminal_records,
    ]:
        write_content_summary(project, artifact)

    report = publish_content_summary_fulls(project, [full_four])

    assert report == {"deleted": 5, "skipped": []}
    stored = {str(item["record_id"]): item for item in read_content_summaries(project)}
    assert set(stored) == {
        "FULL-TWO",
        "FULL-THREE",
        "FULL-FOUR",
        "FULL-DRAFT",
        "FULL-RUNNING",
        "FULL-FAILED",
        "REDUCTION-REACHABLE-LEAF",
        "REDUCTION-REACHABLE-ROOT",
        "FRAGMENT-REACHABLE-A",
        "FRAGMENT-REACHABLE-B",
        "FRAGMENT-COMPLETED-UNREACHABLE",
        "FRAGMENT-DRAFT",
        "FRAGMENT-RUNNING",
        "FRAGMENT-FAILED",
        "REDUCTION-DRAFT",
        "REDUCTION-RUNNING",
        "REDUCTION-FAILED",
    }
    assert stored["FULL-TWO"]["status"] == "stale"
    assert stored["FULL-THREE"]["status"] == "stale"
    assert stored["FULL-FOUR"]["status"] == "completed"
    assert stored["REDUCTION-REACHABLE-ROOT"]["status"] == "stale"
    assert stored["FRAGMENT-COMPLETED-UNREACHABLE"]["status"] == "completed"


def test_publish_fulls_skips_boundary_when_retained_provenance_is_unavailable(
    tmp_path: Path,
) -> None:
    project = create_project(tmp_path)
    project_id = str(read_json(project, project / "project.json")["project_id"])
    fragment = _summary_artifact(project_id, "FRAGMENT-VALID", kind="fragment")
    stale_fragment = _summary_artifact(
        project_id, "FRAGMENT-STALE", kind="fragment", status="stale"
    )
    reduction = _attach_provenance(
        _summary_artifact(project_id, "REDUCTION-OLD", kind="reduction"),
        "llm",
        [_summary_child(stale_fragment)],
    )

    def full(summary_id: str, created_at: str) -> dict[str, object]:
        return _attach_provenance(
            _summary_artifact(
                project_id,
                summary_id,
                kind="full",
                created_at=created_at,
            ),
            "llm",
            [_summary_child(fragment)],
        )

    full_one = full("FULL-ONE", "2024-01-01T00:00:00+00:00")
    full_two = full("FULL-TWO", "2024-01-02T00:00:00+00:00")
    invalid_full = _summary_artifact(
        project_id,
        "FULL-INVALID",
        kind="full",
        created_at="2024-01-03T00:00:00+00:00",
    )
    invalid_full["provenance"] = {
        "origin": "llm",
        "artifact_ids": ["MISSING"],
        "source_ranges": [{}],
        "dependencies": [
            {
                "record_id": "MISSING",
                "kind": "fragment",
                "text_digest": "sha256:text",
                "source_digest": "sha256:source",
            }
        ],
    }
    invalid_full["input_digest"] = digest(invalid_full["provenance"]["dependencies"])
    full_four = full("FULL-FOUR", "2024-01-04T00:00:00+00:00")
    for artifact in [fragment, stale_fragment, reduction, full_one, full_two, invalid_full]:
        write_content_summary(project, artifact)

    report = publish_content_summary_fulls(project, [full_four])

    assert report == {
        "deleted": 0,
        "skipped": [
            {
                "file_id": "F0001",
                "part_id": "document",
                "reason": "provenance_unavailable",
            }
        ],
    }
    stored = {str(item["record_id"]): item for item in read_content_summaries(project)}
    assert set(stored) == {
        "FRAGMENT-VALID",
        "FRAGMENT-STALE",
        "REDUCTION-OLD",
        "FULL-ONE",
        "FULL-TWO",
        "FULL-INVALID",
        "FULL-FOUR",
    }
    assert stored["FULL-ONE"]["status"] == "stale"
    assert stored["FULL-TWO"]["status"] == "stale"
    assert stored["FULL-INVALID"]["status"] == "stale"
    assert stored["FULL-FOUR"]["status"] == "completed"


def test_summary_run_metadata_is_queryable_without_using_run_chunks(
    tmp_path: Path,
) -> None:
    project = create_project(tmp_path)
    project_id = str(read_json(project, project / "project.json")["project_id"])
    run = record_header(
        "summary_run",
        project_id,
        record_id="SUMMARY-RUN-1",
        run_id="SUMMARY-RUN-1",
        mode="fragment",
        status="running",
        source_ranges=[{"file_id": "F0001", "part_id": "document"}],
        input_digest="sha256:input",
        prompt_digest="sha256:prompt",
        model="test-model",
        selected_count=1,
    )

    write_summary_run(project, run)

    stored = read_summary_runs(project)
    assert len(stored) == 1
    assert stored[0]["run_id"] == "SUMMARY-RUN-1"
    assert stored[0]["mode"] == "fragment"
    assert stored[0]["source_ranges"] == run["source_ranges"]
    assert stored[0]["selected_count"] == 1


def test_source_change_keeps_summary_and_can_mark_only_selected_boundaries(
    tmp_path: Path,
) -> None:
    project = create_project(tmp_path)
    project_id = str(read_json(project, project / "project.json")["project_id"])
    for part_id, summary_id in (
        ("document", "SUMMARY-DOCUMENT"),
        ("appendix", "SUMMARY-APPENDIX"),
    ):
        write_content_summary(
            project,
            record_header(
                "content_summary",
                project_id,
                record_id=summary_id,
                kind="full",
                file_id="F0001",
                part_id=part_id,
                status="completed",
                text=f"Summary for {part_id}",
                source_range={"segment_ids": ["F0001-S000001"]},
                source_digest=f"sha256:{part_id}",
                input_digest="sha256:input",
                prompt_digest="sha256:prompt",
                model="test-model",
            ),
        )

    replace_source(
        project,
        read_files(project),
        read_segments(project),
        read_json(project, project / "project.json"),
    )
    mark_content_summaries_source_changed(
        project, [{"file_id": "F0001", "part_id": "document"}]
    )

    stored = {
        item["part_id"]: item for item in read_content_summaries(project)
    }
    assert set(stored) == {"document", "appendix"}
    assert stored["document"]["source_changed"] is True
    assert stored["appendix"]["source_changed"] is False


def test_v3_upgrade_makes_consistent_backup_before_schema_change(
    tmp_path: Path,
) -> None:
    project, _file_record, _segment_record, _stage_record = create_v2_project(tmp_path)

    backup_path = ensure_supported(project)

    assert backup_path is not None
    assert backup_path.is_file()
    assert backup_path.parent == project / "snapshots" / "storage_migrations"
    with sqlite3.connect(project / "project.sqlite") as database:
        assert database.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "4"
        assert database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='content_summaries'"
        ).fetchone() is not None
    with sqlite3.connect(backup_path) as backup:
        assert backup.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "2"


def test_v2_upgrade_rolls_back_all_ddl_after_mid_migration_foreign_key_failure(
    tmp_path: Path,
) -> None:
    project, _file_record, _segment_record, _stage_record = create_v2_project(tmp_path)
    with sqlite3.connect(project / "project.sqlite") as database:
        database.execute("PRAGMA foreign_keys = ON")
        database.execute(
            "CREATE TABLE migration_probe(segment_id TEXT REFERENCES segments(segment_id))"
        )
        database.execute(
            "INSERT INTO migration_probe(segment_id) VALUES ('F0001-S000001')"
        )
        database.commit()

    with pytest.raises(StorageError, match="FOREIGN KEY"):
        ensure_supported(project)

    with sqlite3.connect(project / "project.sqlite") as database:
        assert database.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "2"
        assert database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='segments_v3'"
        ).fetchone() is None
        assert database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='content_summaries'"
        ).fetchone() is None
        assert database.execute(
            "SELECT source FROM segments WHERE segment_id='F0001-S000001'"
        ).fetchone()[0] == "source"

    with sqlite3.connect(project / "project.sqlite") as database:
        database.execute("DROP TABLE migration_probe")
        database.commit()
    ensure_supported(project)
    with sqlite3.connect(project / "project.sqlite") as database:
        assert database.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "4"


def test_schema_version_rejects_non_numeric_value_as_project_error(
    tmp_path: Path,
) -> None:
    project, _file_record, _segment_record, _stage_record = create_v2_project(tmp_path)
    with sqlite3.connect(project / "project.sqlite") as database:
        database.execute(
            "UPDATE schema_meta SET value='future' WHERE key='schema_version'"
        )
        database.commit()

    with pytest.raises(ProjectError, match="schema_version.*future"):
        ensure_supported(project)


def test_schema_upgrade_backup_oserror_is_storage_error_with_project_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _file_record, _segment_record, _stage_record = create_v2_project(tmp_path)
    target = project / "snapshots" / "storage_migrations"
    original_mkdir = Path.mkdir

    def fail_backup_dir(path: Path, *args: object, **kwargs: object) -> None:
        if path == target:
            raise OSError("read-only")
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_backup_dir)

    with pytest.raises(StorageError, match=str(project)):
        ensure_supported(project)

    with sqlite3.connect(project / "project.sqlite") as database:
        assert database.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "2"


def test_v2_migrates_payloads_and_preserves_public_records(tmp_path: Path) -> None:
    project, file_record, segment_record, stage_record = create_v2_project(tmp_path)

    ensure_supported(project)

    with sqlite3.connect(project / "project.sqlite") as database:
        assert database.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "4"
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


def test_v3_run_payload_does_not_duplicate_sql_timestamps(tmp_path: Path) -> None:
    project = create_project(tmp_path)
    project_id = str(read_json(project, project / "project.json")["project_id"])
    run = record_header(
        "run",
        project_id,
        record_id="RUN-TEST",
        run_id="RUN-TEST",
        stage="translation",
        status="completed",
        started_at="2026-08-16T12:00:00+08:00",
        detail="kept",
    )
    manifest_path = project / "runs" / "RUN-TEST" / "manifest.json"

    write_json(project, manifest_path, run)

    with sqlite3.connect(project / "project.sqlite") as database:
        payload = json.loads(
            database.execute(
                "SELECT payload_json FROM runs WHERE run_id = 'RUN-TEST'"
            ).fetchone()[0]
        )
    assert payload == {"detail": "kept"}
    assert read_json(project, manifest_path)["created_at"] == run["started_at"]


def test_v3_payloads_keep_only_nonrelational_fields(tmp_path: Path) -> None:
    project = create_project(tmp_path)
    project_id = str(read_json(project, project / "project.json")["project_id"])
    file_id = str(read_files(project)[0]["file_id"])

    write_json(
        project,
        project / "source" / "adapters" / f"{file_id}.json",
        record_header(
            "document_adapter_state",
            project_id,
            record_id=f"DOCUMENT-{file_id}",
            file_id=file_id,
            state={"kept": True},
        ),
    )
    append_jsonl(
        project,
        project / "stages" / "translation.jsonl",
        record_header(
            "stage_result",
            project_id,
            stage="translation",
            segment_id=f"{file_id}-S000001",
            status="completed",
            text="translated",
        ),
    )
    append_jsonl(
        project,
        project / "terminology" / "scans.jsonl",
        record_header(
            "terminology_scan",
            project_id,
            stage="terminology",
            active_task_id="TERM-TASK-TEST",
            segment_id=f"{file_id}-S000001",
            status="completed",
            detail="scan detail",
        ),
    )
    append_jsonl(
        project,
        project / "terminology" / "candidates.jsonl",
        record_header(
            "terminology_candidates",
            project_id,
            stage="terminology",
            status="completed",
            active_task_id="TERM-TASK-TEST",
            terms=[{"source": "term"}],
        ),
    )
    write_json(
        project,
        project / "terminology" / "terms.json",
        record_header(
            "terminology_library",
            project_id,
            record_id="TERMS-TEST",
            terms=[{"source": "term"}],
        ),
    )
    write_json(
        project,
        project / "terminology" / "overrides.json",
        record_header(
            "terminology_overrides",
            project_id,
            record_id="OVERRIDES-TEST",
            overrides={"term": "译文"},
        ),
    )
    write_json(
        project,
        project / "terminology" / "active_task.json",
        record_header(
            "terminology_task",
            project_id,
            record_id="TERM-TASK-TEST",
            active_task_id="TERM-TASK-TEST",
            status="active",
        ),
    )
    run_id = "RUN-PAYLOAD-TEST"
    manifest_path = project / "runs" / run_id / "manifest.json"
    write_json(
        project,
        manifest_path,
        record_header(
            "run",
            project_id,
            record_id=run_id,
            run_id=run_id,
            stage="translation",
            status="running",
            started_at="2026-08-16T12:00:00+08:00",
        ),
    )
    append_jsonl(
        project,
        project / "runs" / run_id / "chunks.jsonl",
        record_header(
            "chunk_manifest",
            project_id,
            record_id="CHK-PAYLOAD-TEST",
            run_id=run_id,
            segments=[f"{file_id}-S000001"],
        ),
    )

    with sqlite3.connect(project / "project.sqlite") as database:
        payloads = {
            "files": json.loads(
                database.execute("SELECT payload_json FROM files").fetchone()[0]
            ),
            "adapter_states": json.loads(
                database.execute(
                    "SELECT payload_json FROM adapter_states"
                ).fetchone()[0]
            ),
            "stage_results": json.loads(
                database.execute("SELECT payload_json FROM stage_results").fetchone()[0]
            ),
            "terminology_scans": json.loads(
                database.execute(
                    "SELECT payload_json FROM terminology_scans"
                ).fetchone()[0]
            ),
            "terminology_candidates": json.loads(
                database.execute(
                    "SELECT payload_json FROM terminology_candidates"
                ).fetchone()[0]
            ),
            "terms": json.loads(
                database.execute(
                    "SELECT payload_json FROM terms_state WHERE key='terms'"
                ).fetchone()[0]
            ),
            "overrides": json.loads(
                database.execute(
                    "SELECT payload_json FROM terms_state WHERE key='overrides'"
                ).fetchone()[0]
            ),
            "active_task": json.loads(
                database.execute(
                    "SELECT payload_json FROM terms_state WHERE key='active_task'"
                ).fetchone()[0]
            ),
            "runs": json.loads(
                database.execute("SELECT payload_json FROM runs").fetchone()[0]
            ),
            "run_chunks": json.loads(
                database.execute("SELECT payload_json FROM run_chunks").fetchone()[0]
            ),
        }

    for kind in (
        "files",
        "adapter_states",
        "stage_results",
        "terminology_scans",
        "terminology_candidates",
        "runs",
        "run_chunks",
    ):
        assert "schema_version" not in payloads[kind]
        assert "record_type" not in payloads[kind]
        assert "project_id" not in payloads[kind]
    assert "record_id" not in payloads["files"]
    assert "file_id" not in payloads["files"]
    assert "stage" not in payloads["stage_results"]
    assert "run_id" not in payloads["runs"]
    assert payloads["stage_results"]["text"] == "translated"
    assert payloads["adapter_states"]["state"] == {"kept": True}
    for kind in ("terms", "overrides", "active_task"):
        assert "schema_version" not in payloads[kind]
        assert "record_type" not in payloads[kind]
        assert "project_id" not in payloads[kind]
    assert payloads["terms"]["record_id"] == "TERMS-TEST"


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
        assert version == "4"
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
        item["segment_id"]
        for item in query_segments(
            project, file_id="F0002", part_id="document"
        )
    ] == ["F0002-S000001"]


def test_segment_queries_filter_by_file_and_part_pair(tmp_path: Path) -> None:
    project = create_project(tmp_path, "first\nsecond\nthird")
    connection = sqlite3.connect(project / "project.sqlite")
    try:
        connection.executemany(
            "UPDATE segments SET part_id = ? WHERE segment_id = ?",
            [
                ("chapter-1", "F0001-S000001"),
                ("chapter-2", "F0001-S000002"),
                ("chapter-1", "F0001-S000003"),
            ],
        )
        connection.commit()
    finally:
        connection.close()

    assert segment_count(project, file_id="F0001", part_id="chapter-1") == 2
    assert [
        item["segment_id"]
        for item in query_segments(
            project, file_id="F0001", part_id="chapter-2"
        )
    ] == ["F0001-S000002"]
    assert segment_ids(project, file_id="F0001", part_id="chapter-1") == [
        "F0001-S000001",
        "F0001-S000003",
    ]


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
    assert sources[1]["part_id"] == "document"


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
