from __future__ import annotations

import difflib
import inspect
from collections.abc import Awaitable
from typing import TYPE_CHECKING
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Protocol

from .documents import strip_aozora_ruby
from .errors import ExternalError, ProjectError

if TYPE_CHECKING:
    from .decision import DecisionService
    from .decision_prompt import DecisionPromptDeclaration


@dataclass(frozen=True)
class TranslationTermMatch:
    """A terminology match for the Segment being validated."""

    source: str
    matched_text: str
    match_type: str
    preferred_translation: str | None
    category: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class TranslationValidationContext:
    """The complete, private-to-the-host input for one validator call."""

    source: str
    translation: str
    terms: tuple[TranslationTermMatch, ...] = ()
    decision: DecisionService | None = None
    decision_confidence_threshold: float = 0.8
    segment_id: str | None = None
    previous_source: tuple[str, ...] = ()
    decision_prompts: dict[str, dict] = field(default_factory=dict)


@dataclass(frozen=True)
class TranslationValidationMatch:
    """A single finding reported by a validator.

    Error findings point at a span in ``context.translation`` (0:0 when empty). Advisory
    findings may omit that span, for example when a recommended terminology
    translation is absent rather than incorrectly present.
    """

    match_type: str
    text: str | None
    start: int | None
    end: int | None
    severity: str = "error"
    term_source: str | None = None
    matched_source: str | None = None
    expected_translation: str | None = None
    repairable: bool = True


class TranslationValidator(Protocol):
    validator_id: str
    version: str
    label: str
    phase: str
    scope: str

    def validate(
        self, context: TranslationValidationContext
    ) -> (
        list[TranslationValidationMatch]
        | tuple[TranslationValidationMatch, ...]
        | Awaitable[list[TranslationValidationMatch] | tuple[TranslationValidationMatch, ...]]
    ): ...

    def validate_response(
        self, contexts: tuple[TranslationValidationContext, ...],
    ) -> dict[str, tuple[TranslationValidationMatch, ...]] | Awaitable[dict[str, tuple[TranslationValidationMatch, ...]]]: ...


JAPANESE_RE = re.compile(
    "[\u3040-\u30ff\u31f0-\u31ff\uff66-\uff9f"
    "\U0001b000-\U0001b0ff\U0001b100-\U0001b12f"
    "\U0001b130-\U0001b16f\U0001aff0-\U0001afff]"
)
KOREAN_RE = re.compile(
    "[\u1100-\u11ff\u3130-\u318f\ua960-\ua97f\uac00-\ud7ff]"
)


class JapaneseKanaValidator:
    validator_id = "japanese_kana"
    version = "1"
    label = "Japanese Kana residual"
    phase = "mechanical"
    scope = "segment"

    def validate(
        self, context: TranslationValidationContext
    ) -> tuple[TranslationValidationMatch, ...]:
        return tuple(
            TranslationValidationMatch(
                match_type="character",
                text=match.group(),
                start=match.start(),
                end=match.end(),
            )
            for match in JAPANESE_RE.finditer(context.translation)
        )


class KoreanHangulValidator:
    validator_id = "korean_hangul"
    version = "1"
    label = "Korean Hangul residual"
    phase = "mechanical"
    scope = "segment"

    def validate(
        self, context: TranslationValidationContext
    ) -> tuple[TranslationValidationMatch, ...]:
        return tuple(
            TranslationValidationMatch(
                match_type="character",
                text=match.group(),
                start=match.start(),
                end=match.end(),
            )
            for match in KOREAN_RE.finditer(context.translation)
        )


def _normalized_projection(value: str) -> tuple[str, tuple[int, ...]]:
    projected: list[str] = []
    positions: list[int] = []
    for index, character in enumerate(value):
        normalized = unicodedata.normalize("NFKC", character)
        for item in normalized:
            if item.isspace():
                continue
            projected.append(item)
            positions.append(index)
    return "".join(projected), tuple(positions)


class SourceTextResidualValidator:
    validator_id = "source_text_residual"
    version = "1"
    label = "Source text residual"
    phase = "mechanical"
    scope = "segment"

    def validate(
        self, context: TranslationValidationContext
    ) -> tuple[TranslationValidationMatch, ...]:
        source = context.source
        translation = context.translation
        source_text = source.strip()
        if not source_text:
            return ()
        if not any(character.isalpha() for character in source_text):
            return ()
        exact_start = translation.find(source_text)
        if exact_start >= 0:
            return (
                TranslationValidationMatch(
                    match_type="source_full",
                    text=source_text,
                    start=exact_start,
                    end=exact_start + len(source_text),
                ),
            )

        source_projected, _ = _normalized_projection(source_text)
        translation_projected, translation_positions = _normalized_projection(
            translation
        )
        if not source_projected or not translation_projected:
            return ()
        match = difflib.SequenceMatcher(
            None,
            source_projected,
            translation_projected,
            autojunk=False,
        ).find_longest_match(
            0,
            len(source_projected),
            0,
            len(translation_projected),
        )
        if match.size < 12 or match.size * 10 < len(source_projected) * 3:
            return ()
        matched_text = translation_projected[match.b : match.b + match.size]
        if not any(character.isalpha() for character in matched_text):
            return ()
        start = translation_positions[match.b]
        end = translation_positions[match.b + match.size - 1] + 1
        return (
            TranslationValidationMatch(
                match_type="source_span",
                text=translation[start:end],
                start=start,
                end=end,
            ),
        )


VALIDATION_PHASES = ("mechanical", "alignment", "terminology")


def _serialize_matches(context: TranslationValidationContext, validator_id: str,
                       matches: object) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for match in matches:
        if not isinstance(match, TranslationValidationMatch):
            raise ProjectError(
                f"翻译校验器返回了无效匹配：{validator_id}"
            )
        if (
            not isinstance(match.match_type, str)
            or not match.match_type.strip()
        ):
            raise ProjectError(
                f"翻译校验器返回了无效匹配：{validator_id}"
            )
        if match.severity not in {"error", "advisory"}:
            raise ProjectError(
                f"翻译校验器返回了无效严重性：{validator_id}"
            )
        if type(match.repairable) is not bool or (match.severity == "error" and not match.repairable):
            raise ProjectError(f"翻译校验器返回了无效修复标记：{validator_id}")
        has_span = (
            match.text is not None
            or match.start is not None
            or match.end is not None
        )
        if has_span:
            if (
                not isinstance(match.text, str)
                or type(match.start) is not int
                or type(match.end) is not int
                or not 0 <= match.start <= match.end <= len(context.translation)
                or (match.start == match.end and context.translation != "")
                or context.translation[match.start : match.end] != match.text
            ):
                raise ProjectError(
                    f"翻译校验器返回了越界或不一致匹配：{validator_id}"
                )
        elif match.severity == "error":
            raise ProjectError(
                f"硬校验必须返回译文位置：{validator_id}"
            )
        for field_name in (
            "term_source",
            "matched_source",
            "expected_translation",
        ):
            value = getattr(match, field_name)
            if value is not None and (
                not isinstance(value, str) or not value
            ):
                raise ProjectError(
                    f"翻译校验器返回了无效术语字段：{validator_id}"
                )
        finding: dict[str, object] = {
            "validator": validator_id,
            "match_type": match.match_type,
            "severity": match.severity,
            "start": match.start,
            "end": match.end,
        }
        if not match.repairable:
            finding["repairable"] = False
        if match.text is not None:
            finding["matched_text"] = match.text
        if match.term_source is not None:
            finding["term_source"] = match.term_source
        if match.matched_source is not None:
            finding["matched_source"] = match.matched_source
        if match.expected_translation is not None:
            finding["expected_translation"] = match.expected_translation
        if match.text is not None and len(match.text) == 1:
            finding["character"] = match.text
            finding["code_point"] = f"U+{ord(match.text):04X}"
        findings.append(finding)
    return findings


async def validate_translation_response(
    contexts: tuple[TranslationValidationContext, ...],
    validators: tuple[TranslationValidator, ...],
) -> dict[str, list[dict[str, object]]]:
    by_id = {context.segment_id: context for context in contexts}
    findings: dict[str, list[dict[str, object]]] = {}
    for phase in VALIDATION_PHASES:
        for validator in sorted(validators, key=lambda item: item.validator_id):
            if validator.phase != phase:
                continue
            validator_id = validator.validator_id
            try:
                if validator.scope == "response":
                    result = validator.validate_response(contexts)
                    if inspect.isawaitable(result):
                        result = await result
                    if not isinstance(result, dict) or any(key not in by_id for key in result):
                        raise ProjectError(f"翻译校验器返回了无效 Segment ID：{validator_id}")
                else:
                    result = {}
                    for context in contexts:
                        matches = validator.validate(context)
                        if inspect.isawaitable(matches):
                            matches = await matches
                        result[context.segment_id] = matches
                for segment_id, matches in result.items():
                    serialized = _serialize_matches(by_id[segment_id], validator_id, matches)
                    if serialized:
                        findings.setdefault(segment_id, []).extend(serialized)
            except ExternalError:
                raise
            except ProjectError:
                raise
            except Exception as exc:
                raise ProjectError(f"翻译校验器执行失败：{validator_id}") from exc
        if findings:
            break
    return findings


async def validate_translation_text(
    context: TranslationValidationContext,
    validators: tuple[TranslationValidator, ...],
) -> list[dict[str, object]]:
    # Response validators require the complete candidate group, not a manual edit.
    findings = await validate_translation_response(
        (context,), tuple(validator for validator in validators if validator.scope == "segment"),
    )
    return findings.get(context.segment_id, [])


class SegmentAlignmentValidator:
    validator_id = "segment_alignment"
    version = "3"
    label = "Segment alignment"
    phase = "alignment"
    scope = "response"

    @staticmethod
    def prompt_declaration() -> DecisionPromptDeclaration:
        from pathlib import Path
        from .decision_prompt import declaration_from_files
        return declaration_from_files("segment_alignment", ("aligned", "misaligned", "uncertain"), Path(__file__).parent / "decision_prompts")

    def __init__(self, decision: DecisionService | None = None, *,
                 confidence_threshold: float = 0.8, tail_segments: int = 3, prompt: dict | None = None) -> None:
        self.decision = decision
        self.confidence_threshold = confidence_threshold
        self.tail_segments = tail_segments
        self.prompt = prompt if prompt is not None else self.prompt_declaration().defaults["en"]

    async def validate_response(
        self, contexts: tuple[TranslationValidationContext, ...],
    ) -> dict[str, tuple[TranslationValidationMatch, ...]]:
        from .decision import DecisionQuestion
        if self.decision is None:
            raise ProjectError("错位校验缺少 Decision 服务")
        sample = contexts[-self.tail_segments:]
        questions = [DecisionQuestion(f"segment_{index}",
            f"Judge whether segments[{index}] translation corresponds to its own source. "
            + self.prompt["instructions"], self.prompt["criteria"])
            for index, _ in enumerate(sample)]
        answers = await self.decision.choose(
            {"segments": [{"id": context.segment_id, "source": strip_aozora_ruby(context.source),
                           "translation": strip_aozora_ruby(context.translation)} for context in sample]},
            questions, segment_id=sample[-1].segment_id,
        )
        misaligned = False
        uncertain = False
        for question in questions:
            answer = answers[question.name]
            certain = not answer.refused and answer.confidence >= self.confidence_threshold
            misaligned |= certain and answer.choice == "misaligned"
            uncertain |= not certain or answer.choice == "uncertain"
        if not misaligned and not uncertain:
            return {}
        evidence = ", ".join(str(context.segment_id) for context in sample)
        return {context.segment_id: (TranslationValidationMatch(
            "segment_misaligned" if misaligned else "segment_alignment_uncertain",
            context.translation if misaligned else None, 0 if misaligned else None,
            len(context.translation) if misaligned else None,
            severity="error" if misaligned else "advisory", repairable=misaligned,
            matched_source=evidence,
        ),) for context in contexts}
