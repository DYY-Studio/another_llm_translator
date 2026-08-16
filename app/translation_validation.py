from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass
from typing import Protocol

from .errors import ProjectError


@dataclass(frozen=True)
class TranslationValidationMatch:
    """A single source/translation match reported by a validator."""

    match_type: str
    text: str
    start: int
    end: int


class TranslationValidator(Protocol):
    validator_id: str
    version: str
    label: str

    def validate(
        self, source: str, translation: str
    ) -> list[TranslationValidationMatch] | tuple[TranslationValidationMatch, ...]: ...


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
        self, source: str, translation: str
    ) -> tuple[TranslationValidationMatch, ...]:
        del source
        return tuple(
            TranslationValidationMatch(
                match_type="character",
                text=match.group(),
                start=match.start(),
                end=match.end(),
            )
            for match in JAPANESE_RE.finditer(translation)
        )


class KoreanHangulValidator:
    validator_id = "korean_hangul"
    version = "1"
    label = "Korean Hangul residual"

    def validate(
        self, source: str, translation: str
    ) -> tuple[TranslationValidationMatch, ...]:
        del source
        return tuple(
            TranslationValidationMatch(
                match_type="character",
                text=match.group(),
                start=match.start(),
                end=match.end(),
            )
            for match in KOREAN_RE.finditer(translation)
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
        self, source: str, translation: str
    ) -> tuple[TranslationValidationMatch, ...]:
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
    source: str,
    translation: str,
    validators: tuple[TranslationValidator, ...],
) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for validator in validators:
        validator_id = str(validator.validator_id)
        try:
            matches = validator.validate(source, translation)
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
                or not isinstance(match.text, str)
                or not match.text
                or type(match.start) is not int
                or type(match.end) is not int
                or not 0 <= match.start < match.end <= len(translation)
                or translation[match.start : match.end] != match.text
            ):
                raise ProjectError(
                    f"翻译校验器返回了越界或不一致匹配：{validator_id}"
                )
            finding: dict[str, object] = {
                "validator": validator_id,
                "match_type": match.match_type,
                "matched_text": match.text,
                "start": match.start,
                "end": match.end,
            }
            if len(match.text) == 1:
                finding["character"] = match.text
                finding["code_point"] = f"U+{ord(match.text):04X}"
            findings.append(finding)
    return findings
