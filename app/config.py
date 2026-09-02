from __future__ import annotations

import codecs
import json
import re
import tomllib
from copy import deepcopy
from pathlib import Path
from typing import Any

from .errors import ConfigError, ConfigFieldError
from .llm_adapter import load_json_adapter
from .llm_preset import LLMPreset, load_llm_preset, preset_path
from .user_config import APP_ROOT, effective_path

LLM_STAGES = ("terminology", "translation", "proofreading", "polishing")
LLM_MODEL_STAGES = (*LLM_STAGES, "terminology_decision")

SCHEMA: dict[str, Any] = {
    "project": {
        "target_language": None,
        "target_language_tag": None,
        "output_encoding": None,
    },
    "input": {"encoding_confidence_threshold": None, "fallback_encoding": None},
    "llm": {
        "preset": None,
        "preset_terminology": None,
        "preset_terminology_decision": None,
        "preset_translation": None,
        "preset_proofreading": None,
        "preset_polishing": None,
        "temperature_terminology": None,
        "temperature_terminology_decision": None,
        "temperature_translation": None,
        "temperature_proofreading": None,
        "temperature_polishing": None,
    },
    "execution": {"scheduling_mode": None},
    "chunking": {
        "target_chunk_input_tokens": None,
        "allow_split_oversized_segment": None,
        "cross_boundary_batching": None,
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
    "terminology_decision": {
        "allow_soft_target_overflow": None,
        "anchor_overflow_mode": None,
    },
    "validation": {
        "translation": {
            "validators": None,
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

_WELL_FORMED_LANGUAGE_TAG = re.compile(
    r"(?:[A-Za-z]{2,3}(?:-[A-Za-z]{3}){0,3}|[A-Za-z]{4}|[A-Za-z]{5,8})"
    r"(?:-[A-Za-z]{4})?"
    r"(?:-(?:[A-Za-z]{2}|[0-9]{3}))?"
    r"(?:-(?:[A-Za-z0-9]{5,8}|[0-9][A-Za-z0-9]{3}))*"
    r"(?:-[A-WY-Za-wy-z](?:-[A-Za-z0-9]{2,8})+)*"
    r"(?:-x(?:-[A-Za-z0-9]{1,8})+)?$"
)
_PRIVATE_USE_LANGUAGE_TAG = re.compile(
    r"x(?:-[A-Za-z0-9]{1,8})+$"
)
_GRANDFATHERED_LANGUAGE_TAGS = frozenset(
    {
        "art-lojban",
        "cel-gaulish",
        "i-ami",
        "i-bnn",
        "i-default",
        "i-enochian",
        "i-hak",
        "i-klingon",
        "i-lux",
        "i-mingo",
        "i-navajo",
        "i-pwn",
        "i-tao",
        "i-tay",
        "i-tsu",
        "no-bok",
        "no-nyn",
        "sgn-be-fr",
        "sgn-be-nl",
        "sgn-ch-de",
        "zh-guoyu",
        "zh-hakka",
        "zh-min",
        "zh-min-nan",
        "zh-xiang",
    }
)


def is_well_formed_language_tag(value: str) -> bool:
    lowered = value.casefold()
    return bool(
        _WELL_FORMED_LANGUAGE_TAG.fullmatch(value)
        or _PRIVATE_USE_LANGUAGE_TAG.fullmatch(lowered)
        or lowered in _GRANDFATHERED_LANGUAGE_TAGS
    )


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
    _reject_unknown(config, SCHEMA, "config")
    for section, key in (
        ("project", "target_language"),
        ("project", "target_language_tag"),
        ("project", "output_encoding"),
        ("input", "fallback_encoding"),
    ):
        value = config[section][key]
        if not isinstance(value, str) or (
            key != "target_language_tag" and not value.strip()
        ):
            raise ConfigError(f"{section}.{key} 必须是非空字符串")
    target_language_tag = config["project"]["target_language_tag"]
    if target_language_tag and not is_well_formed_language_tag(
        target_language_tag
    ):
        raise ConfigFieldError(
            "project.target_language_tag 必须是格式正确的 BCP 47 语言标签",
            field="project.target_language_tag",
            reason="invalid_bcp47",
        )
    preset_id = config["llm"]["preset"]
    if not isinstance(preset_id, str) or not preset_id.strip():
        raise ConfigError("llm.preset 必须是非空字符串")
    for stage in LLM_MODEL_STAGES:
        value = config["llm"][f"preset_{stage}"]
        if not isinstance(value, str):
            raise ConfigError(f"llm.preset_{stage} 必须是字符串")
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
        "temperature_terminology_decision",
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
    validators = config["validation"]["translation"]["validators"]
    if not isinstance(validators, list) or any(
        not isinstance(validator_id, str) or not validator_id.strip()
        for validator_id in validators
    ):
        raise ConfigError(
            "validation.translation.validators 必须是非空字符串数组"
        )
    if len(validators) != len(set(validators)):
        raise ConfigError("validation.translation.validators 不能包含重复校验器")
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
    cross_boundary_batching = config["chunking"]["cross_boundary_batching"]
    if not isinstance(cross_boundary_batching, list) or any(
        not isinstance(stage, str) for stage in cross_boundary_batching
    ):
        raise ConfigError(
            "chunking.cross_boundary_batching 必须是 LLM 阶段名称数组"
        )
    unknown_stages = sorted(set(cross_boundary_batching) - set(LLM_STAGES))
    if unknown_stages:
        raise ConfigError(
            "chunking.cross_boundary_batching 包含未知阶段："
            + ", ".join(unknown_stages)
        )
    if len(cross_boundary_batching) != len(set(cross_boundary_batching)):
        raise ConfigError("chunking.cross_boundary_batching 不能包含重复阶段")
    normalization = config["terminology"]["unicode_normalization"]
    if not isinstance(normalization, str) or normalization not in {
        "",
        "NFC",
        "NFD",
        "NFKC",
        "NFKD",
    }:
        raise ConfigError(
            "terminology.unicode_normalization 必须是空字符串或 "
            "NFC、NFD、NFKC、NFKD 之一"
        )
    if not isinstance(config["terminology"]["case_insensitive"], bool):
        raise ConfigError("terminology.case_insensitive 必须是布尔值")
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
    decision_config = config["terminology_decision"]
    if not isinstance(decision_config["allow_soft_target_overflow"], bool):
        raise ConfigError(
            "terminology_decision.allow_soft_target_overflow 必须是布尔值"
        )
    anchor_overflow_mode = decision_config["anchor_overflow_mode"]
    if not isinstance(anchor_overflow_mode, str) or anchor_overflow_mode not in {
        "error",
        "trim",
        "compact",
    }:
        raise ConfigError(
            "terminology_decision.anchor_overflow_mode 必须是 error、trim 或 compact"
        )
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

    for section, section_schema in SCHEMA.items():
        write_table((section,), config[section], section_schema)
    return "\n".join(lines) + "\n"


def _toml_scalar(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(_toml_scalar(item) for item in value) + "]"
    return str(value)


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"无法读取配置：{path}: {exc}") from exc
    terminology = config.get("terminology")
    if isinstance(terminology, dict):
        terminology.setdefault("alias_primary_collision", "merge")
    terminology_decision = config.get("terminology_decision")
    if terminology_decision is None:
        terminology_decision = {}
        config["terminology_decision"] = terminology_decision
    if isinstance(terminology_decision, dict):
        terminology_decision.setdefault("allow_soft_target_overflow", True)
        terminology_decision.setdefault("anchor_overflow_mode", "error")
    llm = config.get("llm")
    if isinstance(llm, dict):
        llm.setdefault("preset_terminology_decision", "")
        llm.setdefault("temperature_terminology_decision", 0.1)
    chunking = config.get("chunking")
    if isinstance(chunking, dict):
        chunking.setdefault("cross_boundary_batching", [])
    project = config.get("project")
    if isinstance(project, dict):
        project.setdefault("target_language_tag", "")
    validation = config.get("validation")
    translation_validation = (
        validation.get("translation")
        if isinstance(validation, dict)
        else None
    )
    if (
        isinstance(translation_validation, dict)
        and "validators" not in translation_validation
    ):
        legacy_keys = ("japanese_kana", "korean_hangul")
        if all(key in translation_validation for key in legacy_keys):
            if any(
                type(translation_validation[key]) is not bool
                for key in legacy_keys
            ):
                raise ConfigError(
                    "旧版 validation.translation.japanese_kana 和 "
                    "korean_hangul 必须是布尔值"
                )
            translation_validation["validators"] = [
                key
                for key in legacy_keys
                if translation_validation[key] is True
            ]
            for key in legacy_keys:
                del translation_validation[key]
    validate_config(config)
    return config


def load_project_config(
    project: Path,
    *,
    stage: str | None = None,
    presets_root: Path | None = None,
) -> dict[str, Any]:
    return resolve_project_config(
        load_config(project / "config.toml"),
        stage=stage,
        presets_root=presets_root,
    )


def _resolve_config(
    config: dict[str, Any],
    root: Path,
    *,
    stage: str | None,
    error_kind: str,
) -> dict[str, Any]:
    config = deepcopy(config)
    from .plugins import resolve_translation_validators

    validator_bindings = resolve_translation_validators(
        config["validation"]["translation"]["validators"]
    )
    config["_translation_validators"] = [
        summary for _, summary in validator_bindings
    ]
    config["_translation_validator_instances"] = tuple(
        validator for validator, _ in validator_bindings
    )
    configured_preset_id = _preset_id_for_stage(config, stage)
    preset_path(root, configured_preset_id)
    preset = load_llm_preset(
        effective_path(
            f"llm_presets/{configured_preset_id}.json", builtin_root=root
        )
    )
    if preset.preset_id != configured_preset_id:
        raise ConfigError(f"LLM Preset 文件中的 preset_id 与{error_kind}不一致")
    return _resolve_llm_config(
        config,
        adapter_file=effective_path(
            f"llm_adapters/{preset.adapter_id}.json", builtin_root=root
        ),
        preset=preset,
    )


def resolve_project_config(
    config: dict[str, Any],
    *,
    stage: str | None = None,
    presets_root: Path | None = None,
) -> dict[str, Any]:
    return _resolve_config(
        config,
        presets_root or APP_ROOT,
        stage=stage,
        error_kind="项目配置",
    )


def load_global_config(root: Path) -> dict[str, Any]:
    return resolve_global_config(
        load_config(effective_path("config/config.toml", builtin_root=root)), root
    )


def resolve_global_config(
    config: dict[str, Any], root: Path, *, stage: str | None = None
) -> dict[str, Any]:
    return _resolve_config(config, root, stage=stage, error_kind="全局配置")


def _preset_id_for_stage(config: dict[str, Any], stage: str | None) -> str:
    if stage is not None and stage not in LLM_MODEL_STAGES:
        raise ConfigError(f"未知 LLM 阶段：{stage}")
    override = config["llm"].get(f"preset_{stage}", "") if stage else ""
    return str(override or config["llm"]["preset"])


def load_run_config(run_dir: Path) -> dict[str, Any]:
    config = load_config(run_dir / "config.toml")
    preset = load_llm_preset(run_dir / "llm_preset.json")
    return _resolve_llm_config(
        config,
        adapter_file=run_dir / "llm_adapter.json",
        preset=preset,
    )


def _resolve_llm_config(
    config: dict[str, Any],
    *,
    adapter_file: Path,
    preset: LLMPreset,
) -> dict[str, Any]:
    definition = preset.definition
    config["llm"].update(
        {
            key: definition[key]
            for key in (
                "base_url",
                "endpoint",
                "model",
                "credential",
                "proxy_url",
                "context_window_tokens",
                "max_output_tokens",
                "context_safety_margin_tokens",
                "stream",
                "stream_endpoint",
                "stream_read_timeout_enabled",
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
                "max_parallel_per_key",
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
            "LLM Adapter 文件中的 adapter_id 与配置不一致"
        )
    config["_llm_adapter"] = adapter
    config["_llm_adapter_hash"] = adapter.digest
    stream = bool(config["llm"].get("stream", False))
    if stream and not adapter.streaming_supported:
        raise ConfigError("已启用流式请求，但 LLM Adapter 未声明 streaming 规则")
    adapter.build_request(
        api_key="***",
        model=str(config["llm"]["model"]),
        messages=[],
        temperature=0,
        max_output_tokens=int(config["llm"]["max_output_tokens"]),
        stream=stream,
        extra_body=config["_llm_extra_body"],
    )
    return config
