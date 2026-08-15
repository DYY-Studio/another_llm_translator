from __future__ import annotations

import pytest

from app.errors import ConfigError, ProjectError
from app.plugins import (
    PLUGIN_PROTOCOL_VERSION,
    PluginDescriptor,
    load_plugins,
    translation_validator_summaries,
)
from app.translation_validation import (
    SourceTextResidualValidator,
    TranslationValidationMatch,
    validate_translation_text,
)


class FakeEntryPoint:
    name = "validator-fixture"

    def __init__(self, descriptor: PluginDescriptor) -> None:
        self.descriptor = descriptor

    def load(self) -> object:
        return self.descriptor


def test_source_text_residual_reports_complete_and_long_partial_matches() -> None:
    validator = SourceTextResidualValidator()
    source = "这是一个需要完整翻译的原文句子，用来测试长片段残留。"

    complete = validator.validate(source, f"译文：{source}")
    assert complete[0].match_type == "source_full"
    assert complete[0].text == source

    partial = validator.validate(source, "译文：这是一个需要完整翻译的原文句子")
    assert partial[0].match_type == "source_span"
    assert partial[0].text == "这是一个需要完整翻译的原文句子"
    assert partial[0].start == 3
    assert partial[0].end == 3 + len(partial[0].text)


def test_source_text_residual_handles_whitespace_and_nfkc_without_numeric_noise() -> None:
    validator = SourceTextResidualValidator()
    source = "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴ"
    translation = "译文 ABCDEFGHIJKLMNO"
    matches = validator.validate(source, translation)
    assert matches[0].match_type == "source_span"
    assert matches[0].text == "ABCDEFGHIJKLMNO"

    source = "这是一个很长的原文句子需要保持完整语义和结构。"
    translation = "译文：这是 一个很长的原文句子需要保持完整语义"
    matches = validator.validate(source, translation)
    assert matches[0].match_type == "source_span"
    assert matches[0].text == "这是 一个很长的原文句子需要保持完整语义"

    assert validator.validate("12345678901234567890", "译文 12345678901234567890") == ()
    assert validator.validate("？！；，。", "译文？！；，。") == ()


def test_source_text_residual_respects_both_partial_match_thresholds() -> None:
    validator = SourceTextResidualValidator()
    source = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    assert validator.validate(source, "译文 abcdefghijk") == ()
    assert validator.validate(source, "译文 abcdefghij") == ()
    assert validator.validate(source, "译文 abcdefghijklmnopqrstuvwxyzABCDEF")


def test_translation_validation_rejects_invalid_plugin_match() -> None:
    class InvalidValidator:
        validator_id = "invalid"
        version = "1"
        label = "Invalid"

        def validate(self, source: str, translation: str) -> list[object]:
            del source, translation
            return [
                TranslationValidationMatch(
                    match_type="invalid",
                    text="missing",
                    start=0,
                    end=7,
                )
            ]

    with pytest.raises(ProjectError, match="越界或不一致"):
        validate_translation_text("source", "translation", (InvalidValidator(),))


def test_plugin_host_rejects_duplicate_translation_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Validator:
        validator_id = "duplicate-validator"
        version = "1"
        label = "Duplicate"

        def validate(self, source: str, translation: str) -> tuple[()]:
            del source, translation
            return ()

    descriptor = PluginDescriptor(
        plugin_id="duplicate-validator-plugin",
        version="1",
        protocol_version=PLUGIN_PROTOCOL_VERSION,
        translation_validators=(Validator(),),
    )
    duplicate = PluginDescriptor(
        plugin_id="duplicate-validator-plugin-2",
        version="1",
        protocol_version=PLUGIN_PROTOCOL_VERSION,
        translation_validators=(Validator(),),
    )
    monkeypatch.setattr(
        "app.plugins.entry_points",
        lambda **_: [FakeEntryPoint(descriptor), FakeEntryPoint(duplicate)],
    )
    with pytest.raises(ConfigError, match="翻译校验器描述不完整"):
        load_plugins()


def test_builtin_translation_validator_summaries_are_complete() -> None:
    summaries = translation_validator_summaries()
    assert [item["validator_id"] for item in summaries] == [
        "japanese_kana",
        "korean_hangul",
        "source_text_residual",
    ]
    assert all(item["plugin_id"] == "builtin-translation-validation" for item in summaries)


def test_external_translation_validator_is_discoverable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExternalValidator:
        validator_id = "external_example"
        version = "2"
        label = "External example"

        def validate(self, source: str, translation: str) -> tuple[()]:
            del source, translation
            return ()

    descriptor = PluginDescriptor(
        plugin_id="external-validator-plugin",
        version="3",
        protocol_version=PLUGIN_PROTOCOL_VERSION,
        translation_validators=(ExternalValidator(),),
    )
    monkeypatch.setattr(
        "app.plugins.entry_points",
        lambda **_: [FakeEntryPoint(descriptor)],
    )

    summary = next(
        item
        for item in translation_validator_summaries()
        if item["validator_id"] == "external_example"
    )
    assert summary == {
        "validator_id": "external_example",
        "version": "2",
        "label": "External example",
        "plugin_id": "external-validator-plugin",
        "plugin_version": "3",
    }
