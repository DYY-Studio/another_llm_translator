from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from app.documents import (
    DocumentChoiceOption,
    DocumentImport,
    ImportedFile,
    decode_plaintext,
)
from app.errors import IncompleteError, ProjectError, UsageError

_SRT_EXTENSIONS = frozenset({".srt"})
_SEQUENCE_RE = re.compile(r"^[0-9]+$")
_TIMING_RE = re.compile(
    r"^(?P<start>[0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3})"
    r"(?P<before>[ \t]*)-->"
    r"(?P<after>[ \t]*)(?P<end>[0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3})$"
)


@dataclass(frozen=True)
class _InputFile:
    path: Path
    original_name: str


def _natural_path_key(value: str) -> tuple[tuple[tuple[int, int | str], ...], str]:
    parts = re.split(r"([0-9]+)", value.casefold())
    natural = tuple(
        (0, int(part)) if part.isascii() and part.isdigit() else (1, part)
        for part in parts
    )
    return natural, value


def _validated_relative_name(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or path.name in {"", ".", ".."}
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise UsageError(f"SRT 输入相对路径无效：{value}")
    return path.as_posix()


def _discover_inputs(values: list[str], recursive: bool) -> list[_InputFile]:
    discovered: list[_InputFile] = []
    for raw_value in values:
        path = Path(raw_value)
        if path.is_symlink():
            raise UsageError(f"SRT 不接受显式符号链接输入：{path}")
        if path.is_file():
            if path.suffix.casefold() not in _SRT_EXTENSIONS:
                raise UsageError(f"SRT 输入扩展名无效：{path}")
            discovered.append(_InputFile(path=path, original_name=path.name))
            continue
        if not path.is_dir():
            raise UsageError(f"SRT 输入不存在或不是文件/目录：{path}")

        candidates: list[Path] = []
        if recursive:
            for current, directories, files in os.walk(path, followlinks=False):
                current_path = Path(current)
                directories[:] = sorted(
                    [
                        name
                        for name in directories
                        if not (current_path / name).is_symlink()
                    ],
                    key=_natural_path_key,
                )
                candidates.extend(
                    current_path / name
                    for name in files
                    if not (current_path / name).is_symlink()
                    and (current_path / name).suffix.casefold() in _SRT_EXTENSIONS
                )
        else:
            candidates = [
                child
                for child in path.iterdir()
                if child.is_file()
                and not child.is_symlink()
                and child.suffix.casefold() in _SRT_EXTENSIONS
            ]
        candidates.sort(
            key=lambda item: _natural_path_key(item.relative_to(path).as_posix())
        )
        discovered.extend(
            _InputFile(
                path=item,
                original_name=_validated_relative_name(
                    item.relative_to(path).as_posix()
                ),
            )
            for item in candidates
        )

    if not discovered:
        raise UsageError("没有发现 SRT 输入文件")
    names: dict[str, str] = {}
    duplicates: list[str] = []
    for item in discovered:
        key = item.original_name.casefold()
        if key in names:
            duplicates.append(item.original_name)
        names[key] = item.original_name
    if duplicates:
        raise UsageError(f"重复导出相对路径：{', '.join(sorted(duplicates))}")
    return discovered


def _parse_sequence(value: str, *, context: str) -> str:
    if not _SEQUENCE_RE.fullmatch(value) or int(value) <= 0:
        raise UsageError(f"SRT cue 序号无效：{context}")
    return value


def _parse_clock(value: str, *, context: str) -> int:
    hours, minutes, seconds_ms = value.split(":")
    seconds, milliseconds = seconds_ms.split(",")
    minute_value = int(minutes)
    second_value = int(seconds)
    millisecond_value = int(milliseconds)
    if minute_value > 59 or second_value > 59 or millisecond_value > 999:
        raise UsageError(f"SRT 时间范围无效：{context}")
    return (
        int(hours) * 60 * 60 * 1000
        + minute_value * 60 * 1000
        + second_value * 1000
        + millisecond_value
    )


def _validate_timing(value: str, *, context: str) -> str:
    match = _TIMING_RE.fullmatch(value)
    if match is None:
        raise UsageError(f"SRT 时间行无效：{context}")
    start = _parse_clock(match.group("start"), context=context)
    end = _parse_clock(match.group("end"), context=context)
    if end < start:
        raise UsageError(f"SRT cue 结束时间早于开始时间：{context}")
    return value


def _parse_document(
    text: str, *, source_name: str
) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in normalized.split("\n"):
        if not line.strip():
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(line)
    if current:
        blocks.append(current)
    if not blocks:
        raise UsageError(f"SRT 文件没有 cue：{source_name}")

    seen_sequences: set[str] = set()
    segments: list[str] = []
    cues: list[dict[str, str]] = []
    for block_index, block in enumerate(blocks, start=1):
        context = f"{source_name}（cue {block_index}）"
        if len(block) < 3:
            raise UsageError(f"SRT cue 结构不完整：{context}")
        sequence = _parse_sequence(block[0], context=context)
        if sequence in seen_sequences:
            raise UsageError(f"SRT cue 序号重复：{context}")
        seen_sequences.add(sequence)
        timing = _validate_timing(block[1], context=context)
        body = "\n".join(block[2:])
        if not body.strip():
            raise UsageError(f"SRT cue 正文为空：{context}")
        segments.append(body)
        cues.append({"sequence": sequence, "timing": timing})
    return tuple(segments), cues


def _validate_output_text(text: str, *, context: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        raise IncompleteError(f"SRT 译文为空：{context}")
    if any(not line.strip() for line in normalized.split("\n")):
        raise IncompleteError(f"SRT 译文包含空白分隔行：{context}")
    return normalized


def _validate_state(
    state: dict[str, Any] | None,
    *,
    segment_count: int,
    file_name: str,
) -> list[dict[str, str]]:
    if not isinstance(state, dict):
        raise IncompleteError(f"SRT 文件缺少 Document Adapter 状态：{file_name}")
    if state.get("schema_version") != 1:
        raise IncompleteError(f"SRT Adapter 状态版本无效：{file_name}")
    raw_cues = state.get("cues")
    if not isinstance(raw_cues, list) or len(raw_cues) != segment_count:
        raise IncompleteError(f"SRT Adapter 状态 cue 数量无效：{file_name}")
    cues: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_cues, start=1):
        if not isinstance(raw, dict):
            raise IncompleteError(f"SRT Adapter 状态 cue 无效：{file_name}（{index}）")
        sequence = raw.get("sequence")
        timing = raw.get("timing")
        if not isinstance(sequence, str) or not isinstance(timing, str):
            raise IncompleteError(
                f"SRT Adapter 状态 cue 字段无效：{file_name}（{index}）"
            )
        try:
            _parse_sequence(sequence, context=file_name)
            _validate_timing(timing, context=file_name)
        except UsageError as exc:
            raise IncompleteError(str(exc)) from exc
        if sequence in seen:
            raise IncompleteError(f"SRT Adapter 状态序号重复：{file_name}")
        seen.add(sequence)
        cues.append({"sequence": sequence, "timing": timing})
    return cues


class SRTDocumentAdapter:
    adapter_id = "srt"
    version = "1"
    capabilities = frozenset({"import", "translated_export", "bilingual_export"})
    extensions = _SRT_EXTENSIONS
    import_options: tuple[DocumentChoiceOption, ...] = ()
    run_options: tuple[DocumentChoiceOption, ...] = ()

    def replacement_options(
        self, *, opaque_state: dict[str, Any] | None
    ) -> dict[str, str]:
        del opaque_state
        return {}

    def model_prompt_requirements(
        self,
        *,
        stage: str,
        language: str,
        opaque_state: dict[str, Any] | None,
    ) -> str | None:
        del stage, language, opaque_state
        return None

    def normalize_model_output(
        self, *, segment: dict[str, Any], text: str, stage: str
    ) -> str:
        del stage
        return _validate_output_text(
            text, context=str(segment.get("segment_id", "unknown"))
        )

    def import_sources(
        self,
        inputs: list[str],
        *,
        recursive: bool,
        config: dict[str, Any],
        options: dict[str, str],
    ) -> DocumentImport:
        del options
        input_config = config.get("input")
        if not isinstance(input_config, dict):
            raise ProjectError("input 配置节无效")
        discovered = _discover_inputs(inputs, recursive)
        files: list[ImportedFile] = []
        warnings: list[str] = []
        for item in discovered:
            decoded = decode_plaintext(
                item.path.read_bytes(),
                confidence_threshold=float(
                    input_config.get("encoding_confidence_threshold", 0.6)
                ),
                fallback_encoding=str(input_config.get("fallback_encoding", "utf-8")),
            )
            segments, cues = _parse_document(
                decoded.text, source_name=item.original_name
            )
            files.append(
                ImportedFile(
                    source_path=item.path,
                    original_name=item.original_name,
                    segments=segments,
                    encoding_detected=decoded.encoding_detected,
                    encoding_used=decoded.encoding_used,
                    encoding_confidence=decoded.encoding_confidence,
                    segment_part_ids=tuple("document" for _ in segments),
                    opaque_state={"schema_version": 1, "cues": cues},
                )
            )
            warnings.extend(
                f"{item.original_name}: {warning}" for warning in decoded.warnings
            )
        return DocumentImport(files=tuple(files), warnings=tuple(warnings))

    def export_sources(
        self,
        *,
        project: Path,
        staging_dir: Path,
        file: dict[str, Any],
        segments: list[dict[str, Any]],
        output_text: dict[str, str],
        bilingual: bool,
        output_encoding: str,
        target_language: str,
        target_language_tag: str,
        opaque_state: dict[str, Any] | None,
    ) -> list[Path]:
        del project, target_language, target_language_tag
        file_name = str(file.get("original_name", "unknown.srt"))
        ordered_segments = sorted(segments, key=lambda item: int(item["line_index"]))
        if [int(item["line_index"]) for item in ordered_segments] != list(
            range(len(ordered_segments))
        ):
            raise IncompleteError(f"SRT Segment 行号不连续：{file_name}")
        cues = _validate_state(
            opaque_state,
            segment_count=len(ordered_segments),
            file_name=file_name,
        )
        blocks: list[str] = []
        for cue, segment in zip(cues, ordered_segments, strict=True):
            segment_id = str(segment.get("segment_id", "unknown"))
            translation = output_text.get(segment_id)
            if not isinstance(translation, str):
                raise IncompleteError(f"SRT 缺少 Segment 译文：{segment_id}")
            translation = _validate_output_text(translation, context=segment_id)
            body = f"{segment['source']}\n{translation}" if bilingual else translation
            blocks.append(f"{cue['sequence']}\n{cue['timing']}\n{body}")
        try:
            relative = Path(_validated_relative_name(file_name))
        except UsageError as exc:
            raise IncompleteError(str(exc)) from exc
        destination = staging_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            payload = ("\n\n".join(blocks) + "\n").encode(
                output_encoding, errors="strict"
            )
            destination.write_bytes(payload)
        except UnicodeEncodeError as exc:
            raise IncompleteError(
                f"输出编码 {output_encoding} 无法表示 {relative}: {exc}"
            ) from exc
        except LookupError as exc:
            raise IncompleteError(f"输出编码无效：{output_encoding}") from exc
        return [relative]
