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
    {"model", "messages", "temperature", "max_output_tokens", "stream"}
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
_OPTIONAL_ADAPTER_KEYS = frozenset({"response_reasoning_content_pointer"})


@dataclass(frozen=True)
class LLMResponse:
    content: str
    reasoning_content: str | None


@dataclass(frozen=True)
class JSONLLMAdapter:
    adapter_id: str
    headers_template: dict[str, str]
    body_template: dict[str, Any]
    response_content_pointer: str
    response_reasoning_content_pointer: str | None
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
            "messages": messages,
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
                if token == "-" or not token.isdecimal():
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
    digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    return JSONLLMAdapter(
        adapter_id=adapter_id,
        headers_template=dict(headers),
        body_template=deepcopy(body),
        response_content_pointer=pointer,
        response_reasoning_content_pointer=reasoning_pointer,
        digest=digest,
        definition=deepcopy(value),
    )


def adapter_path(project: Path, adapter_id: str) -> Path:
    return project / "llm_adapters" / f"{adapter_id}.json"


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


def _resolve_json_pointer(value: Any, pointer: str) -> Any:
    return _resolve_json_pointer_tokens(value, _parse_json_pointer(pointer))


def _resolve_json_pointer_tokens(value: Any, tokens: list[str]) -> Any:
    current = value
    try:
        for token in tokens:
            if isinstance(current, list):
                if token == "-" or not token.isdecimal():
                    raise KeyError(token)
                current = current[int(token)]
            elif isinstance(current, dict):
                current = current[token]
            else:
                raise KeyError(token)
    except (KeyError, IndexError) as exc:
        raise ExternalError("LLM 响应缺少配置的正文路径") from exc
    return current
