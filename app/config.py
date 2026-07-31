from __future__ import annotations

import codecs
import json
import tomllib
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import ConfigError
from .llm_adapter import adapter_path, load_json_adapter
from .llm_preset import LLMPreset, load_llm_preset, preset_path


APP_ROOT = Path(__file__).parents[1]


LEGACY_SCHEMA: dict[str, Any] = {
    "project": {"target_language": None, "output_encoding": None},
    "input": {"encoding_confidence_threshold": None, "fallback_encoding": None},
    "llm": {
        "adapter": None,
        "base_url": None,
        "endpoint": None,
        "model": None,
        "api_key_env": None,
        "proxy_url": None,
        "temperature_terminology": None,
        "temperature_translation": None,
        "temperature_proofreading": None,
        "temperature_polishing": None,
        "max_output_tokens": None,
        "context_window_tokens": None,
        "context_safety_margin_tokens": None,
    },
    "execution": {
        "max_parallel": None,
        "requests_per_minute": None,
        "input_tokens_per_minute": None,
        "request_timeout_seconds": None,
        "scheduling_mode": None,
        "token_safety_factor": None,
    },
    "chunking": {
        "target_chunk_input_tokens": None,
        "allow_split_oversized_segment": None,
    },
    "context": {
        "translation": {"enabled": None, "previous_segments": None},
        "proofreading": {"enabled": None, "previous_segments": None},
        "polishing": {"enabled": None, "previous_segments": None},
        "terminology": {"enabled": None, "previous_segments": None},
    },
    "terminology": {
        "unicode_normalization": None,
        "case_insensitive": None,
        "max_terms_per_segment": None,
        "alias_primary_collision": None,
    },
    "validation": {
        "translation": {
            "japanese_kana": None,
            "korean_hangul": None,
            "max_retry_attempts": None,
            "exhausted_mode": None,
        }
    },
    "retry": {
        "http_max_attempts": None,
        "format_max_attempts": None,
        "base_delay_seconds": None,
        "max_delay_seconds": None,
        "jitter_seconds": None,
    },
    "debug": {
        "enabled": None,
        "inject_429_every": None,
        "inject_500_every": None,
        "inject_timeout_every": None,
        "inject_invalid_json_every": None,
        "inject_missing_segment_every": None,
    },
}

SCHEMA: dict[str, Any] = {
    "project": LEGACY_SCHEMA["project"],
    "input": LEGACY_SCHEMA["input"],
    "llm": {
        "preset": None,
        "temperature_terminology": None,
        "temperature_translation": None,
        "temperature_proofreading": None,
        "temperature_polishing": None,
    },
    "execution": {"scheduling_mode": None},
    "chunking": LEGACY_SCHEMA["chunking"],
    "context": LEGACY_SCHEMA["context"],
    "terminology": LEGACY_SCHEMA["terminology"],
    "validation": LEGACY_SCHEMA["validation"],
    "retry": LEGACY_SCHEMA["retry"],
    "debug": LEGACY_SCHEMA["debug"],
}


def _reject_unknown(value: dict[str, Any], schema: dict[str, Any], path: str) -> None:
    unknown = set(value) - set(schema)
    if unknown:
        joined = ", ".join(sorted(unknown))
        raise ConfigError(f"未知配置键 {path}: {joined}")
    missing = set(schema) - set(value)
    if missing:
        joined = ", ".join(sorted(missing))
        raise ConfigError(f"缺少配置键 {path}: {joined}")
    for key, child_schema in schema.items():
        if child_schema is None:
            continue
        child = value.get(key)
        if not isinstance(child, dict):
            raise ConfigError(f"配置节必须是表：{path}.{key}")
        _reject_unknown(child, child_schema, f"{path}.{key}")


def validate_config(config: dict[str, Any]) -> None:
    llm = config.get("llm")
    uses_preset = isinstance(llm, dict) and "preset" in llm
    _reject_unknown(
        config, SCHEMA if uses_preset else LEGACY_SCHEMA, "config"
    )
    for section, key in (
        ("project", "target_language"),
        ("project", "output_encoding"),
        ("input", "fallback_encoding"),
    ):
        value = config[section][key]
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{section}.{key} 必须是非空字符串")
    if uses_preset:
        preset_id = config["llm"]["preset"]
        if not isinstance(preset_id, str) or not preset_id.strip():
            raise ConfigError("llm.preset 必须是非空字符串")
    else:
        for key in ("adapter", "base_url", "endpoint", "model", "api_key_env"):
            value = config["llm"][key]
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"llm.{key} 必须是非空字符串")
        proxy_url = config["llm"]["proxy_url"]
        if not isinstance(proxy_url, str):
            raise ConfigError("llm.proxy_url 必须是字符串")
        if proxy_url:
            parsed_proxy = urlsplit(proxy_url)
            if (
                parsed_proxy.scheme not in {"http", "https"}
                or not parsed_proxy.hostname
            ):
                raise ConfigError(
                    "llm.proxy_url 必须是有效的 http:// 或 https:// URL"
                )
    for section, key in (
        ("project", "output_encoding"),
        ("input", "fallback_encoding"),
    ):
        try:
            codecs.lookup(config[section][key])
        except LookupError as exc:
            raise ConfigError(f"{section}.{key} 不是可用编码") from exc
    for section, key in (
        ("chunking", "target_chunk_input_tokens"),
        ("retry", "http_max_attempts"),
    ):
        value = config[section][key]
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
        ):
            raise ConfigError(f"{section}.{key} 必须是正整数")
    if not uses_preset:
        for section, key in (
            ("llm", "max_output_tokens"),
            ("llm", "context_window_tokens"),
            ("execution", "max_parallel"),
        ):
            value = config[section][key]
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ConfigError(f"{section}.{key} 必须是正整数")
        for key in ("request_timeout_seconds", "token_safety_factor"):
            value = config["execution"][key]
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value <= 0
            ):
                raise ConfigError(f"execution.{key} 必须大于 0")
        for key in ("requests_per_minute", "input_tokens_per_minute"):
            value = config["execution"][key]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ConfigError(f"execution.{key} 必须是非负整数")
    format_attempts = config["retry"]["format_max_attempts"]
    if (
        not isinstance(format_attempts, int)
        or isinstance(format_attempts, bool)
        or format_attempts < 0
    ):
        raise ConfigError("retry.format_max_attempts 必须是非负整数")

    confidence = config["input"]["encoding_confidence_threshold"]
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not 0 <= confidence <= 1
    ):
        raise ConfigError("input.encoding_confidence_threshold 必须在 0 到 1 之间")
    for key in (
        "temperature_terminology",
        "temperature_translation",
        "temperature_proofreading",
        "temperature_polishing",
    ):
        value = config["llm"][key]
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or value < 0
        ):
            raise ConfigError(f"llm.{key} 必须是非负数")
    if not uses_preset:
        margin = config["llm"]["context_safety_margin_tokens"]
        if not isinstance(margin, int) or isinstance(margin, bool) or margin < 0:
            raise ConfigError("llm.context_safety_margin_tokens 必须是非负整数")
        input_limit = config["llm"]["context_window_tokens"] - margin
        if input_limit <= 0:
            raise ConfigError("模型上下文无法容纳输入和安全余量")
    if config["execution"]["scheduling_mode"] not in {
        "ordered_by_file",
        "parallel",
    }:
        raise ConfigError("execution.scheduling_mode 必须是 ordered_by_file 或 parallel")
    if config["validation"]["translation"]["exhausted_mode"] not in {
        "fail",
        "warning",
    }:
        raise ConfigError("validation.translation.exhausted_mode 必须是 fail 或 warning")
    for key in ("japanese_kana", "korean_hangul"):
        if not isinstance(config["validation"]["translation"][key], bool):
            raise ConfigError(f"validation.translation.{key} 必须是布尔值")
    validation_attempts = config["validation"]["translation"]["max_retry_attempts"]
    if (
        not isinstance(validation_attempts, int)
        or isinstance(validation_attempts, bool)
        or validation_attempts < 0
    ):
        raise ConfigError(
            "validation.translation.max_retry_attempts 必须是非负整数"
        )
    for key in ("base_delay_seconds", "max_delay_seconds", "jitter_seconds"):
        value = config["retry"][key]
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or value < 0
        ):
            raise ConfigError(f"retry.{key} 必须是非负数")
    if config["retry"]["max_delay_seconds"] < config["retry"]["base_delay_seconds"]:
        raise ConfigError("retry.max_delay_seconds 不能小于 base_delay_seconds")
    if not isinstance(config["chunking"]["allow_split_oversized_segment"], bool):
        raise ConfigError("chunking.allow_split_oversized_segment 必须是布尔值")
    # TODO: 主流程完成全面验证后，实现可配置归一化与大小写匹配并补充真实用例。
    # 当前两项只是明确占位，术语实现固定使用 NFKC、casefold。
    if not isinstance(config["terminology"]["unicode_normalization"], str):
        raise ConfigError("terminology.unicode_normalization 必须是字符串")
    if config["terminology"]["unicode_normalization"] != "NFKC":
        raise ConfigError("MVP 仅支持 terminology.unicode_normalization = NFKC")
    if not isinstance(config["terminology"]["case_insensitive"], bool):
        raise ConfigError("terminology.case_insensitive 必须是布尔值")
    if not config["terminology"]["case_insensitive"]:
        raise ConfigError("MVP 仅支持 terminology.case_insensitive = true")
    alias_collision = config["terminology"]["alias_primary_collision"]
    if alias_collision not in {"conflict", "merge"}:
        raise ConfigError(
            "terminology.alias_primary_collision 必须是 conflict 或 merge"
        )
    max_terms = config["terminology"]["max_terms_per_segment"]
    if (
        not isinstance(max_terms, int)
        or isinstance(max_terms, bool)
        or max_terms <= 0
    ):
        raise ConfigError("terminology.max_terms_per_segment 必须是正整数")
    if not isinstance(config["debug"]["enabled"], bool):
        raise ConfigError("debug.enabled 必须是布尔值")
    for key in (
        "inject_429_every",
        "inject_500_every",
        "inject_timeout_every",
        "inject_invalid_json_every",
        "inject_missing_segment_every",
    ):
        value = config["debug"][key]
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            raise ConfigError(f"debug.{key} 必须是非负整数")
    for stage, context in config["context"].items():
        if not isinstance(context["enabled"], bool):
            raise ConfigError(f"context.{stage}.enabled 必须是布尔值")
        if (
            not isinstance(context["previous_segments"], int)
            or isinstance(context["previous_segments"], bool)
            or context["previous_segments"] < 0
        ):
            raise ConfigError(f"context.{stage}.previous_segments 必须是非负整数")


def dump_config(config: dict[str, Any]) -> str:
    """Serialize a validated project config in the canonical schema order."""
    validate_config(config)
    lines: list[str] = []

    def write_table(
        path: tuple[str, ...],
        value: dict[str, Any],
        schema: dict[str, Any],
    ) -> None:
        if lines:
            lines.append("")
        lines.append("[" + ".".join(path) + "]")
        for key, child_schema in schema.items():
            if child_schema is None:
                lines.append(f"{key} = {_toml_scalar(value[key])}")
        for key, child_schema in schema.items():
            if child_schema is not None:
                write_table((*path, key), value[key], child_schema)

    schema = SCHEMA if "preset" in config["llm"] else LEGACY_SCHEMA
    for section, section_schema in schema.items():
        write_table((section,), config[section], section_schema)
    return "\n".join(lines) + "\n"


def _toml_scalar(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"无法读取配置：{path}: {exc}") from exc
    terminology = config.get("terminology")
    if isinstance(terminology, dict):
        terminology.setdefault("alias_primary_collision", "conflict")
    validate_config(config)
    return config


def load_project_config(
    project: Path, *, presets_root: Path | None = None
) -> dict[str, Any]:
    return resolve_project_config(
        load_config(project / "config.toml"),
        project,
        presets_root=presets_root,
    )


def resolve_project_config(
    config: dict[str, Any],
    project: Path,
    *,
    presets_root: Path | None = None,
) -> dict[str, Any]:
    config = deepcopy(config)
    preset = None
    if "preset" in config["llm"]:
        configured_preset_id = str(config["llm"]["preset"])
        preset_file = preset_path(
            presets_root or APP_ROOT,
            configured_preset_id,
        )
        preset = load_llm_preset(preset_file)
        if preset.preset_id != configured_preset_id:
            raise ConfigError(
                "LLM Preset 文件中的 preset_id 与项目配置不一致"
            )
        adapter_id = preset.adapter_id
    else:
        adapter_id = str(config["llm"]["adapter"])
    return _resolve_llm_config(
        config,
        adapter_file=adapter_path(project, adapter_id),
        preset=preset,
    )


def load_global_config(root: Path) -> dict[str, Any]:
    return resolve_global_config(load_config(root / "config" / "config.toml"), root)


def resolve_global_config(
    config: dict[str, Any], root: Path
) -> dict[str, Any]:
    config = deepcopy(config)
    preset = None
    if "preset" in config["llm"]:
        configured_preset_id = str(config["llm"]["preset"])
        preset = load_llm_preset(preset_path(root, configured_preset_id))
        if preset.preset_id != configured_preset_id:
            raise ConfigError(
                "LLM Preset 文件中的 preset_id 与全局配置不一致"
            )
        adapter_id = preset.adapter_id
    else:
        adapter_id = str(config["llm"]["adapter"])
    return _resolve_llm_config(
        config,
        adapter_file=root / "llm_adapters" / f"{adapter_id}.json",
        preset=preset,
    )


def load_run_config(run_dir: Path) -> dict[str, Any]:
    config = load_config(run_dir / "config.toml")
    preset = (
        load_llm_preset(run_dir / "llm_preset.json")
        if "preset" in config["llm"]
        else None
    )
    return _resolve_llm_config(
        config,
        adapter_file=run_dir / "llm_adapter.json",
        preset=preset,
    )


def _resolve_llm_config(
    config: dict[str, Any],
    *,
    adapter_file: Path,
    preset: LLMPreset | None,
) -> dict[str, Any]:
    if preset is not None:
        definition = preset.definition
        config["llm"].update(
            {
                key: definition[key]
                for key in (
                    "base_url",
                    "endpoint",
                    "model",
                    "api_key_env",
                    "proxy_url",
                    "context_window_tokens",
                    "max_output_tokens",
                    "context_safety_margin_tokens",
                )
            }
        )
        config["llm"]["adapter"] = preset.adapter_id
        config["execution"].update(
            {
                key: definition[key]
                for key in (
                    "token_safety_factor",
                    "requests_per_minute",
                    "input_tokens_per_minute",
                    "max_parallel",
                    "request_timeout_seconds",
                )
            }
        )
        config["_llm_preset_id"] = preset.preset_id
        config["_llm_preset_hash"] = preset.digest
        config["_llm_preset_definition"] = definition
        config["_llm_extra_body"] = definition["extra_body"]
    adapter_id = str(config["llm"]["adapter"])
    adapter = load_json_adapter(adapter_file)
    if adapter.adapter_id != adapter_id:
        raise ConfigError(
            "LLM Adapter 文件中的 adapter_id 与项目配置不一致"
        )
    config["_llm_adapter"] = adapter
    config["_llm_adapter_hash"] = adapter.digest
    if preset is not None:
        adapter.build_request(
            api_key="***",
            model=str(config["llm"]["model"]),
            messages=[],
            temperature=0,
            max_output_tokens=int(config["llm"]["max_output_tokens"]),
            stream=False,
            extra_body=config["_llm_extra_body"],
        )
    return config
