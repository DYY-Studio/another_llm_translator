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


def test_json_adapter_anthropic_format_extracts_system_and_roles(
    tmp_path: Path,
) -> None:
    value = definition()
    value["messages_format"] = "anthropic"
    value["headers"] = {"x-api-key": "${api_key}"}
    value["body"] = {
        "model": "${model}",
        "system": "${system}",
        "messages": "${messages}",
        "max_tokens": "${max_output_tokens}",
    }
    value["response_content_pointer"] = "/content/-1/text"
    adapter = load_json_adapter(write_adapter(tmp_path, value))

    headers, body = adapter.build_request(
        api_key="secret",
        model="model-a",
        messages=[
            {"role": "system", "content": "prompt"},
            {"role": "assistant", "content": "previous"},
            {"role": "user", "content": "hello"},
        ],
        temperature=0.2,
        max_output_tokens=100,
        stream=False,
    )
    assert headers == {"x-api-key": "secret"}
    assert body == {
        "model": "model-a",
        "system": "prompt",
        "messages": [
            {"role": "assistant", "content": "previous"},
            {"role": "user", "content": "hello"},
        ],
        "max_tokens": 100,
    }
    assert "secret" not in json.dumps(body)
    response = adapter.parse_response(
        {"content": [{"type": "text", "text": "answer"}]}
    )
    assert response.content == "answer"
    assert response.reasoning_content is None


def test_json_adapter_gemini_format_renders_contents_and_system(
    tmp_path: Path,
) -> None:
    value = definition()
    value["messages_format"] = "gemini"
    value["headers"] = {"x-goog-api-key": "${api_key}"}
    value["body"] = {
        "system_instruction": {"parts": [{"text": "${system}"}]},
        "contents": "${messages}",
        "generationConfig": {
            "temperature": "${temperature}",
            "maxOutputTokens": "${max_output_tokens}",
        },
    }
    value["response_content_pointer"] = "/candidates/0/content/parts/-1/text"
    adapter = load_json_adapter(write_adapter(tmp_path, value))

    _, body = adapter.build_request(
        api_key="secret",
        model="model-a",
        messages=[
            {"role": "system", "content": "prompt"},
            {"role": "assistant", "content": "previous"},
            {"role": "user", "content": "hello"},
        ],
        temperature=0.2,
        max_output_tokens=100,
        stream=False,
    )
    assert body == {
        "system_instruction": {"parts": [{"text": "prompt"}]},
        "contents": [
            {"role": "model", "parts": [{"text": "previous"}]},
            {"role": "user", "parts": [{"text": "hello"}]},
        ],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 100},
    }
    response = adapter.parse_response(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "thought", "thought": True},
                            {"text": "answer"},
                        ]
                    }
                }
            ]
        }
    )
    assert response.content == "answer"


def test_json_adapter_rejects_unknown_messages_format(tmp_path: Path) -> None:
    value = definition()
    value["messages_format"] = "azure"
    with pytest.raises(ConfigError, match="messages_format"):
        load_json_adapter(write_adapter(tmp_path, value))


def test_json_adapter_negative_index_pointers(tmp_path: Path) -> None:
    value = definition()
    value["response_content_pointer"] = "/result/-1/text"
    adapter = load_json_adapter(write_adapter(tmp_path, value))
    assert adapter.parse_response(
        {"result": [{"text": "a"}, {"text": "b"}]}
    ).content == "b"

    value["response_content_pointer"] = "/result/-2/text"
    adapter = load_json_adapter(write_adapter(tmp_path, value))
    assert adapter.parse_response(
        {"result": [{"text": "a"}, {"text": "b"}, {"text": "c"}]}
    ).content == "b"

    value["response_content_pointer"] = "/result/-3/text"
    adapter = load_json_adapter(write_adapter(tmp_path, value))
    with pytest.raises(ExternalError, match="正文路径"):
        adapter.parse_response({"result": [{"text": "a"}]})
    with pytest.raises(ExternalError, match="正文路径"):
        adapter.parse_response({"result": []})


@pytest.mark.parametrize(
    "pointer", ["/result/-/text", "/result/--1/text", "/result/abc/text"]
)
def test_json_adapter_rejects_malformed_index_tokens(
    tmp_path: Path, pointer: str
) -> None:
    value = definition()
    value["response_content_pointer"] = pointer
    adapter = load_json_adapter(write_adapter(tmp_path, value))
    with pytest.raises(ExternalError, match="正文路径"):
        adapter.parse_response({"result": [{"text": "x"}]})


def test_json_adapter_negative_index_replace_content(tmp_path: Path) -> None:
    value = definition()
    value["response_content_pointer"] = "/result/-1/text"
    adapter = load_json_adapter(write_adapter(tmp_path, value))
    response = {"result": [{"text": "old"}, {"text": "target"}]}
    adapter.replace_content(response, "new")
    assert response == {"result": [{"text": "old"}, {"text": "new"}]}
