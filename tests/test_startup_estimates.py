from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.errors import ConfigError, RequestSizeError
from app.server_config import load_server_config, save_server_config, server_config_path
from app.web import create_app
from tests.test_term_decision import create_decision_project


def test_missing_estimate_setting_defaults_false_and_round_trips():
    path = server_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[tasks]\nmax_active_projects = 3\n", encoding="utf-8")
    config = load_server_config()
    assert config["tasks"]["estimate_usage_before_run"] is False
    config["tasks"]["estimate_usage_before_run"] = True
    save_server_config(config)
    assert load_server_config()["tasks"]["estimate_usage_before_run"] is True
    config["tasks"]["estimate_usage_before_run"] = 1
    with pytest.raises(ConfigError, match="必须是布尔值"):
        save_server_config(config)


def test_only_enabled_dialog_estimates_and_estimate_failure_does_not_block(tmp_path, monkeypatch):
    from app import web_tasks
    project = create_decision_project(tmp_path)
    app = create_app(app_root=tmp_path / "app-root", projects_root=project.parent, log_path=tmp_path / "app.log")
    original = web_tasks.decision_plan
    calls = []

    def plan(*args, **kwargs):
        if kwargs.get("estimate_usage", True):
            calls.append(True)
            raise RequestSizeError("estimate is over budget", reason="context")
        return original(*args, **kwargs)

    monkeypatch.setattr(web_tasks, "decision_plan", plan)
    with TestClient(app) as client:
        url = f"/api/v1/projects/{project.name}/task-options/terminology_decision"
        assert client.get(url + "?estimate_usage=true").json()["estimated_requests"] is None
        assert calls == []
        app.state.server_config.setdefault("tasks", {})["estimate_usage_before_run"] = True
        assert client.get(url).json()["estimated_requests"] is None
        assert calls == []
        response = client.get(url + "?estimate_usage=true")
        assert response.status_code == 200
        value = response.json()
        assert value["selected"] > 0
        assert value["estimated_requests"] is None
        assert value["estimate_error"]["error"] == "estimate is over budget"
        assert calls == [True]
