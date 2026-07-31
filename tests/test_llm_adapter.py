from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.errors import ConfigError, ExternalError
from app.llm_adapter import load_json_adapter


def write_adapter(tmp_path: Path, value: dict[str, object]) -> Path:
    path = tmp_path / "adapter.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def definition() -> dict[str, object]:
    return {
        "schema_version": 1,
        "adapter_id": "custom-json",
        "headers": {
            "Authorization": "Bearer ${api_key}",
            "X-Model": "${model}",
        },
        "body": {
            "deployment": "${model}",
            "input": {"conversation": "${messages}"},
            "sampling": {
                "temperature": "${temperature}",
                "output": "${max_output_tokens}",
            },
            "streaming": "${stream}",
            "reasoning_effort": "high",
        },
        "response_content_pointer": "/result/0/text",
    }


def test_json_adapter_renders_typed_values_and_custom_fields(
    tmp_path: Path,
) -> None:
    adapter = load_json_adapter(write_adapter(tmp_path, definition()))
    headers, body = adapter.build_request(
        api_key="secret",
        model="model-a",
        messages=[{"role": "user", "content": "hello"}],
        temperature=0.25,
        max_output_tokens=321,
        stream=False,
    )

    assert headers == {
        "Authorization": "Bearer secret",
        "X-Model": "model-a",
    }
    assert body == {
        "deployment": "model-a",
        "input": {
            "conversation": [{"role": "user", "content": "hello"}]
        },
        "sampling": {"temperature": 0.25, "output": 321},
        "streaming": False,
        "reasoning_effort": "high",
    }
    assert "secret" not in json.dumps(body)
    response = adapter.parse_response({"result": [{"text": "answer"}]})
    assert response.content == "answer"
    assert response.reasoning_content is None


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda value: value["body"].update({"secret": "${api_key}"}),
            "未知占位符",
        ),
        (
            lambda value: value["body"].update({"bad": "x-${model}"}),
            "必须独占字符串值",
        ),
        (
            lambda value: value["headers"].update({"X": "${unknown}"}),
            "未知占位符",
        ),
        (
            lambda value: value.update(
                {"response_content_pointer": "/bad~2pointer"}
            ),
            "转义无效",
        ),
        (
            lambda value: value.update(
                {"response_reasoning_content_pointer": "reasoning"}
            ),
            "必须是 JSON Pointer",
        ),
    ],
)
def test_json_adapter_rejects_invalid_templates(
    tmp_path: Path, mutate: object, message: str
) -> None:
    value = definition()
    mutate(value)
    with pytest.raises(ConfigError, match=message):
        load_json_adapter(write_adapter(tmp_path, value))


def test_json_adapter_response_path_fails_fast(tmp_path: Path) -> None:
    adapter = load_json_adapter(write_adapter(tmp_path, definition()))
    with pytest.raises(ExternalError, match="正文路径"):
        adapter.parse_response({"result": []})
    with pytest.raises(ExternalError, match="不是字符串"):
        adapter.parse_response({"result": [{"text": 3}]})


def test_json_adapter_extracts_optional_reasoning_content(tmp_path: Path) -> None:
    value = definition()
    value["response_reasoning_content_pointer"] = "/result/0/reasoning"
    adapter = load_json_adapter(write_adapter(tmp_path, value))

    response = adapter.parse_response(
        {"result": [{"text": "answer", "reasoning": "thought"}]}
    )
    assert response.content == "answer"
    assert response.reasoning_content == "thought"
    assert adapter.parse_response(
        {"result": [{"text": "answer", "reasoning": None}]}
    ).reasoning_content is None

    with pytest.raises(ExternalError, match="思考正文路径"):
        adapter.parse_response({"result": [{"text": "answer"}]})
    with pytest.raises(ExternalError, match="字符串或 null"):
        adapter.parse_response(
            {"result": [{"text": "answer", "reasoning": {}}]}
        )
