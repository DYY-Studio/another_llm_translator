from __future__ import annotations

import codecs
import hashlib
import json
import re
import tomllib
from copy import deepcopy
from pathlib import Path
from typing import Any

from .errors import ConfigError, ConfigFieldError
from .llm_adapter import JSONLLMAdapter, load_json_adapter
from .llm_preset import LLMPreset, load_llm_preset, preset_path
from .user_config import APP_ROOT, BUILTIN_ROOT, effective_path
from .request_budget import BUDGET_FIELDS, BUDGET_STAGES

LLM_STAGES = ("terminology", "translation", "proofreading", "polishing")
LLM_MODEL_STAGES = (*LLM_STAGES, "terminology_decision", "content_summary", "repetition_consistency")

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
        "preset_content_summary": None,
        "preset_repetition_consistency": None,
        "preset_translation": None,
        "preset_proofreading": None,
        "preset_polishing": None,
        "temperature_terminology": None,
        "temperature_terminology_decision": None,
        "temperature_content_summary": None,
        "temperature_repetition_consistency": None,
        "temperature_translation": None,
        "temperature_proofreading": None,
        "temperature_polishing": None,
    },
    "decision": {"preset": None},
    "execution": {"scheduling_mode": None},
    "chunking": {
        **dict.fromkeys(BUDGET_FIELDS),
        "stage_overrides": None,
        "allow_split_oversized_segment": None,
        "cross_boundary_batching": None,
    },
    "context": {
        "translation": {
            "enabled": None,
            "previous_segments": None,
            "previous_summaries": None,
        },
        "proofreading": {"enabled": None, "previous_segments": None},
        "polishing": {"enabled": None, "previous_segments": None},
        "terminology": {"enabled": None, "previous_segments": None},
    },
    "terminology": {
        "unicode_normalization": None,
        "case_insensitive": None,
        "max_terms_per_segment": None,
        "alias_primary_collision": None,
        "sourced_description_injection": None,
        "unsourced_description_injection": None,
    },
    "terminology_decision": {
        "description_generation": None,
    },
    "validation": {
        "translation": {
            "validators": None,
            "decision_enabled": None,
            "decision_preset": None,
            "decision_confidence_threshold": None,
            "decision_warning_threshold": None,
            "decision_context_enabled": None,
            "decision_previous_segments": None,
            "alignment": {
                "decision_preset": None,
                "confidence_threshold": None,
                "warning_threshold": None,
                "tail_segments": None,
            },
            "max_retry_attempts": None,
            "exhausted_mode": None,
        }
    },
    "retry": {
        "http_max_attempts": None,
        "format_max_attempts": None,
        "empty_truncated_mode": None,
        "empty_truncated_max_attempts": None,
        "empty_unknown_mode": None,
        "empty_unknown_max_attempts": None,
        "unresolved_retry_scope": None,
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
    for section, key in (("retry", "http_max_attempts"),):
        value = config[section][key]
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
        ):
            raise ConfigError(f"{section}.{key} 必须是正整数")
    for kind in ("truncated", "unknown"):
        mode_key = f"empty_{kind}_mode"
        count_key = f"empty_{kind}_max_attempts"
        if config["retry"][mode_key] not in ("retry", "split"):
            raise ConfigError(f"retry.{mode_key} 必须是 retry 或 split")
        count = config["retry"][count_key]
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ConfigError(f"retry.{count_key} 必须是非负整数")
    format_attempts = config["retry"]["format_max_attempts"]
    if (
        not isinstance(format_attempts, int)
        or isinstance(format_attempts, bool)
        or format_attempts < 0
    ):
        raise ConfigError("retry.format_max_attempts 必须是非负整数")
    if config["retry"]["unresolved_retry_scope"] not in ("unresolved", "chunk"):
        raise ConfigError("retry.unresolved_retry_scope 必须是 unresolved 或 chunk")

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
        "temperature_content_summary",
        "temperature_repetition_consistency",
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
    decision = config["validation"]["translation"]
    if type(decision["decision_enabled"]) is not bool or not isinstance(decision["decision_preset"], str):
        raise ConfigError("Decision 校验配置无效")
    if type(decision["decision_context_enabled"]) is not bool:
        raise ConfigError("Decision 上文开关必须是布尔值")
    if type(decision["decision_previous_segments"]) is not int or decision["decision_previous_segments"] < 0:
        raise ConfigError("Decision 上文 Segment 数必须是非负整数")
    threshold = decision["decision_confidence_threshold"]
    if type(threshold) not in {int, float} or not 0 <= threshold <= 1:
        raise ConfigError("Decision 置信度门槛必须在 0 到 1 之间")
    warning_threshold = decision["decision_warning_threshold"]
    if type(warning_threshold) not in {int, float} or not 0 <= warning_threshold <= 1:
        raise ConfigError("术语复核警告阈值必须在 0 到 1 之间")
    if not isinstance(config["decision"]["preset"], str):
        raise ConfigError("通用 Decision Preset 必须是字符串")
    if decision["decision_enabled"] and "preferred_term_usage" not in decision["validators"]:
        raise ConfigError("Decision 复核需要启用 preferred_term_usage")
    alignment = decision["alignment"]
    if not isinstance(alignment["decision_preset"], str):
        raise ConfigError("错位校验 Decision Preset 必须是字符串")
    if type(alignment["confidence_threshold"]) not in {int, float} or not 0 <= alignment["confidence_threshold"] <= 1:
        raise ConfigError("错位校验置信度门槛必须在 0 到 1 之间")
    if type(alignment["warning_threshold"]) not in {int, float} or not 0 <= alignment["warning_threshold"] <= 1:
        raise ConfigError("错位校验警告阈值必须在 0 到 1 之间")
    if type(alignment["tail_segments"]) is not int or alignment["tail_segments"] < 1:
        raise ConfigError("错位校验尾部 Segment 数必须是正整数")
    for validator_id, override in decision_validator_overrides(config).items():
        if not (override or config["decision"]["preset"]):
            raise ConfigError(f"{validator_id} 需要选择 Decision Preset 或通用 Decision Preset")
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
    overrides = config["chunking"]["stage_overrides"]
    if not isinstance(overrides, dict) or set(overrides) - set(BUDGET_STAGES):
        raise ConfigError("chunking.stage_overrides 必须是已知预算阶段表")
    budgets = [("chunking", {key: config["chunking"][key] for key in BUDGET_FIELDS})]
    for stage, fields in overrides.items():
        if not isinstance(fields, dict) or set(fields) - set(BUDGET_FIELDS):
            raise ConfigError(f"chunking.stage_overrides.{stage} 必须是预算字段表")
        budgets.append((f"chunking.stage_overrides.{stage}", fields))
    for path, fields in budgets:
        for key, number in fields.items():
            if type(number) is not int or number < (0 if key == "context_reserve_tokens" else 1):
                raise ConfigError(f"{path}.{key} 必须是{'非负' if key == 'context_reserve_tokens' else '正'}整数")
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
    for section, key, choices in (
        ("terminology", "sourced_description_injection", ("none", "prefer_previous", "all")),
        ("terminology", "unsourced_description_injection", ("none", "when_no_sourced", "all")),
        ("terminology_decision", "description_generation", ("sourced", "sourced_and_summary", "summary_only")),
    ):
        if config[section][key] not in choices:
            raise ConfigError(f"{section}.{key} 必须是 {', '.join(choices)}")
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
        if stage == "translation" and not isinstance(
            context["previous_summaries"], bool
        ):
            raise ConfigError("context.translation.previous_summaries 必须是布尔值")


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
            if child_schema is None and key != "stage_overrides":
                lines.append(f"{key} = {_toml_scalar(value[key])}")
        if path == ("chunking",):
            lines.extend(["", "[chunking.stage_overrides]"])
            for stage, fields in value["stage_overrides"].items():
                lines.extend(["", f"[chunking.stage_overrides.{stage}]"])
                lines.extend(f"{key} = {_toml_scalar(fields[key])}" for key in BUDGET_FIELDS if key in fields)
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


def report_unknown(path: str, keys: set[str], warnings: list[str] | None) -> None:
    if not keys:
        return
    message = f"忽略未知配置字段 {path}: {', '.join(sorted(keys))}"
    if warnings is not None:
        warnings.append(message)
    else:
        from .logging_utils import get_logger
        get_logger("config").warning(message)


def normalize_settings(
    value: dict[str, Any], defaults: dict[str, Any], *,
    warnings: list[str] | None = None, path: str = "config",
) -> dict[str, Any]:
    """Fill absent fixed settings while preserving errors in present values."""
    if not isinstance(value, dict):
        raise ConfigError(f"配置节必须是表：{path}")
    report_unknown(path, set(value) - set(defaults), warnings)
    result = {}
    for key, default in defaults.items():
        if key not in value:
            result[key] = deepcopy(default)
        elif path == "config.chunking" and key == "stage_overrides":
            overrides = value[key]
            if not isinstance(overrides, dict):
                raise ConfigError("chunking.stage_overrides 必须是表")
            report_unknown(f"{path}.{key}", set(overrides) - set(BUDGET_STAGES), warnings)
            result[key] = {}
            for stage, fields in overrides.items():
                if stage not in BUDGET_STAGES:
                    continue
                if not isinstance(fields, dict):
                    raise ConfigError(f"chunking.stage_overrides.{stage} 必须是表")
                report_unknown(f"{path}.{key}.{stage}", set(fields) - set(BUDGET_FIELDS), warnings)
                result[key][stage] = {name: deepcopy(child) for name, child in fields.items() if name in BUDGET_FIELDS}
        elif isinstance(default, dict):
            result[key] = normalize_settings(value[key], default, warnings=warnings, path=f"{path}.{key}")
        else:
            result[key] = deepcopy(value[key])
    return result


def builtin_settings() -> dict[str, Any]:
    return _read_settings(BUILTIN_ROOT / "config/config.toml")


def _read_settings(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"无法读取配置：{path}: {exc}") from exc


def load_config(
    path: Path, *, defaults: dict[str, Any] | None = None,
    warnings: list[str] | None = None, snapshot: bool = False,
) -> dict[str, Any]:
    config = _read_settings(path)
    if not snapshot:
        config = normalize_config(config, defaults=defaults, warnings=warnings)
    else:
        validate_config(config)
    return config


def normalize_config(
    config: dict[str, Any], *, defaults: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    result = normalize_settings(config, defaults if defaults is not None else builtin_settings(), warnings=warnings)
    validate_config(result)
    return result


def canonical_config(config: dict[str, Any]) -> dict[str, Any]:
    """Extract settings from an already resolved runtime configuration."""
    def project(value: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        return {key: deepcopy(value[key]) if child is None else project(value[key], child)
                for key, child in schema.items()}
    result = project(config, SCHEMA)
    validate_config(result)
    return result


def load_global_settings(root: Path = APP_ROOT, *, warnings: list[str] | None = None) -> dict[str, Any]:
    return load_config(effective_path("config/config.toml", builtin_root=root),
                       defaults=builtin_settings(), warnings=warnings)


def load_project_settings(project: Path, *, root: Path = APP_ROOT,
                          warnings: list[str] | None = None) -> dict[str, Any]:
    return normalize_project_settings(_read_settings(project / "config.toml"), root=root, warnings=warnings)


def normalize_project_settings(value: dict[str, Any], *, root: Path = APP_ROOT,
                               warnings: list[str] | None = None) -> dict[str, Any]:
    def missing_settings(table: dict[str, Any], schema: dict[str, Any]) -> bool:
        for key, child in schema.items():
            if key == "stage_overrides":
                continue
            if key not in table:
                return True
            if child is not None and isinstance(table[key], dict) and missing_settings(table[key], child):
                return True
        return False

    defaults = load_global_settings(root, warnings=warnings) if missing_settings(value, SCHEMA) else builtin_settings()
    return normalize_config(value, defaults=defaults, warnings=warnings)


def load_project_config(
    project: Path,
    *,
    stage: str | None = None,
    presets_root: Path | None = None,
) -> dict[str, Any]:
    return resolve_project_config(
        load_project_settings(project, root=presets_root or APP_ROOT),
        stage=stage,
        presets_root=presets_root,
        project=project,
    )


def _resolve_config(
    config: dict[str, Any],
    root: Path,
    *,
    stage: str | None,
    error_kind: str,
    project: Path | None = None,
) -> dict[str, Any]:
    config = deepcopy(config)
    config["_request_budget_stage"] = stage or "translation"
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
    _resolve_decision_config(config, root, project=project)
    from .decision_prompt import resolve_run_prompts
    resolve_run_prompts(config, project=project)
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
        adapter=load_json_adapter(effective_path(
            f"llm_adapters/{preset.adapter_id}.json", builtin_root=root
        )),
        preset=preset,
    )


def resolve_project_config(
    config: dict[str, Any],
    *,
    project: Path | None = None,
    stage: str | None = None,
    presets_root: Path | None = None,
) -> dict[str, Any]:
    return _resolve_config(
        config,
        presets_root or APP_ROOT,
        stage=stage,
        error_kind="项目配置",
        project=project,
    )


def load_global_config(root: Path) -> dict[str, Any]:
    return resolve_global_config(
        load_global_settings(root), root
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


def decision_validator_overrides(config: dict[str, Any]) -> dict[str, str]:
    options = config["validation"]["translation"]
    result = {}
    if options["decision_enabled"]:
        result["preferred_term_usage"] = options["decision_preset"]
    if "segment_alignment" in options["validators"]:
        result["segment_alignment"] = options["alignment"]["decision_preset"]
    return result


def _resolve_decision_config(config: dict[str, Any], root: Path, *, snapshot: bool = False, project: Path | None = None) -> None:
    from .decision import decision_preset_path, load_decision_preset, validate_decision_preset
    definitions = {}
    bindings = {}
    for validator_id, override in decision_validator_overrides(config).items():
        preset_id = override or config["decision"]["preset"]
        bindings[validator_id] = preset_id
        if preset_id in definitions:
            continue
        path = decision_preset_path(root, preset_id) if snapshot else effective_path(
            str(decision_preset_path(Path(), preset_id)), builtin_root=root)
        definition = load_decision_preset(path, snapshot=snapshot)
        validate_decision_preset({key: value for key, value in definition.items() if key != "_resources"})
        if definition["protocol"] == "llm" and not snapshot:
            from .decision_llm import resolve_llm_resources
            definition = resolve_llm_resources(definition, root, project=project)
        if definition["preset_id"] != preset_id:
            raise ConfigError("Decision Preset ID 与配置不一致")
        definitions[preset_id] = definition
    config["_decision_preset_definitions"] = definitions
    config["_decision_validator_presets"] = bindings


def load_run_config(run_dir: Path) -> dict[str, Any]:
    config = load_config(run_dir / "config.toml", snapshot=True)
    _resolve_decision_config(config, run_dir, snapshot=True)
    from .decision_prompt import resolve_run_prompts
    resolve_run_prompts(config, snapshot=run_dir)
    preset = load_llm_preset(run_dir / "llm_preset.json", snapshot=True)
    return _resolve_llm_config(
        config,
        adapter=load_json_adapter(run_dir / "llm_adapter.json"),
        preset=preset,
    )


def _resolve_llm_config(
    config: dict[str, Any],
    *,
    adapter: JSONLLMAdapter,
    preset: LLMPreset,
) -> dict[str, Any]:
    from .llm_preset import validate_executable_preset
    validate_executable_preset(preset.definition)
    definition = preset.definition
    if adapter.adapter_id != preset.adapter_id:
        raise ConfigError("LLM Adapter 文件中的 adapter_id 与配置不一致")
    adapter.validate_preset(definition)
    config["llm"].update(
        {
            key: definition[key]
            for key in (
                "base_url",
                "model",
                "credential",
                "proxy_url",
                "context_window_tokens",
                "max_output_tokens",
                "send_temperature",
                "reasoning_effort",
                "stream",
                "stream_read_timeout_enabled",
            )
        }
    )
    config["llm"]["json_schema_enabled"] = definition.get("json_schema_enabled", False)
    config["llm"]["endpoint"] = adapter.endpoint
    config["llm"]["stream_endpoint"] = (
        adapter.streaming_spec["endpoint"]
        if adapter.streaming_spec is not None
        else ""
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
    fingerprint_definition = dict(definition)
    if definition["credential"]["kind"] == "chatgpt":
        from .chatgpt_oauth import ChatGPTConnection
        connection = ChatGPTConnection()
        state = connection.read()
        config["llm"]["proxy_url"] = state["proxy_url"]
        config["_chatgpt_identity"] = connection.identity() if state["active"] else None
        fingerprint_definition["chatgpt_connection"] = state["active"]
        fingerprint_definition["proxy_url"] = state["proxy_url"]
        if adapter.endpoint != "/responses" or adapter.streaming_spec is None or adapter.streaming_spec["endpoint"] != "/responses" or adapter.messages_format != "responses":
            raise ConfigError("ChatGPT Plan Adapter 必须使用 /responses 和 responses 消息格式")
    config["_llm_preset_stage_hash"] = "sha256:" + hashlib.sha256(
        json.dumps(
            fingerprint_definition,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    config["_llm_preset_definition"] = definition
    config["_llm_extra_body"] = definition["extra_body"]
    config["_llm_extra_headers"] = definition["extra_headers"]
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
        send_temperature=definition["send_temperature"],
        reasoning_effort=definition["reasoning_effort"],
        extra_body=config["_llm_extra_body"],
        extra_headers=config["_llm_extra_headers"],
        session_id="RUN-VALIDATION",
        request_id="REQ-VALIDATION",
    )
    return config
