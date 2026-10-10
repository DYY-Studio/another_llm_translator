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
        assert client.get(path).json() == preset()


def test_decision_preset_round_trip_and_invalid_url(tmp_path):
    with TestClient(create_app(app_root=make_app_root(tmp_path), projects_root=tmp_path / "projects", log_path=tmp_path / "app.log")) as client:
        path = "/api/v1/global/decision-presets/local"
        value = preset()
        assert client.put(path, json=value).status_code == 200
        assert client.get(path).json() == value
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
            assert response.json()["credential"]["kind"] == "environment"
