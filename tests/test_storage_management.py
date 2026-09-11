from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import app.storage_management as storage_management
from app.config import load_project_config
from app.errors import UsageError
from app.execution import create_run, finalize_run
from app.project import init_project
from app.storage_management import StorageManager, lightweight_project_storage
from app.sqlite_storage import append_jsonl, read_json, record_header
from app.user_config import user_root
from tests.test_foundation import make_app_root


def make_storage_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    app_root = make_app_root(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")
    projects_root = user_root() / "projects"
    project, _ = init_project(
        [str(source)],
        name="sample",
        app_root=app_root,
        projects_root=projects_root,
    )
    assert project is not None
    return app_root, projects_root, project


def test_storage_scan_classifies_files_and_deduplicates_registered_projects(
    tmp_path: Path,
) -> None:
    app_root, projects_root, project = make_storage_project(tmp_path)
    (app_root / "not-user-data.bin").write_bytes(b"builtin")

    (user_root() / "config").mkdir(parents=True)
    (user_root() / "llm_presets").mkdir()
    (user_root() / "logs").mkdir()
    (user_root() / "config" / "config.toml").write_bytes(b"global-config")
    (user_root() / "llm_presets" / "custom.json").write_bytes(b"preset")
    (user_root() / "logs" / "app.log").write_bytes(b"global-log")
    (user_root() / "unclassified.bin").write_bytes(b"other")
    (project / "logs" / "app.log").write_bytes(b"project-log")
    (project / "output" / "result.txt").write_bytes(b"output")

    manager = StorageManager(
        user_data_root=user_root(),
        projects_root=projects_root,
        app_root=app_root,
        project_paths=lambda: [project],
    )

    summary = manager.scan()

    assert summary["complete"] is True
    global_categories = {item["id"]: item for item in summary["global"]}
    project_summary = summary["projects"][0]
    project_categories = {
        item["id"]: item for item in project_summary["categories"]
    }
    assert global_categories["settings"]["bytes"] >= len(b"global-config")
    assert global_categories["logs"]["bytes"] == len(b"global-log")
    assert global_categories["other"]["bytes"] == len(b"other")
    assert project_categories["logs"]["bytes"] == len(b"project-log")
    assert project_categories["output"]["bytes"] == len(b"output")
    assert summary["total_bytes"] == sum(
        item["bytes"] for item in summary["global"]
    ) + sum(
        item["bytes"]
        for item in project_summary["categories"]
    )
    assert summary["total_bytes"] > len(b"builtin")
    assert summary["total_bytes"] == sum(
        item["bytes"] for item in summary["global"]
    ) + project_summary["total_bytes"]


def _manager(app_root: Path, projects_root: Path, project: Path, **kwargs: object) -> StorageManager:
    return StorageManager(
        user_data_root=user_root(),
        projects_root=projects_root,
        app_root=app_root,
        project_paths=lambda: [project],
        **kwargs,
    )


def _debug_run(project: Path) -> tuple[str, Path]:
    run_id, run_dir = create_run(
        project,
        config=load_project_config(project, stage="translation"),
        stage="translation",
        fingerprint="storage-test",
        prompt="not returned",
        selected_count=1,
        requested_count=1,
        reused_count=0,
    )
    payloads = run_dir / "payloads"
    payloads.mkdir()
    (payloads / "request.json").write_text(json.dumps({"secret": "value"}))
    (run_dir / "attempts.jsonl").write_text('{"status":"completed"}\n')
    project_id = str(read_json(project, project / "project.json")["project_id"])
    append_jsonl(
        project,
        run_dir / "chunks.jsonl",
        record_header(
            "chunk_manifest",
            project_id,
            record_id="CHUNK-STORAGE",
            run_id=run_id,
            stage="translation",
            chunk_id="CHUNK-STORAGE",
            file_id="F0001",
            segment_ids=["F0001-S000001"],
        ),
    )
    return run_id, run_dir


def test_storage_debug_cleanup_preserves_snapshots_and_sqlite_chunks(
    tmp_path: Path,
) -> None:
    app_root, projects_root, project = make_storage_project(tmp_path)
    run_id, run_dir = _debug_run(project)
    manager = _manager(app_root, projects_root, project)

    with pytest.raises(UsageError, match="运行中的 Run"):
        manager.clear_debug(project, run_id, confirm=True)

    finalize_run(project, run_dir, status="completed", completed=1, failed=0)
    result = manager.clear_debug(project, run_id, confirm=True)

    assert result["affected_files"] == 2
    assert result["reclaimed_bytes"] > 0
    assert not (run_dir / "payloads").exists()
    assert not (run_dir / "attempts.jsonl").exists()
    assert (run_dir / "config.toml").is_file()
    with sqlite3.connect(project / "project.sqlite") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM run_chunks WHERE run_id = ?", (run_id,)
        ).fetchone()[0] == 1


def test_storage_cleanup_rejects_confirmation_path_traversal_and_symlink(
    tmp_path: Path,
) -> None:
    app_root, projects_root, project = make_storage_project(tmp_path)
    output = project / "output" / "result.txt"
    output.write_text("result", encoding="utf-8")
    manager = _manager(app_root, projects_root, project)

    with pytest.raises(UsageError):
        manager.clear_output(project, "result.txt", confirm=False)
    with pytest.raises(UsageError):
        manager.clear_output(project, "../config.toml", confirm=True)
    with pytest.raises(UsageError):
        manager.clear_output(project, "missing.txt", confirm=True)

    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (project / "output" / "linked.txt").symlink_to(outside)
    with pytest.raises(UsageError):
        manager.clear_output(project, "linked.txt", confirm=True)
    assert outside.read_text(encoding="utf-8") == "outside"

    cleared = manager.clear_output(project, "result.txt", confirm=True)
    assert cleared == {"affected_files": 1, "reclaimed_bytes": len("result")}


def test_storage_log_cleanup_truncates_current_log_and_removes_rotations(
    tmp_path: Path,
) -> None:
    app_root, projects_root, project = make_storage_project(tmp_path)
    global_logs = user_root() / "logs"
    global_logs.mkdir(parents=True)
    (global_logs / "app.log").write_bytes(b"current")
    (global_logs / "app.log.1").write_bytes(b"rotation")
    (project / "logs" / "app.log").write_bytes(b"project")
    (project / "logs" / "app.log.1").write_bytes(b"project-rotation")
    manager = _manager(app_root, projects_root, project)

    blocked = _manager(
        app_root,
        projects_root,
        project,
        has_active_tasks=lambda: True,
    )
    with pytest.raises(UsageError, match="活动 Web 任务"):
        blocked.clear_global_logs(confirm=True)

    global_result = manager.clear_global_logs(confirm=True)
    project_result = manager.clear_project_logs(project, confirm=True)

    assert global_result == {"affected_files": 2, "reclaimed_bytes": 15}
    assert project_result == {"affected_files": 2, "reclaimed_bytes": 23}
    assert (global_logs / "app.log").read_bytes() == b""
    assert not (global_logs / "app.log.1").exists()
    assert (project / "logs" / "app.log").read_bytes() == b""
    assert not (project / "logs" / "app.log.1").exists()


def test_lightweight_project_storage_keeps_legacy_totals_and_sqlite_main_size(
    tmp_path: Path,
) -> None:
    app_root, projects_root, project = make_storage_project(tmp_path)
    (project / "project.sqlite-wal").write_bytes(b"wal")
    (project / "project.sqlite-shm").write_bytes(b"shm")
    (project / "runs" / "unknown.bin").write_bytes(b"run")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    (project / "input" / "linked.bin").symlink_to(outside)

    totals = lightweight_project_storage(project)

    regular_files = [
        path
        for path in project.rglob("*")
        if path.is_file() and not path.is_symlink()
    ]
    assert totals["total_bytes"] == sum(path.stat().st_size for path in regular_files)
    assert totals["sqlite_bytes"] == (project / "project.sqlite").stat().st_size


def test_storage_scan_counts_sqlite_sidecars_and_registered_external_project(
    tmp_path: Path,
) -> None:
    app_root, projects_root, project = make_storage_project(tmp_path)
    (project / "project.sqlite-wal").write_bytes(b"wal")
    (project / "project.sqlite-shm").write_bytes(b"shm")
    external_parent = tmp_path / "external"
    external, _ = init_project(
        [],
        name="external",
        empty=True,
        app_root=app_root,
        projects_root=external_parent,
    )
    assert external is not None
    (external / "other.bin").write_bytes(b"external")

    manager = StorageManager(
        user_data_root=user_root(),
        projects_root=projects_root,
        app_root=app_root,
        project_paths=lambda: [project, external, external],
    )
    summary = manager.scan()

    project_by_name = {item["name"]: item for item in summary["projects"]}
    sqlite_category = {
        item["id"]: item
        for item in project_by_name["sample"]["categories"]
    }["sqlite"]
    assert sqlite_category["bytes"] == (
        sum(
            path.stat().st_size
            for path in project.iterdir()
            if path.name.startswith("project.sqlite")
        )
    )
    assert sqlite_category["can_clear"] is False
    assert len(summary["projects"]) == 2
    assert project_by_name["external"]["total_bytes"] >= len(b"external")
    assert {
        item["id"]: item for item in summary["global"]
    }["other"]["bytes"] == 0


def test_storage_scan_returns_partial_results_and_blocks_affected_category(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_root, projects_root, project = make_storage_project(tmp_path)
    logs = user_root() / "logs"
    logs.mkdir(parents=True)
    (logs / "app.log").write_bytes(b"global-log")
    original_scandir = storage_management.os.scandir

    def failing_scandir(path: object):
        if Path(path) == logs:
            raise OSError("permission denied")
        return original_scandir(path)

    monkeypatch.setattr(storage_management.os, "scandir", failing_scandir)
    summary = _manager(app_root, projects_root, project).scan()

    assert summary["complete"] is False
    assert any("permission denied" in error for error in summary["errors"])
    global_logs = next(item for item in summary["global"] if item["id"] == "logs")
    assert global_logs["can_clear"] is False
    assert global_logs["blocked_reason"]
