from __future__ import annotations

from app.plugins import PluginDescriptor

from .adapter import SRTDocumentAdapter


def descriptor() -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id="srt-documents",
        version="0.1.0",
        protocol_version=10,
        document_adapters=(SRTDocumentAdapter(),),
    )
