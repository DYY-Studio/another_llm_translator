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
        "api_key_env",
        "proxy_url",
        "context_window_tokens",
        "max_output_tokens",
        "context_safety_margin_tokens",
        "token_safety_factor",
        "requests_per_minute",
        "input_tokens_per_minute",
        "max_parallel",
        "request_timeout_seconds",
        "extra_body",
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
    if value["schema_version"] != 1:
        raise ConfigError("LLM Preset schema_version 必须是 1")
    preset_id = value["preset_id"]
    if not isinstance(preset_id, str) or not _PRESET_ID_RE.fullmatch(preset_id):
        raise ConfigError("LLM Preset preset_id 格式无效")
    if path.stem != preset_id:
        raise ConfigError("LLM Preset 文件名必须与 preset_id 一致")
    for key in (
        "adapter_id",
        "base_url",
        "endpoint",
        "model",
        "api_key_env",
    ):
        if not isinstance(value[key], str) or not value[key].strip():
            raise ConfigError(f"LLM Preset {key} 必须是非空字符串")
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
