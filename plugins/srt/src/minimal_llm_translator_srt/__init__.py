"""SRT Document Adapter plugin for minimal-llm-translator."""

from .adapter import SRTDocumentAdapter
from .plugin import descriptor

__all__ = ["SRTDocumentAdapter", "descriptor"]
