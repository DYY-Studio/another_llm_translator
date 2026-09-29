from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import data_root


def test_relocation_copies_root_then_switches_locator_and_removes_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANOTHER_LLM_USER_ROOT", raising=False)
    monkeypatch.setattr(
        "app.user_config._platform_data_base", lambda: tmp_path / "system"
    )
    source = tmp_path / "system" / "another-llm-translator"
    source.mkdir(parents=True)
    (source / "settings.toml").write_text("settings", encoding="utf-8")
    (source / "linked-settings.toml").symlink_to("settings.toml")
    target_parent = tmp_path / "external"
    target_parent.mkdir()

    data_root.request_relocation(target_parent)
    result = data_root.apply_pending()

    target = target_parent / "another-llm-translator"
    assert result["active_root"] == str(target)
    assert (target / "settings.toml").read_text(encoding="utf-8") == "settings"
    assert (target / "linked-settings.toml").is_symlink()
    assert data_root.user_root() == target
    assert not source.exists()


def test_relocation_rejects_existing_target_without_touching_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANOTHER_LLM_USER_ROOT", raising=False)
    monkeypatch.setattr(
        "app.user_config._platform_data_base", lambda: tmp_path / "system"
    )
    source = tmp_path / "system" / "another-llm-translator"
    source.mkdir(parents=True)
    (source / "settings.toml").write_text("source", encoding="utf-8")
    parent = tmp_path / "external"
    parent.mkdir()
    target = parent / "another-llm-translator"
    data_root.request_relocation(parent)
    target.mkdir(parents=True)
    (target / "keep").write_text("existing", encoding="utf-8")

    with pytest.raises(ValueError, match="already exists"):
        data_root.apply_pending()

    assert (target / "keep").read_text(encoding="utf-8") == "existing"
    assert data_root.user_root() == source


def test_relocation_fails_if_selected_volume_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANOTHER_LLM_USER_ROOT", raising=False)
    monkeypatch.setattr(
        "app.user_config._platform_data_base", lambda: tmp_path / "system"
    )
    source = tmp_path / "system" / "another-llm-translator"
    source.mkdir(parents=True)
    parent = tmp_path / "external"
    parent.mkdir()
    data_root.request_relocation(parent)
    parent.rmdir()

    with pytest.raises(ValueError, match="target parent is unavailable"):
        data_root.apply_pending()

    assert source.is_dir()
    assert not parent.exists()


def test_apply_pending_recovers_only_its_published_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANOTHER_LLM_USER_ROOT", raising=False)
    monkeypatch.setattr(
        "app.user_config._platform_data_base", lambda: tmp_path / "system"
    )
    source = tmp_path / "system" / "another-llm-translator"
    source.mkdir(parents=True)
    target = tmp_path / "external" / "another-llm-translator"
    target.mkdir(parents=True)
    transaction_id = "tx-test"
    marker = data_root.transaction_marker(target)
    marker.write_text(
        json.dumps(
            {
                "transaction_id": transaction_id,
                "source_root": str(source),
                "target_root": str(target),
            }
        ),
        encoding="utf-8",
    )
    data_root.write_pending(source, target, transaction_id)

    result = data_root.apply_pending()

    assert data_root.user_root() == target
    assert result["active_root"] == str(target)
    assert not source.exists()


def test_locator_failure_keeps_source_and_retry_finishes_its_published_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANOTHER_LLM_USER_ROOT", raising=False)
    monkeypatch.setattr(
        "app.user_config._platform_data_base", lambda: tmp_path / "system"
    )
    source = tmp_path / "system" / "another-llm-translator"
    source.mkdir(parents=True)
    (source / "settings.toml").write_text("settings", encoding="utf-8")
    parent = tmp_path / "external"
    parent.mkdir()
    data_root.request_relocation(parent)
    target = parent / "another-llm-translator"
    with monkeypatch.context() as failing_locator:
        failing_locator.setattr(
            data_root,
            "_write_locator",
            lambda _target: (_ for _ in ()).throw(OSError("locator unavailable")),
        )
        with pytest.raises(OSError, match="locator unavailable"):
            data_root.apply_pending()

    assert source.is_dir()
    assert (target / "settings.toml").read_text(encoding="utf-8") == "settings"
    assert data_root.user_root() == source

    result = data_root.apply_pending()

    assert data_root.user_root() == target
    assert result["active_root"] == str(target)
    assert not source.exists()


def test_environment_override_disables_relocation_and_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "environment-root"
    override.mkdir()
    monkeypatch.setenv("ANOTHER_LLM_USER_ROOT", str(override))
    with pytest.raises(ValueError, match="environment"):
        data_root.request_relocation(tmp_path / "external")
    with pytest.raises(ValueError, match="environment"):
        data_root.reset_to_default(confirm=True)
