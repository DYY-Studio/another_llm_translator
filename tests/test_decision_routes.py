from pathlib import Path
from shutil import copytree

from fastapi.testclient import TestClient
import pytest

from app.config import dump_config, load_config
from app.project import init_project
from app.web import create_app
from tests.test_decision import preset
from tests.test_foundation import make_app_root


@pytest.mark.parametrize("scope,reference", [
    ("global", "common"), ("project", "alignment"), ("external", "terminology"),
])
def test_cannot_delete_referenced_decision_preset(tmp_path, scope, reference):
    root = make_app_root(tmp_path)
    projects = tmp_path / "projects"
    app = create_app(app_root=root, projects_root=projects, log_path=tmp_path / "app.log")
    config_path = root / "config" / "config.toml"
    if scope != "global":
        source = tmp_path / "source.txt"
        source.write_text("Source.")
        project, _ = init_project([str(source)], name="example", app_root=root,
                                 projects_root=projects if scope == "project" else tmp_path / "external")
        config_path = project / "config.toml"
        if scope == "external":
            app.state.external_projects.add(project)
    config = load_config(config_path)
    if reference == "common":
        config["decision"]["preset"] = "local"
    elif reference == "alignment":
        config["validation"]["translation"]["alignment"]["decision_preset"] = "local"
    else:
        config["validation"]["translation"]["decision_preset"] = "local"
    config_path.write_text(dump_config(config))
    with TestClient(app) as client:
        path = "/api/v1/global/decision-presets/local"
        assert client.put(path, json=preset()).status_code == 200
        assert client.delete(path).status_code == 400
        assert client.get(path).json() == {"preset": preset(), "warnings": [], "missing_fields": []}


def test_decision_preset_round_trip_and_invalid_url(tmp_path):
    with TestClient(create_app(app_root=make_app_root(tmp_path), projects_root=tmp_path / "projects", log_path=tmp_path / "app.log")) as client:
        path = "/api/v1/global/decision-presets/local"
        value = preset()
        assert client.put(path, json=value).status_code == 200
        assert client.get(path).json() == {"preset": value, "warnings": [], "missing_fields": []}
        assert client.get("/api/v1/global/decision-presets").json()["presets"][0]["preset_id"] == "local"
        value["url"] = "https://example.com"
        assert client.put(path, json=value).status_code == 400
        assert client.delete(path).status_code == 200
        assert client.get("/api/v1/global/decision-presets").json()["presets"] == []


def test_builtin_examples_cover_both_protocols(tmp_path):
    root = make_app_root(tmp_path)
    copytree(Path(__file__).parents[1] / "decision_presets", root / "decision_presets")
    with TestClient(create_app(app_root=root, projects_root=tmp_path / "projects", log_path=tmp_path / "app.log")) as client:
        presets = client.get("/api/v1/global/decision-presets").json()["presets"]
        assert {item["protocol"] for item in presets if item["valid"]} == {"typesafe", "openai-decisions"}
        for item in presets:
            response = client.get(f"/api/v1/global/decision-presets/{item['preset_id']}")
            assert response.status_code == 200
            assert response.json()["preset"]["credential"]["kind"] == "environment"


def test_llm_decision_preset_and_shared_prompt_resources(tmp_path):
    root = make_app_root(tmp_path)
    with TestClient(create_app(app_root=root, projects_root=tmp_path / "projects", log_path=tmp_path / "app.log")) as client:
        path = "/api/v1/global/decision-presets/judge"
        value = dict(preset_id="judge", protocol="llm", llm_preset="default", prompt_language="en")
        assert client.put(path, json=value).status_code == 200
        assert client.get(path).json() == {"preset": value, "warnings": [], "missing_fields": []}
        incomplete = {**value, "llm_preset": "missing"}
        assert client.put(path, json=incomplete).status_code == 200
        assert client.get(path).json()["preset"] == incomplete
        from app.decision_llm import resolve_llm_resources
        from app.errors import ConfigError
        with pytest.raises(ConfigError, match="无法读取 LLM Preset"):
            resolve_llm_resources(incomplete, root)
        prompt = client.get("/api/v1/global/prompts/decision_llm?language=en")
        assert prompt.status_code == 200
        assert prompt.json()["content"] in prompt.json()["assembled"]
        assert "probabilities" in prompt.json()["assembled"]
        assert client.put("/api/v1/prompt-library/decision_llm/en/custom", json={"content": "Use evidence"}).status_code == 200


def test_llm_decision_project_override_and_run_snapshot(tmp_path):
    from app.config import load_run_config, resolve_project_config
    from app.execution import _write_llm_snapshots, stage_fingerprint
    from app.prompt_resources import ProjectPrompts
    from app.sqlite_storage import atomic_write_json

    root = make_app_root(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text("Evidence")
    projects = tmp_path / "projects"
    project, _ = init_project([str(source)], name="sample", app_root=root, projects_root=projects)
    preset = dict(preset_id="judge", protocol="llm", llm_preset="default", prompt_language="en")
    atomic_write_json(root / "decision_presets" / "judge.json", preset)
    config = load_config(project / "config.toml")
    config["decision"]["preset"] = "judge"
    config["validation"]["translation"]["validators"] = ["segment_alignment"]
    with TestClient(create_app(app_root=root, projects_root=projects, log_path=tmp_path / "app.log")) as client:
        path = "/api/v1/projects/sample/prompts/decision_llm"
        assert client.put(path, json={"language": "en", "content": "Project decision policy"}).status_code == 200
        preview = client.get(path + "?language=en")
        assert preview.status_code == 200
        assert "Project decision policy" in preview.json()["assembled"]
        resolved = resolve_project_config(config, project=project, presets_root=root)
        fingerprint = stage_fingerprint(resolved, "translation", {"translation": "en"})
        run = tmp_path / "run"
        run.mkdir()
        (run / "config.toml").write_text(dump_config(config))
        _write_llm_snapshots(run, resolved)
        ProjectPrompts(project, builtin_root=root).save("decision_llm", "en", "New policy")
        changed = resolve_project_config(load_config(run / "config.toml"), project=project, presets_root=root)
        assert stage_fingerprint(changed, "translation", {"translation": "en"}) != fingerprint
        frozen = load_run_config(run)
        assert frozen["_decision_preset_definitions"]["judge"] == resolved["_decision_preset_definitions"]["judge"]
        response = client.request("DELETE", path, json={"confirm": True, "language": "en"})
        assert response.status_code == 200
        assert client.get(path + "?language=en").json()["inherited"] is True
