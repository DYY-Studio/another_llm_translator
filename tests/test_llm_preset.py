from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.errors import ConfigError
from app.llm_adapter import load_json_adapter
from app.llm_preset import load_llm_preset


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


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"extra_body": []}, "extra_body 必须是 JSON 对象"),
        ({"extra_body": {"secret": "${api_key}"}}, "不允许模板占位符"),
        ({"context_window_tokens": 0}, "context_window_tokens 必须是正整数"),
        ({"requests_per_minute": -1}, "requests_per_minute 必须是非负整数"),
        ({"token_safety_factor": 0}, "token_safety_factor 必须大于 0"),
        ({"base_url": "not-a-url"}, "base_url 必须是有效"),
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
