from __future__ import annotations

import json
from pathlib import Path

from app.llm_migration import migrate_llm_resources

ROOT = Path(__file__).parents[1]


def old_preset(root: Path, effort: dict) -> Path:
    directory = root / "llm_presets"
    directory.mkdir()
    value = json.loads((ROOT / "llm_presets/openai-responses.json").read_text())
    value["schema_version"] = 7
    value.pop("send_temperature")
    value.pop("reasoning_effort")
    value["extra_body"] = effort
    path = directory / "sample.json"
    value["preset_id"] = "sample"
    path.write_text(json.dumps(value))
    return path


def test_migration_previews_then_moves_effort_without_touching_snapshots(tmp_path: Path) -> None:
    path = old_preset(tmp_path, {"reasoning": {"effort": "high", "summary": "auto"}})
    original = path.read_bytes()
    snapshot = tmp_path / "projects/sample/runs/old/llm_preset.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(original)
    reports = migrate_llm_resources(root=tmp_path, builtin_root=ROOT)
    assert reports[0]["status"] == "preview"
    assert path.read_bytes() == original
    reports = migrate_llm_resources(root=tmp_path, builtin_root=ROOT, apply=True)
    assert reports[0]["status"] == "updated"
    upgraded = json.loads(path.read_text())
    assert upgraded["schema_version"] == 8
    assert upgraded["send_temperature"] is True
    assert upgraded["reasoning_effort"] == "high"
    assert upgraded["extra_body"] == {"reasoning": {"summary": "auto"}}
    assert Path(reports[0]["backup"]).read_bytes() == original
    assert snapshot.read_bytes() == original


def test_migration_leaves_conflicting_effort_unchanged(tmp_path: Path) -> None:
    path = old_preset(tmp_path, {"reasoning": {"effort": "unsupported"}})
    original = path.read_bytes()
    reports = migrate_llm_resources(root=tmp_path, builtin_root=ROOT, apply=True)
    assert reports[0]["status"] == "manual"
    assert path.read_bytes() == original


def test_failed_adapter_apply_keeps_dependent_preset_unchanged(tmp_path: Path, monkeypatch) -> None:
    import hashlib
    from app import llm_migration
    preset_path = old_preset(tmp_path, {})
    original = preset_path.read_bytes()
    directory = tmp_path / "llm_adapters"
    directory.mkdir()
    path = directory / "openai-responses.json"
    legacy = json.loads((ROOT / "llm_adapters/openai-responses.json").read_text())
    legacy["schema_version"] = 3
    path.write_text(json.dumps(legacy))
    monkeypatch.setitem(llm_migration._LEGACY_ADAPTER_HASHES, "openai-responses", hashlib.sha256(path.read_bytes()).hexdigest())
    path.with_suffix(".json.pre-contract-upgrade.bak").write_text("existing backup")
    reports = migrate_llm_resources(root=tmp_path, builtin_root=ROOT, apply=True)
    assert [report["status"] for report in reports] == ["manual", "manual"]
    assert preset_path.read_bytes() == original
    assert "Adapter 迁移失败" in reports[1]["detail"]
