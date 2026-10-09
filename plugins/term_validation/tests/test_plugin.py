from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from app.plugin_api import (
    DecisionAnswer,
    TranslationTermMatch,
    TranslationValidationContext,
)
from plugins.term_validation.plugin import (
    PreferredTermUsageValidator,
    descriptor,
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
    assert value.protocol_version == 13


@pytest.mark.parametrize("choice,confidence,count,repairable", [
    ("required", 0.9, 1, True),
    ("ordinary", 0.9, 0, False),
    ("acceptable", 0.9, 0, False),
    ("acceptable", 0.5, 1, False),
    ("required", 0.5, 1, False),
    ("uncertain", 0.9, 1, False),
])
def test_decision_reviews_only_missing_terms(choice, confidence, count, repairable):
    class Decision:
        async def choose(self, state, questions, **kwargs):
            assert state["source"] == "Alice"
            assert state["translation"] == "其他"
            assert len(questions) == 1
            assert state["terms"][questions[0].name]["matched_text"] == "Ally"
            return {questions[0].name: DecisionAnswer(choice, {}, confidence)}
    validator = PreferredTermUsageValidator()
    findings = asyncio.run(validator.validate(replace(_context("其他"), decision=Decision())))
    assert len(findings) == count
    if count:
        assert findings[0].repairable is repairable
    assert validator.validate(replace(_context("爱丽丝"), decision=Decision())) == ()
