from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.errors import UsageError
from app.project import init_project
from app.prompt_library import (
    delete_prompt_library,
    list_prompt_library,
    prompt_library_path,
    read_prompt_library,
    save_prompt_library,
)
from app.sqlite_storage import read_json
from app.web import create_app
from tests.test_foundation import make_app_root


def make_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    app_root = make_app_root(tmp_path)
    source = tmp_path / "input.txt"
    source.write_text("one", encoding="utf-8")
    projects_root = tmp_path / "projects"
    project, _ = init_project(
        [str(source)],
        name="sample",
        app_root=app_root,
        projects_root=projects_root,
    )
    assert project is not None
    return app_root, projects_root, project


def test_prompt_library_roundtrip_and_scope_isolation(tmp_path: Path) -> None:
    assert list_prompt_library("translation", "zh-CN") == []
    digest = save_prompt_library("translation", "zh-CN", "strict", "project policy")
    assert digest.startswith("sha256:")
    assert list_prompt_library("translation", "zh-CN") == [
        {"id": "strict", "digest": digest}
    ]
    assert read_prompt_library("translation", "zh-CN", "strict") == (
        "project policy",
        digest,
    )
    assert list_prompt_library("translation", "en") == []
    assert list_prompt_library("proofreading", "zh-CN") == []

    updated = save_prompt_library("translation", "zh-CN", "strict", "updated policy")
    assert updated != digest
    assert read_prompt_library("translation", "zh-CN", "strict")[0] == (
        "updated policy"
    )
    delete_prompt_library("translation", "zh-CN", "strict")
    with pytest.raises(UsageError, match="不存在"):
        read_prompt_library("translation", "zh-CN", "strict")


@pytest.mark.parametrize(
    ("stage", "language", "prompt_id"),
    [
        ("unknown", "zh-CN", "strict"),
        ("translation", "fr", "strict"),
        ("translation", "zh-CN", "../strict"),
        ("translation", "zh-CN", "strict_id"),
    ],
)
def test_prompt_library_rejects_invalid_scope_and_ids(
    stage: str, language: str, prompt_id: str
) -> None:
    with pytest.raises(UsageError):
        prompt_library_path(stage, language, prompt_id)


def test_prompt_api_reports_sync_and_keeps_library_separate(
    tmp_path: Path,
) -> None:
    app_root, projects_root, project = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root, app_root=app_root))
    project_prompt_path = project / "prompts" / "translation.zh-CN.middle.txt"
    original_project_prompt = project_prompt_path.read_text(encoding="utf-8")

    initial = client.get(
        "/api/v1/projects/sample/prompts/translation",
        params={"language": "zh-CN"},
    )
    assert initial.status_code == 200
    assert initial.json()["global_sync"] == {
        "available": True,
        "same": True,
        "language": "zh-CN",
    }

    changed_global = "GLOBAL ONLY"
    assert (
        client.put(
            "/api/v1/global/prompts/translation",
            json={"language": "zh-CN", "content": changed_global},
        ).status_code
        == 200
    )
    out_of_sync = client.get(
        "/api/v1/projects/sample/prompts/translation",
        params={"language": "zh-CN"},
    ).json()
    assert out_of_sync["global_sync"]["same"] is False
    assert project_prompt_path.read_text(encoding="utf-8") == original_project_prompt

    saved = client.put(
        "/api/v1/projects/sample/prompts/translation",
        json={"language": "zh-CN", "content": changed_global},
    )
    assert saved.status_code == 200
    assert (
        client.get(
            "/api/v1/projects/sample/prompts/translation",
            params={"language": "zh-CN"},
        ).json()["global_sync"]["same"]
        is True
    )

    assert (
        client.put(
            "/api/v1/prompt-library/translation/zh-CN/strict",
            json={"content": "LIBRARY ONLY"},
        ).status_code
        == 200
    )
    detail = client.get("/api/v1/prompt-library/translation/zh-CN/strict")
    assert detail.status_code == 200
    assert detail.json()["content"] == "LIBRARY ONLY"
    assert "用户消息为 JSON" in detail.json()["assembled"]
    assert project_prompt_path.read_text(encoding="utf-8") == changed_global
    assert (
        client.get("/api/v1/global/prompts/translation").json()["content"]
        == changed_global
    )

    listing = client.get("/api/v1/prompt-library/translation/zh-CN")
    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()["entries"]] == ["strict"]
    assert (
        client.put(
            "/api/v1/prompt-library/translation/zh-CN/strict",
            json={"content": "LIBRARY UPDATED"},
        ).status_code
        == 200
    )
    assert client.delete("/api/v1/prompt-library/translation/zh-CN/strict").json() == {
        "deleted": True,
        "id": "strict",
    }
    assert (
        client.get("/api/v1/prompt-library/translation/zh-CN/strict").status_code == 400
    )
    assert (
        client.put(
            "/api/v1/prompt-library/translation/zh-CN/%2E%2E/escape",
            json={"content": "bad"},
        ).status_code
        == 400
    )
    assert (
        client.put(
            "/api/v1/prompt-library/translation/zh-CN/strict_id",
            json={"content": "bad"},
        ).status_code
        == 400
    )
    assert (
        client.put(
            "/api/v1/prompt-library/translation/zh-CN/strict",
            json={"content": ""},
        ).status_code
        == 400
    )

    metadata = read_json(project, project / "project.json")
    assert "prompt_library" not in metadata


def test_project_prompt_sync_uses_language_fallback(tmp_path: Path) -> None:
    app_root, projects_root, project = make_project(tmp_path)
    (project / "prompts" / "translation.en.middle.txt").unlink()
    client = TestClient(create_app(projects_root=projects_root, app_root=app_root))
    response = client.get(
        "/api/v1/projects/sample/prompts/translation",
        params={"language": "en"},
    )
    assert response.status_code == 200
    value = response.json()
    assert value["language"] == "zh-CN"
    assert value["global_sync"]["language"] == "zh-CN"
    assert value["global_sync"]["same"] is True


def test_project_prompt_reports_unavailable_global_prompt(tmp_path: Path) -> None:
    app_root, projects_root, _ = make_project(tmp_path)
    (app_root / "prompts" / "translation.zh-CN.middle.txt").unlink()
    client = TestClient(create_app(projects_root=projects_root, app_root=app_root))
    response = client.get(
        "/api/v1/projects/sample/prompts/translation",
        params={"language": "zh-CN"},
    )
    assert response.status_code == 200
    assert response.json()["global_sync"] == {
        "available": False,
        "same": False,
        "language": "zh-CN",
    }
