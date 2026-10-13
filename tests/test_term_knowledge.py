from pathlib import Path

import pytest

from app.errors import UsageError
from app.sqlite_storage import append_jsonl, read_segments, record_header
from tests.test_terms_exchange import make_project


def scan_record(project: Path, text: str, *, terms: bool = True) -> dict:
    from app.term_knowledge import source_range
    segments = read_segments(project)
    record = record_header(
        "terminology_candidates", "test", active_task_id="scan", status="completed",
        terms=[{"source": "Alpha", "category": "name", "description": text}] if terms else [],
        source_range=source_range(segments, {s["segment_id"]: s for s in segments}, {}),
    )
    record["active_task_id"] = record["record_id"]
    append_jsonl(project, project / "terminology" / "candidates.jsonl", record)
    return record


def library() -> dict:
    from app.term_knowledge import empty_knowledge
    return {"terms_revision": 1, "terms": [
        {"source": "Alpha", "normalized": "alpha", "group_primary": None},
    ], "knowledge": empty_knowledge()}


def test_published_evidence_needs_application_and_source_changes_block_use(tmp_path: Path) -> None:
    from app.term_knowledge import publish_evidence, entity_view, save_items, project_descriptions
    from app.term_library import term_normalization
    from app.config import load_project_config
    project = make_project(tmp_path)
    data = library()
    spec = term_normalization(load_project_config(project))
    first = scan_record(project, "Alpha is a captain")
    publish_evidence(project, data, [first], spec)
    project_descriptions(project, data)
    assert data["terms"][0]["description"] == ""
    view = entity_view(project, data, "alpha")
    assert view["status"] == "pending"
    fragment = view["fragments"][0]["id"]
    save_items(project, data, "alpha", [{"text": "船长", "fragment_ids": [fragment]}])
    assert entity_view(project, data, "alpha")["status"] == "current"
    second = scan_record(project, "Alpha knows Beta")
    publish_evidence(project, data, [second], spec, replace=False)
    assert entity_view(project, data, "alpha")["status"] == "pending"
    project_descriptions(project, data)
    assert data["terms"][0]["description"] == "船长"
    # Actual persisted source mutation, not a test of implementation text.
    from app.sqlite_storage import database_path
    import sqlite3
    with sqlite3.connect(database_path(project)) as connection:
        connection.execute("UPDATE segments SET source = 'changed'")
    assert entity_view(project, data, "alpha")["status"] == "stale"
    with pytest.raises(UsageError, match="过期"):
        entity_view(project, data, "alpha", require_current=True)


def test_empty_success_replaces_evidence_and_publication_is_idempotent(tmp_path: Path) -> None:
    from app.term_knowledge import publish_evidence, entity_view, save_items
    from app.term_library import term_normalization
    from app.config import load_project_config
    project = make_project(tmp_path)
    data = library()
    spec = term_normalization(load_project_config(project))
    first = scan_record(project, "captain")
    publish_evidence(project, data, [first], spec)
    view = entity_view(project, data, "alpha")
    save_items(project, data, "alpha", [{"text": "captain", "fragment_ids": [view["fragments"][0]["id"]]}])
    publish_evidence(project, data, [first], spec)
    assert entity_view(project, data, "alpha")["status"] == "current"
    empty = scan_record(project, "", terms=False)
    publish_evidence(project, data, [first, empty], spec)
    assert entity_view(project, data, "alpha")["status"] == "stale"
    assert all(f["state"] == "superseded" for f in entity_view(project, data, "alpha")["fragments"])


def test_items_validate_references_and_explicit_manual_items(tmp_path: Path) -> None:
    from app.term_knowledge import save_items, entity_view
    project = make_project(tmp_path)
    data = library()
    with pytest.raises(UsageError, match="引用"):
        save_items(project, data, "alpha", [{"text": "invented", "fragment_ids": ["missing"]}])
    save_items(project, data, "alpha", [{"text": "人工说明", "fragment_ids": []}])
    view = entity_view(project, data, "alpha")
    assert view["status"] == "current"
    assert view["fragments"][0]["origin"] == "manual"
    assert view["fragments"][0]["source_range"] is None


def test_migration_preserves_old_explanations_without_fabricating_sources(tmp_path: Path) -> None:
    from scripts.migrate_term_knowledge import migrate
    from app.sqlite_storage import read_json, write_json, database_path
    from app.term_library import load_terms
    project = make_project(tmp_path)
    data = {"terms_revision": 3, "terms": [{"normalized": "alpha", "source": "Alpha", "description": "已有说明", "group_primary": None}]}
    write_json(project, project / "terminology" / "terms.json", record_header("terminology_library", "test", **data))
    with pytest.raises(UsageError, match="迁移"):
        load_terms(project)
    backup = migrate(project)
    assert backup.is_file()
    import sqlite3, json
    with sqlite3.connect(backup) as connection:
        old = json.loads(connection.execute("SELECT payload_json FROM terms_state WHERE key='terms'").fetchone()[0])
    assert "knowledge" not in old
    migrated = load_terms(project)
    assert migrated["terms"][0]["description"] == "已有说明"
    assert migrated["terms"][0]["knowledge_status"] == "current"
    from app.term_knowledge import entity_view
    fragment = entity_view(project, migrated, "alpha")["fragments"][0]
    assert fragment["origin"] == "legacy"
    assert fragment["source_range"] is None
