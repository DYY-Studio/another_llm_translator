from __future__ import annotations

import pytest

from app.credentials import (
    credential_summaries,
    delete_credential,
    read_credential,
    resolve_api_key,
    save_credential,
)
from app.errors import ConfigError, ExternalError
from tests.conftest import FakeKeyring


def test_credential_crud_updates_index_and_never_exposes_secret(
    fake_keyring: FakeKeyring,
) -> None:
    save_credential("openai-main", "secret-1")
    assert read_credential("openai-main") == "secret-1"
    assert fake_keyring.values[("minimal-llm-translator", "openai-main")] == (
        "secret-1"
    )
    assert [item["id"] for item in credential_summaries()] == ["openai-main"]
    assert all(
        isinstance(item["updated_at"], int)
        for item in credential_summaries()
    )
    assert "secret" not in str(credential_summaries())

    save_credential("openai-main", "secret-2")
    assert read_credential("openai-main") == "secret-2"
    assert len(credential_summaries()) == 1

    delete_credential("openai-main")
    assert read_credential("openai-main") is None
    assert credential_summaries() == []
    with pytest.raises(ConfigError, match="凭据不存在"):
        delete_credential("openai-main")


def test_credential_rejects_invalid_ids_and_empty_secret() -> None:
    with pytest.raises(ConfigError, match="凭据 ID 格式无效"):
        save_credential("../bad", "x")
    with pytest.raises(ConfigError, match="非空字符串"):
        save_credential("openai-main", "")


def test_resolve_environment_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DRAFT_KEY", "value")
    assert resolve_api_key({"kind": "environment", "name": "DRAFT_KEY"}) == "value"
    monkeypatch.delenv("DRAFT_KEY")
    with pytest.raises(ExternalError, match="缺少环境变量：DRAFT_KEY"):
        resolve_api_key({"kind": "environment", "name": "DRAFT_KEY"})


def test_resolve_keychain_credential() -> None:
    save_credential("keychain-main", "value")
    assert resolve_api_key({"kind": "keychain", "name": "keychain-main"}) == "value"
    with pytest.raises(ExternalError, match="缺少钥匙串凭据：missing"):
        resolve_api_key({"kind": "keychain", "name": "missing"})


def test_resolve_rejects_unknown_kind() -> None:
    with pytest.raises(ExternalError, match="未知凭据类型"):
        resolve_api_key({"kind": "file", "name": "x"})
