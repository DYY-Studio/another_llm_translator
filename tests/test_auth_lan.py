from __future__ import annotations

from fastapi.testclient import TestClient

from app.server_config import save_server_config
from app.credentials import save_lan_password
from app.user_config import user_root
from app.web import create_app
from tests.conftest import FakeKeyring

LAN = ("192.168.1.10", 12345)
LOOPBACK = ("127.0.0.1", 12345)


def make_client(
    server_config: dict | None = None,
    client: tuple[str, int] = LOOPBACK,
) -> TestClient:
    return TestClient(create_app(server_config=server_config), client=client)


def lan_config(*, lan: dict | None = None, auth: dict | None = None) -> dict:
    config = {
        "lan": {"enabled": True, "bind_address": "192.168.1.5"},
        "auth": {"required": False, "username": ""},
    }
    config["lan"].update(lan or {})
    config["auth"].update(auth or {})
    return config


def enable_auth(fake_keyring: FakeKeyring, *, required: bool = True) -> dict:
    config = lan_config(
        auth={"required": required, "username": "translator", "password": "p@ss"}
    )
    save_server_config(config)
    save_lan_password("p@ss")
    return config


def test_lan_blocked_by_default(tmp_path) -> None:
    client = make_client(client=LAN)
    response = client.get("/api/v1/server/status")
    assert response.status_code == 403
    assert response.json()["code"] == "local_only"


def test_lan_open_without_auth_but_status_warns() -> None:
    client = make_client(server_config=lan_config(), client=LAN)
    assert client.get("/api/v1/projects").status_code == 200
    status = client.get("/api/v1/server/status").json()
    assert status["lan"]["enabled"] is True
    assert status["auth"]["required"] is False
    assert status["authed"] is True


def test_loopback_stays_open_when_auth_enabled(fake_keyring: FakeKeyring) -> None:
    config = enable_auth(fake_keyring)
    client = make_client(server_config=config, client=LOOPBACK)
    assert client.get("/api/v1/projects").status_code == 200
    assert client.get("/api/v1/server/status").json()["authed"] is True


def test_lan_requires_login_and_validates_credentials(
    fake_keyring: FakeKeyring,
) -> None:
    config = enable_auth(fake_keyring)
    client = make_client(server_config=config, client=LAN)

    protected = client.get("/api/v1/projects")
    assert protected.status_code == 401
    assert protected.json()["code"] == "auth_required"

    assert client.get("/api/v1/server/status").status_code == 200
    assert client.get("/api/v1/server/status").json()["authed"] is False

    wrong = client.post(
        "/api/v1/auth/login",
        json={"username": "translator", "password": "wrong"},
    )
    assert wrong.status_code == 400
    assert "用户名或密码错误" in wrong.json()["error"]

    login = client.post(
        "/api/v1/auth/login",
        json={"username": "translator", "password": "p@ss"},
    )
    assert login.status_code == 200
    cookie = login.cookies.get("minimal_llm_session")
    assert cookie
    assert login.headers["set-cookie"]
    assert "HttpOnly" in login.headers["set-cookie"]
    assert "SameSite=lax" in login.headers["set-cookie"]
    assert client.get("/api/v1/projects").status_code == 200
    assert client.get("/api/v1/server/status").json()["authed"] is True

    assert client.post("/api/v1/auth/logout").status_code == 200
    assert client.get("/api/v1/projects").status_code == 401


def test_sessions_invalidate_on_restart(fake_keyring: FakeKeyring) -> None:
    config = enable_auth(fake_keyring)
    first = make_client(server_config=config, client=LAN)
    login = first.post(
        "/api/v1/auth/login",
        json={"username": "translator", "password": "p@ss"},
    )
    token = login.cookies.get("minimal_llm_session")
    assert first.get("/api/v1/projects").status_code == 200

    restarted = make_client(server_config=config, client=LAN)
    restarted.cookies.set("minimal_llm_session", token)
    assert restarted.get("/api/v1/projects").status_code == 401


def test_disabling_sharing_clears_sessions_and_blocks_lan(
    fake_keyring: FakeKeyring,
) -> None:
    config = enable_auth(fake_keyring)
    client = make_client(server_config=config, client=LAN)
    client.post(
        "/api/v1/auth/login",
        json={"username": "translator", "password": "p@ss"},
    )
    assert client.get("/api/v1/projects").status_code == 200

    saved = client.put(
        "/api/v1/server/config",
        json={
            "lan": {"enabled": False, "bind_address": ""},
            "auth": {"required": False, "username": ""},
        },
    )
    assert saved.status_code == 200
    assert client.get("/api/v1/projects").status_code == 403
    assert client.get("/api/v1/projects", cookies={}).status_code == 403


def test_server_config_validation_and_password_flow(
    fake_keyring: FakeKeyring,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.web.lan_interfaces",
        lambda: [{"name": "en0", "address": "192.168.1.5"}],
    )
    client = make_client(client=LOOPBACK)
    bad_address = client.put(
        "/api/v1/server/config",
        json={
            "lan": {"enabled": True, "bind_address": "10.0.0.99"},
            "auth": {"required": False, "username": ""},
        },
    )
    assert bad_address.status_code == 400

    no_password = client.put(
        "/api/v1/server/config",
        json={
            "lan": {"enabled": True, "bind_address": "192.168.1.5"},
            "auth": {"required": True, "username": "me"},
        },
    )
    assert no_password.status_code == 400

    saved = client.put(
        "/api/v1/server/config",
        json={
            "lan": {"enabled": True, "bind_address": "192.168.1.5"},
            "auth": {"required": True, "username": "me", "password": "s3cret"},
        },
    )
    assert saved.status_code == 200
    assert saved.json()["warning"] == ""
    assert fake_keyring.values[("minimal-llm-translator", "lan-auth")] == "s3cret"

    warning = client.put(
        "/api/v1/server/config",
        json={
            "lan": {"enabled": True, "bind_address": "192.168.1.5"},
            "auth": {"required": False, "username": ""},
        },
    )
    assert warning.status_code == 200
    assert "同网段设备" in warning.json()["warning"]

    interfaces = client.get("/api/v1/server/interfaces").json()
    assert isinstance(interfaces["interfaces"], list)


def test_lan_auth_uses_username_from_config(fake_keyring: FakeKeyring) -> None:
    config = lan_config(auth={"required": True, "username": "me", "password": "x"})
    save_server_config(config)
    client = make_client(server_config=config, client=LAN)
    wrong_user = client.post(
        "/api/v1/auth/login",
        json={"username": "other", "password": "x"},
    )
    assert wrong_user.status_code == 400
