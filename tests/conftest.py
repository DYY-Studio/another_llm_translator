from __future__ import annotations

from pathlib import Path

import keyring
import keyring.backend
import pytest


class FakeKeyring(keyring.backend.KeyringBackend):
    """In-memory keyring; automated tests never touch the real keychain."""

    priority = 10

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self.values[(service, username)]
        except KeyError as exc:
            raise keyring.errors.PasswordDeleteError(username) from exc


@pytest.fixture(autouse=True)
def fake_keyring() -> FakeKeyring:
    fake = FakeKeyring()
    keyring.set_keyring(fake)
    return fake


@pytest.fixture(autouse=True)
def isolated_user_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANOTHER_LLM_USER_ROOT", str(tmp_path / "user-root"))
    monkeypatch.setenv("ANOTHER_LLM_LANGUAGE", "zh-CN")
