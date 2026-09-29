from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import data_root


@pytest.mark.parametrize("mismatch", ("paths", "type", "size", "symlink"))
def test_copy_mismatch_is_rejected_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch: str
) -> None:
    monkeypatch.delenv("ANOTHER_LLM_USER_ROOT", raising=False)
    monkeypatch.setattr(
        "app.user_config._platform_data_base", lambda: tmp_path / "system"
    )
    source = tmp_path / "system" / "another-llm-translator"
    source.mkdir(parents=True)
    (source / "entry.txt").write_text("content", encoding="utf-8")
    (source / "linked.txt").symlink_to("entry.txt")
    parent = tmp_path / "external"
    parent.mkdir()
    data_root.request_relocation(parent)
    target = parent / "another-llm-translator"
    copytree = data_root.shutil.copytree

    def corrupt_copy(src: Path, dst: Path, **kwargs: object) -> Path:
        result = copytree(src, dst, **kwargs)
        if mismatch == "paths":
            (dst / "entry.txt").rename(dst / "renamed.txt")
        elif mismatch == "type":
            (dst / "entry.txt").unlink()
            (dst / "entry.txt").mkdir()
        elif mismatch == "size":
            (dst / "entry.txt").write_text("different size", encoding="utf-8")
        else:
            (dst / "linked.txt").unlink()
            (dst / "linked.txt").symlink_to("missing.txt")
        return result

    monkeypatch.setattr(data_root.shutil, "copytree", corrupt_copy)

    with pytest.raises(ValueError, match="verification"):
        data_root.apply_pending()

    assert data_root.user_root() == source
    assert source.is_dir()
    assert not target.exists()
    assert not list(parent.glob(".*.staging-*"))


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


def test_apply_pending_rejects_target_parent_replaced_by_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANOTHER_LLM_USER_ROOT", raising=False)
    monkeypatch.setattr(
        "app.user_config._platform_data_base", lambda: tmp_path / "system"
    )
    source = tmp_path / "system" / "another-llm-translator"
    source.mkdir(parents=True)
    (source / "entry.txt").write_text("source", encoding="utf-8")
    parent = tmp_path / "external"
    parent.mkdir()
    data_root.request_relocation(parent)
    redirected_parent = tmp_path / "redirected"
    redirected_parent.mkdir()
    parent.rename(tmp_path / "external-original")
    parent.symlink_to(redirected_parent, target_is_directory=True)

    with pytest.raises(ValueError, match="canonical"):
        data_root.apply_pending()

    assert data_root.user_root() == source
    assert source.is_dir()
    assert not (redirected_parent / "another-llm-translator").exists()


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


def test_apply_pending_restarts_its_marked_interrupted_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANOTHER_LLM_USER_ROOT", raising=False)
    monkeypatch.setattr(
        "app.user_config._platform_data_base", lambda: tmp_path / "system"
    )
    source = tmp_path / "system" / "another-llm-translator"
    source.mkdir(parents=True)
    (source / "entry.txt").write_text("complete", encoding="utf-8")
    parent = tmp_path / "external"
    parent.mkdir()
    data_root.request_relocation(parent)
    target = parent / "another-llm-translator"
    _, _, transaction_id = data_root._read_pending()
    staging = target.with_name(f".{target.name}.staging-{transaction_id}")
    staging.mkdir()
    (staging / "partial.txt").write_text("interrupted", encoding="utf-8")
    (staging.with_name(f"{staging.name}.json")).write_text(
        json.dumps(
            {
                "transaction_id": transaction_id,
                "source_root": str(source),
                "target_root": str(target),
            }
        ),
        encoding="utf-8",
    )

    result = data_root.apply_pending()

    assert result["active_root"] == str(target)
    assert (target / "entry.txt").read_text(encoding="utf-8") == "complete"
    assert not (target / "partial.txt").exists()
    assert not staging.exists()
    assert not staging.with_name(f"{staging.name}.json").exists()


def test_apply_pending_preserves_unmarked_staging_directory(
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
    target = parent / "another-llm-translator"
    _, _, transaction_id = data_root._read_pending()
    staging = target.with_name(f".{target.name}.staging-{transaction_id}")
    staging.mkdir()
    (staging / "keep.txt").write_text("unowned", encoding="utf-8")

    with pytest.raises(ValueError, match="no transaction marker"):
        data_root.apply_pending()

    assert (staging / "keep.txt").read_text(encoding="utf-8") == "unowned"
    assert source.is_dir()
    assert data_root.user_root() == source


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


def test_source_cleanup_failure_reports_active_and_old_roots_and_can_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ANOTHER_LLM_USER_ROOT", raising=False)
    monkeypatch.setattr(
        "app.user_config._platform_data_base", lambda: tmp_path / "system"
    )
    source = tmp_path / "system" / "another-llm-translator"
    source.mkdir(parents=True)
    (source / "entry.txt").write_text("data", encoding="utf-8")
    parent = tmp_path / "external"
    parent.mkdir()
    data_root.request_relocation(parent)
    target = parent / "another-llm-translator"
    rmtree = data_root.shutil.rmtree

    def fail_source(path: Path, *args: object, **kwargs: object) -> None:
        if Path(path) == source:
            raise OSError("source busy")
        rmtree(path, *args, **kwargs)

    monkeypatch.setattr(data_root.shutil, "rmtree", fail_source)
    exit_code = data_root.main(["apply-pending"])
    output = capsys.readouterr()

    assert exit_code == 0
    result = json.loads(output.out)
    assert result["active_root"] == str(target)
    assert result["old_root"] == str(source)
    assert "warning" in result
    assert "source busy" in result["warning"]
    assert data_root.user_root() == target
    assert source.is_dir()

    monkeypatch.setattr(data_root.shutil, "rmtree", rmtree)
    data_root.apply_pending()
    assert not source.exists()
    assert not data_root.pending_path().exists()


def test_non_object_pending_json_fails_without_cli_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ANOTHER_LLM_USER_ROOT", raising=False)
    monkeypatch.setattr(
        "app.user_config._platform_data_base", lambda: tmp_path / "system"
    )
    data_root.pending_path().parent.mkdir(parents=True)
    data_root.pending_path().write_text("[]", encoding="utf-8")

    exit_code = data_root.main(["apply-pending"])
    output = capsys.readouterr()

    assert exit_code == 1
    assert output.err.startswith("error:")
    assert "Traceback" not in output.err


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
