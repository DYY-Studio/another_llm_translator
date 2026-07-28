from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from .errors import ConfigError


SCHEMA: dict[str, Any] = {
    "project": {"target_language": None, "output_encoding": None},
    "input": {"encoding_confidence_threshold": None, "fallback_encoding": None},
    "llm": {
        "base_url": None,
        "endpoint": None,
        "model": None,
        "api_key_env": None,
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


def _positive(config: dict[str, Any], section: str, key: str) -> float:
    value = config[section][key]
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{section}.{key} 必须大于 0")
    return float(value)


def validate_config(config: dict[str, Any]) -> None:
    _reject_unknown(config, SCHEMA, "config")
    for section, key in (
        ("llm", "max_output_tokens"),
        ("llm", "context_window_tokens"),
        ("execution", "max_parallel"),
        ("execution", "requests_per_minute"),
        ("execution", "input_tokens_per_minute"),
        ("execution", "request_timeout_seconds"),
        ("execution", "token_safety_factor"),
        ("chunking", "target_chunk_input_tokens"),
        ("retry", "http_max_attempts"),
        ("retry", "format_max_attempts"),
    ):
        _positive(config, section, key)

    margin = config["llm"]["context_safety_margin_tokens"]
    if not isinstance(margin, int) or isinstance(margin, bool) or margin < 0:
        raise ConfigError("llm.context_safety_margin_tokens 必须是非负整数")
    hard_limit = (
        config["llm"]["context_window_tokens"]
        - config["llm"]["max_output_tokens"]
        - margin
    )
    if hard_limit <= 0:
        raise ConfigError("模型上下文无法容纳输出预算和安全余量")
    if config["chunking"]["target_chunk_input_tokens"] > hard_limit:
        raise ConfigError("chunking.target_chunk_input_tokens 超过 Prompt 硬限制")
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
    for stage, context in config["context"].items():
        if not isinstance(context["enabled"], bool):
            raise ConfigError(f"context.{stage}.enabled 必须是布尔值")
        if (
            not isinstance(context["previous_segments"], int)
            or isinstance(context["previous_segments"], bool)
            or context["previous_segments"] < 0
        ):
            raise ConfigError(f"context.{stage}.previous_segments 必须是非负整数")


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"无法读取配置：{path}: {exc}") from exc
    validate_config(config)
    return config

