"""Public contract for trusted Document Adapter and validation plugins."""

from __future__ import annotations

from dataclasses import dataclass

from .documents import (
    DecodedPlaintext,
    DocumentAdapter,
    DocumentChoiceOption,
    DocumentImport,
    ImportedFile,
    decode_plaintext,
)
from .errors import IncompleteError, ProjectError, UsageError
from .translation_validation import (
    TranslationTermMatch,
    TranslationValidationContext,
    TranslationValidationMatch,
    TranslationValidator,
)

PLUGIN_PROTOCOL_VERSION = 12


@dataclass(frozen=True)
class PluginDescriptor:
    plugin_id: str
    version: str
    protocol_version: int
    document_adapters: tuple[DocumentAdapter, ...] = ()
    translation_validators: tuple[TranslationValidator, ...] = ()


__all__ = [
    "DecodedPlaintext",
    "DocumentAdapter",
    "DocumentChoiceOption",
    "DocumentImport",
    "ImportedFile",
    "IncompleteError",
    "PLUGIN_PROTOCOL_VERSION",
    "PluginDescriptor",
    "ProjectError",
    "TranslationTermMatch",
    "TranslationValidationContext",
    "TranslationValidationMatch",
    "TranslationValidator",
    "UsageError",
    "decode_plaintext",
]
