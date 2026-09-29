from __future__ import annotations

import app.plugins as plugins
from app import documents, errors, plugin_api, translation_validation


def test_plugin_api_exposes_shared_contract_without_host_descriptor_alias() -> None:
    assert plugin_api.PLUGIN_PROTOCOL_VERSION == 12
    assert plugin_api.DocumentAdapter is documents.DocumentAdapter
    assert plugin_api.DocumentChoiceOption is documents.DocumentChoiceOption
    assert plugin_api.DocumentImport is documents.DocumentImport
    assert plugin_api.ImportedFile is documents.ImportedFile
    assert plugin_api.DecodedPlaintext is documents.DecodedPlaintext
    assert plugin_api.decode_plaintext is documents.decode_plaintext
    assert plugin_api.TranslationValidator is translation_validation.TranslationValidator
    assert plugin_api.TranslationTermMatch is translation_validation.TranslationTermMatch
    assert (
        plugin_api.TranslationValidationContext
        is translation_validation.TranslationValidationContext
    )
    assert (
        plugin_api.TranslationValidationMatch
        is translation_validation.TranslationValidationMatch
    )
    assert plugin_api.IncompleteError is errors.IncompleteError
    assert plugin_api.ProjectError is errors.ProjectError
    assert plugin_api.UsageError is errors.UsageError
    assert not hasattr(plugins, "PluginDescriptor")
