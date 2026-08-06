from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import ConfigError, dump_config, load_config, load_project_config
from app.errors import StorageError, UsageError
from app.main import run
from app.project import (
    bundle_hash,
    decode_txt,
    discover_inputs,
    init_project,
    resolve_project_parent,
    sync_global_templates,
)
from app.sqlite_storage import (
    _validate_record,
    read_files,
    read_json,
    read_segments,
)


def make_app_root(tmp_path: Path) -> Path:
    root = tmp_path / "app-root"
    (root / "config").mkdir(parents=True)
    (root / "prompts").mkdir()
    (root / "llm_adapters").mkdir()
    (root / "llm_presets").mkdir()
    source_root = Path(__file__).parents[1]
    (root / "config" / "config.toml").write_text(
        (source_root / "config" / "config.toml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    for source in (source_root / "prompts").glob("*.middle.txt"):
        (root / "prompts" / source.name).write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8"
        )
    for source in (source_root / "llm_adapters").glob("*.json"):
        (root / "llm_adapters" / source.name).write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8"
        )
    for source in (source_root / "llm_presets").glob("*.json"):
        (root / "llm_presets" / source.name).write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8"
        )
    return root


def test_init_does_not_copy_adapters_into_project(tmp_path: Path) -> None:
    app_root = make_app_root(tmp_path)
    adapter = json.loads(
        (app_root / "llm_adapters" / "openai-compatible.json").read_text(
            encoding="utf-8"
        )
    )
    adapter["adapter_id"] = "alternate"
    (app_root / "llm_adapters" / "alternate.json").write_text(
        json.dumps(adapter), encoding="utf-8"
    )
    preset = json.loads(
        (app_root / "llm_presets" / "default.json").read_text(encoding="utf-8")
    )
    preset.update(preset_id="alternate", adapter_id="alternate")
    (app_root / "llm_presets" / "alternate.json").write_text(
        json.dumps(preset), encoding="utf-8"
    )
    config_path = app_root / "config" / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'preset_translation = ""',
            'preset_translation = "alternate"',
        ),
        encoding="utf-8",
    )
    source = tmp_path / "input.txt"
    source.write_text("one", encoding="utf-8")

    project, _ = init_project(
        [str(source)],
        name="stage-adapters",
        app_root=app_root,
        projects_root=tmp_path / "projects",
    )

    assert project is not None
    assert not (project / "llm_adapters").exists()
    terminology = load_project_config(project, presets_root=app_root)
    translation = load_project_config(
        project, stage="translation", presets_root=app_root
    )
    assert terminology["_llm_adapter"].adapter_id == "openai-compatible"
    assert translation["_llm_adapter"].adapter_id == "alternate"


def test_cli_init_creates_project_in_explicit_parent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    parent = tmp_path / "external"
    parent.mkdir()

    assert run(
        [
            "init",
            "--name",
            "external-project",
            "--empty",
            "--parent-dir",
            str(parent),
        ]
    ) == 0

    summary = json.loads(capsys.readouterr().out)
    assert Path(summary["project_path"]) == parent / "external-project"
    assert (parent / "external-project" / "project.sqlite").is_file()


def test_project_parent_rejects_relative_and_unwritable_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(UsageError, match="必须是绝对路径"):
        resolve_project_parent("relative", require_absolute=True)

    monkeypatch.setattr("app.project.os.access", lambda *_: False)
    with pytest.raises(UsageError, match="不可写"):
        resolve_project_parent(tmp_path)


CONFIG_TEMPLATE = Path(__file__).parents[1] / "config" / "config.toml"


@pytest.mark.parametrize(
    ("old", "new", "key", "expected"),
    [
        (
            'unicode_normalization = "NFKC"',
            'unicode_normalization = ""',
            "unicode_normalization",
            "",
        ),
        (
            'unicode_normalization = "NFKC"',
            'unicode_normalization = "NFC"',
            "unicode_normalization",
            "NFC",
        ),
        (
            'unicode_normalization = "NFKC"',
            'unicode_normalization = "NFD"',
            "unicode_normalization",
            "NFD",
        ),
        (
            'unicode_normalization = "NFKC"',
            'unicode_normalization = "NFKC"',
            "unicode_normalization",
            "NFKC",
        ),
        (
            'unicode_normalization = "NFKC"',
            'unicode_normalization = "NFKD"',
            "unicode_normalization",
            "NFKD",
        ),
        (
            "case_insensitive = true",
            "case_insensitive = false",
            "case_insensitive",
            False,
        ),
    ],
)
def test_config_accepts_selectable_terminology_settings(
    tmp_path: Path, old: str, new: str, key: str, expected: object
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG_TEMPLATE.read_text(encoding="utf-8").replace(old, new),
        encoding="utf-8",
    )
    assert load_config(path)["terminology"][key] == expected


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            'output_encoding = "utf-8-sig"',
            'output_encoding = "utf-8-sig"\nunknown = true',
            "未知配置键",
        ),
        (
            "target_chunk_input_tokens = 11000",
            "target_chunk_input_tokens = 1.5",
            "target_chunk_input_tokens 必须是正整数",
        ),
        (
            'alias_primary_collision = "conflict"',
            'alias_primary_collision = "guess"',
            "alias_primary_collision 必须是 conflict 或 merge",
        ),
        (
            'unicode_normalization = "NFKC"',
            'unicode_normalization = "FOO"',
            "unicode_normalization 必须是空字符串或",
        ),
        (
            "case_insensitive = true",
            'case_insensitive = "yes"',
            "case_insensitive 必须是布尔值",
        ),
    ],
)
def test_config_rejects_invalid_values(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG_TEMPLATE.read_text(encoding="utf-8").replace(old, new),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_config_defaults_alias_collision_for_existing_projects(
    tmp_path: Path,
) -> None:
    app_root = make_app_root(tmp_path)
    config_path = app_root / "config" / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'alias_primary_collision = "conflict"\n',
            "",
        ).replace("cross_boundary_batching = []\n", ""),
        encoding="utf-8",
    )
    assert load_config(config_path)["terminology"]["alias_primary_collision"] == (
        "conflict"
    )
    assert load_config(config_path)["chunking"]["cross_boundary_batching"] == []


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ('["unknown"]', "未知阶段"),
        ('["translation", "translation"]', "重复阶段"),
    ],
)
def test_config_rejects_invalid_cross_boundary_batching(
    tmp_path: Path, value: str, message: str
) -> None:
    source = Path(__file__).parents[1] / "config" / "config.toml"
    path = tmp_path / "config.toml"
    path.write_text(
        source.read_text(encoding="utf-8").replace(
            "cross_boundary_batching = []", f"cross_boundary_batching = {value}"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_config_canonical_serialization_round_trips(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "config" / "config.toml"
    config = load_config(source)
    config["project"]["target_language"] = '简体中文 "测试"'

    path = tmp_path / "config.toml"
    path.write_text(dump_config(config), encoding="utf-8")

    assert load_config(path) == config
    text = path.read_text(encoding="utf-8")
    assert "[context.translation]" in text
    assert 'target_language = "简体中文 \\"测试\\""' in text
    assert "cross_boundary_batching = []" in text


def test_decode_gbk_as_gb18030() -> None:
    source = "你好，世界。这是一段用于编码探测的简体中文测试文本。"
    text, detected, used, _, _ = decode_txt(
        source.encode("gb18030"),
        confidence_threshold=0.0,
        fallback_encoding="utf-8",
    )
    assert text == source
    assert detected
    assert used.casefold() in {"gb18030", "gb2312"}


def test_init_preserves_files_segments_and_empty_lines(tmp_path: Path) -> None:
    app_root = make_app_root(tmp_path)
    inputs = tmp_path / "inputs"
    (inputs / "chapter").mkdir(parents=True)
    (inputs / "10.txt").write_text(
        "first\n\u3000\n \t\n  text  \nlast", encoding="utf-8-sig"
    )
    (inputs / "2.txt").write_text("second\n", encoding="utf-8")
    (inputs / "chapter" / "1.txt").write_text("\ninside", encoding="utf-8")

    project, summary = init_project(
        [str(inputs)],
        name="demo",
        recursive=True,
        app_root=app_root,
        projects_root=tmp_path / "projects",
    )

    assert project is not None
    assert summary["file_count"] == 3
    assert read_json(project, project / "project.json")["name"] == "demo"
    files = read_files(project)
    assert len(files) == 3
    assert [item["original_name"] for item in files] == [
        "2.txt",
        "10.txt",
        "chapter/1.txt",
    ]
    segments = read_segments(project)
    assert len(segments) == 9
    assert sum(bool(item["is_empty"]) for item in segments) == 4
    by_source = {str(item["source"]): bool(item["is_empty"]) for item in segments}
    assert by_source[""] is True
    assert by_source["\u3000"] is True
    assert by_source[" \t"] is True
    assert by_source["  text  "] is False
    assert (project / "prompts" / "translation.middle.txt").is_file()
    assert load_config(project / "config.toml")["project"]["target_language"]


def test_explicit_inputs_reject_case_insensitive_output_collision(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first" / "A.txt"
    second = tmp_path / "second" / "a.txt"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")
    with pytest.raises(UsageError, match="重复导出相对路径"):
        discover_inputs([str(first), str(second)], recursive=False)


def test_init_dry_run_writes_nothing(tmp_path: Path) -> None:
    app_root = make_app_root(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text("one\ntwo", encoding="utf-8")
    project, summary = init_project(
        [str(source)],
        name="demo",
        dry_run=True,
        app_root=app_root,
        projects_root=tmp_path / "projects",
    )
    assert project is None
    assert summary["segment_count"] == 2
    assert not (tmp_path / "projects").exists()


def test_template_sync_keep_and_update(tmp_path: Path) -> None:
    app_root = make_app_root(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text("one", encoding="utf-8")
    project, _ = init_project(
        [str(source)],
        name="demo",
        app_root=app_root,
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    project_prompt = project / "prompts" / "translation.middle.txt"
    project_prompt.write_text("project custom", encoding="utf-8")
    global_prompt = app_root / "prompts" / "translation.middle.txt"
    global_prompt.write_text("global changed", encoding="utf-8")

    warnings = sync_global_templates(
        project, app_root=app_root, interactive=True, choice="keep"
    )
    assert "已保留项目模板" in warnings
    assert project_prompt.read_text(encoding="utf-8") == "project custom"
    assert read_json(project, project / "project.json")["global_bundle_hash_seen"] == bundle_hash(
        app_root
    )

    global_prompt.write_text("global changed again", encoding="utf-8")
    warnings = sync_global_templates(
        project, app_root=app_root, interactive=True, choice="update"
    )
    assert any("已更新项目模板" in item for item in warnings)
    assert project_prompt.read_text(encoding="utf-8") == "global changed again"
    assert list((project / "snapshots" / "template_updates").iterdir())


def test_template_sync_interactive_prompt_uses_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app_root = make_app_root(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text("one", encoding="utf-8")
    project, _ = init_project(
        [str(source)],
        name="demo",
        app_root=app_root,
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    (app_root / "prompts" / "translation.middle.txt").write_text(
        "changed", encoding="utf-8"
    )
    monkeypatch.setattr("builtins.input", lambda: "keep")

    sync_global_templates(project, app_root=app_root, interactive=True)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "更新项目模板" in captured.err


def test_invalid_global_template_does_not_block_project_copy(tmp_path: Path) -> None:
    app_root = make_app_root(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text("one", encoding="utf-8")
    project, _ = init_project(
        [str(source)],
        name="demo",
        app_root=app_root,
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    original = (project / "config.toml").read_text(encoding="utf-8")
    (app_root / "config" / "config.toml").write_text(
        original + "\nunknown = true\n", encoding="utf-8"
    )

    warnings = sync_global_templates(project, app_root=app_root)

    assert warnings and "全局模板无效" in warnings[0]
    assert (project / "config.toml").read_text(encoding="utf-8") == original


def test_noninteractive_template_sync_preserves_seen_hash(tmp_path: Path) -> None:
    app_root = make_app_root(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text("one", encoding="utf-8")
    project, _ = init_project(
        [str(source)],
        name="demo",
        app_root=app_root,
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    before = read_json(project, project / "project.json")["global_bundle_hash_seen"]
    (app_root / "prompts" / "translation.middle.txt").write_text(
        "changed", encoding="utf-8"
    )
    warnings = sync_global_templates(
        project, app_root=app_root, interactive=False
    )
    assert any("非交互环境" in warning for warning in warnings)
    assert read_json(project, project / "project.json")["global_bundle_hash_seen"] == before


def test_persisted_record_rejects_unsupported_enum() -> None:
    with pytest.raises(StorageError, match="不支持的 status"):
        _validate_record({"schema_version": 1, "status": "unknown"}, "test")
