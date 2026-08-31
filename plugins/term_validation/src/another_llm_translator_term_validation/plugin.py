from __future__ import annotations

import re
import unicodedata

from app.plugins import PluginDescriptor
from app.translation_validation import (
    TranslationValidationContext,
    TranslationValidationMatch,
)


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", normalized).strip()


class PreferredTermUsageValidator:
    validator_id = "preferred_term_usage"
    version = "1"
    label = "Preferred terminology usage"

    def validate(
        self, context: TranslationValidationContext
    ) -> tuple[TranslationValidationMatch, ...]:
        translation = _normalize(context.translation)
        findings: list[TranslationValidationMatch] = []
        seen: set[tuple[str, str]] = set()
        for term in context.terms:
            preferred = term.preferred_translation
            if not isinstance(preferred, str) or not preferred.strip():
                continue
            key = (term.source, preferred)
            if key in seen or _normalize(preferred) in translation:
                continue
            seen.add(key)
            findings.append(
                TranslationValidationMatch(
                    match_type="preferred_term_missing",
                    text=None,
                    start=None,
                    end=None,
                    severity="advisory",
                    term_source=term.source,
                    matched_source=term.matched_text,
                    expected_translation=preferred,
                )
            )
        return tuple(findings)


def descriptor() -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id="term-validation",
        version="0.1.0",
        protocol_version=11,
        translation_validators=(PreferredTermUsageValidator(),),
    )
