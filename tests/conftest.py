from __future__ import annotations

import json
import shutil
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
    user_root = tmp_path / "user-root"
    monkeypatch.setenv("ANOTHER_LLM_USER_ROOT", str(user_root))
    monkeypatch.setenv("ANOTHER_LLM_LANGUAGE", "zh-CN")

    runtime_root = tmp_path / "runtime-global"
    (runtime_root / "llm_adapters").mkdir(parents=True)
    (runtime_root / "llm_presets").mkdir()
    source_root = Path(__file__).parents[1]
    for source in (source_root / "llm_adapters").glob("*.json"):
        shutil.copy2(source, runtime_root / "llm_adapters" / source.name)
    preset = json.loads(
        (source_root / "llm_presets" / "default.json").read_text(encoding="utf-8")
    )
    preset.update(requests_per_minute=0, input_tokens_per_minute=0)
    (runtime_root / "llm_presets" / "default.json").write_text(
        json.dumps(preset, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr("app.config.APP_ROOT", runtime_root)
