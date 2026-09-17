from __future__ import annotations

import importlib
import importlib.util
import re
import stat
import sys
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import plugin_api
from .documents import (
    normalize_document_output,
)
from .errors import ConfigError, ProjectError, UsageError
from .translation_validation import (
    JapaneseKanaValidator,
    KoreanHangulValidator,
    SourceTextResidualValidator,
)
from .user_config import BUILTIN_ROOT, user_root


@dataclass(frozen=True)
class _PluginManifest:
    path: Path
    plugin_id: str
    version: str
    protocol: int
    entrypoint: str


_PLUGIN_CACHE: tuple[plugin_api.PluginDescriptor, ...] | None = None
_PLUGIN_LOAD_LOCK = threading.Lock()
_PLUGIN_MANIFEST_SCHEMA = 1
_PLUGIN_NAMESPACE_PREFIX = "_another_llm_plugin_"
_ENTRYPOINT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$")


def _official_plugin_root() -> Path:
    return BUILTIN_ROOT / "plugins"


def _plugin_failure(path: Path, stage: str, reason: str) -> ConfigError:
    return ConfigError(f"插件加载失败 [{path}] 阶段 {stage}：{reason}")


def _descriptor_failure(
    message: str,
    source: str,
    *,
    context: str | None = None,
) -> ConfigError:
    return ConfigError(
        f"{message}（{context or f'来源：{source}'}；阶段：descriptor）"
    )


def _manifest_text(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise _plugin_failure(path, "manifest", str(exc)) from exc
    if not isinstance(value, dict):
        raise _plugin_failure(path, "manifest", "根值必须是表")
    return value


def _read_manifest(plugin_dir: Path) -> _PluginManifest:
    manifest_path = plugin_dir / "plugin.toml"
    if not manifest_path.is_file():
        raise _plugin_failure(plugin_dir, "manifest", "缺少 plugin.toml")
    value = _manifest_text(manifest_path)
    if set(value) != {"schema", "plugin"} or value.get("schema") != _PLUGIN_MANIFEST_SCHEMA:
        raise _plugin_failure(plugin_dir, "manifest", "schema 必须为 1 且只允许 plugin 表")
    plugin = value.get("plugin")
    if not isinstance(plugin, dict) or set(plugin) != {"id", "version", "protocol", "entrypoint"}:
        raise _plugin_failure(
            manifest_path,
            "manifest",
            "plugin 必须包含 id、version、protocol、entrypoint 且不含未知字段",
        )
    plugin_id = plugin.get("id")
    version = plugin.get("version")
    protocol = plugin.get("protocol")
    entrypoint = plugin.get("entrypoint")
    if (
        not isinstance(plugin_id, str)
        or not plugin_id.strip()
        or not isinstance(version, str)
        or not version.strip()
        or not isinstance(protocol, int)
        or isinstance(protocol, bool)
        or not isinstance(entrypoint, str)
        or not _ENTRYPOINT_RE.fullmatch(entrypoint)
    ):
        raise _plugin_failure(manifest_path, "manifest", "字段类型或值无效")
    if protocol != plugin_api.PLUGIN_PROTOCOL_VERSION:
        raise _plugin_failure(
            manifest_path,
            "manifest",
            f"协议版本不兼容：{protocol}，宿主需要 {plugin_api.PLUGIN_PROTOCOL_VERSION}",
        )
    return _PluginManifest(
        path=plugin_dir,
        plugin_id=plugin_id,
        version=version,
        protocol=protocol,
        entrypoint=entrypoint,
    )


def _discover_manifests(root: Path, *, required: bool) -> tuple[_PluginManifest, ...]:
    try:
        mode = root.stat().st_mode
    except FileNotFoundError:
        if not required:
            return ()
        raise _plugin_failure(root, "discover", "官方插件目录不存在")
    except OSError as exc:
        raise _plugin_failure(root, "discover", str(exc)) from exc
    if not stat.S_ISDIR(mode):
        raise _plugin_failure(root, "discover", "插件根路径必须是目录")
    try:
        children = sorted(root.iterdir(), key=lambda value: value.name)
    except OSError as exc:
        raise _plugin_failure(root, "discover", str(exc)) from exc
    return tuple(
        _read_manifest(child)
        for child in children
        if child.is_dir()
    )


def _discard_plugin_namespace(package_name: str) -> None:
    for name in tuple(sys.modules):
        if name == package_name or name.startswith(package_name + "."):
            sys.modules.pop(name, None)


def _load_manifest_descriptor(manifest: _PluginManifest, index: int) -> plugin_api.PluginDescriptor:
    plugin_dir = manifest.path
    init_path = plugin_dir / "__init__.py"
    if not init_path.is_file():
        raise _plugin_failure(plugin_dir, "import", "缺少 __init__.py")
    module_text, separator, attribute = manifest.entrypoint.partition(":")
    if not separator or not module_text or not attribute:
        raise _plugin_failure(plugin_dir, "entrypoint", "格式必须为 module:attribute")
    module_parts = module_text.split(".")
    module_path = plugin_dir.joinpath(*module_parts).with_suffix(".py")
    try:
        module_path.relative_to(plugin_dir)
    except ValueError as exc:
        raise _plugin_failure(plugin_dir, "entrypoint", "入口必须位于插件目录内") from exc
    if not module_path.is_file():
        raise _plugin_failure(plugin_dir, "entrypoint", f"入口模块不存在：{module_text}")
    package_name = (
        f"{_PLUGIN_NAMESPACE_PREFIX}{index}_{manifest.plugin_id.replace('-', '_')}"
    )
    try:
        package_spec = importlib.util.spec_from_file_location(
            package_name,
            init_path,
            submodule_search_locations=[str(plugin_dir)],
        )
        if package_spec is None or package_spec.loader is None:
            raise ImportError("无法创建插件包加载器")
        package = importlib.util.module_from_spec(package_spec)
        sys.modules[package_name] = package
        package_spec.loader.exec_module(package)
        module = importlib.import_module(f"{package_name}.{module_text}")
        factory = getattr(module, attribute)
    except Exception as exc:
        _discard_plugin_namespace(package_name)
        raise _plugin_failure(plugin_dir, "import", str(exc)) from exc
    if not callable(factory):
        _discard_plugin_namespace(package_name)
        raise _plugin_failure(plugin_dir, "entrypoint", "入口必须是可调用的无参工厂")
    try:
        descriptor = factory()
    except TypeError as exc:
        _discard_plugin_namespace(package_name)
        raise _plugin_failure(plugin_dir, "factory", f"入口必须是无参工厂：{exc}") from exc
    except Exception as exc:
        _discard_plugin_namespace(package_name)
        raise _plugin_failure(plugin_dir, "factory", str(exc)) from exc
    if not isinstance(descriptor, plugin_api.PluginDescriptor):
        _discard_plugin_namespace(package_name)
        raise _plugin_failure(plugin_dir, "factory", "入口未返回 PluginDescriptor")
    if (
        descriptor.plugin_id != manifest.plugin_id
        or descriptor.version != manifest.version
        or descriptor.protocol_version != manifest.protocol
    ):
        _discard_plugin_namespace(package_name)
        raise _plugin_failure(
            plugin_dir,
            "descriptor",
            "manifest 与 PluginDescriptor 的 id、version 或 protocol 不一致",
        )
    return descriptor


def _builtin_plugins() -> tuple[plugin_api.PluginDescriptor, ...]:
    from .epub_adapter import EPUBDocumentAdapter
    from .project import TXTDocumentAdapter

    return (
        plugin_api.PluginDescriptor(
            plugin_id="builtin-documents",
            version="1",
            protocol_version=plugin_api.PLUGIN_PROTOCOL_VERSION,
            document_adapters=(TXTDocumentAdapter(), EPUBDocumentAdapter()),
        ),
        plugin_api.PluginDescriptor(
            plugin_id="builtin-translation-validation",
            version="1",
            protocol_version=plugin_api.PLUGIN_PROTOCOL_VERSION,
            translation_validators=(
                JapaneseKanaValidator(),
                KoreanHangulValidator(),
                SourceTextResidualValidator(),
            ),
        ),
    )


def _validate_plugins(
    plugins: list[plugin_api.PluginDescriptor],
    source_paths: dict[int, str] | None = None,
) -> tuple[plugin_api.PluginDescriptor, ...]:
    source_paths = source_paths or {}
    seen_plugins: dict[str, str] = {}
    seen_adapters: dict[str, tuple[str, str]] = {}
    seen_extensions: dict[str, tuple[str, str, str]] = {}
    seen_validators: dict[str, tuple[str, str]] = {}
    for plugin in plugins:
        source = source_paths.get(id(plugin), f"插件：{plugin.plugin_id}")

        if plugin.protocol_version != plugin_api.PLUGIN_PROTOCOL_VERSION:
            raise _descriptor_failure(
                f"插件协议版本不兼容：{plugin.plugin_id} {plugin.protocol_version}",
                source,
            )
        previous_source = seen_plugins.get(plugin.plugin_id)
        if not plugin.plugin_id or previous_source is not None:
            context = (
                f"来源：{previous_source}、{source}"
                if previous_source is not None
                else f"来源：{source}"
            )
            raise _descriptor_failure(
                f"插件 ID 重复或为空：{plugin.plugin_id}",
                source,
                context=context,
            )
        seen_plugins[plugin.plugin_id] = source
        for adapter in plugin.document_adapters:
            previous_adapter = seen_adapters.get(adapter.adapter_id)
            if not adapter.adapter_id or previous_adapter is not None:
                context = (
                    f"{previous_adapter[0]} 来源：{previous_adapter[1]}；"
                    f"{plugin.plugin_id} 来源：{source}"
                    if previous_adapter is not None
                    else f"来源：{source}"
                )
                raise _descriptor_failure(
                    f"Document Adapter ID 重复或为空：{adapter.adapter_id}",
                    source,
                    context=context,
                )
            if not adapter.version or not adapter.capabilities:
                raise _descriptor_failure(
                    f"Document Adapter 描述不完整：{adapter.adapter_id}",
                    source,
                )
            readable_versions = getattr(adapter, "readable_versions", None)
            if readable_versions is not None and (
                not isinstance(readable_versions, frozenset)
                or adapter.version not in readable_versions
                or not all(
                    isinstance(value, str) and value
                    for value in readable_versions
                )
            ):
                raise _descriptor_failure(
                    f"Document Adapter 可读版本声明无效：{adapter.adapter_id}",
                    source,
                )
            extensions = getattr(adapter, "extensions", None)
            if not isinstance(extensions, frozenset) or not all(
                isinstance(value, str)
                and value.startswith(".")
                and value == value.casefold()
                and len(value) > 1
                for value in extensions
            ):
                raise _descriptor_failure(
                    f"Document Adapter 扩展名声明无效：{adapter.adapter_id}",
                    source,
                )
            if "import" in adapter.capabilities and not extensions:
                raise _descriptor_failure(
                    "可导入的 Document Adapter 必须声明扩展名："
                    f"{adapter.adapter_id}",
                    source,
                )
            for extension in extensions:
                owner = seen_extensions.get(extension)
                if owner is not None:
                    context = (
                        f"{owner[2]}.{owner[0]} 来源：{owner[1]}，"
                        f"{plugin.plugin_id}.{adapter.adapter_id} 来源：{source}"
                    )
                    raise _descriptor_failure(
                        f"Document Adapter 扩展名重复：{extension}",
                        source,
                        context=context,
                    )
                seen_extensions[extension] = (
                    adapter.adapter_id,
                    source,
                    plugin.plugin_id,
                )
            options = getattr(adapter, "import_options", None)
            if not isinstance(options, tuple) or not all(
                isinstance(option, plugin_api.DocumentChoiceOption)
                for option in options
            ):
                raise _descriptor_failure(
                    f"Document Adapter 导入选项声明无效：{adapter.adapter_id}",
                    source,
                )
            if options and "import" not in adapter.capabilities:
                raise _descriptor_failure(
                    "不可导入的 Document Adapter 不能声明导入选项："
                    f"{adapter.adapter_id}",
                    source,
                )
            seen_options: set[str] = set()
            for option in options:
                choice_ids = [choice_id for choice_id, _ in option.choices]
                replacement_choice_ids = [
                    choice_id for choice_id, _ in option.replacement_choices
                ]
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
                    or len(set(replacement_choice_ids)) != len(replacement_choice_ids)
                    or set(choice_ids) & set(replacement_choice_ids)
                    or not all(
                        choice_id and label
                        for choice_id, label in option.replacement_choices
                    )
                ):
                    raise _descriptor_failure(
                        "Document Adapter 导入选项声明无效："
                        f"{adapter.adapter_id}.{option.option_id}",
                        source,
                    )
                seen_options.add(option.option_id)
            run_options = getattr(adapter, "run_options", ())
            if not isinstance(run_options, tuple) or not all(
                isinstance(option, plugin_api.DocumentChoiceOption)
                for option in run_options
            ):
                raise _descriptor_failure(
                    f"Document Adapter 运行选项声明无效：{adapter.adapter_id}",
                    source,
                )
            for option in run_options:
                choice_ids = [choice_id for choice_id, _ in option.choices]
                replacement_choice_ids = [
                    choice_id for choice_id, _ in option.replacement_choices
                ]
                if (
                    not option.option_id
                    or option.option_id in seen_options
                    or not option.label
                    or len(choice_ids) < 2
                    or len(set(choice_ids)) != len(choice_ids)
                    or not all(choice_id and label for choice_id, label in option.choices)
                    or option.default not in choice_ids
                    or len(set(replacement_choice_ids)) != len(replacement_choice_ids)
                    or set(choice_ids) & set(replacement_choice_ids)
                    or not all(
                        choice_id and label
                        for choice_id, label in option.replacement_choices
                    )
                ):
                    raise _descriptor_failure(
                        "Document Adapter 运行选项声明无效："
                        f"{adapter.adapter_id}.{option.option_id}",
                        source,
                    )
                seen_options.add(option.option_id)
            if not callable(getattr(adapter, "model_prompt_requirements", None)):
                raise _descriptor_failure(
                    "Document Adapter 缺少 model_prompt_requirements："
                    f"{adapter.adapter_id}",
                    source,
                )
            if not callable(getattr(adapter, "render_model_source", None)):
                raise _descriptor_failure(
                    "Document Adapter 缺少 render_model_source："
                    f"{adapter.adapter_id}",
                    source,
                )
            if not callable(getattr(adapter, "segment_format_count", None)):
                raise _descriptor_failure(
                    "Document Adapter 缺少 segment_format_count："
                    f"{adapter.adapter_id}",
                    source,
                )
            if not callable(getattr(adapter, "replacement_options", None)):
                raise _descriptor_failure(
                    "Document Adapter 缺少 replacement_options："
                    f"{adapter.adapter_id}",
                    source,
                )
            seen_adapters[adapter.adapter_id] = (plugin.plugin_id, source)
        validators = getattr(plugin, "translation_validators", None)
        if not isinstance(validators, tuple):
            raise _descriptor_failure(
                f"翻译校验器声明无效：{plugin.plugin_id}",
                source,
            )
        for validator in validators:
            validator_id = getattr(validator, "validator_id", None)
            version = getattr(validator, "version", None)
            label = getattr(validator, "label", None)
            validate = getattr(validator, "validate", None)
            if (
                not isinstance(validator_id, str)
                or not validator_id.strip()
                or not isinstance(version, str)
                or not version.strip()
                or not isinstance(label, str)
                or not label.strip()
                or not callable(validate)
            ):
                raise _descriptor_failure(
                    f"翻译校验器描述不完整：{plugin.plugin_id}.{validator_id}",
                    source,
                )
            previous_validator = seen_validators.get(validator_id)
            if previous_validator is not None:
                context = (
                    f"{previous_validator[0]} 来源：{previous_validator[1]}；"
                    f"{plugin.plugin_id} 来源：{source}"
                )
                raise _descriptor_failure(
                    f"翻译校验器 ID 重复：{validator_id}",
                    source,
                    context=context,
                )
            seen_validators[validator_id] = (plugin.plugin_id, source)
    return tuple(plugins)


def _load_external_descriptors() -> list[
    tuple[plugin_api.PluginDescriptor, Path]
]:
    manifests = (
        *_discover_manifests(_official_plugin_root(), required=True),
        *_discover_manifests(user_root() / "plugins", required=False),
    )
    seen_manifest_sources = {
        plugin.plugin_id: f"内置插件：{plugin.plugin_id}"
        for plugin in _builtin_plugins()
    }
    for manifest in manifests:
        previous_source = seen_manifest_sources.get(manifest.plugin_id)
        if previous_source is not None:
            raise _plugin_failure(
                manifest.path,
                "manifest",
                f"插件 ID 重复或占用内置 ID：{manifest.plugin_id} "
                f"（已有来源：{previous_source}；当前来源：{manifest.path}）",
            )
        seen_manifest_sources[manifest.plugin_id] = str(manifest.path)
    return [
        (_load_manifest_descriptor(manifest, index), manifest.path)
        for index, manifest in enumerate(manifests)
    ]


def _load_plugins_uncached() -> tuple[plugin_api.PluginDescriptor, ...]:
    module_names_before = set(sys.modules)
    try:
        builtins = list(_builtin_plugins())
        loaded_external = _load_external_descriptors()
        descriptors = [descriptor for descriptor, _ in loaded_external]
        sources = {
            id(plugin): f"内置插件：{plugin.plugin_id}" for plugin in builtins
        }
        sources.update(
            {id(descriptor): str(path) for descriptor, path in loaded_external}
        )
        return _validate_plugins([*builtins, *descriptors], sources)
    except Exception:
        for name in tuple(sys.modules):
            if (
                name.startswith(_PLUGIN_NAMESPACE_PREFIX)
                and name not in module_names_before
            ):
                sys.modules.pop(name, None)
        raise


def load_plugins() -> tuple[plugin_api.PluginDescriptor, ...]:
    global _PLUGIN_CACHE
    if _PLUGIN_CACHE is not None:
        return _PLUGIN_CACHE
    with _PLUGIN_LOAD_LOCK:
        if _PLUGIN_CACHE is None:
            _PLUGIN_CACHE = _load_plugins_uncached()
        return _PLUGIN_CACHE


def get_document_adapter(adapter_id: str) -> plugin_api.DocumentAdapter:
    for plugin in load_plugins():
        for adapter in plugin.document_adapters:
            if adapter.adapter_id == adapter_id:
                return adapter
    raise UsageError(f"未安装 Document Adapter：{adapter_id}")


def resolve_translation_validators(
    validator_ids: list[str] | tuple[str, ...] | None = None,
) -> tuple[tuple[plugin_api.TranslationValidator, dict[str, str]], ...]:
    requested = set(validator_ids) if validator_ids is not None else None
    values: list[tuple[plugin_api.TranslationValidator, dict[str, str]]] = []
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
        adapter,
        segment=segment,
        text=text,
        stage=stage,
        opaque_state=segment.get("_adapter_state"),
        run_options=segment.get("_adapter_run_options", {}),
    )


def document_adapter_segment_format_count(
    adapter: plugin_api.DocumentAdapter,
    *,
    segment: dict[str, Any],
    opaque_state: dict[str, Any] | None,
) -> int:
    value = adapter.segment_format_count(
        segment=segment,
        opaque_state=opaque_state,
    )
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProjectError(
            "Document Adapter segment_format_count 必须返回非负整数："
            f"{adapter.adapter_id}"
        )
    return value


def get_document_adapter_for_extension(
    extension: str,
) -> plugin_api.DocumentAdapter:
    normalized = extension.casefold()
    for plugin in load_plugins():
        for adapter in plugin.document_adapters:
            if normalized in adapter.extensions:
                return adapter
    raise UsageError(f"没有 Document Adapter 支持扩展名：{extension or '（无）'}")


def validate_document_import_options(
    adapter: plugin_api.DocumentAdapter,
    values: dict[str, str] | None,
    *,
    allow_replacement_choices: bool = False,
) -> dict[str, str]:
    provided = values or {}
    declarations = {
        option.option_id: option for option in adapter.import_options
    }
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
        if allow_replacement_choices:
            choices.update(choice_id for choice_id, _ in option.replacement_choices)
        if value not in choices:
            raise UsageError(
                f"{adapter.adapter_id}.{option_id} 取值无效：{value}"
            )
        resolved[option_id] = value
    return resolved


def validate_document_run_options(
    adapter: plugin_api.DocumentAdapter,
    values: dict[str, str] | None,
    *,
    use_defaults: bool = True,
) -> dict[str, str]:
    if values is not None and not isinstance(values, dict):
        raise UsageError(f"{adapter.adapter_id} 运行选项格式无效")
    provided = values or {}
    declarations = {option.option_id: option for option in adapter.run_options}
    unknown = sorted(set(provided) - set(declarations))
    if unknown:
        raise UsageError(f"{adapter.adapter_id} 包含未知运行选项：{', '.join(unknown)}")
    resolved: dict[str, str] = {}
    for option_id, option in declarations.items():
        if option_id not in provided:
            if not use_defaults:
                raise UsageError(
                    f"{adapter.adapter_id} run_options 缺少选项：{option_id}"
                )
            value = option.default
        else:
            value = provided[option_id]
        if not isinstance(value, str):
            raise UsageError(
                f"{adapter.adapter_id}.{option_id} 运行选项值格式无效"
            )
        if value not in {item[0] for item in option.choices}:
            raise UsageError(f"{adapter.adapter_id}.{option_id} 取值无效：{value}")
        resolved[option_id] = value
    return resolved


def split_document_adapter_options(
    adapter: plugin_api.DocumentAdapter,
    values: dict[str, str] | None,
    *,
    allow_replacement_choices: bool = False,
) -> tuple[dict[str, str], dict[str, str]]:
    """Validate the shared input shape and keep import/run ownership separate."""
    provided = values or {}
    declared = {option.option_id for option in (*adapter.import_options, *adapter.run_options)}
    unknown = sorted(set(provided) - declared)
    if unknown:
        raise UsageError(f"{adapter.adapter_id} 包含未知选项：{', '.join(unknown)}")
    imports = {key: value for key, value in provided.items() if key in {option.option_id for option in adapter.import_options}}
    runs = {key: value for key, value in provided.items() if key in {option.option_id for option in adapter.run_options}}
    return (
        validate_document_import_options(
            adapter,
            imports,
            allow_replacement_choices=allow_replacement_choices,
        ),
        validate_document_run_options(adapter, runs),
    )


def document_adapter_replacement_options(
    adapter: plugin_api.DocumentAdapter,
    *,
    opaque_state: dict[str, Any] | None,
    overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    """Resolve persisted Adapter choices and apply explicit replacements."""
    values = adapter.replacement_options(opaque_state=opaque_state)
    if not isinstance(values, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in values.items()
    ):
        raise ConfigError(
            f"Document Adapter 替换选项返回值无效：{adapter.adapter_id}"
        )
    declared = {option.option_id for option in adapter.import_options}
    if set(values) != declared:
        raise ConfigError(
            f"Document Adapter 替换选项不完整：{adapter.adapter_id}"
        )
    try:
        resolved = validate_document_import_options(
            adapter, values, allow_replacement_choices=True
        )
    except UsageError as exc:
        raise ConfigError(
            f"Document Adapter 替换选项取值无效：{adapter.adapter_id}"
        ) from exc
    if overrides:
        explicit = _validate_replacement_overrides(
            adapter, overrides, current=resolved
        )
        candidate = {
            **resolved,
            **{option_id: explicit[option_id] for option_id in overrides},
        }
        resolved = validate_document_import_options(
            adapter, candidate, allow_replacement_choices=True
        )
    return resolved


def _validate_replacement_overrides(
    adapter: plugin_api.DocumentAdapter,
    overrides: dict[str, str],
    *,
    current: dict[str, str],
) -> dict[str, str]:
    declarations = {
        option.option_id: option
        for option in adapter.import_options
    }
    unknown = sorted(set(overrides) - set(declarations))
    if unknown:
        raise UsageError(
            f"{adapter.adapter_id} 包含未知替换选项：{', '.join(unknown)}"
        )
    resolved: dict[str, str] = {}
    for option_id, value in overrides.items():
        option = declarations[option_id]
        current_choices = {choice_id for choice_id, _ in option.choices}
        legacy_choices = {
            choice_id for choice_id, _ in option.replacement_choices
        }
        if value in current_choices or (
            value in legacy_choices and current.get(option_id) == value
        ):
            resolved[option_id] = value
            continue
        raise UsageError(f"{adapter.adapter_id}.{option_id} 取值无效：{value}")
    return resolved


def document_adapter_summaries(
    *, replacement_values: dict[str, str] | None = None
) -> list[dict[str, object]]:
    values = []
    for plugin in load_plugins():
        for adapter in plugin.document_adapters:
            def summarize_option(
                option: plugin_api.DocumentChoiceOption,
            ) -> dict[str, object]:
                choices = list(option.choices)
                if (
                    replacement_values is not None
                    and replacement_values.get(option.option_id)
                    in {value for value, _ in option.replacement_choices}
                ):
                    choices.extend(option.replacement_choices)
                return {
                    "option_id": option.option_id,
                    "label": option.label,
                    "default": option.default,
                    "choices": [
                        {"value": value, "label": label}
                        for value, label in choices
                    ],
                }

            values.append(
                {
                    "adapter_id": adapter.adapter_id,
                    "version": adapter.version,
                    "plugin_id": plugin.plugin_id,
                    "plugin_version": plugin.version,
                    "capabilities": sorted(adapter.capabilities),
                    "extensions": sorted(adapter.extensions),
                    "import_options": [
                        summarize_option(option) for option in adapter.import_options
                    ],
                    "run_options": [
                        summarize_option(option)
                        for option in getattr(adapter, "run_options", ())
                    ],
                }
            )
    return sorted(values, key=lambda value: str(value["adapter_id"]))
