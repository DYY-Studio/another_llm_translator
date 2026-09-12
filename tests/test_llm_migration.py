from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.errors import ConfigError
from app.llm_migration import migrate_llm_resources
from app.llm_preset import load_llm_preset
from app.user_config import default_user_root

ROOT = Path(__file__).parents[1]


def test_llm_resource_migration_upgrades_preset_and_adapter_idempotently(
    tmp_path: Path,
) -> None:
    root = default_user_root(base=tmp_path)
    (root / "llm_presets").mkdir(parents=True)
    (root / "llm_adapters").mkdir(parents=True)
    preset = json.loads(
        (ROOT / "llm_presets" / "default.json").read_text("utf-8")
    )
    preset["schema_version"] = 2
    preset.pop("stream")
    preset.pop("stream_endpoint")
    (root / "llm_presets" / "custom.json").write_text(
        json.dumps(preset), encoding="utf-8"
    )
    preset_v3 = json.loads(
        (ROOT / "llm_presets" / "default.json").read_text("utf-8")
    )
    preset_v3["schema_version"] = 3
    preset_v3.pop("stream_read_timeout_enabled")
    (root / "llm_presets" / "custom-v3.json").write_text(
        json.dumps(preset_v3), encoding="utf-8"
    )
    adapter = json.loads(
        (ROOT / "llm_adapters" / "openai-compatible.json").read_text("utf-8")
    )
    adapter["schema_version"] = 1
    adapter.pop("streaming")
    (root / "llm_adapters" / "custom.json").write_text(
        json.dumps(adapter), encoding="utf-8"
    )

    assert migrate_llm_resources(base=tmp_path) == 3
    assert migrate_llm_resources(base=tmp_path) == 0
    upgraded_preset = json.loads(
        (root / "llm_presets" / "custom.json").read_text("utf-8")
    )
    upgraded_adapter = json.loads(
        (root / "llm_adapters" / "custom.json").read_text("utf-8")
    )
    assert upgraded_preset["schema_version"] == 5
    assert upgraded_preset["stream"] is False
    assert upgraded_preset["stream_endpoint"] == ""
    assert upgraded_preset["stream_read_timeout_enabled"] is True
    upgraded_v3 = json.loads(
        (root / "llm_presets" / "custom-v3.json").read_text("utf-8")
    )
    assert upgraded_v3["schema_version"] == 5
    assert upgraded_v3["stream_read_timeout_enabled"] is True
    assert upgraded_preset == load_llm_preset(
        root / "llm_presets" / "custom.json"
    ).definition
    assert upgraded_adapter["schema_version"] == 2
    assert "streaming" not in upgraded_adapter


def test_llm_resource_migration_uses_user_root_override_without_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "override"
    monkeypatch.setenv("ANOTHER_LLM_USER_ROOT", str(root))
    presets = root / "llm_presets"
    presets.mkdir(parents=True)
    value = json.loads(
        (ROOT / "llm_presets" / "default.json").read_text("utf-8")
    )
    value["schema_version"] = 2
    value.pop("stream")
    value.pop("stream_endpoint")
    (presets / "default.json").write_text(json.dumps(value), encoding="utf-8")

    assert migrate_llm_resources() == 1
    assert (
        json.loads((presets / "default.json").read_text("utf-8"))["schema_version"]
        == 5
    )


def test_llm_resource_migration_upgrades_v4_per_key_concurrency(
    tmp_path: Path,
) -> None:
    root = default_user_root(base=tmp_path)
    presets = root / "llm_presets"
    presets.mkdir(parents=True)
    value = json.loads(
        (ROOT / "llm_presets" / "default.json").read_text("utf-8")
    )
    value["schema_version"] = 4
    value.pop("max_parallel_per_key")
    (presets / "default.json").write_text(json.dumps(value), encoding="utf-8")

    assert migrate_llm_resources(base=tmp_path) == 1
    upgraded = json.loads((presets / "default.json").read_text("utf-8"))
    assert upgraded["schema_version"] == 5
    assert upgraded["max_parallel_per_key"] == upgraded["max_parallel"]


def test_llm_resource_migration_rejects_invalid_file_without_rewriting(
    tmp_path: Path,
) -> None:
    root = default_user_root(base=tmp_path)
    presets = root / "llm_presets"
    presets.mkdir(parents=True)
    path = presets / "broken.json"
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(ConfigError, match="迁移文件"):
        migrate_llm_resources(base=tmp_path)
    assert path.read_text("utf-8") == "{not-json"


def test_llm_resource_migration_reports_atomic_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = default_user_root(base=tmp_path)
    presets = root / "llm_presets"
    presets.mkdir(parents=True)
    value = json.loads(
        (ROOT / "llm_presets" / "default.json").read_text("utf-8")
    )
    value["schema_version"] = 2
    value.pop("stream")
    value.pop("stream_endpoint")
    path = presets / "default.json"
    path.write_text(json.dumps(value), encoding="utf-8")

    def fail(*_: object, **__: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("app.llm_migration.atomic_write_json", fail)
    with pytest.raises(ConfigError, match="无法写入"):
        migrate_llm_resources(base=tmp_path)
    assert json.loads(path.read_text("utf-8"))["schema_version"] == 2
