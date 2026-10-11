from dataclasses import replace

import pytest

from app.errors import ConfigError
from app.plugins import _builtin_plugins, _validate_plugins, load_plugins


def test_plugins_declare_optional_decision_prompts():
    plugins = load_plugins()
    declarations = [prompt for plugin in plugins for prompt in plugin.decision_prompts]
    assert {prompt.validator_id for prompt in declarations} == {"preferred_term_usage", "segment_alignment"}
    for prompt in declarations:
        assert not hasattr(prompt, "defaults")
        assert set(prompt.default["criteria"]) == set(prompt.choice_ids)


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
    global_content = {**prompt.default, "instructions": "Global rule"}
    project_content = {**global_content, "instructions": "Project nickname rule"}
    assert resolve_prompt(prompt)[1] == "default"
    atomic_write_json(prompt_path(tmp_path / "user", prompt.validator_id), global_content)
    assert resolve_prompt(prompt, project)[0] == global_content
    atomic_write_json(prompt_path(project, prompt.validator_id), project_content)
    assert resolve_prompt(prompt, project) == (project_content, "project")
    atomic_write_json(prompt_path(project, prompt.validator_id), {**project_content, "criteria": {}})
    with pytest.raises(ConfigError, match="选项"):
        resolve_prompt(prompt, project)


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
    library = "/api/v1/decision-prompt-library/preferred_term_usage/nicknames"
    with TestClient(app) as client:
        view = client.get(path).json()
        assert view["inherited"] and "language" not in view
        assert view["global_sync"] == {"available": True, "same": True}
        content = {**view["content"], "instructions": "Nicknames are allowed"}
        assert client.put(global_path, json={"content": content}).status_code == 200
        assert client.get(path).json()["content"] == content
        custom = {**content, "instructions": "Project abbreviations are allowed"}
        assert client.put(path, json={"content": custom}).status_code == 200
        assert not client.get(path).json()["inherited"]
        assert not client.get(path).json()["global_sync"]["same"]
        from app.decision_prompt import prompt_path
        from app.user_config import user_root
        prompt_path(user_root(), "preferred_term_usage").write_text('{"invalid": true}')
        unavailable = client.get(path).json()
        assert unavailable["content"] == custom
        assert not unavailable["global_sync"]["available"] and unavailable["global_sync"]["error"]
        assert client.put(global_path, json={"content": content}).status_code == 200
        assert client.put(path, json={"content": content}).status_code == 200
        equal_override = client.get(path).json()
        assert not equal_override["inherited"] and equal_override["global_sync"]["same"]
        assert client.put(path, json={"content": custom}).status_code == 200
        before = (project / "decision_prompts/preferred_term_usage.json").read_bytes()
        assert client.put(path, json={"content": {**custom, "criteria": {}}}).status_code == 400
        assert (project / "decision_prompts/preferred_term_usage.json").read_bytes() == before
        assert client.put(library, json={"content": custom}).status_code == 200
        assert client.get(library).json()["content"] == custom
        assert client.get(library.rsplit("/", 1)[0]).json()["entries"][0]["id"] == "nicknames"
        assert client.delete(library).status_code == 200
        (project / "decision_prompts/preferred_term_usage.json").write_text('{"bad": true}')
        assert client.get(path).status_code == 400
        assert client.get("/api/v1/decision-prompts/preferred_term_usage/default").json()["content"]["instructions"]
        assert client.put(global_path, json={"language": "en", "content": content}).status_code == 400
        assert client.delete(path).status_code == 200
        assert client.get(path).json()["content"] == content
        custom_language = {**content, "instructions": "允许使用昵称和缩写。"}
        assert client.put(path, json={"content": custom_language}).status_code == 200
        assert client.get(path).json()["content"] == custom_language
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
    assert set(frozen["preferred_term_usage"]) == {"choice_ids", "content"}
    content = {**frozen["preferred_term_usage"]["content"], "instructions": "Changed nickname rule"}
    atomic_write_json(prompt_path(project, "preferred_term_usage"), content)
    changed = load_project_config(project, presets_root=root)
    assert stage_fingerprint(changed, "translation", None) != stage_fingerprint(resolved, "translation", None)
    assert load_run_config(snapshot)["_decision_prompt_definitions"] == frozen
    config["validation"]["translation"]["decision_enabled"] = False
    (project / "config.toml").write_text(dump_config(config))
    atomic_write_json(prompt_path(project, "preferred_term_usage"), {"invalid": True})
    assert load_project_config(project, presets_root=root)["_decision_prompt_definitions"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["typesafe", "openai-decisions"])
@pytest.mark.parametrize("validator_id", ["preferred_term_usage", "segment_alignment"])
async def test_custom_prompt_reaches_both_apis_and_keeps_outcome(protocol, validator_id, monkeypatch):
    import json
    import httpx
    from app.decision import DecisionClient
    from app.decision_prompt import get_declaration
    from app.translation_validation import SegmentAlignmentValidator, TranslationTermMatch, TranslationValidationContext
    from plugins.term_validation.plugin import PreferredTermUsageValidator
    from tests.test_decision import preset
    monkeypatch.setenv("DECISION_TEST_KEY", "secret")
    declaration = get_declaration(validator_id)
    content = {"instructions": "Custom nickname and abbreviation policy", "criteria": {choice: "Custom " + choice for choice in declaration.choice_ids}}
    choice = "acceptable" if validator_id == "preferred_term_usage" else "aligned"
    calls = []
    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        questions = list(body["questions"].values()) if protocol == "typesafe" else body["questions"]
        assert all(question["instructions"].endswith(content["instructions"]) for question in questions)
        if protocol == "typesafe":
            assert all(question["criteria"] == content["criteria"] for question in questions)
            answers = {name: {"type": "choice", "choice": choice, "confidence": .9, "probabilities": {key: int(key == choice) for key in declaration.choice_ids}} for name in body["questions"]}
        else:
            assert all(question["choices"] == [{"value": key, "description": value} for key, value in content["criteria"].items()] for question in questions)
            answers = [{"name": question["name"], "type": "choice", "choice": choice, "confidence": .9, "probabilities": [{"value": key, "probability": int(key == choice)} for key in declaration.choice_ids]} for question in questions]
        return httpx.Response(200, json={"model": "test-model", "answers": answers})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = DecisionClient(preset(protocol), http_client=http)
        context = TranslationValidationContext("Alice arrived", "她到了", terms=(TranslationTermMatch("Alice", "Alice", "source", "爱丽丝"),), segment_id="S1", decision=client, decision_prompts={validator_id: content})
        if validator_id == "preferred_term_usage":
            assert await PreferredTermUsageValidator().validate(context) == ()
        else:
            assert await SegmentAlignmentValidator(client, prompt=content).validate_response((context,)) == {}
    assert len(calls) == 1


def test_optional_plugin_custom_choices_uninstall_and_reinstall(tmp_path, monkeypatch):
    from app import plugins
    from app.decision_prompt import DecisionPromptDeclaration, prompt_declarations, prompt_path, resolve_prompt, resolve_run_prompts
    from app.plugin_api import PluginDescriptor, PLUGIN_PROTOCOL_VERSION
    from app.sqlite_storage import atomic_write_json
    class Validator:
        validator_id = "custom_validator"
        version = "1"
        label = "Custom validator"
        phase = "alignment"
        scope = "segment"
        def validate(self, context):
            return ()
    content = {"instructions": "Custom rule", "criteria": {"yes": "Yes", "no": "No"}}
    declaration = DecisionPromptDeclaration("custom_validator", ("yes", "no"), content)
    plugin = PluginDescriptor("custom", "1", PLUGIN_PROTOCOL_VERSION, translation_validators=(Validator(),), decision_prompts=(declaration,))
    _validate_plugins([plugin, PluginDescriptor("no-prompts", "1", PLUGIN_PROTOCOL_VERSION)])
    monkeypatch.setattr(plugins, "_PLUGIN_CACHE", (plugin,))
    config = {"decision": {}, "validation": {"translation": {"validators": ["custom_validator"], "decision_enabled": False}}}
    resolve_run_prompts(config)
    assert config["_decision_prompt_definitions"]["custom_validator"]["choice_ids"] == ["yes", "no"]
    path = prompt_path(tmp_path, "custom_validator")
    custom = {**content, "instructions": "Saved custom policy"}
    atomic_write_json(path, custom)
    monkeypatch.setattr(plugins, "_PLUGIN_CACHE", ())
    assert prompt_declarations() == {}
    assert path.is_file()
    with pytest.raises(ConfigError, match="未安装"):
        plugins.resolve_translation_validators(["custom_validator"])
    monkeypatch.setattr(plugins, "_PLUGIN_CACHE", (plugin,))
    assert resolve_prompt(prompt_declarations()["custom_validator"], tmp_path)[0] == custom
    changed = replace(declaration, choice_ids=("pass", "fail"), default={"instructions": "New rule", "criteria": {"pass": "Pass", "fail": "Fail"}})
    monkeypatch.setattr(plugins, "_PLUGIN_CACHE", (replace(plugin, decision_prompts=(changed,)),))
    with pytest.raises(ConfigError, match="选项"):
        resolve_prompt(prompt_declarations()["custom_validator"], tmp_path)
