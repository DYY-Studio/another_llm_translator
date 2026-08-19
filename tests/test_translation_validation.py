from __future__ import annotations

from pathlib import Path

import pytest

from app.config import load_project_config
from app.errors import ConfigError, ProjectError
from app.plugins import (
    PLUGIN_PROTOCOL_VERSION,
    PluginDescriptor,
    load_plugins,
    resolve_translation_validators,
)
from app.project import init_project
from app.translation_validation import (
    SourceTextResidualValidator,
    TranslationValidationContext,
    TranslationValidationMatch,
    validate_translation_text,
)
from app.web_store import WebStore
from tests.test_foundation import make_app_root


class FakeEntryPoint:
    name = "validator-fixture"

    def __init__(self, descriptor: PluginDescriptor) -> None:
        self.descriptor = descriptor

    def load(self) -> object:
        return self.descriptor


def test_source_text_residual_reports_complete_and_long_partial_matches() -> None:
    validator = SourceTextResidualValidator()
    source = "这是一个需要完整翻译的原文句子，用来测试长片段残留。"

    complete = validator.validate(
        TranslationValidationContext(source, f"译文：{source}")
    )
    assert complete[0].match_type == "source_full"
    assert complete[0].text == source

    partial = validator.validate(
        TranslationValidationContext(source, "译文：这是一个需要完整翻译的原文句子")
    )
    assert partial[0].match_type == "source_span"
    assert partial[0].text == "这是一个需要完整翻译的原文句子"
    assert partial[0].start == 3
    assert partial[0].end == 3 + len(partial[0].text)


def test_source_text_residual_handles_whitespace_and_nfkc_without_numeric_noise() -> None:
    validator = SourceTextResidualValidator()
    source = "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴ"
    translation = "译文 ABCDEFGHIJKLMNO"
    matches = validator.validate(TranslationValidationContext(source, translation))
    assert matches[0].match_type == "source_span"
    assert matches[0].text == "ABCDEFGHIJKLMNO"

    source = "这是一个很长的原文句子需要保持完整语义和结构。"
    translation = "译文：这是 一个很长的原文句子需要保持完整语义"
    matches = validator.validate(TranslationValidationContext(source, translation))
    assert matches[0].match_type == "source_span"
    assert matches[0].text == "这是 一个很长的原文句子需要保持完整语义"

    assert (
        validator.validate(
            TranslationValidationContext(
                "12345678901234567890", "译文 12345678901234567890"
            )
        )
        == ()
    )
    assert (
        validator.validate(TranslationValidationContext("？！；，。", "译文？！；，。"))
        == ()
    )


def test_source_text_residual_respects_both_partial_match_thresholds() -> None:
    validator = SourceTextResidualValidator()
    source = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    assert validator.validate(TranslationValidationContext(source, "译文 abcdefghijk")) == ()
    assert validator.validate(TranslationValidationContext(source, "译文 abcdefghij")) == ()
    assert validator.validate(
        TranslationValidationContext(
            source, "译文 abcdefghijklmnopqrstuvwxyzABCDEF"
        )
    )


def test_translation_validation_rejects_invalid_plugin_match() -> None:
    class InvalidValidator:
        validator_id = "invalid"
        version = "1"
        label = "Invalid"

        def validate(self, context: TranslationValidationContext) -> list[object]:
            del context
            return [
                TranslationValidationMatch(
                    match_type="invalid",
                    text="missing",
                    start=0,
                    end=7,
                )
            ]

    with pytest.raises(ProjectError, match="越界或不一致"):
        validate_translation_text(
            TranslationValidationContext("source", "translation"),
            (InvalidValidator(),),
        )


def test_plugin_host_rejects_duplicate_translation_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Validator:
        validator_id = "duplicate-validator"
        version = "1"
        label = "Duplicate"

        def validate(self, context: TranslationValidationContext) -> tuple[()]:
            del context
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


def test_plugin_host_rejects_old_validator_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = PluginDescriptor(
        plugin_id="old-validator-plugin",
        version="1",
        protocol_version=8,
        translation_validators=(),
    )
    monkeypatch.setattr(
        "app.plugins.entry_points",
        lambda **_: [FakeEntryPoint(descriptor)],
    )
    with pytest.raises(ConfigError, match="协议版本不兼容"):
        load_plugins()


def test_builtin_translation_validator_summaries_are_complete() -> None:
    bindings = resolve_translation_validators()
    summaries = [summary for _, summary in bindings]
    assert [item["validator_id"] for item in summaries] == [
        "japanese_kana",
        "korean_hangul",
        "preferred_term_usage",
        "source_text_residual",
    ]
    assert all(
        item["plugin_id"] == "builtin-translation-validation"
        for item in summaries[:2]
    )
    assert summaries[2]["plugin_id"] == "term-validation"


def test_external_translation_validator_is_discoverable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExternalValidator:
        validator_id = "external_example"
        version = "2"
        label = "External example"

        def validate(self, context: TranslationValidationContext) -> tuple[()]:
            del context
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
        for _, item in resolve_translation_validators()
        if item["validator_id"] == "external_example"
    )
    assert summary == {
        "validator_id": "external_example",
        "version": "2",
        "label": "External example",
        "plugin_id": "external-validator-plugin",
        "plugin_version": "3",
    }


def test_translation_validator_resolution_loads_entry_point_once_and_reuses_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExternalValidator:
        validator_id = "external_example"
        version = "2"
        label = "External example"

        def validate(self, context: TranslationValidationContext) -> tuple[()]:
            del context
            return ()

    validator = ExternalValidator()
    descriptor = PluginDescriptor(
        plugin_id="external-validator-plugin",
        version="3",
        protocol_version=PLUGIN_PROTOCOL_VERSION,
        translation_validators=(validator,),
    )
    load_count = 0

    class CountingEntryPoint:
        name = "validator-fixture"

        def load(self) -> object:
            nonlocal load_count
            load_count += 1
            return descriptor

    monkeypatch.setattr(
        "app.plugins.entry_points",
        lambda **_: [CountingEntryPoint()],
    )

    bindings = resolve_translation_validators(["external_example"])

    assert load_count == 1
    assert len(bindings) == 1
    instance, summary = bindings[0]
    assert instance is validator
    assert summary["validator_id"] == "external_example"
    assert summary["plugin_version"] == "3"


def test_project_config_and_web_store_reuse_one_validator_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_root = make_app_root(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text("one", encoding="utf-8")
    project, _ = init_project(
        [str(source)],
        name="validator-project",
        app_root=app_root,
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "validators = []", 'validators = ["external_example"]'
        ),
        encoding="utf-8",
    )

    class ExternalValidator:
        validator_id = "external_example"
        version = "2"
        label = "External example"

        def validate(self, context: TranslationValidationContext) -> tuple[()]:
            del context
            return ()

    validator = ExternalValidator()
    descriptor = PluginDescriptor(
        plugin_id="external-validator-plugin",
        version="3",
        protocol_version=PLUGIN_PROTOCOL_VERSION,
        translation_validators=(validator,),
    )
    load_count = 0

    class CountingEntryPoint:
        name = "validator-fixture"

        def load(self) -> object:
            nonlocal load_count
            load_count += 1
            return descriptor

    monkeypatch.setattr(
        "app.plugins.entry_points",
        lambda **_: [CountingEntryPoint()],
    )

    config = load_project_config(project, presets_root=app_root)

    assert load_count == 1
    assert config["_translation_validators"] == [
        {
            "validator_id": "external_example",
            "version": "2",
            "label": "External example",
            "plugin_id": "external-validator-plugin",
            "plugin_version": "3",
        }
    ]
    assert config["_translation_validator_instances"] == (validator,)
    assert config["_translation_validator_instances"][0] is validator

    load_count = 0
    store = WebStore(project)
    assert load_count == 1
    assert store.config["_translation_validator_instances"][0] is validator
