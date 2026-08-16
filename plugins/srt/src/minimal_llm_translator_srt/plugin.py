from __future__ import annotations

from app.plugins import PLUGIN_PROTOCOL_VERSION, PluginDescriptor

from .adapter import SRTDocumentAdapter


def descriptor() -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id="srt-documents",
        version="0.1.0",
        protocol_version=PLUGIN_PROTOCOL_VERSION,
        document_adapters=(SRTDocumentAdapter(),),
    )
