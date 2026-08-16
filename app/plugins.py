from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any

from .documents import (
    DocumentAdapter,
    DocumentChoiceOption,
    normalize_document_output,
)
from .errors import ConfigError, ProjectError, UsageError
from .translation_validation import (
    JapaneseKanaValidator,
    KoreanHangulValidator,
    SourceTextResidualValidator,
    TranslationValidator,
)

PLUGIN_PROTOCOL_VERSION = 9
PLUGIN_ENTRY_POINT = "another_llm_translator.plugins"


@dataclass(frozen=True)
class PluginDescriptor:
    plugin_id: str
    version: str
    protocol_version: int
    document_adapters: tuple[DocumentAdapter, ...] = ()
    translation_validators: tuple[TranslationValidator, ...] = ()


def _builtin_plugins() -> tuple[PluginDescriptor, ...]:
    from .epub_adapter import EPUBDocumentAdapter
    from .project import TXTDocumentAdapter

    return (
        PluginDescriptor(
            plugin_id="builtin-documents",
            version="1",
            protocol_version=PLUGIN_PROTOCOL_VERSION,
            document_adapters=(TXTDocumentAdapter(), EPUBDocumentAdapter()),
        ),
        PluginDescriptor(
            plugin_id="builtin-translation-validation",
            version="1",
            protocol_version=PLUGIN_PROTOCOL_VERSION,
            translation_validators=(
                JapaneseKanaValidator(),
                KoreanHangulValidator(),
                SourceTextResidualValidator(),
            ),
        ),
    )


def load_plugins() -> tuple[PluginDescriptor, ...]:
    plugins = list(_builtin_plugins())
    for entry_point in entry_points(group=PLUGIN_ENTRY_POINT):
        loaded = entry_point.load()
        descriptor = loaded() if callable(loaded) else loaded
        if not isinstance(descriptor, PluginDescriptor):
            raise ConfigError(
                f"插件入口未返回 PluginDescriptor：{entry_point.name}"
            )
        plugins.append(descriptor)
    seen_plugins: set[str] = set()
    seen_adapters: set[str] = set()
    seen_extensions: dict[str, str] = {}
    seen_validators: set[str] = set()
    for plugin in plugins:
        if plugin.protocol_version != PLUGIN_PROTOCOL_VERSION:
            raise ConfigError(
                f"插件协议版本不兼容：{plugin.plugin_id} "
                f"{plugin.protocol_version}"
            )
        if not plugin.plugin_id or plugin.plugin_id in seen_plugins:
            raise ConfigError(f"插件 ID 重复或为空：{plugin.plugin_id}")
        seen_plugins.add(plugin.plugin_id)
        for adapter in plugin.document_adapters:
            if not adapter.adapter_id or adapter.adapter_id in seen_adapters:
                raise ConfigError(
                    f"Document Adapter ID 重复或为空：{adapter.adapter_id}"
                )
            if not adapter.version or not adapter.capabilities:
                raise ConfigError(
                    f"Document Adapter 描述不完整：{adapter.adapter_id}"
                )
            extensions = getattr(adapter, "extensions", None)
            if not isinstance(extensions, frozenset) or not all(
                isinstance(value, str)
                and value.startswith(".")
                and value == value.casefold()
                and len(value) > 1
                for value in extensions
            ):
                raise ConfigError(
                    f"Document Adapter 扩展名声明无效：{adapter.adapter_id}"
                )
            if "import" in adapter.capabilities and not extensions:
                raise ConfigError(
                    f"可导入的 Document Adapter 必须声明扩展名："
                    f"{adapter.adapter_id}"
                )
            for extension in extensions:
                owner = seen_extensions.get(extension)
                if owner is not None:
                    raise ConfigError(
                        f"Document Adapter 扩展名重复：{extension} "
                        f"({owner}, {adapter.adapter_id})"
                    )
                seen_extensions[extension] = adapter.adapter_id
            options = getattr(adapter, "import_options", None)
            if not isinstance(options, tuple) or not all(
                isinstance(option, DocumentChoiceOption) for option in options
            ):
                raise ConfigError(
                    f"Document Adapter 导入选项声明无效：{adapter.adapter_id}"
                )
            if options and "import" not in adapter.capabilities:
                raise ConfigError(
                    f"不可导入的 Document Adapter 不能声明导入选项："
                    f"{adapter.adapter_id}"
                )
            seen_options: set[str] = set()
            for option in options:
                choice_ids = [choice_id for choice_id, _ in option.choices]
                choices_valid = all(
                    choice_id and label
                    for choice_id, label in option.choices
                )
                if (
                    not option.option_id
                    or option.option_id in seen_options
                    or not option.label
                    or len(choice_ids) < 2
                    or len(set(choice_ids)) != len(choice_ids)
                    or not choices_valid
                    or option.default not in choice_ids
                ):
                    raise ConfigError(
                        f"Document Adapter 导入选项声明无效："
                        f"{adapter.adapter_id}.{option.option_id}"
                    )
                seen_options.add(option.option_id)
            run_options = getattr(adapter, "run_options", ())
            if not isinstance(run_options, tuple) or not all(
                isinstance(option, DocumentChoiceOption) for option in run_options
            ):
                raise ConfigError(
                    f"Document Adapter 运行选项声明无效：{adapter.adapter_id}"
                )
            for option in run_options:
                choice_ids = [choice_id for choice_id, _ in option.choices]
                if (
                    not option.option_id
                    or option.option_id in seen_options
                    or not option.label
                    or len(choice_ids) < 2
                    or len(set(choice_ids)) != len(choice_ids)
                    or not all(choice_id and label for choice_id, label in option.choices)
                    or option.default not in choice_ids
                ):
                    raise ConfigError(
                        f"Document Adapter 运行选项声明无效："
                        f"{adapter.adapter_id}.{option.option_id}"
                    )
                seen_options.add(option.option_id)
            seen_adapters.add(adapter.adapter_id)
        validators = getattr(plugin, "translation_validators", None)
        if not isinstance(validators, tuple):
            raise ConfigError(
                f"翻译校验器声明无效：{plugin.plugin_id}"
            )
        for validator in validators:
            validator_id = getattr(validator, "validator_id", None)
            version = getattr(validator, "version", None)
            label = getattr(validator, "label", None)
            validate = getattr(validator, "validate", None)
            if (
                not isinstance(validator_id, str)
                or not validator_id.strip()
                or validator_id in seen_validators
                or not isinstance(version, str)
                or not version.strip()
                or not isinstance(label, str)
                or not label.strip()
                or not callable(validate)
            ):
                raise ConfigError(
                    f"翻译校验器描述不完整：{plugin.plugin_id}.{validator_id}"
                )
            seen_validators.add(validator_id)
    return tuple(plugins)


def get_document_adapter(adapter_id: str) -> DocumentAdapter:
    for plugin in load_plugins():
        for adapter in plugin.document_adapters:
            if adapter.adapter_id == adapter_id:
                return adapter
    raise UsageError(f"未安装 Document Adapter：{adapter_id}")


def resolve_translation_validators(
    validator_ids: list[str] | tuple[str, ...] | None = None,
) -> tuple[tuple[TranslationValidator, dict[str, str]], ...]:
    requested = set(validator_ids) if validator_ids is not None else None
    values: list[tuple[TranslationValidator, dict[str, str]]] = []
    for plugin in load_plugins():
        for validator in plugin.translation_validators:
            if requested is not None and validator.validator_id not in requested:
                continue
            values.append(
                (
                    validator,
                    {
                        "validator_id": validator.validator_id,
                        "version": validator.version,
                        "label": validator.label,
                        "plugin_id": plugin.plugin_id,
                        "plugin_version": plugin.version,
                    },
                )
            )
    values.sort(key=lambda value: value[1]["validator_id"])
    if requested is not None:
        found = {summary["validator_id"] for _, summary in values}
        missing = sorted(requested - found)
        if missing:
            raise ConfigError("未安装翻译校验器：" + ", ".join(missing))
    return tuple(values)


def normalize_model_text(
    files: list[dict[str, Any]],
    segment: dict[str, Any],
    text: str,
    stage: str,
) -> str:
    """Normalize model output through the segment's document adapter."""
    file_id = str(segment["file_id"])
    file_record = next(
        (item for item in files if str(item["file_id"]) == file_id), None
    )
    if file_record is None:
        raise ProjectError(f"模型文本引用了未知文件：{file_id}")
    adapter = get_document_adapter(str(file_record["document_adapter_id"]))
    return normalize_document_output(
        adapter, segment=segment, text=text, stage=stage
    )


def get_document_adapter_for_extension(extension: str) -> DocumentAdapter:
    normalized = extension.casefold()
    for plugin in load_plugins():
        for adapter in plugin.document_adapters:
            if normalized in adapter.extensions:
                return adapter
    raise UsageError(f"没有 Document Adapter 支持扩展名：{extension or '（无）'}")


def validate_document_import_options(
    adapter: DocumentAdapter, values: dict[str, str] | None
) -> dict[str, str]:
    provided = values or {}
    declarations = {
        option.option_id: option for option in adapter.import_options
    }
    run_options = getattr(adapter, "run_options", ())
    declarations.update(
        {option.option_id: option for option in run_options}
    )
    unknown = sorted(set(provided) - set(declarations))
    if unknown:
        raise UsageError(
            f"{adapter.adapter_id} 包含未知导入选项：{', '.join(unknown)}"
        )
    resolved: dict[str, str] = {}
    for option_id, option in {
        option.option_id: option for option in adapter.import_options
    }.items():
        value = provided.get(option_id, option.default)
        choices = {choice_id for choice_id, _ in option.choices}
        if value not in choices:
            raise UsageError(
                f"{adapter.adapter_id}.{option_id} 取值无效：{value}"
            )
        resolved[option_id] = value
    for option in run_options:
        if option.option_id not in provided:
            continue
        value = provided[option.option_id]
        choices = {choice_id for choice_id, _ in option.choices}
        if value not in choices:
            raise UsageError(
                f"{adapter.adapter_id}.{option.option_id} 取值无效：{value}"
            )
        resolved[option.option_id] = value
    return resolved


def document_adapter_summaries() -> list[dict[str, object]]:
    values = []
    for plugin in load_plugins():
        for adapter in plugin.document_adapters:
            values.append(
                {
                    "adapter_id": adapter.adapter_id,
                    "version": adapter.version,
                    "plugin_id": plugin.plugin_id,
                    "plugin_version": plugin.version,
                    "capabilities": sorted(adapter.capabilities),
                    "extensions": sorted(adapter.extensions),
                    "import_options": [
                        {
                            "option_id": option.option_id,
                            "label": option.label,
                            "default": option.default,
                            "choices": [
                                {"value": value, "label": label}
                                for value, label in option.choices
                            ],
                        }
                        for option in adapter.import_options
                    ],
                    "run_options": [
                        {
                            "option_id": option.option_id,
                            "label": option.label,
                            "default": option.default,
                            "choices": [
                                {"value": value, "label": label}
                                for value, label in option.choices
                            ],
                        }
                        for option in getattr(adapter, "run_options", ())
                    ],
                }
            )
    return sorted(values, key=lambda value: str(value["adapter_id"]))
