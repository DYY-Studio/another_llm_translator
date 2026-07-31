from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError, ExternalError


_PLACEHOLDER_RE = re.compile(r"\$\{([a-z_][a-z0-9_]*)\}")
_BODY_PLACEHOLDERS = frozenset(
    {"model", "system", "messages", "temperature", "max_output_tokens", "stream"}
)
_HEADER_PLACEHOLDERS = _BODY_PLACEHOLDERS | {"api_key"}
_REQUIRED_ADAPTER_KEYS = frozenset(
    {
        "schema_version",
        "adapter_id",
        "headers",
        "body",
        "response_content_pointer",
    }
)
_OPTIONAL_ADAPTER_KEYS = frozenset(
    {"messages_format", "models", "response_reasoning_content_pointer", "usage"}
)
_MESSAGES_FORMATS = frozenset({"openai", "anthropic", "gemini"})
_MODELS_KEYS = frozenset(
    {
        "endpoint",
        "headers",
        "response_models_pointer",
        "response_model_id",
        "response_model_display",
        "response_model_strip_prefix",
    }
)
_MODELS_REQUIRED_KEYS = frozenset(
    {"endpoint", "headers", "response_models_pointer", "response_model_id"}
)
_USAGE_KEYS = frozenset(
    {"input_tokens_pointer", "output_tokens_pointer", "total_tokens_pointer"}
)


@dataclass(frozen=True)
class LLMResponse:
    content: str
    reasoning_content: str | None


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class JSONLLMAdapter:
    adapter_id: str
    headers_template: dict[str, str]
    body_template: dict[str, Any]
    response_content_pointer: str
    response_reasoning_content_pointer: str | None
    messages_format: str
    models_spec: dict[str, Any] | None
    usage_pointers: tuple[str | None, str | None, str | None] | None
    digest: str
    definition: dict[str, Any]

    def build_request(
        self,
        *,
        api_key: str,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_output_tokens: int,
        stream: bool,
        extra_body: dict[str, Any] | None = None,
    ) -> tuple[dict[str, str], dict[str, Any]]:
        values: dict[str, Any] = {
            "api_key": api_key,
            "model": model,
            "system": _extract_system(messages),
            "messages": _transform_messages(messages, self.messages_format),
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
            "stream": stream,
        }
        headers = {
            name: _render_header(value, values)
            for name, value in self.headers_template.items()
        }
        body = _render_body(deepcopy(self.body_template), values)
        if extra_body:
            conflicts = set(body) & set(extra_body)
            if conflicts:
                raise ConfigError(
                    "LLM Preset extra_body 与 Adapter body 字段冲突："
                    + ", ".join(sorted(conflicts))
                )
            body.update(deepcopy(extra_body))
        return headers, body

    def parse_response(self, response: Any) -> LLMResponse:
        try:
            content = _resolve_json_pointer(
                response, self.response_content_pointer
            )
        except ExternalError as exc:
            raise ExternalError(
                "LLM 响应缺少正文路径："
                f"{self.response_content_pointer}"
            ) from exc
        if not isinstance(content, str):
            raise ExternalError("LLM 响应正文不是字符串")
        reasoning_content = None
        if self.response_reasoning_content_pointer is not None:
            try:
                reasoning_content = _resolve_json_pointer(
                    response, self.response_reasoning_content_pointer
                )
            except ExternalError as exc:
                raise ExternalError(
                    "LLM 响应缺少思考正文路径："
                    f"{self.response_reasoning_content_pointer}"
                ) from exc
            if reasoning_content is not None and not isinstance(
                reasoning_content, str
            ):
                raise ExternalError("LLM 响应思考正文不是字符串或 null")
        return LLMResponse(
            content=content,
            reasoning_content=reasoning_content,
        )

    def replace_content(self, response: Any, content: str) -> None:
        tokens = _parse_json_pointer(self.response_content_pointer)
        if not tokens:
            raise ExternalError("LLM 响应正文路径不能指向整个响应")
        parent = _resolve_json_pointer_tokens(response, tokens[:-1])
        token = tokens[-1]
        try:
            if isinstance(parent, list):
                if not _is_index(token):
                    raise KeyError(token)
                parent[int(token)] = content
            elif isinstance(parent, dict):
                if token not in parent:
                    raise KeyError(token)
                parent[token] = content
            else:
                raise KeyError(token)
        except (KeyError, IndexError) as exc:
            raise ExternalError(
                f"LLM 响应缺少正文路径：{self.response_content_pointer}"
            ) from exc

    def build_models_request(
        self, *, api_key: str
    ) -> tuple[str, dict[str, str]]:
        if self.models_spec is None:
            raise ExternalError("该 Adapter 未声明模型发现规格")
        headers = {
            name: _render_header(template, {"api_key": api_key})
            for name, template in self.models_spec["headers"].items()
        }
        return self.models_spec["endpoint"], headers

    def parse_models_response(self, response: Any) -> list[dict[str, str]]:
        if self.models_spec is None:
            raise ExternalError("该 Adapter 未声明模型发现规格")
        items = _resolve_json_pointer(
            response, self.models_spec["response_models_pointer"]
        )
        if not isinstance(items, list):
            raise ExternalError("LLM 模型列表响应不是数组")
        id_key = self.models_spec["response_model_id"]
        display_key = self.models_spec.get("response_model_display")
        strip_prefix = self.models_spec.get("response_model_strip_prefix", "")
        result: list[dict[str, str]] = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get(id_key), str):
                raise ExternalError("LLM 模型列表条目缺少模型 ID")
            model_id = item[id_key]
            if strip_prefix and model_id.startswith(strip_prefix):
                model_id = model_id[len(strip_prefix) :]
            display = item.get(display_key) if display_key else None
            if not isinstance(display, str) or not display:
                display = model_id
            result.append({"id": model_id, "display": display})
        return result

    def extract_usage(self, response: Any) -> Usage | None:
        if self.usage_pointers is None:
            return None
        values: list[int] = []
        for pointer in self.usage_pointers:
            if pointer is None:
                values.append(0)
                continue
            try:
                value = _resolve_json_pointer(response, pointer)
            except ExternalError:
                return None
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                return None
            values.append(value)
        return Usage(
            input_tokens=values[0],
            output_tokens=values[1],
            total_tokens=values[2],
        )


def load_json_adapter(path: Path) -> JSONLLMAdapter:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except OSError as exc:
        raise ConfigError(f"无法读取 LLM Adapter：{path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"LLM Adapter 不是合法 JSON：{path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"LLM Adapter 顶层必须是 JSON 对象：{path}")
    unknown = set(value) - _REQUIRED_ADAPTER_KEYS - _OPTIONAL_ADAPTER_KEYS
    missing = _REQUIRED_ADAPTER_KEYS - set(value)
    if unknown:
        raise ConfigError(f"LLM Adapter 包含未知字段：{', '.join(sorted(unknown))}")
    if missing:
        raise ConfigError(f"LLM Adapter 缺少字段：{', '.join(sorted(missing))}")
    if value["schema_version"] != 1:
        raise ConfigError("LLM Adapter schema_version 必须是 1")
    adapter_id = value["adapter_id"]
    if (
        not isinstance(adapter_id, str)
        or not adapter_id
        or not re.fullmatch(r"[a-z][a-z0-9-]*", adapter_id)
    ):
        raise ConfigError("LLM Adapter adapter_id 格式无效")
    messages_format = value.get("messages_format", "openai")
    if not isinstance(messages_format, str) or messages_format not in _MESSAGES_FORMATS:
        raise ConfigError("LLM Adapter messages_format 无效")
    headers = value["headers"]
    if not isinstance(headers, dict) or not all(
        isinstance(name, str)
        and name
        and isinstance(template, str)
        for name, template in headers.items()
    ):
        raise ConfigError("LLM Adapter headers 必须是字符串到字符串的对象")
    for template in headers.values():
        _validate_placeholders(
            template, allowed=_HEADER_PLACEHOLDERS, location="headers"
        )
    body = value["body"]
    if not isinstance(body, dict):
        raise ConfigError("LLM Adapter body 必须是 JSON 对象")
    _validate_body(body)
    pointer = value["response_content_pointer"]
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ConfigError("LLM Adapter response_content_pointer 必须是 JSON Pointer")
    _parse_json_pointer(pointer)
    reasoning_pointer = value.get("response_reasoning_content_pointer")
    if reasoning_pointer is not None:
        if (
            not isinstance(reasoning_pointer, str)
            or not reasoning_pointer.startswith("/")
        ):
            raise ConfigError(
                "LLM Adapter response_reasoning_content_pointer "
                "必须是 JSON Pointer"
            )
        _parse_json_pointer(reasoning_pointer)
    models_spec = _validate_models_spec(value.get("models"))
    usage_pointers = _validate_usage_mapping(value.get("usage"))
    digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    return JSONLLMAdapter(
        adapter_id=adapter_id,
        headers_template=dict(headers),
        body_template=deepcopy(body),
        response_content_pointer=pointer,
        response_reasoning_content_pointer=reasoning_pointer,
        messages_format=messages_format,
        models_spec=models_spec,
        usage_pointers=usage_pointers,
        digest=digest,
        definition=deepcopy(value),
    )


def _validate_models_spec(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigError("LLM Adapter models 必须是 JSON 对象")
    unknown = set(value) - _MODELS_KEYS
    missing = _MODELS_REQUIRED_KEYS - set(value)
    if unknown:
        raise ConfigError(
            f"LLM Adapter models 包含未知字段：{', '.join(sorted(unknown))}"
        )
    if missing:
        raise ConfigError(
            f"LLM Adapter models 缺少字段：{', '.join(sorted(missing))}"
        )
    endpoint = value["endpoint"]
    if not isinstance(endpoint, str) or not endpoint.strip() or "${" in endpoint:
        raise ConfigError("LLM Adapter models endpoint 必须是无占位符的非空字符串")
    headers = value["headers"]
    if not isinstance(headers, dict) or not all(
        isinstance(name, str)
        and name
        and isinstance(template, str)
        for name, template in headers.items()
    ):
        raise ConfigError("LLM Adapter models headers 必须是字符串到字符串的对象")
    for template in headers.values():
        _validate_placeholders(
            template, allowed=frozenset({"api_key"}), location="models headers"
        )
    models_pointer = value["response_models_pointer"]
    if (
        not isinstance(models_pointer, str)
        or not models_pointer.startswith("/")
    ):
        raise ConfigError(
            "LLM Adapter models response_models_pointer 必须是 JSON Pointer"
        )
    _parse_json_pointer(models_pointer)
    model_id = value["response_model_id"]
    if not isinstance(model_id, str) or not model_id:
        raise ConfigError("LLM Adapter models response_model_id 必须是非空字符串")
    for key in ("response_model_display", "response_model_strip_prefix"):
        if key in value and (
            not isinstance(value[key], str) or not value[key]
        ):
            raise ConfigError(f"LLM Adapter models {key} 必须是非空字符串")
    return deepcopy(value)


def _validate_usage_mapping(
    value: Any,
) -> tuple[str | None, str | None, str | None] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigError("LLM Adapter usage 必须是 JSON 对象")
    unknown = set(value) - _USAGE_KEYS
    if unknown:
        raise ConfigError(
            f"LLM Adapter usage 包含未知字段：{', '.join(sorted(unknown))}"
        )
    if not value:
        raise ConfigError("LLM Adapter usage 至少需要一个 token 指针")
    pointers: list[str | None] = []
    for key in (
        "input_tokens_pointer",
        "output_tokens_pointer",
        "total_tokens_pointer",
    ):
        pointer = value.get(key)
        if pointer is None:
            pointers.append(None)
            continue
        if not isinstance(pointer, str) or not pointer.startswith("/"):
            raise ConfigError(f"LLM Adapter usage {key} 必须是 JSON Pointer")
        _parse_json_pointer(pointer)
        pointers.append(pointer)
    return (pointers[0], pointers[1], pointers[2])


def _validate_placeholders(
    template: str, *, allowed: frozenset[str], location: str
) -> None:
    names = set(_PLACEHOLDER_RE.findall(template))
    unknown = names - allowed
    if unknown:
        raise ConfigError(
            f"LLM Adapter {location} 包含未知占位符："
            f"{', '.join(sorted(unknown))}"
        )


def _validate_body(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ConfigError("LLM Adapter body 的对象键必须是字符串")
            _validate_body(child)
        return
    if isinstance(value, list):
        for child in value:
            _validate_body(child)
        return
    if isinstance(value, str):
        _validate_placeholders(
            value, allowed=_BODY_PLACEHOLDERS, location="body"
        )
        if _PLACEHOLDER_RE.search(value) and not _PLACEHOLDER_RE.fullmatch(value):
            raise ConfigError("LLM Adapter body 占位符必须独占字符串值")


def _render_header(template: str, values: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        value = values[match.group(1)]
        if isinstance(value, (dict, list)):
            raise ConfigError("LLM Adapter header 不能插入对象或数组")
        return str(value).lower() if isinstance(value, bool) else str(value)

    return _PLACEHOLDER_RE.sub(replace, template)


def _render_body(value: Any, values: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: _render_body(child, values) for key, child in value.items()}
    if isinstance(value, list):
        return [_render_body(child, values) for child in value]
    if isinstance(value, str):
        match = _PLACEHOLDER_RE.fullmatch(value)
        if match:
            return deepcopy(values[match.group(1)])
    return value


def _extract_system(messages: list[dict[str, str]]) -> str:
    return "\n\n".join(
        message["content"]
        for message in messages
        if message["role"] == "system"
    )


def _transform_messages(
    messages: list[dict[str, str]], messages_format: str
) -> list[dict[str, Any]]:
    if messages_format == "openai":
        return messages
    if messages_format == "anthropic":
        return [
            {"role": message["role"], "content": message["content"]}
            for message in messages
            if message["role"] in ("user", "assistant")
        ]
    if messages_format == "gemini":
        role = {"user": "user", "assistant": "model"}
        return [
            {
                "role": role[message["role"]],
                "parts": [{"text": message["content"]}],
            }
            for message in messages
            if message["role"] in ("user", "assistant")
        ]
    raise ConfigError("LLM Adapter messages_format 无效")


def _parse_json_pointer(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ConfigError("LLM Adapter response_content_pointer 必须是 JSON Pointer")
    tokens = pointer[1:].split("/")
    for token in tokens:
        index = 0
        while index < len(token):
            if token[index] == "~":
                if index + 1 >= len(token) or token[index + 1] not in {"0", "1"}:
                    raise ConfigError("LLM Adapter JSON Pointer 转义无效")
                index += 2
            else:
                index += 1
    return [token.replace("~1", "/").replace("~0", "~") for token in tokens]


def _is_index(token: str) -> bool:
    if token == "-":
        return False
    if token.startswith("-"):
        return token[1:].isdecimal()
    return token.isdecimal()


def _resolve_json_pointer(value: Any, pointer: str) -> Any:
    return _resolve_json_pointer_tokens(value, _parse_json_pointer(pointer))


def _resolve_json_pointer_tokens(value: Any, tokens: list[str]) -> Any:
    current = value
    try:
        for token in tokens:
            if isinstance(current, list):
                if not _is_index(token):
                    raise KeyError(token)
                current = current[int(token)]
            elif isinstance(current, dict):
                current = current[token]
            else:
                raise KeyError(token)
    except (KeyError, IndexError) as exc:
        raise ExternalError("LLM 响应缺少配置的正文路径") from exc
    return current
