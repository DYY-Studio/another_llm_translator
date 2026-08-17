from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .errors import ProjectError


_AOZORA_DELIMITERS = frozenset("｜《》\r\n<>")


def parse_aozora_text(
    value: str,
) -> tuple[list[tuple[str, str, str | None]], bool]:
    """Parse only strict, non-nested Aozora ruby expressions."""
    fragments: list[tuple[str, str, str | None]] = []
    plain_start = 0
    cursor = 0
    found_ruby = False
    while cursor < len(value):
        marker = value.find("｜", cursor)
        if marker < 0:
            break
        opening = value.find("《", marker + 1)
        closing = value.find("》", opening + 1) if opening >= 0 else -1
        if opening < 0 or closing < 0:
            break
        base = value[marker + 1 : opening]
        reading = value[opening + 1 : closing]
        candidate = f"{base}{reading}"
        if (
            not base
            or not reading
            or any(character in _AOZORA_DELIMITERS for character in candidate)
        ):
            cursor = closing + 1
            continue
        if marker > plain_start:
            fragments.append(("text", value[plain_start:marker], None))
        fragments.append(("ruby", base, reading))
        found_ruby = True
        cursor = closing + 1
        plain_start = cursor
    if plain_start < len(value):
        fragments.append(("text", value[plain_start:], None))
    if not fragments:
        fragments.append(("text", value, None))
    return fragments, found_ruby


def aozora_match_views(value: str) -> tuple[str, ...]:
    """Return independent base and adjacent-reading views for term matching."""
    fragments, found_ruby = parse_aozora_text(value)
    if not found_ruby:
        return (value,)
    if value.count("｜") != sum(kind == "ruby" for kind, _, _ in fragments):
        return (value,)

    base_parts: list[str] = []
    reading_views: list[str] = []
    adjacent_readings: list[str] = []
    for kind, text, reading in fragments:
        if kind == "ruby":
            base_parts.append(text)
            adjacent_readings.append(reading or "")
            continue
        base_parts.append(text)
        if adjacent_readings:
            reading_views.append("".join(adjacent_readings))
            adjacent_readings = []
    if adjacent_readings:
        reading_views.append("".join(adjacent_readings))
    return ("".join(base_parts), *reading_views)


@dataclass(frozen=True)
class ImportedFile:
    source_path: Path
    original_name: str
    segments: tuple[str, ...]
    encoding_detected: str
    encoding_used: str
    encoding_confidence: float
    opaque_state: dict[str, Any] | None = None
    segment_part_ids: tuple[str, ...] | None = None
    model_sources: tuple[str | None, ...] | None = None


@dataclass(frozen=True)
class DocumentImport:
    files: tuple[ImportedFile, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class DocumentChoiceOption:
    option_id: str
    label: str
    default: str
    choices: tuple[tuple[str, str], ...]


class DocumentAdapter(Protocol):
    adapter_id: str
    version: str
    capabilities: frozenset[str]
    extensions: frozenset[str]
    import_options: tuple[DocumentChoiceOption, ...]
    run_options: tuple[DocumentChoiceOption, ...]

    def model_prompt_requirements(
        self,
        *,
        stage: str,
        language: str,
        opaque_state: dict[str, Any] | None,
    ) -> str | None: ...

    def import_sources(
        self,
        inputs: list[str],
        *,
        recursive: bool,
        config: dict[str, Any],
        options: dict[str, str],
    ) -> DocumentImport: ...

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
    ) -> list[Path]: ...


def normalize_document_output(
    adapter: DocumentAdapter,
    *,
    segment: dict[str, Any],
    text: str,
    stage: str,
) -> str:
    normalizer = getattr(adapter, "normalize_model_output", None)
    if normalizer is None:
        return text
    value = normalizer(segment=segment, text=text, stage=stage)
    if not isinstance(value, str):
        raise ProjectError("Document Adapter 返回了无效的模型文本")
    return value


@dataclass(frozen=True)
class DocumentExportJob:
    adapter: DocumentAdapter
    file: dict[str, Any]
    segments: list[dict[str, Any]]
    opaque_state: dict[str, Any] | None


def publish_document_exports(
    jobs: list[DocumentExportJob],
    *,
    project: Path,
    directory: Path,
    output_text: dict[str, str],
    bilingual: bool,
    output_encoding: str,
    target_language: str,
    target_language_tag: str,
) -> list[str]:
    staging_parent = project / "output" / ".staging"
    staging_parent.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    with tempfile.TemporaryDirectory(
        prefix="documents-", dir=staging_parent
    ) as raw:
        staging_dir = Path(raw)
        sources: list[tuple[Path, Path]] = []
        seen: set[Path] = set()
        for job in jobs:
            generated = job.adapter.export_sources(
                project=project,
                staging_dir=staging_dir,
                file=job.file,
                segments=job.segments,
                output_text=output_text,
                bilingual=bilingual,
                output_encoding=output_encoding,
                target_language=target_language,
                target_language_tag=target_language_tag,
                opaque_state=job.opaque_state,
            )
            for relative in generated:
                if relative.is_absolute() or ".." in relative.parts:
                    raise ProjectError(
                        f"Document Adapter 返回了不安全输出路径：{relative}"
                    )
                if relative in seen:
                    raise ProjectError(
                        f"Document Adapter 返回了重复输出路径：{relative}"
                    )
                seen.add(relative)
                source = staging_dir / relative
                if not source.is_file():
                    raise ProjectError(
                        f"Document Adapter 未生成声明的输出：{relative}"
                    )
                destination = directory / relative
                sources.append((source, destination))
        for source, destination in sources:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            written.append(str(destination.relative_to(project)))
    return written
