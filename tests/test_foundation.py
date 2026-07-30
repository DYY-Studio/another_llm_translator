from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.config import ConfigError, load_config
from app.errors import StorageError, UsageError
from app.project import (
    bundle_hash,
    decode_txt,
    discover_inputs,
    init_project,
    sync_global_templates,
)
from app.storage import append_jsonl, read_json, read_jsonl, record_header


def make_app_root(tmp_path: Path) -> Path:
    root = tmp_path / "app-root"
    (root / "config").mkdir(parents=True)
    (root / "prompts").mkdir()
    source_root = Path(__file__).parents[1]
    (root / "config" / "config.toml").write_text(
        (source_root / "config" / "config.toml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    for source in (source_root / "prompts").glob("*.middle.txt"):
        (root / "prompts" / source.name).write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8"
        )
    return root


def test_config_rejects_unknown_key(tmp_path: Path) -> None:
    config = Path(__file__).parents[1] / "config" / "config.toml"
    text = config.read_text(encoding="utf-8").replace(
        'output_encoding = "utf-8-sig"',
        'output_encoding = "utf-8-sig"\nunknown = true',
    )
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="未知配置键"):
        load_config(path)


def test_config_rejects_invalid_numeric_types(tmp_path: Path) -> None:
    config = Path(__file__).parents[1] / "config" / "config.toml"
    text = re.sub(
        r"(?m)^max_parallel\s*=.*$",
        "max_parallel = 1.5",
        config.read_text(encoding="utf-8"),
    )
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="max_parallel 必须是正整数"):
        load_config(path)


def test_config_allows_output_cap_larger_than_context(tmp_path: Path) -> None:
    config = Path(__file__).parents[1] / "config" / "config.toml"
    text = config.read_text(encoding="utf-8")
    for key, value in (
        ("max_output_tokens", "65536"),
        ("context_window_tokens", "8192"),
        ("context_safety_margin_tokens", "0"),
        ("target_chunk_input_tokens", "11000"),
    ):
        text = re.sub(rf"(?m)^{key}\s*=.*$", f"{key} = {value}", text)
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    loaded = load_config(path)
    assert loaded["llm"]["max_output_tokens"] == 65536
    assert loaded["llm"]["context_window_tokens"] == 8192


def test_config_accepts_disabled_rate_limits(tmp_path: Path) -> None:
    config = Path(__file__).parents[1] / "config" / "config.toml"
    text = (
        config.read_text(encoding="utf-8")
        .replace("requests_per_minute = 30", "requests_per_minute = 0")
        .replace("input_tokens_per_minute = 50000", "input_tokens_per_minute = 0")
    )
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    loaded = load_config(path)
    assert loaded["execution"]["requests_per_minute"] == 0
    assert loaded["execution"]["input_tokens_per_minute"] == 0


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
    assert read_json(project / "project.json")["name"] == "demo"
    files = read_jsonl(project / "source" / "files.jsonl")
    assert len(files) == 3
    assert [item["original_name"] for item in files] == [
        "2.txt",
        "10.txt",
        "chapter/1.txt",
    ]
    segments = read_jsonl(project / "source" / "segments.jsonl")
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
    assert read_json(project / "project.json")["global_bundle_hash_seen"] == bundle_hash(
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
    before = read_json(project / "project.json")["global_bundle_hash_seen"]
    (app_root / "prompts" / "translation.middle.txt").write_text(
        "changed", encoding="utf-8"
    )
    warnings = sync_global_templates(
        project, app_root=app_root, interactive=False
    )
    assert any("非交互环境" in warning for warning in warnings)
    assert read_json(project / "project.json")["global_bundle_hash_seen"] == before


def test_jsonl_tail_repair(tmp_path: Path) -> None:
    path = tmp_path / "records.jsonl"
    append_jsonl(path, record_header("test", "PRJ", value=1))
    with path.open("ab") as handle:
        handle.write(b'{"schema_version":1')
    records = read_jsonl(path)
    assert [item["value"] for item in records] == [1]
    assert list(tmp_path.glob("records.jsonl.*.corrupt-tail"))
    assert json.loads(path.read_text(encoding="utf-8"))


def test_jsonl_middle_corruption_stops_without_repair(tmp_path: Path) -> None:
    path = tmp_path / "records.jsonl"
    path.write_text(
        '{"schema_version":1,"value":1}\n'
        '{"schema_version":\n'
        '{"schema_version":1,"value":2}\n',
        encoding="utf-8",
    )
    original = path.read_bytes()
    with pytest.raises(StorageError, match="中间行损坏"):
        read_jsonl(path)
    assert path.read_bytes() == original


def test_persisted_record_rejects_unsupported_enum(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    path.write_text(
        '{"schema_version":1,"status":"unknown"}\n',
        encoding="utf-8",
    )
    with pytest.raises(StorageError, match="不支持的 status"):
        read_json(path)
