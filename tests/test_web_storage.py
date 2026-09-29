from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.data_root import pending_path
from app.user_config import USER_ROOT_NAME
from app.web import create_app
from tests.test_web import make_project


def test_web_storage_inventory_detail_and_output_cleanup(tmp_path: Path) -> None:
    projects_root, project = make_project(tmp_path)
    output = project / "output" / "nested" / "result.txt"
    output.parent.mkdir(parents=True)
    output.write_text("result", encoding="utf-8")
    client = TestClient(create_app(projects_root=projects_root))

    summary = client.get("/api/v1/storage")
    assert summary.status_code == 200
    payload = summary.json()
    assert {"scanned_at", "complete", "errors", "total_bytes", "reclaimable_bytes", "global", "projects"} <= payload.keys()
    assert payload["projects"][0]["selector"] == "sample"
    assert any(item["id"] == "output" for item in payload["projects"][0]["categories"])

    detail = client.get("/api/v1/storage/projects/sample")
    assert detail.status_code == 200
    assert detail.json()["output_files"] == [
        {
            "path": "output/nested/result.txt",
            "bytes": len("result"),
            "can_clear": True,
            "blocked_reason": None,
        }
    ]

    rejected = client.post(
        "/api/v1/projects/sample/storage/outputs/clear",
        json={"path": "output/nested/result.txt", "confirm": False},
    )
    assert rejected.status_code == 400

    cleared = client.post(
        "/api/v1/projects/sample/storage/outputs/clear",
        json={"path": "output/nested/result.txt", "confirm": True},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json() == {
        "affected_files": 1,
        "reclaimed_bytes": len("result"),
    }
    assert client.get("/api/v1/storage/projects/sample").json()["output_files"] == []


def test_web_storage_cleanup_respects_active_task_and_running_run_guards(
    tmp_path: Path,
    monkeypatch,
) -> None:
    projects_root, project = make_project(tmp_path)
    (project / "logs" / "app.log").write_text("log", encoding="utf-8")
    app = create_app(projects_root=projects_root)
    client = TestClient(app)
    monkeypatch.setattr(app.state.tasks, "is_project_running", lambda _: True)

    blocked = client.post(
        "/api/v1/projects/sample/storage/logs/clear",
        json={"confirm": True},
    )

    assert blocked.status_code == 400
    assert (project / "logs" / "app.log").read_text(encoding="utf-8") == "log"


def test_web_storage_global_log_cleanup_and_strict_confirm(tmp_path: Path) -> None:
    projects_root, _ = make_project(tmp_path)
    app = create_app(projects_root=projects_root)
    client = TestClient(app)
    log_path = app.state.diagnostics.log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_bytes(b"current")
    log_path.with_name("app.log.1").write_bytes(b"rotation")

    invalid = client.post(
        "/api/v1/storage/logs/clear",
        json={"confirm": "true"},
    )
    assert invalid.status_code == 400

    cleared = client.post(
        "/api/v1/storage/logs/clear",
        json={"confirm": True},
    )

    assert cleared.status_code == 200
    assert cleared.json() == {
        "affected_files": 2,
        "reclaimed_bytes": len("current") + len("rotation"),
    }
    assert log_path.read_bytes() == b""
    assert not log_path.with_name("app.log.1").exists()


def test_web_data_root_reports_environment_and_default_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects_root, _ = make_project(tmp_path)
    locator = tmp_path / "location.json"
    monkeypatch.setattr("app.user_config.user_root_locator_path", lambda: locator)
    monkeypatch.setattr("app.data_root.user_root_locator_path", lambda: locator)
    client = TestClient(create_app(projects_root=projects_root))

    overridden = client.get("/api/v1/storage/data-root")
    assert overridden.status_code == 200
    assert overridden.json()["mode"] == "environment"
    assert overridden.json()["can_change"] is False

    monkeypatch.delenv("ANOTHER_LLM_USER_ROOT")
    default = client.get("/api/v1/storage/data-root")
    assert default.status_code == 200
    assert default.json()["mode"] == "default"
    assert default.json()["can_change"] is True
    assert set(default.json()["pending"] or {}) <= {"source_root", "target_root"}


def test_web_data_root_reports_custom_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects_root, _ = make_project(tmp_path)
    monkeypatch.delenv("ANOTHER_LLM_USER_ROOT")
    custom_root = tmp_path / "custom" / USER_ROOT_NAME
    custom_root.mkdir(parents=True)
    locator = tmp_path / "location.json"
    monkeypatch.setattr("app.user_config.user_root_locator_path", lambda: locator)
    monkeypatch.setattr("app.data_root.user_root_locator_path", lambda: locator)
    locator.write_text(
        f'{{"version": 1, "active_root": "{custom_root}"}}', encoding="utf-8"
    )
    response = TestClient(create_app(projects_root=projects_root)).get(
        "/api/v1/storage/data-root"
    )

    assert response.status_code == 200
    assert response.json()["mode"] == "custom"
    assert response.json()["active_root"] == str(custom_root)


def test_web_data_root_rejects_incomplete_pending_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects_root, _ = make_project(tmp_path)
    locator = tmp_path / "location.json"
    monkeypatch.setattr("app.user_config.user_root_locator_path", lambda: locator)
    monkeypatch.setattr("app.data_root.user_root_locator_path", lambda: locator)
    pending_path().write_text('{"version": 1}', encoding="utf-8")

    response = TestClient(create_app(projects_root=projects_root)).get(
        "/api/v1/storage/data-root"
    )

    assert response.status_code == 400
    assert "relocation request" in response.json()["error"]


def test_web_data_root_relocation_requires_confirmation_and_cancels_without_data_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects_root, _ = make_project(tmp_path)
    monkeypatch.delenv("ANOTHER_LLM_USER_ROOT")
    source_root = tmp_path / "source" / USER_ROOT_NAME
    source_root.mkdir(parents=True)
    source_file = source_root / "keep.txt"
    source_file.write_text("keep", encoding="utf-8")
    locator = tmp_path / "location.json"
    monkeypatch.setattr("app.user_config.user_root_locator_path", lambda: locator)
    monkeypatch.setattr("app.data_root.user_root_locator_path", lambda: locator)
    locator.write_text(
        f'{{"version": 1, "active_root": "{source_root}"}}', encoding="utf-8"
    )
    target_parent = tmp_path / "target"
    target_parent.mkdir()
    app = create_app(projects_root=projects_root)
    client = TestClient(app)

    rejected = client.post(
        "/api/v1/storage/data-root/relocation",
        json={"parent_dir": str(target_parent), "confirm": False},
    )
    assert rejected.status_code == 400
    assert not pending_path().exists()

    created = client.post(
        "/api/v1/storage/data-root/relocation",
        json={"parent_dir": str(target_parent), "confirm": True},
    )
    assert created.status_code == 200, created.text
    assert created.json() == {
        "source_root": str(source_root),
        "target_root": str(target_parent / USER_ROOT_NAME),
    }
    pending = client.get("/api/v1/storage/data-root").json()["pending"]
    assert pending == created.json()
    assert "transaction_id" not in pending

    cancelled = client.request(
        "DELETE",
        "/api/v1/storage/data-root/relocation",
        json={"confirm": True},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json() == {"cancelled": True}
    assert not pending_path().exists()
    assert not (target_parent / USER_ROOT_NAME).exists()
    assert source_file.read_text(encoding="utf-8") == "keep"


def test_web_data_root_relocation_rejects_active_tasks_and_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects_root, _ = make_project(tmp_path)
    locator = tmp_path / "location.json"
    monkeypatch.setattr("app.user_config.user_root_locator_path", lambda: locator)
    monkeypatch.setattr("app.data_root.user_root_locator_path", lambda: locator)
    assert pending_path().parent == tmp_path
    target_parent = tmp_path / "target"
    target_parent.mkdir()
    app = create_app(projects_root=projects_root)
    client = TestClient(app)
    monkeypatch.setattr(app.state.tasks, "active_tasks", lambda: [object()])

    active = client.post(
        "/api/v1/storage/data-root/relocation",
        json={"parent_dir": str(target_parent), "confirm": True},
    )
    assert active.status_code == 400
    assert not pending_path().exists()

    monkeypatch.setattr(app.state.tasks, "active_tasks", list)
    env_override = client.post(
        "/api/v1/storage/data-root/relocation",
        json={"parent_dir": str(target_parent), "confirm": True},
    )
    assert env_override.status_code == 400
    assert not pending_path().exists()
