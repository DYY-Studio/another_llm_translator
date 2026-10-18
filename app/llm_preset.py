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

CURRENT_PRESET_SCHEMA_VERSION = 9

_PRESET_ID_RE = re.compile(r"[a-z][a-z0-9-]*")
_PRESET_KEYS = frozenset(
    {
        "schema_version",
        "preset_id",
        "adapter_id",
        "base_url",
        "model",
        "credential",
        "proxy_url",
        "context_window_tokens",
        "max_output_tokens",
        "send_temperature",
        "json_schema_enabled",
        "reasoning_effort",
        "token_safety_factor",
        "requests_per_minute",
        "input_tokens_per_minute",
        "max_parallel",
        "max_parallel_per_key",
        "request_timeout_seconds",
        "extra_body",
        "extra_headers",
        "stream",
        "stream_read_timeout_enabled",
    }
)
_REQUIRED_PRESET_KEYS = _PRESET_KEYS


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


def load_llm_preset(path: Path, *, model_discovery: bool = False, snapshot: bool = False, warnings: list[str] | None = None) -> LLMPreset:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except OSError as exc:
        raise ConfigError(f"无法读取 LLM Preset：{path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"LLM Preset 不是合法 JSON：{path}: {exc}") from exc
    if not snapshot:
        value = normalize_llm_preset(value, preset_id=path.stem, warnings=warnings)
    return _parse_llm_preset(value, model_discovery=model_discovery, editable=not snapshot)


def normalize_llm_preset(value: Any, *, preset_id: str, warnings: list[str] | None = None) -> dict[str, Any]:
    from .config import report_unknown
    from .user_config import APP_ROOT
    if not isinstance(value, dict):
        raise ConfigError("LLM Preset 顶层必须是 JSON 对象")
    report_unknown("LLM Preset", set(value) - _PRESET_KEYS, warnings)
    version = value.get("schema_version", CURRENT_PRESET_SCHEMA_VERSION)
    if type(version) is not int or version not in {8, CURRENT_PRESET_SCHEMA_VERSION}:
        raise ConfigError("LLM Preset schema_version 必须是 9")
    if version == 8:
        message = "LLM Preset schema_version 8 已按当前设置读取，保存时将写入 9"
        if warnings is not None:
            warnings.append(message)
        else:
            from .logging_utils import get_logger
            get_logger("config").warning(message)
    defaults = json.loads((APP_ROOT / "llm_presets/default.json").read_text(encoding="utf-8"))
    defaults.update(preset_id=preset_id, adapter_id="", base_url="", model="", credential={"kind": "", "name": ""})
    result = {key: deepcopy(value.get(key, defaults[key])) for key in _PRESET_KEYS}
    result["schema_version"] = CURRENT_PRESET_SCHEMA_VERSION
    credential = result["credential"]
    if not isinstance(credential, dict):
        raise ConfigError("LLM Preset credential 必须是对象")
    report_unknown("LLM Preset.credential", set(credential) - {"kind", "name"}, warnings)
    result["credential"] = {key: credential.get(key, "") for key in ("kind", "name")}
    return result


def missing_preset_fields(value: dict[str, Any]) -> list[str]:
    fields = [key for key in ("adapter_id", "base_url", "model") if not value[key].strip()]
    fields.extend(f"credential.{key}" for key in ("kind", "name") if not value["credential"][key].strip())
    return fields


def validate_executable_preset(value: dict[str, Any]) -> None:
    missing = missing_preset_fields(value)
    if missing:
        raise ConfigError(f"LLM Preset 缺少执行配置：{', '.join(missing)}")
    _parse_llm_preset(value)


def _parse_llm_preset(value: Any, *, model_discovery: bool = False, editable: bool = False) -> LLMPreset:
    if not isinstance(value, dict):
        raise ConfigError("LLM Preset 顶层必须是 JSON 对象")
    schema_version = value.get("schema_version")
    if type(schema_version) is not int or schema_version != CURRENT_PRESET_SCHEMA_VERSION:
        raise ConfigError("LLM Preset schema_version 必须是 9")
    if type(value.get("json_schema_enabled", False)) is not bool:
        raise ConfigError("json_schema_enabled 必须是布尔值")
    if type(value.get("send_temperature")) is not bool:
        raise ConfigError("send_temperature 必须是布尔值")
    if value.get("reasoning_effort") is not None and not isinstance(value["reasoning_effort"], str):
        raise ConfigError("reasoning_effort 必须是字符串或 null")
    unknown = set(value) - _PRESET_KEYS
    missing = _REQUIRED_PRESET_KEYS - set(value)
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
    preset_id = value["preset_id"]
    if not isinstance(preset_id, str) or not _PRESET_ID_RE.fullmatch(preset_id):
        raise ConfigError("LLM Preset preset_id 格式无效")
    for key in (
        "adapter_id",
        "base_url",
        "model",
    ):
        if (editable or (key == "model" and model_discovery)) and value[key] == "":
            continue
        if not isinstance(value[key], str) or not value[key].strip():
            raise ConfigError(f"LLM Preset {key} 必须是非空字符串")
    credential = value["credential"]
    if not isinstance(credential, dict) or set(credential) != {"kind", "name"}:
        raise ConfigError("LLM Preset credential 必须是包含 kind 和 name 的对象")
    if not isinstance(credential["kind"], str) or credential["kind"] not in ({"", "environment", "keychain", "chatgpt"} if editable else {"environment", "keychain", "chatgpt"}):
        raise ConfigError(
            "LLM Preset credential.kind 必须是 environment、keychain 或 chatgpt"
        )
    if (
        not isinstance(credential["name"], str)
        or (credential["name"] != "" and not credential["name"].strip())
        or (not editable and credential["name"] == "")
    ):
        raise ConfigError("LLM Preset credential.name 必须是非空字符串")
    if value["adapter_id"] and not _PRESET_ID_RE.fullmatch(value["adapter_id"]):
        raise ConfigError("LLM Preset adapter_id 格式无效")
    from .chatgpt_oauth import validate_plan_preset
    if not editable or not missing_preset_fields(value):
        validate_plan_preset(value)
    parsed_base = urlsplit(value["base_url"])
    if not (editable and value["base_url"] == "") and (parsed_base.scheme not in {"http", "https"} or not parsed_base.hostname):
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
        "max_parallel",
        "max_parallel_per_key",
    ):
        _require_integer(value, key, positive=True)
    _require_integer(value, "max_output_tokens", positive=False)
    for key in (
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
    extra_body = value["extra_body"]
    if not isinstance(extra_body, dict):
        raise ConfigError("LLM Preset extra_body 必须是 JSON 对象")
    _reject_placeholders(extra_body)
    extra_headers = value["extra_headers"]
    if not isinstance(extra_headers, dict):
        raise ConfigError("LLM Preset extra_headers 必须是字符串到字符串的对象")
    header_names: set[str] = set()
    for name, template in extra_headers.items():
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
        ):
            raise ConfigError("LLM Preset extra_headers Header 名称无效")
        normalized_name = name.lower()
        if normalized_name in header_names:
            raise ConfigError("LLM Preset extra_headers Header 名称重复")
        header_names.add(normalized_name)
        if not isinstance(template, str) or "\r" in template or "\n" in template:
            raise ConfigError("LLM Preset extra_headers Header 值无效")
        placeholders = re.findall(r"\$\{([^}]*)\}", template)
        if template.count("${") != len(placeholders):
            raise ConfigError("LLM Preset extra_headers 包含无效占位符")
        unknown = set(placeholders) - {"session_id", "request_id"}
        if unknown:
            raise ConfigError("LLM Preset extra_headers 包含未知占位符")
    digest_definition = value
    digest_bytes = json.dumps(
        digest_definition,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return LLMPreset(
        preset_id=preset_id,
        adapter_id=value["adapter_id"],
        definition=deepcopy(value),
        digest=f"sha256:{hashlib.sha256(digest_bytes).hexdigest()}",
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
    """Join a base URL and relative endpoint into a request URL."""
    if model is not None:
        endpoint = str(endpoint).replace("${model}", str(model))
    return str(base_url).rstrip("/") + "/" + str(endpoint).lstrip("/")
