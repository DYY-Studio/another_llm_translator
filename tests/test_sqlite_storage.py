from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
import zlib
from pathlib import Path

import pytest

from app.errors import ProjectError, StorageError
from app.execution import stage_result_path
from app.project import init_project
from app.sqlite_storage import (
    SCHEMA_VERSION,
    append_jsonl,
    compact_project_database,
    ensure_supported,
    latest_stage_summary,
    mark_content_summaries_source_changed,
    publish_content_summary_fulls,
    query_segments,
    read_content_summaries,
    read_adapter_state,
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


def _adapter_payload_json(raw: str | bytes) -> str:
    return zlib.decompress(raw).decode("utf-8") if isinstance(raw, bytes) else raw


def _seed_fingerprint_records(project: Path, *, same_scan_fingerprint: bool = True) -> tuple[dict, dict]:
    project_id = read_json(project, project / "project.json")["project_id"]
    fingerprint = digest("run settings")
    write_json(project, project / "runs" / "RUN-SHARED" / "manifest.json", record_header(
        "run", project_id, record_id="RUN-SHARED", run_id="RUN-SHARED",
        stage="translation", status="completed", stage_fingerprint=fingerprint,
    ))
    stage = record_header(
        "stage_result", project_id, record_id="RESULT-SHARED", stage="translation",
        segment_id="F0001-S000001", status="completed", text="译文",
        run_id="RUN-SHARED", request_id="REQ-SHARED", stage_fingerprint=fingerprint,
    )
    scan = record_header(
        "terminology_scan", project_id, record_id="SCAN-SHARED", stage="terminology",
        segment_id="F0001-S000001", status="completed", active_task_id="TASK-SHARED",
        run_id="RUN-SHARED", request_id="REQ-SHARED", stage_fingerprint=fingerprint if same_scan_fingerprint else digest("other settings"),
    )
    scan["created_at"] = stage["created_at"]
    append_jsonl(project, project / "stages" / "translation.jsonl", stage)
    append_jsonl(project, project / "terminology" / "scans.jsonl", scan)
    return stage, scan


@pytest.mark.parametrize("same_scan_fingerprint", [False, True])
def test_request_metadata_sharing_preserves_records_and_progress(
    tmp_path: Path, same_scan_fingerprint: bool,
) -> None:
    from app.sqlite_storage import latest_stage_results, latest_stage_states, terminology_scan_state

    project = create_project(tmp_path)
    stage, scan = _seed_fingerprint_records(project, same_scan_fingerprint=same_scan_fingerprint)
    ids = [stage["segment_id"]]
    assert read_jsonl(project, project / "stages" / "translation.jsonl") == [stage]
    assert read_jsonl(project, project / "terminology" / "scans.jsonl") == [scan]
    assert latest_stage_results(project, "translation", ids)[ids[0]] == stage
    assert latest_stage_states(project, "translation", ids)[ids[0]]["completed"] == stage
    assert latest_stage_summary(project, "translation", ids)[ids[0]]["stage_fingerprint"] == stage["stage_fingerprint"]
    assert terminology_scan_state(project, "TASK-SHARED", ids) == (set(ids), {scan["stage_fingerprint"]})
    with sqlite3.connect(project / "project.sqlite") as database:
        payload = json.loads(database.execute("SELECT payload_json FROM stage_results").fetchone()[0])
        reference = payload["_request_meta"]
        assert not ({"run_id", "request_id", "created_at", "stage_fingerprint"} & payload.keys())
        payload = json.loads(database.execute("SELECT payload_json FROM terminology_scans").fetchone()[0])
        assert (payload["_request_meta"] == reference) == same_scan_fingerprint
        assert database.execute("SELECT count(*) FROM request_metadata").fetchone()[0] == (1 if same_scan_fingerprint else 2)


def test_v6_migration_shares_request_metadata_without_changing_records(tmp_path: Path) -> None:
    from app import sqlite_storage

    project = create_project(tmp_path)
    stage, scan = _seed_fingerprint_records(project)
    with sqlite3.connect(project / "project.sqlite") as database:
        for table, record in [("stage_results", stage), ("terminology_scans", scan)]:
            payload = json.loads(database.execute(f"SELECT payload_json FROM {table}").fetchone()[0])
            payload.pop("_request_meta", None)
            payload.update({key:record[key] for key in ("run_id","request_id","created_at","stage_fingerprint")})
            database.execute(f"UPDATE {table} SET payload_json=?", (json.dumps(payload),))
        database.execute("DROP TABLE request_metadata")
        database.execute("UPDATE schema_meta SET value='6' WHERE key='schema_version'")
    sqlite_storage._SUPPORTED_CACHE.discard(project / "project.sqlite")
    backup = ensure_supported(project)
    assert backup is not None
    assert read_jsonl(project, project / "stages" / "translation.jsonl") == [stage]
    assert read_jsonl(project, project / "terminology" / "scans.jsonl") == [scan]
    with sqlite3.connect(project / "project.sqlite") as database:
        assert database.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "7"
        for table in ("stage_results", "terminology_scans"):
            payload = json.loads(database.execute(f"SELECT payload_json FROM {table}").fetchone()[0])
            assert payload["_request_meta"] > 0
        assert database.execute("SELECT count(*) FROM request_metadata").fetchone()[0] == 1


def test_dangling_request_metadata_reference_fails_explicitly(tmp_path: Path) -> None:
    project = create_project(tmp_path)
    _seed_fingerprint_records(project)
    with sqlite3.connect(project / "project.sqlite") as database:
        database.execute("DELETE FROM request_metadata")
    with pytest.raises(StorageError, match="请求元数据引用"):
        read_jsonl(project, project / "stages" / "translation.jsonl")


def test_request_metadata_cleanup_preserves_other_references(tmp_path: Path) -> None:
    project = create_project(tmp_path)
    _seed_fingerprint_records(project)
    with sqlite3.connect(project / "project.sqlite") as database:
        database.execute("DELETE FROM stage_results")
        assert database.execute("SELECT count(*) FROM request_metadata").fetchone()[0] == 1
        database.execute("DELETE FROM terminology_scans")
        assert database.execute("SELECT count(*) FROM request_metadata").fetchone()[0] == 0


def test_request_metadata_preserves_missing_and_null_fields(tmp_path: Path) -> None:
    project = create_project(tmp_path)
    stage, scan = _seed_fingerprint_records(project)
    missing = {**scan, "record_id":"SCAN-MISSING"}
    missing.pop("request_id")
    explicit_null = {**missing, "record_id":"SCAN-NULL", "request_id":None}
    different_time = {**scan, "record_id":"SCAN-TIME", "created_at":"2026-10-11T12:00:00+08:00"}
    for record in (missing, explicit_null, different_time):
        append_jsonl(project, project / "terminology" / "scans.jsonl", record)
    assert read_jsonl(project, project / "terminology" / "scans.jsonl") == [scan, missing, explicit_null, different_time]
    with sqlite3.connect(project / "project.sqlite") as database:
        assert database.execute("SELECT count(*) FROM request_metadata").fetchone()[0] == 4


def test_request_metadata_insert_rolls_back_with_failed_record(tmp_path: Path) -> None:
    project = create_project(tmp_path)
    stage, scan = _seed_fingerprint_records(project)
    duplicate = {**stage, "request_id":"REQ-NEW"}
    with pytest.raises(StorageError):
        append_jsonl(project, project / "stages" / "translation.jsonl", duplicate)
    assert read_jsonl(project, project / "stages" / "translation.jsonl") == [stage]
    with sqlite3.connect(project / "project.sqlite") as database:
        assert database.execute("SELECT count(*) FROM request_metadata").fetchone()[0] == 1


@pytest.mark.parametrize("query", ["records", "summary"])
def test_request_metadata_reads_share_a_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, query: str) -> None:
    from app import sqlite_storage

    project = create_project(tmp_path)
    stage, scan = _seed_fingerprint_records(project)
    original = sqlite_storage._request_metadata

    def remove_records_before_metadata_read(connection):
        with sqlite3.connect(project / "project.sqlite") as writer:
            writer.execute("DELETE FROM stage_results")
            writer.execute("DELETE FROM terminology_scans")
        return original(connection)

    monkeypatch.setattr(sqlite_storage, "_request_metadata", remove_records_before_metadata_read)
    if query == "records":
        assert read_jsonl(project, project / "stages" / "translation.jsonl") == [stage]
    else:
        assert latest_stage_summary(project, "translation", [stage["segment_id"]])[stage["segment_id"]]["stage_fingerprint"] == stage["stage_fingerprint"]


def test_v6_adapter_state_compresses_on_open_preserving_history(
    tmp_path: Path,
) -> None:
    from app import sqlite_storage
    from tests.test_documents import init_epub

    project = init_epub(tmp_path)
    version = 6
    expected = read_adapter_state(project, "F0001")
    with sqlite3.connect(project / "project.sqlite") as database:
        raw = _adapter_payload_json(database.execute("SELECT payload_json FROM adapter_states").fetchone()[0])
        database.execute("UPDATE adapter_states SET payload_json = ?", (raw,))
        database.execute("UPDATE schema_meta SET value = ? WHERE key='schema_version'", (str(version),))
    sqlite_storage._SUPPORTED_CACHE.discard(project / "project.sqlite")
    backup = ensure_supported(project)
    assert backup is not None
    with sqlite3.connect(backup) as database:
        assert database.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == str(version)
        assert database.execute("SELECT payload_json FROM adapter_states").fetchone()[0] == raw
    with sqlite3.connect(project / "project.sqlite") as database:
        packed = database.execute("SELECT payload_json FROM adapter_states").fetchone()[0]
        assert isinstance(packed, bytes)
        assert zlib.decompress(packed).decode("utf-8") == raw
        assert database.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "7"
    assert read_adapter_state(project, "F0001") == expected
    assert sqlite_storage._take_migration_backup_notice(project) is None
    assert ensure_supported(project) is None
    assert list((project / "snapshots" / "storage_migrations").glob("*.sqlite")) == [backup]


def test_adapter_state_writes_and_source_replacement_keep_compressed_storage(tmp_path: Path) -> None:
    from tests.test_documents import init_epub

    project = init_epub(tmp_path)
    state = read_adapter_state(project, "F0001")
    assert state is not None
    state["state"]["kept"] = {"text": "原文🙂", "values": [None, False, 1]}
    write_json(project, project / "source" / "adapters" / "F0001.json", state)
    replace_source(project, read_files(project), read_segments(project), read_json(project, project / "project.json"), adapter_states=[state])
    assert read_adapter_state(project, "F0001") == state
    with sqlite3.connect(project / "project.sqlite") as database:
        assert database.execute("SELECT typeof(payload_json) FROM adapter_states").fetchone()[0] == "blob"


@pytest.mark.parametrize("payload", [b"broken", zlib.compress(b"\xff"), zlib.compress(b"not json")])
def test_corrupt_compressed_adapter_state_fails_explicitly(tmp_path: Path, payload: bytes) -> None:
    from tests.test_documents import init_epub

    project = init_epub(tmp_path)
    with sqlite3.connect(project / "project.sqlite") as database:
        database.execute("UPDATE adapter_states SET payload_json = ?", (payload,))
    with pytest.raises(StorageError, match="损坏"):
        read_adapter_state(project, "F0001")


def test_invalid_adapter_json_rolls_back_v6_migration(tmp_path: Path) -> None:
    from app import sqlite_storage
    from tests.test_documents import init_epub

    project = init_epub(tmp_path)
    with sqlite3.connect(project / "project.sqlite") as database:
        database.execute("UPDATE adapter_states SET payload_json = 'not json'")
        database.execute("UPDATE schema_meta SET value = '6' WHERE key='schema_version'")
    sqlite_storage._SUPPORTED_CACHE.discard(project / "project.sqlite")
    with pytest.raises(StorageError, match="损坏"):
        ensure_supported(project)
    with sqlite3.connect(project / "project.sqlite") as database:
        assert database.execute("SELECT payload_json FROM adapter_states").fetchone()[0] == "not json"
        assert database.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "6"
    assert len(list((project / "snapshots" / "storage_migrations").glob("*.sqlite"))) == 1


def _text_summary(project: Path, summary_id: str = "TEXT-FRAGMENT", *, same_model_text: bool = False) -> dict:
    project_id = read_json(project, project / "project.json")["project_id"]
    source = "生成时原文"
    model_text = source if same_model_text else "模型输入"
    values = [{
        "segment_id": "F0001-S000001", "original_segment_id": "F0001-S000001",
        "slice_index": 0, "source": source, "source_digest": digest(source),
        "original_source_digest": digest(source), "model_text": model_text,
        "model_text_digest": digest(model_text),
        "original_model_text_digest": digest(model_text),
        "slice_id": f"F0001-S000001#slice-0000-{digest(source)[7:15]}-{digest(model_text)[7:15]}",
    }]
    return record_header(
        "content_summary", project_id, record_id=summary_id, kind="fragment",
        file_id="F0001", part_id="document", status="completed", text="摘要",
        source_range={"file_id": "F0001", "part_id": "document",
                      "segment_ids": ["F0001-S000001"], "segments": values},
        source_digest=digest(values), input_digest=digest(model_text),
        prompt_digest="sha256:prompt", model="test-model",
    )


@pytest.mark.parametrize("same_model_text", [False, True])
def test_summary_source_snapshots_deduplicate_and_restore_historical_text(tmp_path: Path, same_model_text: bool) -> None:
    project = create_project(tmp_path)
    summary = _text_summary(project, same_model_text=same_model_text)
    write_content_summary(project, summary)
    full = {**summary, "record_id": "TEXT-FULL", "kind": "full"}
    _attach_provenance(full, "adopted_fragment", [summary])
    publish_content_summary_fulls(project, [full])
    with sqlite3.connect(project / "project.sqlite") as database:
        assert database.execute("SELECT COUNT(*) FROM summary_source_texts").fetchone()[0] == (1 if same_model_text else 2)
        packed = json.loads(database.execute(
            "SELECT source_range_json FROM content_summaries LIMIT 1"
        ).fetchone()[0])
        expected_keys = {"segment_id", "slice_index", "source_digest"}
        if not same_model_text:
            expected_keys.add("model_text_digest")
        assert set(packed["segments"][0]) == expected_keys
        assert "segment_ids" not in packed
    segments = read_segments(project)
    segments[0]["source"] = "变更后的原文"
    replace_source(project, read_files(project), segments, read_json(project, project / "project.json"))
    assert read_content_summaries(project, kind="fragment")[0]["source_range"] == summary["source_range"]
    with sqlite3.connect(project / "project.sqlite") as database:
        database.execute("DELETE FROM summary_source_texts WHERE digest = ?", (summary["source_range"]["segments"][0]["model_text_digest"],))
    with pytest.raises(StorageError, match="概括源文本快照缺失"):
        read_content_summaries(project)


def _seed_v6_summary(project: Path, *, same_model_text: bool = True) -> dict:
    summary = _text_summary(project, same_model_text=same_model_text)
    write_content_summary(project, summary)
    packed = json.loads(json.dumps(summary["source_range"]))
    packed.pop("segment_ids")
    for value in packed["segments"]:
        for key in ("source", "model_text", "original_segment_id", "slice_id"):
            value.pop(key)
    with sqlite3.connect(project / "project.sqlite") as database:
        database.execute("UPDATE schema_meta SET value = '6' WHERE key = 'schema_version'")
        database.execute(
            "UPDATE content_summaries SET source_range_json = ? WHERE summary_id = ?",
            (json.dumps(packed, ensure_ascii=False), summary["record_id"]),
        )
    from app.sqlite_storage import _SUPPORTED_CACHE
    _SUPPORTED_CACHE.discard(project / "project.sqlite")
    return summary


@pytest.mark.parametrize("same_model_text", [False, True])
def test_v6_summary_upgrade_deduplicates_hashes_without_changing_history(
    tmp_path: Path, same_model_text: bool,
) -> None:
    from app.sqlite_storage import _take_migration_backup_notice

    project = create_project(tmp_path)
    summary = _seed_v6_summary(project, same_model_text=same_model_text)
    backup = ensure_supported(project)
    assert backup is not None
    with sqlite3.connect(backup) as database:
        assert database.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "6"
        assert "original_source_digest" in json.loads(database.execute(
            "SELECT source_range_json FROM content_summaries"
        ).fetchone()[0])["segments"][0]
    restored = read_content_summaries(project)[0]
    for key in ("record_id", "source_range", "source_digest", "input_digest", "text", "status"):
        assert restored[key] == summary[key]
    with sqlite3.connect(project / "project.sqlite") as database:
        assert database.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == str(SCHEMA_VERSION)
        value = json.loads(database.execute("SELECT source_range_json FROM content_summaries").fetchone()[0])["segments"][0]
        assert "original_source_digest" not in value
        assert "original_model_text_digest" not in value
        assert ("model_text_digest" not in value) == same_model_text
    assert _take_migration_backup_notice(project) is None
    assert ensure_supported(project) is None
    assert list((project / "snapshots" / "storage_migrations").glob("*.sqlite")) == [backup]


def test_summary_hash_dedup_preserves_distinct_original_hashes(tmp_path: Path) -> None:
    project = create_project(tmp_path)
    summary = _text_summary(project)
    value = summary["source_range"]["segments"][0]
    value["slice_index"] = 1
    value["original_source_digest"] = digest("完整原文")
    value["original_model_text_digest"] = digest("完整模型输入")
    value["slice_id"] = f"F0001-S000001#slice-0001-{value['source_digest'][7:15]}-{value['model_text_digest'][7:15]}"
    summary["source_digest"] = digest(summary["source_range"]["segments"])
    write_content_summary(project, summary)
    assert read_content_summaries(project)[0]["source_range"] == summary["source_range"]
    with sqlite3.connect(project / "project.sqlite") as database:
        packed = json.loads(database.execute("SELECT source_range_json FROM content_summaries").fetchone()[0])["segments"][0]
        for key in ("source_digest", "model_text_digest", "original_source_digest", "original_model_text_digest"):
            assert packed[key] == value[key]


def test_v6_summary_upgrade_failure_preserves_schema_and_ranges(tmp_path: Path) -> None:
    project = create_project(tmp_path)
    _seed_v6_summary(project)
    with sqlite3.connect(project / "project.sqlite") as database:
        before = database.execute("SELECT source_range_json FROM content_summaries").fetchone()[0]
        database.execute("DELETE FROM summary_source_texts")
    with pytest.raises(StorageError, match="概括源文本快照缺失"):
        ensure_supported(project)
    with sqlite3.connect(project / "project.sqlite") as database:
        assert database.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "6"
        assert database.execute("SELECT source_range_json FROM content_summaries").fetchone()[0] == before


def _seed_v5_summary(project: Path, *, corrupt: bool = False, legacy: bool = False) -> dict:
    summary = _text_summary(project)
    value = dict(summary)
    full = {**summary, "record_id": "TEXT-FULL", "kind": "full"}
    # Build the old payload explicitly; new producers no longer duplicate ranges.
    _attach_provenance(full, "adopted_fragment", [summary])
    full["provenance"]["source_ranges"] = [summary["source_range"]]
    for dependency in full["provenance"]["dependencies"]:
        dependency.pop("source_range_digest")
    full["input_digest"] = digest(full["provenance"]["dependencies"])
    if legacy:
        full["provenance"].pop("dependencies")
        full["input_digest"] = "sha256:historical-input"
    with sqlite3.connect(project / "project.sqlite") as database:
        database.execute("UPDATE schema_meta SET value = '5' WHERE key = 'schema_version'")
        database.execute("DROP TABLE IF EXISTS summary_source_texts")
        for record in (value, full):
            residual = {"provenance": record["provenance"]} if "provenance" in record else {}
            database.execute(
                "INSERT INTO content_summaries(summary_id, kind, file_id, part_id, status, text, "
                "source_range_json, source_digest, input_digest, prompt_digest, model, run_id, "
                "source_changed, created_at, updated_at, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record["record_id"], record["kind"], "F0001", "document", "completed", "摘要",
                 "bad json" if corrupt else json.dumps(record["source_range"], ensure_ascii=False),
                 record["source_digest"], record["input_digest"], "sha256:prompt", "test-model",
                 None, 0, record["created_at"], record["created_at"], json.dumps(residual, ensure_ascii=False)),
            )
        database.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?)",
            ("RUN-HISTORY", "content_summary", "interrupted", None,
             json.dumps({"summary_boundaries": [summary["source_range"]], "usage": {"total_tokens": 123}})),
        )
        database.execute(
            "INSERT INTO summary_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("RUN-HISTORY", "aggregation", "interrupted", json.dumps([summary["source_range"]]),
             "sha256:input", "sha256:prompt", "test-model", None, summary["created_at"], "{}"),
        )
    from app.sqlite_storage import _SUPPORTED_CACHE
    _SUPPORTED_CACHE.discard(project / "project.sqlite")
    manifest = project / "runs" / "RUN-HISTORY" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"summary_boundaries": [summary["source_range"]]}))
    return summary


@pytest.mark.parametrize("legacy", [False, True])
def test_v5_summary_upgrade_preserves_history_and_compacts_once(tmp_path: Path, legacy: bool) -> None:
    project = create_project(tmp_path)
    summary = _seed_v5_summary(project, legacy=legacy)
    backup = ensure_supported(project)
    assert backup is not None
    with sqlite3.connect(backup) as database:
        assert database.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "5"
    assert read_content_summaries(project, kind="fragment")[0]["source_range"] == summary["source_range"]
    full = read_content_summaries(project, kind="full")[0]
    assert "source_ranges" not in full["provenance"]
    assert full["input_digest"] == digest(full["provenance"]["dependencies"])
    run = read_json(project, project / "runs" / "RUN-HISTORY" / "manifest.json")
    assert run["summary_boundaries"] == [{"file_id": "F0001", "part_id": "document"}]
    assert run["usage"] == {"total_tokens": 123}
    manifest = json.loads((project / "runs" / "RUN-HISTORY" / "manifest.json").read_text())
    assert manifest["summary_boundaries"] == run["summary_boundaries"]
    from app.sqlite_storage import _take_migration_backup_notice
    assert _take_migration_backup_notice(project) is None
    assert read_summary_runs(project)[0]["source_ranges"] == [{
        "file_id": "F0001", "part_id": "document", "segment_ids": ["F0001-S000001"],
    }]
    with sqlite3.connect(project / "project.sqlite") as database:
        assert database.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == str(SCHEMA_VERSION)
        assert database.execute("PRAGMA freelist_count").fetchone()[0] == 0
    assert ensure_supported(project) is None


def test_v5_summary_upgrade_rolls_back_and_keeps_backup_on_corrupt_range(tmp_path: Path) -> None:
    project = create_project(tmp_path)
    _seed_v5_summary(project, corrupt=True)
    with pytest.raises(StorageError):
        ensure_supported(project)
    with sqlite3.connect(project / "project.sqlite") as database:
        assert database.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "5"
        assert database.execute("SELECT COUNT(*) FROM content_summaries").fetchone()[0] == 2
    assert len(list((project / "snapshots" / "storage_migrations").glob("*.sqlite"))) == 1


def test_summary_write_reclaims_only_unreferenced_source_snapshots(tmp_path: Path) -> None:
    project = create_project(tmp_path)
    summary = _text_summary(project)
    write_content_summary(project, summary)
    changed = json.loads(json.dumps(summary))
    value = changed["source_range"]["segments"][0]
    value["source"] = "替换的源文"
    value["source_digest"] = digest(value["source"])
    value["original_source_digest"] = value["source_digest"]
    changed["source_digest"] = digest(changed["source_range"]["segments"])
    write_content_summary(project, changed)
    with sqlite3.connect(project / "project.sqlite") as database:
        assert {row[0] for row in database.execute("SELECT text FROM summary_source_texts")} == {"替换的源文", "模型输入"}


def test_summary_write_rolls_back_source_snapshots_when_digest_is_invalid(tmp_path: Path) -> None:
    project = create_project(tmp_path)
    summary = _text_summary(project)
    summary["source_range"]["segments"][0]["model_text_digest"] = "sha256:wrong"
    with pytest.raises(StorageError, match="model_text_digest"):
        write_content_summary(project, summary)
    with sqlite3.connect(project / "project.sqlite") as database:
        assert database.execute("SELECT COUNT(*) FROM summary_source_texts").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM content_summaries").fetchone()[0] == 0


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


@pytest.mark.parametrize("version", [3, 4])
def test_v3_and_v4_upgrade_rebuild_epub_state_and_interrupt_runs(
    tmp_path: Path, version: int
) -> None:
    from tests.test_documents import init_epub

    project = init_epub(tmp_path)
    with sqlite3.connect(project / "project.sqlite") as database:
        database.row_factory = sqlite3.Row
        file_row = database.execute(
            "SELECT file_id, payload_json FROM files"
        ).fetchone()
        assert file_row is not None
        file_payload = json.loads(str(file_row["payload_json"]))
        file_payload["document_adapter_version"] = "0.5"
        state_row = database.execute(
            "SELECT payload_json FROM adapter_states WHERE file_id = ?",
            (str(file_row["file_id"]),),
        ).fetchone()
        assert state_row is not None
        state_payload = json.loads(_adapter_payload_json(state_row["payload_json"]))
        state_payload["adapter_version"] = "0.5"
        state_payload.pop("run_options", None)
        state_payload["state"].update(
            ruby_mode="compact",
            inline_format_mode="markers",
            inline_format_policy="strict",
        )
        if version == 4:
            state_payload["run_options"] = {
                "ruby_mode": "short_xml",
                "inline_format_mode": "plain",
                "inline_format_policy": "tiered",
            }
        segment_ids = [
            str(row[0])
            for row in database.execute(
                "SELECT segment_id FROM segments ORDER BY line_index"
            )
        ]
        database.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
            (str(version),),
        )
        database.execute(
            "UPDATE files SET payload_json = ? WHERE file_id = ?",
            (json.dumps(file_payload), str(file_row["file_id"])),
        )
        database.execute(
            "UPDATE adapter_states SET payload_json = ? WHERE file_id = ?",
            (json.dumps(state_payload), str(file_row["file_id"])),
        )
        if version == 3:
            database.execute("DROP TABLE summary_runs")
            database.execute("DROP TABLE content_summaries")
            database.execute("DROP TABLE summary_participation")
        else:
            database.execute(
                "INSERT INTO summary_runs("
                "run_id, mode, status, source_ranges_json, input_digest, "
                "prompt_digest, model, started_at, updated_at, payload_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "SUMMARY-MIGRATION",
                    "fragment",
                    "running",
                    "[]",
                    "sha256:input",
                    "sha256:prompt",
                    "test-model",
                    "2026-09-15T00:00:00+00:00",
                    "2026-09-15T00:00:00+00:00",
                    json.dumps({"run_id": "SUMMARY-MIGRATION", "status": "running"}),
                ),
            )
        database.execute(
            "INSERT INTO runs(run_id, stage, status, started_at, payload_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "RUN-MIGRATION",
                "translation",
                "running",
                "2026-09-15T00:00:00+00:00",
                json.dumps({"run_id": "RUN-MIGRATION", "status": "running"}),
            ),
        )
        database.commit()

    backup_path = ensure_supported(project)

    assert backup_path is not None and backup_path.is_file()
    with sqlite3.connect(backup_path) as backup:
        assert backup.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == str(version)
        assert [
            str(row[0])
            for row in backup.execute(
                "SELECT segment_id FROM segments ORDER BY line_index"
            )
        ] == segment_ids
    with sqlite3.connect(project / "project.sqlite") as database:
        database.row_factory = sqlite3.Row
        assert database.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == str(SCHEMA_VERSION)
        file_payload = json.loads(
            database.execute("SELECT payload_json FROM files").fetchone()[0]
        )
        state_payload = json.loads(
            _adapter_payload_json(database.execute("SELECT payload_json FROM adapter_states").fetchone()[0])
        )
        assert file_payload["document_adapter_version"] == "0.6"
        assert state_payload["adapter_version"] == "0.6"
        assert state_payload["run_options"] == (
            {
                "ruby_mode": "compact",
                "inline_format_mode": "markers",
                "inline_format_policy": "strict",
            }
            if version == 3
            else {
                "ruby_mode": "short_xml",
                "inline_format_mode": "plain",
                "inline_format_policy": "tiered",
            }
        )
        assert state_payload["state"].get("ruby_mode") != "compact"
        assert [
            str(row[0])
            for row in database.execute(
                "SELECT segment_id FROM segments ORDER BY line_index"
            )
        ] == segment_ids
        run = database.execute(
            "SELECT status, payload_json FROM runs WHERE run_id = 'RUN-MIGRATION'"
        ).fetchone()
        assert run["status"] == "interrupted"
        assert json.loads(str(run["payload_json"]))["error_message"] == (
            "Document Adapter 运行协议已升级、必须新建 Run"
        )
        if version == 3:
            assert database.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'summary_runs'"
            ).fetchone() is not None
        else:
            summary_run = database.execute(
                "SELECT status, payload_json FROM summary_runs "
                "WHERE run_id = 'SUMMARY-MIGRATION'"
            ).fetchone()
            assert summary_run["status"] == "interrupted"
            assert json.loads(str(summary_run["payload_json"]))["error_message"] == (
                "Document Adapter 运行协议已升级、必须新建 Run"
            )


@pytest.mark.parametrize(
    ("ruby_mode", "outer_options", "should_fail"),
    [
        ("compact", {"inline_format_mode": "markers"}, False),
        (None, {"inline_format_mode": "markers"}, True),
        ("compact", {"ruby_mode": "invalid"}, True),
    ],
)
def test_v4_epub_upgrade_defaults_missing_options_and_rejects_invalid_values(
    tmp_path: Path,
    ruby_mode: str | None,
    outer_options: dict[str, str],
    should_fail: bool,
) -> None:
    from tests.test_documents import init_epub

    project = init_epub(tmp_path)
    with sqlite3.connect(project / "project.sqlite") as database:
        state_payload = json.loads(
            _adapter_payload_json(database.execute("SELECT payload_json FROM adapter_states").fetchone()[0])
        )
        state_payload["adapter_version"] = "0.5"
        state_payload.pop("run_options", None)
        state_payload["state"]["ruby_mode"] = ruby_mode
        state_payload["state"].pop("inline_format_policy", None)
        state_payload["run_options"] = outer_options
        database.execute("UPDATE schema_meta SET value = '4' WHERE key = 'schema_version'")
        database.execute(
            "UPDATE adapter_states SET payload_json = ?",
            (json.dumps(state_payload),),
        )
        database.commit()

    if should_fail:
        with pytest.raises(StorageError, match="ruby_mode"):
            ensure_supported(project)
        with sqlite3.connect(project / "project.sqlite") as database:
            assert database.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0] == "4"
        backups = list((project / "snapshots" / "storage_migrations").glob("*.sqlite"))
        assert len(backups) == 1
        with sqlite3.connect(backups[0]) as backup:
            assert backup.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0] == "4"
    else:
        ensure_supported(project)
        with sqlite3.connect(project / "project.sqlite") as database:
            options = json.loads(
                _adapter_payload_json(database.execute("SELECT payload_json FROM adapter_states").fetchone()[0])
            )["run_options"]
        assert options == {
            "ruby_mode": "compact",
            "inline_format_mode": "markers",
            "inline_format_policy": "tiered",
        }


@pytest.mark.parametrize("version", [1, 2])
def test_v1_and_v2_projects_are_rejected_without_mutation(
    tmp_path: Path, version: int
) -> None:
    project, _file_record, _segment_record, _stage_record = create_v2_project(tmp_path)
    with sqlite3.connect(project / "project.sqlite") as database:
        database.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
            (str(version),),
        )
        database.commit()

    with pytest.raises(ProjectError, match="schema_version.*重新创建项目"):
        ensure_supported(project)

    assert not (project / "snapshots" / "storage_migrations").exists()
    with sqlite3.connect(project / "project.sqlite") as database:
        assert database.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == str(version)


def test_epub_upgrade_failure_rolls_back_and_keeps_backup(tmp_path: Path) -> None:
    from tests.test_documents import init_epub

    project = init_epub(tmp_path)
    with sqlite3.connect(project / "project.sqlite") as database:
        database.row_factory = sqlite3.Row
        file_row = database.execute(
            "SELECT file_id, payload_json FROM files"
        ).fetchone()
        assert file_row is not None
        file_payload = json.loads(str(file_row["payload_json"]))
        file_payload["document_adapter_version"] = "0.5"
        state_payload = json.loads(
            _adapter_payload_json(database.execute(
                "SELECT payload_json FROM adapter_states WHERE file_id = ?",
                (str(file_row["file_id"]),),
            ).fetchone()[0])
        )
        state_payload["adapter_version"] = "0.5"
        database.execute(
            "UPDATE schema_meta SET value = '4' WHERE key = 'schema_version'"
        )
        database.execute(
            "UPDATE files SET payload_json = ? WHERE file_id = ?",
            (json.dumps(file_payload), str(file_row["file_id"])),
        )
        database.execute(
            "UPDATE adapter_states SET payload_json = ? WHERE file_id = ?",
            (json.dumps(state_payload), str(file_row["file_id"])),
        )
        database.execute(
            "UPDATE segments SET part_id = 'missing.xhtml' WHERE file_id = ?",
            (str(file_row["file_id"]),),
        )
        database.commit()

    with pytest.raises(StorageError, match="Segment 定位"):
        ensure_supported(project)

    backups = list((project / "snapshots" / "storage_migrations").glob("*.sqlite"))
    assert len(backups) == 1
    with sqlite3.connect(project / "project.sqlite") as database:
        assert database.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "4"
        assert database.execute(
            "SELECT part_id FROM segments"
        ).fetchone()[0] == "missing.xhtml"
        assert json.loads(
            database.execute("SELECT payload_json FROM files").fetchone()[0]
        )["document_adapter_version"] == "0.5"


def _downgrade_epub_project_to_v4(project: Path) -> None:
    with sqlite3.connect(project / "project.sqlite") as database:
        database.row_factory = sqlite3.Row
        file_row = database.execute(
            "SELECT file_id, payload_json FROM files"
        ).fetchone()
        assert file_row is not None
        file_payload = json.loads(str(file_row["payload_json"]))
        file_payload["document_adapter_version"] = "0.5"
        state_row = database.execute(
            "SELECT payload_json FROM adapter_states WHERE file_id = ?",
            (str(file_row["file_id"]),),
        ).fetchone()
        assert state_row is not None
        state_payload = json.loads(_adapter_payload_json(state_row["payload_json"]))
        state_payload["adapter_version"] = "0.5"
        database.execute(
            "UPDATE schema_meta SET value = '4' WHERE key = 'schema_version'"
        )
        database.execute(
            "UPDATE files SET payload_json = ? WHERE file_id = ?",
            (json.dumps(file_payload, ensure_ascii=False), str(file_row["file_id"])),
        )
        database.execute(
            "UPDATE adapter_states SET payload_json = ? WHERE file_id = ?",
            (json.dumps(state_payload, ensure_ascii=False), str(file_row["file_id"])),
        )
        database.commit()

    from app import sqlite_storage

    sqlite_storage._SUPPORTED_CACHE.discard(sqlite_storage.database_path(project))


def _migration_history_rows(project: Path) -> dict[str, list[tuple[object, ...]]]:
    with sqlite3.connect(project / "project.sqlite") as database:
        return {
            table: [
                tuple(row)
                for row in database.execute(
                    f"SELECT * FROM {table} ORDER BY rowid"
                ).fetchall()
            ]
            for table in (
                "stage_results",
                "terminology_scans",
                "terminology_candidates",
            )
        }


def _add_terminology_history(project: Path) -> None:
    with sqlite3.connect(project / "project.sqlite") as database:
        database.execute(
            "INSERT INTO terminology_scans("
            "record_id, active_task_id, segment_id, status, payload_json"
            ") VALUES (?, ?, ?, ?, ?)",
            (
                "SCAN-MIGRATION",
                "TERM-TASK-MIGRATION",
                "F0001-S000001",
                "completed",
                json.dumps({"marker": "scan"}),
            ),
        )
        database.execute(
            "INSERT INTO terminology_candidates("
            "record_id, active_task_id, payload_json"
            ") VALUES (?, ?, ?)",
            (
                "CANDIDATE-MIGRATION",
                "TERM-TASK-MIGRATION",
                json.dumps({"marker": "candidate"}),
            ),
        )
        database.commit()


def test_epub_v4_migration_keeps_legacy_parts_when_new_nav_is_present(
    tmp_path: Path,
) -> None:
    from tests.test_documents import add_translations, init_epub, make_epub

    project = init_epub(tmp_path)
    add_translations(project)
    new_source = tmp_path / "book-with-nav.epub"
    make_epub(
        new_source,
        nav_xhtml=(
            b'<html xmlns="http://www.w3.org/1999/xhtml"><body><nav><ol>'
            b'<li><a href="text/ch1.xhtml">Contents</a></li>'
            b'<li><a href="text/ch1.xhtml#chapter">Chapter One</a></li>'
            b"</ol></nav></body></html>"
        ),
    )
    with sqlite3.connect(project / "project.sqlite") as database:
        stored_name = json.loads(
            database.execute("SELECT payload_json FROM files").fetchone()[0]
        )["stored_name"]
    (project / "input" / stored_name).write_bytes(new_source.read_bytes())
    _add_terminology_history(project)
    history_before = _migration_history_rows(project)
    _downgrade_epub_project_to_v4(project)

    ensure_supported(project)

    with sqlite3.connect(project / "project.sqlite") as database:
        assert database.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0] == str(SCHEMA_VERSION)
    segments = read_segments(project)
    assert [item["segment_id"] for item in segments] == [
        "F0001-S000001",
        "F0001-S000002",
    ]
    assert [item["part_id"] for item in segments] == [
        "OEBPS/text/ch1.xhtml",
        "OEBPS/text/ch1.xhtml",
    ]
    state = read_adapter_state(project, "F0001")
    assert state is not None
    assert len(state["state"]["locators"]) == 2
    assert all(
        locator["path"] == "OEBPS/text/ch1.xhtml"
        for locator in state["state"]["locators"]
    )
    with zipfile.ZipFile(project / "input" / stored_name) as archive:
        assert "OEBPS/nav.xhtml" in archive.namelist()
    assert _migration_history_rows(project) == history_before


def test_epub_v4_migration_accepts_emphasis_compaction_and_preserves_history(
    tmp_path: Path,
) -> None:
    from tests.test_documents import add_translations, make_epub

    source = tmp_path / "emphasis.epub"
    make_epub(
        source,
        xhtml=(
            '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>'
            "<ruby>强<rt>・</rt></ruby><ruby>调<rt>・</rt></ruby>"
            "</p></body></html>"
        ).encode(),
    )
    new_source = tmp_path / "emphasis-with-nav.epub"
    make_epub(
        new_source,
        xhtml=(
            '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>'
            "<ruby>强<rt>・</rt></ruby><ruby>调<rt>・</rt></ruby>"
            "</p></body></html>"
        ).encode(),
        nav_xhtml=(
            b'<html xmlns="http://www.w3.org/1999/xhtml"><body><nav><ol>'
            b'<li><a href="text/ch1.xhtml">Contents</a></li>'
            b"</ol></nav></body></html>"
        ),
    )
    project, _ = init_project(
        [str(source)],
        name="emphasis",
        document_adapter_id="epub",
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    add_translations(project)
    with sqlite3.connect(project / "project.sqlite") as database:
        stored_name = json.loads(
            database.execute("SELECT payload_json FROM files").fetchone()[0]
        )["stored_name"]
    (project / "input" / stored_name).write_bytes(new_source.read_bytes())
    with sqlite3.connect(project / "project.sqlite") as database:
        database.execute(
            "UPDATE segments SET source = '｜强《・》｜调《・・》'"
        )
        database.commit()
    history_before = _migration_history_rows(project)
    _downgrade_epub_project_to_v4(project)

    ensure_supported(project)

    with sqlite3.connect(project / "project.sqlite") as database:
        assert database.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0] == str(SCHEMA_VERSION)
    segments = read_segments(project)
    assert [item["segment_id"] for item in segments] == ["F0001-S000001"]
    assert [item["source"] for item in segments] == ["｜强调《・》"]
    assert _migration_history_rows(project) == history_before


def test_epub_v4_migration_accepts_reverse_emphasis_compaction_at_format_boundary(
    tmp_path: Path,
) -> None:
    from tests.test_documents import make_epub

    source = tmp_path / "boundary.epub"
    make_epub(
        source,
        xhtml=(
            '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>'
            "<span><ruby>誰でも<rt>・</rt></ruby></span>"
            "<span><ruby>自由に<rt>・</rt></ruby></span>"
            "</p></body></html>"
        ).encode(),
    )
    project, _ = init_project(
        [str(source)],
        name="boundary",
        document_adapter_id="epub",
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    with sqlite3.connect(project / "project.sqlite") as database:
        database.execute(
            "UPDATE segments SET source = '｜誰でも自由に《・》'"
        )
        database.commit()
    _downgrade_epub_project_to_v4(project)

    ensure_supported(project)

    segments = read_segments(project)
    assert [item["segment_id"] for item in segments] == ["F0001-S000001"]
    assert [item["source"] for item in segments] == [
        "｜誰でも《・》｜自由に《・》"
    ]
    state = read_adapter_state(project, "F0001")
    assert state is not None
    assert state["state"]["locators"][0]["slot"]["kind"] == "composite"


def test_epub_v4_migration_rejects_non_ruby_source_without_mutation(
    tmp_path: Path,
) -> None:
    from tests.test_documents import add_translations, init_epub, make_epub

    project = init_epub(tmp_path)
    add_translations(project)
    new_source = tmp_path / "changed.epub"
    make_epub(
        new_source,
        xhtml=(
            b'<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            b"<h1>Changed Chapter</h1><p>Hello world.</p>"
            b"</body></html>"
        ),
    )
    with sqlite3.connect(project / "project.sqlite") as database:
        stored_name = json.loads(
            database.execute("SELECT payload_json FROM files").fetchone()[0]
        )["stored_name"]
    (project / "input" / stored_name).write_bytes(new_source.read_bytes())
    history_before = _migration_history_rows(project)
    _downgrade_epub_project_to_v4(project)
    database_hash_before = hashlib.sha256(
        (project / "project.sqlite").read_bytes()
    ).hexdigest()

    with pytest.raises(StorageError, match="Segment 源文本"):
        ensure_supported(project)

    assert hashlib.sha256((project / "project.sqlite").read_bytes()).hexdigest() == (
        database_hash_before
    )
    assert _migration_history_rows(project) == history_before
    with sqlite3.connect(project / "project.sqlite") as database:
        assert database.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
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
                _adapter_payload_json(database.execute(
                    "SELECT payload_json FROM adapter_states"
                ).fetchone()[0])
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


def test_stage_retention_dependencies_reset_and_orphan_parent(tmp_path: Path) -> None:
    from app.sqlite_storage import append_stage_results

    project = create_project(tmp_path)
    pid = str(read_json(project, project / "project.json")["project_id"])
    sid = "F0001-S000001"

    def result(key: str, stage: str, status: str = "completed", **fields: object):
        return record_header("stage_result", pid, record_id=key, stage=stage,
                             segment_id=sid, status=status, **fields)

    def save(*records):
        append_stage_results(project, records)

    def ids(stage):
        return {r["record_id"] for r in read_jsonl(project, stage_result_path(project, stage))}

    save(result("t1", "translation", text="one"))
    save(result("a1", "proofreading_applied", base_result_id="t1", text="one"))
    save(result("reset", "translation", "reset"))
    save(result("f1", "translation", "failed"))
    save(result("f2", "translation", "failed"))
    assert ids("translation") == {"t1", "reset", "f2"}
    assert not latest_stage_summary(project, "translation", [sid])[sid]["completed"]
    save(result("t2", "translation", text="two"))
    assert ids("translation") == {"t1", "t2"}
    save(result("a2", "proofreading_applied", base_result_id="t2", text="two"))
    assert ids("translation") == {"t2"}
    assert ids("proofreading_applied") == {"a2"}
    with pytest.raises(StorageError, match="引用"):
        save(result("broken", "proofreading_applied", base_result_id="missing"))
    assert ids("proofreading_applied") == {"a2"}


def test_removed_segment_keeps_referenced_parent_then_reclaims_it(tmp_path: Path) -> None:
    project = create_project(tmp_path)
    pid = str(read_json(project, project / "project.json")["project_id"])
    from app.sqlite_storage import append_stage_results
    append_stage_results(project, [
        record_header("stage_result", pid, record_id="parent", stage="translation",
                      segment_id="F0001-S000001", status="completed", text="old"),
        record_header("stage_result", pid, record_id="child", stage="proofreading",
                      segment_id="F0001-S000003", status="completed", base_result_id="parent"),
    ])
    # Preserve only the second nonempty Segment, simulating a source replacement.
    segments = [r for r in read_segments(project) if r["segment_id"] == "F0001-S000003"]
    replace_source(project, read_files(project), segments, read_json(project, project / "project.json"))
    assert len(read_jsonl(project, stage_result_path(project, "translation"))) == 1
    append_stage_results(project, [record_header("stage_result", pid, stage="proofreading",
        segment_id="F0001-S000003", status="completed")])
    assert read_jsonl(project, stage_result_path(project, "translation")) == []
    replace_source(project, [], [], read_json(project, project / "project.json"))
    assert read_jsonl(project, stage_result_path(project, "proofreading")) == []


def test_bulk_results_and_states_respect_sqlite_parameter_limit(tmp_path: Path, monkeypatch) -> None:
    import app.sqlite_storage as storage
    project = create_project(tmp_path, "\n".join(["fixture"] * 1200))
    segments = read_segments(project)
    pid = str(read_json(project, project / "project.json")["project_id"])
    original = storage._connect
    def connect(path):
        connection = original(path)
        connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        return connection
    monkeypatch.setattr(storage, "_connect", connect)
    storage.append_stage_results(project, [record_header("stage_result", pid,
        stage="translation", segment_id=row["segment_id"], status="completed", text="fixture")
        for row in segments])
    states = storage.latest_stage_states(project, "translation", [row["segment_id"] for row in segments])
    assert len(states) == 1200
    assert all(state["completed"] for state in states.values())
    summary = storage.latest_stage_summary(project, "translation", [row["segment_id"] for row in segments])
    assert len(summary) == 1200 and all(state["completed"] for state in summary.values())
    latest = storage.latest_stage_results(project, "translation", [row["segment_id"] for row in segments])
    assert len(latest) == 1200


def test_applied_text_resolution_preserves_raw_records_and_exact_parent(tmp_path: Path) -> None:
    from app.sqlite_storage import append_stage_results, resolve_stage_result_texts
    project = create_project(tmp_path)
    pid = str(read_json(project, project / "project.json")["project_id"])
    def result(key, stage, **fields):
        return record_header("stage_result", pid, record_id=key, stage=stage,
            segment_id="F0001-S000001", status="completed", **fields)
    records = [
        result("base", "translation", text="original"),
        result("review", "proofreading", review_status="accepted", base_result_id="base"),
        result("applied", "proofreading_applied", base_result_id="base", suggestion_result_id="review"),
        result("polish", "polishing", review_status="accepted", base_result_id="applied"),
        result("polished", "polishing_applied", base_result_id="applied", suggestion_result_id="polish"),
    ]
    append_stage_results(project, records)
    append_stage_results(project, [result("new-base", "translation", text="new")])
    resolved = resolve_stage_result_texts(project, records[2:])
    assert resolved[0]["text"] == resolved[2]["text"] == "original"
    assert "text" not in records[2] and "text" not in records[4]
    assert "text" not in read_jsonl(project, stage_result_path(project, "polishing_applied"))[0]
    append_stage_results(project, [record_header("stage_result", pid, stage="translation",
        segment_id="F0001-S000001", status="reset")])
    assert resolve_stage_result_texts(project, [records[4]])[0]["text"] == "original"
    suggested = result("suggested", "proofreading", review_status="suggested", suggested_text="revision", base_result_id="base")
    application = result("application", "proofreading_applied", base_result_id="base", suggestion_result_id="suggested")
    lineage = {r["record_id"]: r for r in [*records, suggested, application]}
    assert resolve_stage_result_texts(project, [application], records_by_id=lineage)[0]["text"] == "revision"
    assert resolve_stage_result_texts(project, [{**application, "text": " override"}], records_by_id=lineage)[0]["text"] == " override"
    lineage["suggested"]["review_status"] = "accepted"
    lineage["application"]["base_result_id"] = "application"
    with pytest.raises(StorageError, match="循环"):
        resolve_stage_result_texts(project, [application], records_by_id=lineage)
    application["base_result_id"] = "missing"
    with pytest.raises(StorageError, match="引用"):
        resolve_stage_result_texts(project, [application], records_by_id=lineage)


def test_summary_reads_keep_source_texts_in_the_same_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app import sqlite_storage

    project = create_project(tmp_path)
    artifact = _text_summary(project)
    write_content_summary(project, artifact)
    expected = read_content_summaries(project)
    original = sqlite_storage._summary_source_texts

    def reclaim_before_text_read(connection):
        with sqlite3.connect(project / "project.sqlite") as writer:
            writer.execute("DELETE FROM content_summaries")
            sqlite_storage._prune_summary_source_texts(writer)
        return original(connection)

    monkeypatch.setattr(sqlite_storage, "_summary_source_texts", reclaim_before_text_read)
    assert read_content_summaries(project) == expected


def test_scan_progress_batches_segment_ids_with_low_bind_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app import sqlite_storage

    project = create_project(tmp_path)
    _, scan = _seed_fingerprint_records(project)
    original = sqlite_storage._with_db

    def limited_connection(project):
        connection = original(project)
        connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        return connection

    monkeypatch.setattr(sqlite_storage, "_with_db", limited_connection)
    ids = [scan["segment_id"], *(f"absent-{i}" for i in range(1000))]
    assert sqlite_storage.terminology_scan_state(project, "TASK-SHARED", ids) == (
        {scan["segment_id"]}, {scan["stage_fingerprint"]},
    )


@pytest.mark.parametrize("maintenance", [False, True])
def test_retention_removes_unneeded_reset_in_one_pass(tmp_path: Path, maintenance: bool) -> None:
    from app import sqlite_storage
    from app.stage_result_retention import maintain_stage_results, obsolete_stage_results

    project = create_project(tmp_path)
    project_id = read_json(project, project / "project.json")["project_id"]
    records = [record_header("stage_result", project_id, record_id=key,
                            stage="translation", segment_id="F0001-S000001", status=status)
               for key, status in [("old", "completed"), ("reset", "reset"), ("failed", "failed")]]
    if maintenance:
        connection = sqlite_storage._with_db(project)
        try:
            with connection:
                sqlite_storage._insert_stage(connection, records)
                assert obsolete_stage_results(connection) == {"old", "reset"}
                assert maintain_stage_results(connection) == 2
                assert not obsolete_stage_results(connection)
        finally:
            connection.close()
    else:
        sqlite_storage.append_stage_results(project, records)
    assert [record["record_id"] for record in read_jsonl(project, project / "stages" / "translation.jsonl")] == ["failed"]
