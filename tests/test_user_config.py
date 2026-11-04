from __future__ import annotations

import json
from pathlib import Path

import keyring
import pytest
from fastapi.testclient import TestClient

from app.project import bundle_hash, init_project, sync_global_templates
from app.user_config import user_root
from app.web import create_app
from tests.test_foundation import make_app_root


def test_user_root_precedence_and_no_legacy_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.user_config._platform_data_base", lambda: tmp_path)
    legacy_name = "-".join(("minimal", "llm", "translator"))
    legacy = tmp_path / legacy_name
    legacy.mkdir()
    (legacy / "marker").write_text("legacy", encoding="utf-8")

    monkeypatch.delenv("ANOTHER_LLM_USER_ROOT", raising=False)
    assert user_root() == tmp_path / "another-llm-translator"
    assert legacy.is_dir()
    assert not (tmp_path / "another-llm-translator").exists()

    custom = tmp_path / "external" / "another-llm-translator"
    custom.mkdir(parents=True)
    (tmp_path / "another-llm-translator-location.json").write_text(
        json.dumps({"version": 1, "active_root": str(custom)}), encoding="utf-8"
    )
    assert user_root() == custom

    override = tmp_path / "custom-user-root"
    monkeypatch.setenv("ANOTHER_LLM_USER_ROOT", str(override))
    assert user_root() == override


def test_user_root_rejects_invalid_locators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANOTHER_LLM_USER_ROOT", raising=False)
    monkeypatch.setattr("app.user_config._platform_data_base", lambda: tmp_path)
    locator = tmp_path / "another-llm-translator-location.json"

    locator.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="locator"):
        user_root()

    wrong_name = tmp_path / "external" / "translator-data"
    wrong_name.mkdir(parents=True)
    locator.write_text(
        json.dumps({"version": 1, "active_root": str(wrong_name)}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="locator"):
        user_root()

    actual = tmp_path / "actual-root"
    actual.mkdir()
    custom = tmp_path / "another-llm-translator"
    try:
        custom.symlink_to(actual, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    locator.write_text(
        json.dumps({"version": 1, "active_root": str(custom)}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="locator"):
        user_root()


def test_web_app_creation_does_not_access_keyring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANOTHER_LLM_USER_ROOT", raising=False)
    monkeypatch.setattr("app.user_config._platform_data_base", lambda: tmp_path)

    def fail_get_password(*args: object, **kwargs: object) -> None:
        raise AssertionError("web app creation must not access the keyring")

    monkeypatch.setattr(keyring, "get_password", fail_get_password)
    create_app(
        projects_root=tmp_path / "projects",
        app_root=tmp_path / "app-root",
    )


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

    logging.getLogger("another_llm_translator").warning("diagnostics write")
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
