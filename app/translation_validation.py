from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass
from typing import Protocol

from .errors import ProjectError


@dataclass(frozen=True)
class TranslationTermMatch:
    """A terminology match for the Segment being validated."""

    source: str
    matched_text: str
    match_type: str
    preferred_translation: str | None


@dataclass(frozen=True)
class TranslationValidationContext:
    """The complete, private-to-the-host input for one validator call."""

    source: str
    translation: str
    terms: tuple[TranslationTermMatch, ...] = ()


@dataclass(frozen=True)
class TranslationValidationMatch:
    """A single finding reported by a validator.

    Error findings point at a span in ``context.translation``. Advisory
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


class TranslationValidator(Protocol):
    validator_id: str
    version: str
    label: str

    def validate(
        self, context: TranslationValidationContext
    ) -> (
        list[TranslationValidationMatch]
        | tuple[TranslationValidationMatch, ...]
    ): ...


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


def validate_translation_text(
    context: TranslationValidationContext,
    validators: tuple[TranslationValidator, ...],
) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for validator in validators:
        validator_id = str(validator.validator_id)
        try:
            matches = validator.validate(context)
            matches = list(matches)
        except Exception as exc:
            raise ProjectError(f"翻译校验器执行失败：{validator_id}") from exc
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
            has_span = (
                match.text is not None
                or match.start is not None
                or match.end is not None
            )
            if has_span:
                if (
                    not isinstance(match.text, str)
                    or not match.text
                    or type(match.start) is not int
                    or type(match.end) is not int
                    or not 0 <= match.start < match.end <= len(context.translation)
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
