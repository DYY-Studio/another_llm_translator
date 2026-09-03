from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import (
    dump_config,
    load_config,
    load_global_config,
    load_project_config,
    load_run_config,
)
from app.errors import ConfigError
from app.execution import create_run, stage_fingerprint
from app.llm_adapter import load_json_adapter
from app.llm_preset import load_llm_preset
from app.project import init_project
from tests.test_foundation import make_app_root

ROOT = Path(__file__).parents[1]


def preset_definition() -> dict[str, object]:
    return json.loads((ROOT / "llm_presets" / "default.json").read_text("utf-8"))


def write_preset(tmp_path: Path, value: dict[str, object]) -> Path:
    path = tmp_path / f"{value.get('preset_id', 'default')}.json"
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def test_preset_loads_nested_extra_body_and_hashes_content(tmp_path: Path) -> None:
    value = preset_definition()
    value["extra_body"] = {
        "provider": {
            "order": ["anthropic", "google"],
            "allow_fallbacks": False,
        }
    }
    preset = load_llm_preset(write_preset(tmp_path, value))

    assert preset.preset_id == "default"
    assert preset.definition["extra_body"] == value["extra_body"]
    assert preset.digest.startswith("sha256:")
    assert preset.definition["extra_headers"] == {}


def test_preset_accepts_extra_headers_and_rejects_invalid_values(tmp_path: Path) -> None:
    value = preset_definition()
    value["extra_headers"] = {"x-opencode-session": "${session_id}"}
    preset = load_llm_preset(write_preset(tmp_path, value))
    assert preset.definition["extra_headers"] == value["extra_headers"]

    value["extra_headers"] = {"bad header": "value"}
    with pytest.raises(ConfigError, match="Header 名称"):
        load_llm_preset(write_preset(tmp_path, value))

    value["extra_headers"] = {"x-session": "${unknown}"}
    with pytest.raises(ConfigError, match="占位符"):
        load_llm_preset(write_preset(tmp_path, value))

    value["extra_headers"] = {"x-session": "${UNKNOWN}"}
    with pytest.raises(ConfigError, match="占位符"):
        load_llm_preset(write_preset(tmp_path, value))

    value["extra_headers"] = {"x-session": "${session_id"}
    with pytest.raises(ConfigError, match="占位符"):
        load_llm_preset(write_preset(tmp_path, value))


def test_preset_allows_disabled_limits_small_safety_factor_and_large_output(
    tmp_path: Path,
) -> None:
    value = preset_definition()
    value.update(
        requests_per_minute=0,
        input_tokens_per_minute=0,
        token_safety_factor=0.5,
        max_output_tokens=65536,
        context_window_tokens=8192,
    )
    preset = load_llm_preset(write_preset(tmp_path, value))
    assert preset.definition["requests_per_minute"] == 0
    assert preset.definition["token_safety_factor"] == 0.5
    assert preset.definition["max_output_tokens"] == 65536


def test_preset_allows_zero_max_output_tokens(tmp_path: Path) -> None:
    value = preset_definition()
    value["max_output_tokens"] = 0

    preset = load_llm_preset(write_preset(tmp_path, value))

    assert preset.definition["max_output_tokens"] == 0


def test_preset_v5_requires_per_key_concurrency(tmp_path: Path) -> None:
    value = preset_definition()
    value["schema_version"] = 5
    value["max_parallel_per_key"] = 2
    preset = load_llm_preset(write_preset(tmp_path, value))
    assert preset.definition["max_parallel_per_key"] == 2


def test_preset_v5_rejects_invalid_per_key_concurrency(tmp_path: Path) -> None:
    value = preset_definition()
    value["schema_version"] = 5
    value["max_parallel_per_key"] = 0
    with pytest.raises(ConfigError, match="max_parallel_per_key"):
        load_llm_preset(write_preset(tmp_path, value))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"extra_body": []}, "extra_body 必须是 JSON 对象"),
        ({"extra_body": {"secret": "${api_key}"}}, "不允许模板占位符"),
        ({"context_window_tokens": 0}, "context_window_tokens 必须是正整数"),
        ({"max_output_tokens": -1}, "max_output_tokens 必须是非负整数"),
        ({"requests_per_minute": -1}, "requests_per_minute 必须是非负整数"),
        ({"token_safety_factor": 0}, "token_safety_factor 必须大于 0"),
        ({"base_url": "not-a-url"}, "base_url 必须是有效"),
        (
            {"endpoint": "/v1/models/${other}:generateContent"},
            "endpoint 只允许",
        ),
        (
            {"endpoint": "/v1/models/${model"},
            "endpoint 只允许",
        ),
    ],
)
def test_preset_rejects_invalid_values(
    tmp_path: Path,
    change: dict[str, object],
    message: str,
) -> None:
    value = preset_definition()
    value.update(change)
    with pytest.raises(ConfigError, match=message):
        load_llm_preset(write_preset(tmp_path, value))


def test_preset_endpoint_allows_model_placeholder(tmp_path: Path) -> None:
    value = preset_definition()
    value["endpoint"] = "/v1beta/models/${model}:generateContent"
    preset = load_llm_preset(write_preset(tmp_path, value))
    assert preset.definition["endpoint"] == (
        "/v1beta/models/${model}:generateContent"
    )


def test_preset_v2_is_normalized_to_non_streaming_in_memory(tmp_path: Path) -> None:
    value = preset_definition()
    value["schema_version"] = 2
    value.pop("stream")
    value.pop("stream_endpoint")
    preset = load_llm_preset(write_preset(tmp_path, value))
    assert preset.definition["schema_version"] == 5
    assert preset.definition["stream"] is False
    assert preset.definition["stream_endpoint"] == ""
    assert preset.definition["stream_read_timeout_enabled"] is True
    assert preset.definition["max_parallel_per_key"] == preset.definition["max_parallel"]


def test_preset_v3_enables_stream_read_timeout_in_memory(tmp_path: Path) -> None:
    value = preset_definition()
    value["schema_version"] = 3
    value.pop("stream_read_timeout_enabled")
    preset = load_llm_preset(write_preset(tmp_path, value))
    assert preset.definition["schema_version"] == 5
    assert preset.definition["stream_read_timeout_enabled"] is True
    assert preset.definition["max_parallel_per_key"] == preset.definition["max_parallel"]


@pytest.mark.parametrize(
    "stream_endpoint",
    [
        "https://provider.example/stream",
        "/v1/${other}/stream",
        "/v1/${model}/stream/${other}",
    ],
)
def test_preset_rejects_invalid_stream_endpoint(
    tmp_path: Path, stream_endpoint: str
) -> None:
    value = preset_definition()
    value["stream"] = True
    value["stream_endpoint"] = stream_endpoint
    with pytest.raises(ConfigError, match="stream_endpoint|相对路径"):
        load_llm_preset(write_preset(tmp_path, value))


def test_preset_accepts_keychain_credential_reference(tmp_path: Path) -> None:
    value = preset_definition()
    value["credential"] = {"kind": "keychain", "name": "openai-main"}
    preset = load_llm_preset(write_preset(tmp_path, value))
    assert preset.definition["credential"] == {
        "kind": "keychain",
        "name": "openai-main",
    }


def test_preset_requires_boolean_stream_read_timeout(tmp_path: Path) -> None:
    value = preset_definition()
    value["stream_read_timeout_enabled"] = "false"
    with pytest.raises(ConfigError, match="stream_read_timeout_enabled 必须是布尔值"):
        load_llm_preset(write_preset(tmp_path, value))


@pytest.mark.parametrize(
    ("credential", "message"),
    [
        ("string", "必须是包含 kind 和 name 的对象"),
        ({"kind": "environment"}, "必须是包含 kind 和 name 的对象"),
        ({"kind": "environment", "name": "X", "extra": 1}, "必须是包含 kind 和 name 的对象"),
        ({"kind": "file", "name": "X"}, "kind 必须是 environment 或 keychain"),
        ({"kind": "environment", "name": " "}, "name 必须是非空字符串"),
    ],
)
def test_preset_rejects_invalid_credential_reference(
    tmp_path: Path,
    credential: object,
    message: str,
) -> None:
    value = preset_definition()
    value["credential"] = credential
    with pytest.raises(ConfigError, match=message):
        load_llm_preset(write_preset(tmp_path, value))


def test_preset_rejects_v1_schema_with_clear_message(tmp_path: Path) -> None:
    value = preset_definition()
    value["schema_version"] = 1
    value["api_key_env"] = "LLM_API_KEY"
    del value["credential"]
    with pytest.raises(ConfigError, match="schema_version 必须是 5"):
        load_llm_preset(write_preset(tmp_path, value))


def test_adapter_merges_extra_body_without_overwriting(tmp_path: Path) -> None:
    adapter = load_json_adapter(
        ROOT / "llm_adapters" / "openai-compatible.json"
    )
    _, body = adapter.build_request(
        api_key="secret",
        model="model",
        messages=[],
        temperature=0.2,
        max_output_tokens=100,
        stream=False,
        extra_body={"provider": {"order": ["google"]}},
    )
    assert body["provider"] == {"order": ["google"]}

    with pytest.raises(ConfigError, match="字段冲突：model"):
        adapter.build_request(
            api_key="secret",
            model="model",
            messages=[],
            temperature=0.2,
            max_output_tokens=100,
            stream=False,
            extra_body={"model": "override"},
        )


def test_project_resolves_live_preset_and_run_freezes_snapshot(
    tmp_path: Path,
) -> None:
    app_root = make_app_root(tmp_path)
    source = tmp_path / "input.txt"
    source.write_text("one", encoding="utf-8")
    project, _ = init_project(
        [str(source)],
        name="preset-project",
        app_root=app_root,
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    raw = load_config(project / "config.toml")
    assert raw["llm"]["preset"] == "default"
    assert "model" not in raw["llm"]

    first = load_project_config(project, presets_root=app_root)
    assert first["execution"]["max_parallel_per_key"] == 4
    first_fingerprint = stage_fingerprint(first, "translation", "prompt")
    preset_file = app_root / "llm_presets" / "default.json"
    definition = json.loads(preset_file.read_text("utf-8"))
    definition["model"] = "changed-model"
    definition["extra_body"] = {"provider": {"order": ["google"]}}
    preset_file.write_text(json.dumps(definition), encoding="utf-8")

    second = load_project_config(project, presets_root=app_root)
    assert second["llm"]["model"] == "changed-model"
    assert second["_llm_extra_body"] == definition["extra_body"]
    assert stage_fingerprint(second, "translation", "prompt") != first_fingerprint
    second["_document_adapters"] = {
        "F0001": {"adapter_id": "txt", "version": "1"}
    }
    second["_document_adapter_options"] = {
        "F0001": {"inline_format_mode": "plain"}
    }
    adapter_fingerprint = stage_fingerprint(second, "translation", "prompt")
    second["_document_adapters"]["F0001"]["version"] = "2"
    assert stage_fingerprint(second, "translation", "prompt") != adapter_fingerprint
    second["_document_adapters"]["F0001"]["version"] = "1"
    run_id, run_dir = create_run(
        project,
        config=second,
        stage="translation",
        fingerprint="fingerprint",
        prompt="prompt",
        selected_count=1,
        requested_count=1,
        reused_count=0,
    )
    assert run_id
    assert json.loads((run_dir / "llm_preset.json").read_text("utf-8")) == definition
    manifest = json.loads((run_dir / "manifest.json").read_text("utf-8"))
    assert manifest["document_adapters"] == {
        "F0001": {"adapter_id": "txt", "version": "1"}
    }
    assert manifest["document_adapter_options"] == {
        "F0001": {"inline_format_mode": "plain"}
    }
    assert load_run_config(run_dir)["llm"]["model"] == "changed-model"

    (run_dir / "llm_preset.json").unlink()
    with pytest.raises(ConfigError, match="无法读取 LLM Preset"):
        load_run_config(run_dir)


def test_project_resolves_stage_preset_override_and_inherits_global(
    tmp_path: Path,
) -> None:
    app_root = make_app_root(tmp_path)
    alternate = preset_definition()
    alternate.update(preset_id="alternate", model="alternate-model")
    (app_root / "llm_presets" / "alternate.json").write_text(
        json.dumps(alternate), encoding="utf-8"
    )
    source = tmp_path / "input.txt"
    source.write_text("one", encoding="utf-8")
    project, _ = init_project(
        [str(source)],
        name="stage-preset-project",
        app_root=app_root,
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    config = load_config(project / "config.toml")
    config["llm"]["preset_translation"] = "alternate"
    (project / "config.toml").write_text(dump_config(config), encoding="utf-8")

    terminology = load_project_config(
        project, stage="terminology", presets_root=app_root
    )
    translation = load_project_config(
        project, stage="translation", presets_root=app_root
    )

    assert terminology["_llm_preset_id"] == "default"
    assert translation["_llm_preset_id"] == "alternate"
    assert translation["llm"]["model"] == "alternate-model"
    assert stage_fingerprint(terminology, "translation", "prompt") != (
        stage_fingerprint(translation, "translation", "prompt")
    )


def test_project_preset_requires_existing_global_adapter(tmp_path: Path) -> None:
    app_root = make_app_root(tmp_path)
    source = tmp_path / "input.txt"
    source.write_text("one", encoding="utf-8")
    project, _ = init_project(
        [str(source)],
        name="preset-project",
        app_root=app_root,
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    preset_file = app_root / "llm_presets" / "default.json"
    definition = json.loads(preset_file.read_text("utf-8"))
    definition["adapter_id"] = "missing-adapter"
    preset_file.write_text(json.dumps(definition), encoding="utf-8")

    with pytest.raises(ConfigError, match="无法读取 LLM Adapter"):
        load_project_config(project, presets_root=app_root)


def test_inline_project_config_is_rejected(tmp_path: Path) -> None:
    app_root = make_app_root(tmp_path)
    source = tmp_path / "input.txt"
    source.write_text("one", encoding="utf-8")
    project, _ = init_project(
        [str(source)],
        name="legacy-project",
        app_root=app_root,
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'preset = "default"', 'adapter = "openai-compatible"'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="未知配置键 config.llm: adapter"):
        load_project_config(project, presets_root=app_root)


def test_inline_global_config_is_rejected(tmp_path: Path) -> None:
    app_root = make_app_root(tmp_path)
    config_path = app_root / "config" / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'preset = "default"', 'model = "legacy-model"'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="未知配置键 config.llm: model"):
        load_global_config(app_root)


def test_shipped_adapters_and_presets_load_and_dry_run() -> None:
    adapters = {path.stem for path in (ROOT / "llm_adapters").glob("*.json")}
    presets = {path.stem for path in (ROOT / "llm_presets").glob("*.json")}
    assert adapters >= {
        "openai-compatible",
        "anthropic",
        "google-gemini",
        "openai-responses",
    }
    assert presets >= {
        "default",
        "anthropic-claude",
        "google-gemini",
        "openai-responses",
    }
    for preset_file in sorted((ROOT / "llm_presets").glob("*.json")):
        preset = load_llm_preset(preset_file)
        adapter = load_json_adapter(
            ROOT / "llm_adapters" / f"{preset.adapter_id}.json"
        )
        adapter.build_request(
            api_key="***",
            model=preset.definition["model"],
            messages=[
                {"role": "system", "content": "prompt"},
                {"role": "user", "content": '{"segments":[]}'},
            ],
            temperature=0.2,
            max_output_tokens=int(preset.definition["max_output_tokens"]),
            stream=False,
            extra_body=preset.definition["extra_body"],
        )
        if adapter.models_spec is not None:
            adapter.build_models_request(api_key="***")


def test_project_resolves_gemini_preset_endpoint_placeholder(tmp_path: Path) -> None:
    app_root = make_app_root(tmp_path)
    config_path = app_root / "config" / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'preset = "default"', 'preset = "google-gemini"'
        ),
        encoding="utf-8",
    )
    source = tmp_path / "input.txt"
    source.write_text("one", encoding="utf-8")
    project, _ = init_project(
        [str(source)],
        name="gemini-project",
        app_root=app_root,
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    assert not (project / "llm_adapters").exists()

    resolved = load_project_config(project, presets_root=app_root)
    assert resolved["llm"]["adapter"] == "google-gemini"
    assert resolved["llm"]["endpoint"] == (
        "/models/${model}:generateContent"
    )
    assert resolved["_llm_adapter"].messages_format == "gemini"
