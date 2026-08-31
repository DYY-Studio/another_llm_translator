from __future__ import annotations

from another_llm_translator_term_validation.plugin import (
    PreferredTermUsageValidator,
    descriptor,
)

from app.translation_validation import (
    TranslationTermMatch,
    TranslationValidationContext,
)


def _context(translation: str) -> TranslationValidationContext:
    return TranslationValidationContext(
        source="Alice",
        translation=translation,
        terms=(
            TranslationTermMatch(
                source="Alice",
                matched_text="Ally",
                match_type="alias",
                preferred_translation="爱丽丝",
            ),
            TranslationTermMatch(
                source="Bob",
                matched_text="Bob",
                match_type="source",
                preferred_translation=None,
            ),
        ),
    )


def test_preferred_term_usage_is_advisory_and_normalizes_text() -> None:
    validator = PreferredTermUsageValidator()
    assert validator.validate(_context("译文：爱丽丝")) == ()

    findings = validator.validate(_context("译文：其他"))
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "advisory"
    assert finding.term_source == "Alice"
    assert finding.matched_source == "Ally"
    assert finding.expected_translation == "爱丽丝"
    assert finding.text is None
    assert finding.start is None
    assert finding.end is None

    casefolded = TranslationValidationContext(
        source="Alice",
        translation="alice",
        terms=(
            TranslationTermMatch(
                source="Alice",
                matched_text="Alice",
                match_type="source",
                preferred_translation="ＡＬＩＣＥ",
            ),
        ),
    )
    assert validator.validate(casefolded) == ()


def test_descriptor_uses_fixed_protocol_version() -> None:
    value = descriptor()
    assert value.plugin_id == "term-validation"
    assert value.protocol_version == 11
