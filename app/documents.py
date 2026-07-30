from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .errors import ProjectError


@dataclass(frozen=True)
class ImportedFile:
    source_path: Path
    original_name: str
    segments: tuple[str, ...]
    encoding_detected: str
    encoding_used: str
    encoding_confidence: float


@dataclass(frozen=True)
class DocumentImport:
    files: tuple[ImportedFile, ...]
    warnings: tuple[str, ...] = ()
    opaque_state: dict[str, Any] | None = None


class DocumentAdapter(Protocol):
    adapter_id: str
    version: str
    capabilities: frozenset[str]

    def import_sources(
        self,
        inputs: list[str],
        *,
        recursive: bool,
        config: dict[str, Any],
    ) -> DocumentImport: ...

    def export_sources(
        self,
        *,
        project: Path,
        staging_dir: Path,
        files: list[dict[str, Any]],
        segments: list[dict[str, Any]],
        output_text: dict[str, str],
        bilingual: bool,
        output_encoding: str,
        opaque_state: dict[str, Any] | None,
    ) -> list[Path]: ...


def publish_document_export(
    adapter: DocumentAdapter,
    *,
    project: Path,
    directory: Path,
    files: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    output_text: dict[str, str],
    bilingual: bool,
    output_encoding: str,
    opaque_state: dict[str, Any] | None,
) -> list[str]:
    staging_parent = project / "output" / ".staging"
    staging_parent.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    with tempfile.TemporaryDirectory(
        prefix=f"{adapter.adapter_id}-", dir=staging_parent
    ) as raw:
        staging_dir = Path(raw)
        generated = adapter.export_sources(
            project=project,
            staging_dir=staging_dir,
            files=files,
            segments=segments,
            output_text=output_text,
            bilingual=bilingual,
            output_encoding=output_encoding,
            opaque_state=opaque_state,
        )
        sources: list[tuple[Path, Path]] = []
        seen: set[Path] = set()
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
