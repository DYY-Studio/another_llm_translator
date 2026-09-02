from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import ConfigError

_PRESET_ID_RE = re.compile(r"[a-z][a-z0-9-]*")
_PRESET_KEYS = frozenset(
    {
        "schema_version",
        "preset_id",
        "adapter_id",
        "base_url",
        "endpoint",
        "model",
        "credential",
        "proxy_url",
        "context_window_tokens",
        "max_output_tokens",
        "context_safety_margin_tokens",
        "token_safety_factor",
        "requests_per_minute",
        "input_tokens_per_minute",
        "max_parallel",
        "max_parallel_per_key",
        "request_timeout_seconds",
        "extra_body",
        "stream",
        "stream_endpoint",
        "stream_read_timeout_enabled",
    }
)


@dataclass(frozen=True)
class LLMPreset:
    preset_id: str
    adapter_id: str
    definition: dict[str, Any]
    digest: str


def preset_path(root: Path, preset_id: str) -> Path:
    if not _PRESET_ID_RE.fullmatch(preset_id):
        raise ConfigError("LLM Preset ID 格式无效")
    return root / "llm_presets" / f"{preset_id}.json"


def load_llm_preset(path: Path) -> LLMPreset:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except OSError as exc:
        raise ConfigError(f"无法读取 LLM Preset：{path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"LLM Preset 不是合法 JSON：{path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError("LLM Preset 顶层必须是 JSON 对象")
    schema_version = value.get("schema_version")
    if schema_version not in {2, 3, 4, 5}:
        raise ConfigError(
            "LLM Preset schema_version 必须是 5；v1 的 api_key_env 字段已移除，"
            "请改用显式 credential 引用"
        )
    if schema_version in {2, 3, 4}:
        value = deepcopy(value)
        value["schema_version"] = 5
        if schema_version == 2:
            value.setdefault("stream", False)
            value.setdefault("stream_endpoint", "")
        if schema_version in {2, 3}:
            value.setdefault("stream_read_timeout_enabled", True)
        value.setdefault("max_parallel_per_key", value.get("max_parallel"))
    unknown = set(value) - _PRESET_KEYS
    missing = _PRESET_KEYS - set(value)
    if unknown:
        raise ConfigError(
            f"LLM Preset 包含未知字段：{', '.join(sorted(unknown))}"
        )
    if missing:
        raise ConfigError(
            f"LLM Preset 缺少字段：{', '.join(sorted(missing))}"
        )
    if not isinstance(value["stream"], bool):
        raise ConfigError("LLM Preset stream 必须是布尔值")
    if not isinstance(value["stream_read_timeout_enabled"], bool):
        raise ConfigError(
            "LLM Preset stream_read_timeout_enabled 必须是布尔值"
        )
    stream_endpoint = value["stream_endpoint"]
    if not isinstance(stream_endpoint, str):
        raise ConfigError("LLM Preset stream_endpoint 必须是字符串")
    if stream_endpoint and not stream_endpoint.strip():
        raise ConfigError("LLM Preset stream_endpoint 不能为空白")
    preset_id = value["preset_id"]
    if not isinstance(preset_id, str) or not _PRESET_ID_RE.fullmatch(preset_id):
        raise ConfigError("LLM Preset preset_id 格式无效")
    for key in (
        "adapter_id",
        "base_url",
        "endpoint",
        "model",
    ):
        if not isinstance(value[key], str) or not value[key].strip():
            raise ConfigError(f"LLM Preset {key} 必须是非空字符串")
    credential = value["credential"]
    if not isinstance(credential, dict) or set(credential) != {"kind", "name"}:
        raise ConfigError("LLM Preset credential 必须是包含 kind 和 name 的对象")
    if credential["kind"] not in {"environment", "keychain"}:
        raise ConfigError(
            "LLM Preset credential.kind 必须是 environment 或 keychain"
        )
    if (
        not isinstance(credential["name"], str)
        or not credential["name"].strip()
    ):
        raise ConfigError("LLM Preset credential.name 必须是非空字符串")
    if "${" in value["endpoint"].replace("${model}", ""):
        raise ConfigError("LLM Preset endpoint 只允许 ${model} 占位符")
    if "${" in stream_endpoint.replace("${model}", ""):
        raise ConfigError("LLM Preset stream_endpoint 只允许 ${model} 占位符")
    if (
        "://" in value["endpoint"]
        or "://" in stream_endpoint
        or value["endpoint"].startswith("//")
        or stream_endpoint.startswith("//")
    ):
        raise ConfigError("LLM Preset endpoint 必须是相对路径")
    if not _PRESET_ID_RE.fullmatch(value["adapter_id"]):
        raise ConfigError("LLM Preset adapter_id 格式无效")
    parsed_base = urlsplit(value["base_url"])
    if parsed_base.scheme not in {"http", "https"} or not parsed_base.hostname:
        raise ConfigError("LLM Preset base_url 必须是有效的 HTTP/HTTPS URL")
    proxy_url = value["proxy_url"]
    if not isinstance(proxy_url, str):
        raise ConfigError("LLM Preset proxy_url 必须是字符串")
    if proxy_url:
        parsed_proxy = urlsplit(proxy_url)
        if (
            parsed_proxy.scheme not in {"http", "https"}
            or not parsed_proxy.hostname
        ):
            raise ConfigError("LLM Preset proxy_url 必须是有效的 HTTP/HTTPS URL")
    for key in (
        "context_window_tokens",
        "max_output_tokens",
        "max_parallel",
        "max_parallel_per_key",
    ):
        _require_integer(value, key, positive=True)
    for key in (
        "context_safety_margin_tokens",
        "requests_per_minute",
        "input_tokens_per_minute",
    ):
        _require_integer(value, key, positive=False)
    for key in ("token_safety_factor", "request_timeout_seconds"):
        number = value[key]
        if (
            not isinstance(number, (int, float))
            or isinstance(number, bool)
            or number <= 0
        ):
            raise ConfigError(f"LLM Preset {key} 必须大于 0")
    if value["context_safety_margin_tokens"] >= value["context_window_tokens"]:
        raise ConfigError("LLM Preset 上下文安全余量必须小于上下文窗口")
    extra_body = value["extra_body"]
    if not isinstance(extra_body, dict):
        raise ConfigError("LLM Preset extra_body 必须是 JSON 对象")
    _reject_placeholders(extra_body)
    return LLMPreset(
        preset_id=preset_id,
        adapter_id=value["adapter_id"],
        definition=deepcopy(value),
        digest=f"sha256:{hashlib.sha256(raw).hexdigest()}",
    )


def _require_integer(value: dict[str, Any], key: str, *, positive: bool) -> None:
    number = value[key]
    if (
        not isinstance(number, int)
        or isinstance(number, bool)
        or (number <= 0 if positive else number < 0)
    ):
        requirement = "正整数" if positive else "非负整数"
        raise ConfigError(f"LLM Preset {key} 必须是{requirement}")


def _reject_placeholders(value: Any) -> None:
    if isinstance(value, dict):
        for child in value.values():
            _reject_placeholders(child)
    elif isinstance(value, list):
        for child in value:
            _reject_placeholders(child)
    elif isinstance(value, str) and "${" in value:
        raise ConfigError("LLM Preset extra_body 不允许模板占位符")


def endpoint_url(
    base_url: str, endpoint: str, model: str | None = None
) -> str:
    """Join a preset's base_url and endpoint into a request URL."""
    if model is not None:
        endpoint = str(endpoint).replace("${model}", str(model))
    return str(base_url).rstrip("/") + "/" + str(endpoint).lstrip("/")
