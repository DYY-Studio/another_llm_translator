from fastapi.testclient import TestClient

from app.web import create_app
from tests.test_decision import preset
from tests.test_foundation import make_app_root


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
