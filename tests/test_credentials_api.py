from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.web import create_app
from tests.conftest import FakeKeyring
from tests.test_foundation import make_app_root


def test_web_credentials_crud_and_test(tmp_path: Path, fake_keyring: FakeKeyring) -> None:
    projects_root = tmp_path / "projects"
    client = TestClient(create_app(projects_root=projects_root))

    empty = client.get("/api/v1/credentials")
    assert empty.status_code == 200
    assert empty.json() == {"credentials": []}

    created = client.post(
        "/api/v1/credentials",
        json={"id": "openai-main", "secret": "s3cret\nsecond-secret\n"},
    )
    assert created.status_code == 200
    assert created.json() == {"saved": True}
    assert fake_keyring.values[("another-llm-translator", "openai-main")] == "s3cret\nsecond-secret\n"

    listed = client.get("/api/v1/credentials").json()
    assert [item["id"] for item in listed["credentials"]] == ["openai-main"]
    assert "s3cret" not in json.dumps(listed)

    tested = client.post("/api/v1/credentials/openai-main/test")
    assert tested.status_code == 200
    assert tested.json() == {"ok": True}

    updated = client.put(
        "/api/v1/credentials/openai-main",
        json={"secret": "s3cret-2\nnext-secret"},
    )
    assert updated.status_code == 200
    assert fake_keyring.values[("another-llm-translator", "openai-main")] == "s3cret-2\nnext-secret"

    missing_test = client.post("/api/v1/credentials/nope/test")
    assert missing_test.status_code == 400
    assert "凭据不存在" in missing_test.json()["error"]

    missing_update = client.put("/api/v1/credentials/nope", json={"secret": "x"})
    assert missing_update.status_code == 400

    deleted = client.delete("/api/v1/credentials/openai-main")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}
    assert client.get("/api/v1/credentials").json() == {"credentials": []}
    assert client.delete("/api/v1/credentials/openai-main").status_code == 400


def test_web_credentials_reject_invalid_payloads(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    client = TestClient(create_app(projects_root=projects_root))
    assert client.post("/api/v1/credentials", json={"id": 1, "secret": "x"}).status_code == 400
    assert client.post("/api/v1/credentials", json={"id": "ok", "secret": ""}).status_code == 400
    assert client.post("/api/v1/credentials", json={"id": "../bad", "secret": "x"}).status_code == 400
    assert client.put("/api/v1/credentials/openai-main", json={"secret": 1}).status_code == 400
    assert client.post(
        "/api/v1/credentials", json={"id": "blank", "secret": "one\n\ntwo"}
    ).status_code == 400
    assert client.post(
        "/api/v1/credentials", json={"id": "duplicate", "secret": "one\none"}
    ).status_code == 400


def test_web_preset_with_keychain_credential_validates(
    tmp_path: Path,
) -> None:
    projects_root = tmp_path / "projects"
    app_root = make_app_root(tmp_path)
    client = TestClient(create_app(projects_root=projects_root, app_root=app_root))
    preset = client.get("/api/v1/global/presets/default").json()
    preset["credential"] = {"kind": "keychain", "name": "openai-main"}
    saved = client.put("/api/v1/global/presets/default", json=preset)
    assert saved.status_code == 200
    loaded = client.get("/api/v1/global/presets/default").json()
    assert loaded["credential"] == {"kind": "keychain", "name": "openai-main"}
    assert "schema_version" in loaded and loaded["schema_version"] == 5


def test_web_welcome_first_and_dismiss(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "welcome"
    marker.mkdir()
    monkeypatch.setattr("app.web.user_root", lambda: marker)
    client = TestClient(create_app(projects_root=tmp_path / "projects"))
    assert client.get("/api/v1/welcome").json() == {"first": True}
    assert client.post("/api/v1/welcome/dismiss").json() == {"ok": True}
    assert client.get("/api/v1/welcome").json() == {"first": False}
