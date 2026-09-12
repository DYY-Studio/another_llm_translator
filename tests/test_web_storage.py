from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

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
