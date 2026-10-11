from dataclasses import replace

import pytest

from app.errors import ConfigError
from app.plugins import _builtin_plugins, _validate_plugins, load_plugins


def test_plugins_declare_optional_decision_prompts():
    plugins = load_plugins()
    declarations = [prompt for plugin in plugins for prompt in plugin.decision_prompts]
    assert {prompt.validator_id for prompt in declarations} == {"preferred_term_usage", "segment_alignment"}
    for prompt in declarations:
        assert set(prompt.defaults) == {"en", "zh-CN"}
        assert all(set(content["criteria"]) == set(prompt.choice_ids) for content in prompt.defaults.values())


def test_prompt_declaration_must_belong_to_its_plugin():
    plugin = _builtin_plugins()[1]
    foreign = replace(plugin.decision_prompts[0], validator_id="foreign")
    with pytest.raises(ConfigError, match="Decision"):
        _validate_plugins([replace(plugin, decision_prompts=(foreign,))])


def test_prompt_resolution_precedence_and_invalid_override(tmp_path, monkeypatch):
    from app.decision_prompt import resolve_prompt, prompt_path
    from app.sqlite_storage import atomic_write_json
    monkeypatch.setenv("ANOTHER_LLM_USER_ROOT", str(tmp_path / "user"))
    prompt = next(p for plugin in load_plugins() for p in plugin.decision_prompts if p.validator_id == "preferred_term_usage")
    project = tmp_path / "project"
    global_content = {**prompt.defaults["en"], "instructions": "Global rule"}
    project_content = {**global_content, "instructions": "Project nickname rule"}
    assert resolve_prompt(prompt, "en")[1] == "default"
    atomic_write_json(prompt_path(tmp_path / "user", prompt.validator_id, "en"), global_content)
    assert resolve_prompt(prompt, "en", project)[0] == global_content
    atomic_write_json(prompt_path(project, prompt.validator_id, "en"), project_content)
    assert resolve_prompt(prompt, "en", project) == (project_content, "project")
    atomic_write_json(prompt_path(project, prompt.validator_id, "en"), {**project_content, "criteria": {}})
    with pytest.raises(ConfigError, match="选项"):
        resolve_prompt(prompt, "en", project)


def test_prompt_routes_project_override_restore_and_library(tmp_path):
    from fastapi.testclient import TestClient
    from app.project import init_project
    from app.web import create_app
    from tests.test_foundation import make_app_root
    root = make_app_root(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text("Source")
    projects = tmp_path / "projects"
    project, _ = init_project([str(source)], name="example", app_root=root, projects_root=projects)
    app = create_app(app_root=root, projects_root=projects, log_path=tmp_path / "app.log")
    path = "/api/v1/projects/example/decision-prompts/preferred_term_usage"
    global_path = "/api/v1/global/decision-prompts/preferred_term_usage"
    library = "/api/v1/decision-prompt-library/preferred_term_usage/en/nicknames"
    with TestClient(app) as client:
        view = client.get(path).json()
        assert view["inherited"] and view["language"] == "en"
        content = {**view["content"], "instructions": "Nicknames are allowed"}
        assert client.put(global_path, json={"language": "en", "content": content}).status_code == 200
        assert client.get(path).json()["content"] == content
        custom = {**content, "instructions": "Project abbreviations are allowed"}
        assert client.put(path, json={"language": "en", "content": custom}).status_code == 200
        assert not client.get(path).json()["inherited"]
        before = (project / "decision_prompts/preferred_term_usage/en.json").read_bytes()
        assert client.put(path, json={"language": "en", "content": {**custom, "criteria": {}}}).status_code == 400
        assert (project / "decision_prompts/preferred_term_usage/en.json").read_bytes() == before
        assert client.put(library, json={"content": custom}).status_code == 200
        assert client.get(library).json()["content"] == custom
        assert client.get(library.rsplit("/", 1)[0]).json()["entries"][0]["id"] == "nicknames"
        assert client.delete(library).status_code == 200
        (project / "decision_prompts/preferred_term_usage/en.json").write_text('{"bad": true}')
        assert client.get(path).status_code == 400
        assert client.get(path + "/language").json()["language"] == "en"
        assert client.get("/api/v1/decision-prompts/preferred_term_usage/default?language=en").status_code == 200
        for invalid_language in (["en"], 1, None):
            assert client.put(global_path, json={"language": invalid_language, "content": content}).status_code == 400
        assert client.delete(path + "?language=en").status_code == 200
        assert client.get(path).json()["content"] == content
        assert client.put(global_path + "/language", json={"language": "zh-CN"}).status_code == 200
        assert client.get(path).json()["language"] == "zh-CN"
        assert client.put(path + "/language", json={"language": "en"}).status_code == 200
        assert client.get(path).json()["language"] == "en"
        assert client.get("/api/v1/global/decision-prompts/missing").status_code == 400


def test_run_prompt_snapshot_and_fingerprint(tmp_path):
    from app.config import dump_config, load_config, load_project_config, load_run_config
    from app.decision_prompt import prompt_path
    from app.execution import _write_llm_snapshots, stage_fingerprint
    from app.sqlite_storage import atomic_write_json
    from app.user_config import write_user
    from tests.test_decision import preset
    from tests.test_foundation import make_app_root
    root = make_app_root(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    config = load_config(root / "config/config.toml")
    config["validation"]["translation"].update(validators=["preferred_term_usage"], decision_enabled=True, decision_preset="local")
    atomic_write_json(write_user("decision_presets/local.json"), preset())
    (project / "config.toml").write_text(dump_config(config))
    resolved = load_project_config(project, presets_root=root)
    snapshot = tmp_path / "run"
    snapshot.mkdir()
    (snapshot / "config.toml").write_text(dump_config(config))
    _write_llm_snapshots(snapshot, resolved)
    frozen = resolved["_decision_prompt_definitions"]
    content = {**frozen["preferred_term_usage"]["content"], "instructions": "Changed nickname rule"}
    atomic_write_json(prompt_path(project, "preferred_term_usage", "en"), content)
    changed = load_project_config(project, presets_root=root)
    assert stage_fingerprint(changed, "translation", None) != stage_fingerprint(resolved, "translation", None)
    assert load_run_config(snapshot)["_decision_prompt_definitions"] == frozen
    config["validation"]["translation"]["decision_enabled"] = False
    (project / "config.toml").write_text(dump_config(config))
    atomic_write_json(prompt_path(project, "preferred_term_usage", "en"), {"invalid": True})
    assert load_project_config(project, presets_root=root)["_decision_prompt_definitions"] == {}
