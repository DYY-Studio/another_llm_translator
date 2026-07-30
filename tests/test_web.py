from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.errors import UsageError
from app.locking import project_write_lock
from app.project import init_project
from app.web import create_app
from tests.test_foundation import make_app_root


def make_project(tmp_path: Path, source: str = "one\ntwo") -> tuple[Path, Path]:
    app_root = make_app_root(tmp_path)
    input_path = tmp_path / "input.txt"
    input_path.write_text(source, encoding="utf-8")
    projects_root = tmp_path / "projects"
    project, _ = init_project(
        [str(input_path)],
        name="sample",
        app_root=app_root,
        projects_root=projects_root,
    )
    assert project is not None
    return projects_root, project


def test_web_lists_project_edits_translation_and_rejects_remote_origin(
    tmp_path: Path,
) -> None:
    projects_root, _ = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root))

    listed = client.get("/api/v1/projects")
    assert listed.status_code == 200
    assert listed.json()["projects"][0]["name"] == "sample"
    overview = client.get("/api/v1/projects/sample").json()
    segment_id = overview["segments"][0]["segment_id"]
    saved = client.post(
        "/api/v1/projects/sample/translations",
        json={"segment_id": segment_id, "text": "一"},
    )
    assert saved.status_code == 200
    assert saved.json()["text"] == "一"
    assert (
        client.get(
            "/api/v1/projects",
            headers={"Origin": "https://remote.example"},
        ).status_code
        == 403
    )


def test_web_creates_project_from_uploaded_files(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    app = create_app(projects_root=projects_root)
    client = TestClient(app)
    response = client.post(
        "/api/v1/projects",
        data={"name": "uploaded"},
        files=[
            ("files", ("first.txt", b"one", "text/plain")),
            ("files", ("second.txt", b"two", "text/plain")),
        ],
    )
    # The endpoint uses the repository global template bundle.
    assert response.status_code == 200
    overview = client.get("/api/v1/projects/uploaded").json()
    assert [item["name"] for item in overview["files"]] == [
        "first.txt",
        "second.txt",
    ]
    adapters = client.get("/api/v1/document-adapters").json()["adapters"]
    assert [item["adapter_id"] for item in adapters] == ["epub", "txt"]


def test_web_adapter_validation_preview_and_secret_redaction(
    tmp_path: Path,
) -> None:
    projects_root, project = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root))
    adapter = client.get(
        "/api/v1/projects/sample/adapters/openai-compatible"
    ).json()
    adapter["body"]["response_format"] = {"type": "json_object"}
    saved = client.put(
        "/api/v1/projects/sample/adapters/openai-compatible",
        json=adapter,
    )
    assert saved.status_code == 200
    assert json.loads(
        (
            project / "llm_adapters" / "openai-compatible.json"
        ).read_text(encoding="utf-8")
    )["body"]["response_format"] == {"type": "json_object"}
    preview = client.get(
        "/api/v1/projects/sample/adapter-preview"
    ).json()
    assert preview["headers"]["Authorization"] == "Bearer ***"
    assert "***" not in json.dumps(preview["body"])


def test_project_write_lock_rejects_second_writer(tmp_path: Path) -> None:
    _, project = make_project(tmp_path)
    with project_write_lock(project):
        with pytest.raises(UsageError, match="另一个写入任务"):
            with project_write_lock(project):
                pass


def test_web_task_manager_allows_one_task_and_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects_root, _ = make_project(tmp_path)
    entered = asyncio.Event()

    async def fake_translation(*_: object, **__: object) -> dict[str, object]:
        entered.set()
        await asyncio.Future()
        return {}

    monkeypatch.setattr("app.web_tasks.run_translation", fake_translation)
    with TestClient(create_app(projects_root=projects_root)) as client:
        first = client.post(
            "/api/v1/projects/sample/tasks",
            json={"stage": "translation"},
        )
        assert first.status_code == 200
        task_id = first.json()["task_id"]
        second = client.post(
            "/api/v1/projects/sample/tasks",
            json={"stage": "translation"},
        )
        assert second.status_code == 400
        cancelled = client.post(f"/api/v1/tasks/{task_id}/cancel")
        assert cancelled.status_code == 200
        for _ in range(20):
            state = client.get(f"/api/v1/tasks/{task_id}").json()
            if state["status"] == "cancelled":
                break
        assert state["status"] == "cancelled"
