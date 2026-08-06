from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.project import bundle_hash, init_project, sync_global_templates
from app.user_config import effective_path, user_root, write_user
from app.web import create_app
from tests.test_foundation import make_app_root


def test_user_root_honors_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "custom-user-root"
    monkeypatch.setenv("MINIMAL_LLM_USER_ROOT", str(override))
    assert user_root() == override


def test_effective_path_prefers_user_copy_and_falls_back_to_builtin(
    tmp_path: Path,
) -> None:
    builtin = tmp_path / "builtin"
    (builtin / "config").mkdir(parents=True)
    (builtin / "config" / "config.toml").write_text("builtin", encoding="utf-8")
    assert effective_path("config/config.toml", builtin_root=builtin) == (
        builtin / "config" / "config.toml"
    )

    (user_root() / "config").mkdir(parents=True)
    (user_root() / "config" / "config.toml").write_text("user", encoding="utf-8")
    assert effective_path("config/config.toml", builtin_root=builtin) == (
        user_root() / "config" / "config.toml"
    )


def test_write_user_creates_parent_directories(tmp_path: Path) -> None:
    target = write_user("prompts/translation.zh-CN.middle.txt")
    assert target == user_root() / "prompts" / "translation.zh-CN.middle.txt"
    assert not target.exists()
    target.write_text("content", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "content"


def test_init_project_defaults_to_user_root_and_uses_user_override(
    tmp_path: Path,
) -> None:
    app_root = make_app_root(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text("one", encoding="utf-8")

    project, _ = init_project(
        [str(source)],
        name="defaults",
        app_root=app_root,
    )
    assert project is not None
    assert project.parent == user_root() / "projects"
    assert (project / "prompts" / "translation.zh-CN.middle.txt").is_file()

    (user_root() / "prompts").mkdir(parents=True)
    (user_root() / "prompts" / "translation.zh-CN.middle.txt").write_text(
        "USER GLOBAL PROMPT", encoding="utf-8"
    )
    overridden, _ = init_project(
        [str(source)],
        name="overridden",
        app_root=app_root,
    )
    assert overridden is not None
    assert (
        overridden / "prompts" / "translation.zh-CN.middle.txt"
    ).read_text(encoding="utf-8") == "USER GLOBAL PROMPT"


def test_bundle_hash_tracks_user_root_override_and_sync_propagates(
    tmp_path: Path,
) -> None:
    app_root = make_app_root(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text("one", encoding="utf-8")
    project, _ = init_project(
        [str(source)],
        name="demo",
        app_root=app_root,
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    before = bundle_hash(app_root)

    (user_root() / "prompts").mkdir(parents=True)
    (user_root() / "prompts" / "translation.zh-CN.middle.txt").write_text(
        "USER OVERRIDE", encoding="utf-8"
    )
    assert bundle_hash(app_root) != before

    warnings = sync_global_templates(
        project, app_root=app_root, interactive=True, choice="update"
    )
    assert any("已更新项目模板" in item for item in warnings)
    assert (
        project / "prompts" / "translation.zh-CN.middle.txt"
    ).read_text(encoding="utf-8") == "USER OVERRIDE"


def test_web_global_writes_land_in_user_root_and_builtin_stays_read_only(
    tmp_path: Path,
) -> None:
    projects_root, _ = make_project(tmp_path)
    app_root = tmp_path / "app-root"
    client = TestClient(create_app(
        projects_root=projects_root,
        app_root=app_root,
    ))
    builtin_prompt = (app_root / "prompts" / "translation.zh-CN.middle.txt").read_text(
        encoding="utf-8"
    )

    assert client.put(
        "/api/v1/global/prompts/translation",
        json={"content": "USER PROMPT"},
    ).status_code == 200
    assert client.get("/api/v1/global/prompts/translation").json()[
        "content"
    ] == "USER PROMPT"
    assert (
        app_root / "prompts" / "translation.zh-CN.middle.txt"
    ).read_text(encoding="utf-8") == builtin_prompt
    assert (
        tmp_path / "user-root" / "prompts" / "translation.zh-CN.middle.txt"
    ).read_text(encoding="utf-8") == "USER PROMPT"


def test_web_rejects_deleting_builtin_preset_but_allows_user_preset(
    tmp_path: Path,
) -> None:
    projects_root, _ = make_project(tmp_path)
    client = TestClient(create_app(
        projects_root=projects_root,
        app_root=tmp_path / "app-root",
    ))
    rejected = client.delete("/api/v1/global/presets/openai-responses")
    assert rejected.status_code == 400
    assert "内置" in rejected.json()["error"]

    default = client.get("/api/v1/global/presets/default").json()
    custom = {**default, "preset_id": "custom-user", "model": "user-model"}
    assert client.put(
        "/api/v1/global/presets/custom-user", json=custom
    ).status_code == 200
    assert (
        tmp_path / "user-root" / "llm_presets" / "custom-user.json"
    ).is_file()
    deleted = client.delete("/api/v1/global/presets/custom-user")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert not (
        tmp_path / "user-root" / "llm_presets" / "custom-user.json"
    ).exists()


def test_web_diagnostics_log_lands_in_user_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(projects_root=tmp_path / "projects", app_root=tmp_path)
    client = TestClient(app)
    client.get("/api/v1/projects")
    log_path = tmp_path / "user-root" / "logs" / "app.log"
    assert log_path.is_file()
    import logging

    logging.getLogger("minimal_llm_translator").warning("diagnostics write")
    assert "diagnostics write" in log_path.read_text(encoding="utf-8")


def make_project(tmp_path: Path) -> tuple[Path, Path]:
    app_root = make_app_root(tmp_path)
    input_path = tmp_path / "input.txt"
    input_path.write_text("one\ntwo", encoding="utf-8")
    projects_root = tmp_path / "projects"
    project, _ = init_project(
        [str(input_path)],
        name="sample",
        app_root=app_root,
        projects_root=projects_root,
    )
    assert project is not None
    return projects_root, project
