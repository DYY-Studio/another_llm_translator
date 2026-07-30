from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import entry_points

from .documents import DocumentAdapter
from .errors import ConfigError, UsageError


PLUGIN_PROTOCOL_VERSION = 1
PLUGIN_ENTRY_POINT = "minimal_llm_translator.plugins"


@dataclass(frozen=True)
class PluginDescriptor:
    plugin_id: str
    version: str
    protocol_version: int
    document_adapters: tuple[DocumentAdapter, ...] = ()


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
            seen_adapters.add(adapter.adapter_id)
    return tuple(plugins)


def get_document_adapter(adapter_id: str) -> DocumentAdapter:
    for plugin in load_plugins():
        for adapter in plugin.document_adapters:
            if adapter.adapter_id == adapter_id:
                return adapter
    raise UsageError(f"未安装 Document Adapter：{adapter_id}")


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
                }
            )
    return sorted(values, key=lambda value: str(value["adapter_id"]))
