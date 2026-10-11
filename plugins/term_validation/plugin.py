from __future__ import annotations

import re
import unicodedata
from collections.abc import Awaitable
from dataclasses import asdict, replace

from app.plugin_api import (
    DecisionQuestion,
    DecisionPromptDeclaration,
    PluginDescriptor,
    TranslationValidationContext,
    TranslationValidationMatch,
)


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", normalized).strip()


class PreferredTermUsageValidator:
    validator_id = "preferred_term_usage"
    version = "8"
    label = "Preferred terminology usage"
    phase = "terminology"
    scope = "segment"

    @staticmethod
    def prompt_declaration() -> DecisionPromptDeclaration:
        from pathlib import Path
        from app.decision_prompt import declaration_from_file
        return declaration_from_file("preferred_term_usage", ("required", "acceptable", "uncertain"), Path(__file__).parent / "prompts/default.json")

    def validate(
        self, context: TranslationValidationContext
    ) -> tuple[TranslationValidationMatch, ...] | Awaitable[tuple[TranslationValidationMatch, ...]]:
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
        if context.decision is not None and findings:
            return self._review(context, findings)
        return tuple(findings)

    async def _review(self, context: TranslationValidationContext,
                      findings: list[TranslationValidationMatch]) -> tuple[TranslationValidationMatch, ...]:
        assert context.decision is not None
        prompt = (context.decision_prompts[self.validator_id] if self.validator_id in context.decision_prompts
                  else self.prompt_declaration().default)
        terms = {}
        questions = []
        for index, finding in enumerate(findings):
            name = f"term_{index}"
            term = next(term for term in context.terms if term.source == finding.term_source
                        and term.preferred_translation == finding.expected_translation)
            terms[name] = asdict(term)
            questions.append(DecisionQuestion(name,
                f"Evaluate terms.{name}: does the current translation need a terminology repair? "
                + prompt["instructions"], prompt["criteria"]))
        questioned = {(finding.term_source, finding.expected_translation) for finding in findings}
        answers = await context.decision.choose(
            {"source": context.source, "translation": context.translation, "terms": terms,
             "matched_terms": [asdict(term) for term in context.terms
                               if (term.source, term.preferred_translation) not in questioned]}, questions,
            segment_id=context.segment_id,
            reference_context=list(context.previous_source) if context.previous_source else None)
        result = []
        for question, finding in zip(questions, findings, strict=True):
            answer = answers[question.name]
            certain = not answer.refused and answer.confidence >= context.decision_confidence_threshold
            if certain and answer.choice == "acceptable":
                continue
            if certain and answer.choice == "required":
                result.append(finding)
            else:
                result.append(replace(finding, match_type="preferred_term_uncertain", repairable=False))
        return tuple(result)


def descriptor() -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id="term-validation",
        version="0.6.0",
        protocol_version=16,
        translation_validators=(PreferredTermUsageValidator(),),
        decision_prompts=(PreferredTermUsageValidator.prompt_declaration(),),
    )
