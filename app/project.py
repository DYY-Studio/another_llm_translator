from __future__ import annotations

import codecs
import hashlib
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import chardet

from .config import load_config
from .documents import DocumentAdapter, DocumentImport, ImportedFile
from .errors import ConfigError, IncompleteError, ProjectError, UsageError
from .llm_adapter import load_json_adapter
from .llm_preset import load_llm_preset, preset_path
from .storage import (
    atomic_write_json,
    new_record_id,
    read_json,
    read_jsonl,
    record_header,
    utc_now,
    write_jsonl,
)

_SOURCE_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = (
    _SOURCE_ROOT
    if (_SOURCE_ROOT / "config" / "config.toml").is_file()
    else Path(sys.prefix)
)
GLOBAL_CONFIG = APP_ROOT / "config" / "config.toml"
GLOBAL_PROMPTS = APP_ROOT / "prompts"
PROJECTS_ROOT = APP_ROOT / "projects"
PROMPT_NAMES = (
    "terminology.middle.txt",
    "translation.middle.txt",
    "proofreading.middle.txt",
    "polishing.middle.txt",
)


@dataclass(frozen=True)
class InputFile:
    path: Path
    original_name: str


def _natural_key(value: str) -> list[tuple[int, int | str]]:
    parts = re.split(r"(\d+)", value.casefold())
    return [(0, int(part)) if part.isdigit() else (1, part) for part in parts]


def _directory_txt_files(root: Path, recursive: bool) -> list[InputFile]:
    candidates: list[Path] = []
    if recursive:
        for current, dirs, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            dirs[:] = sorted(
                [name for name in dirs if not (current_path / name).is_symlink()],
                key=_natural_key,
            )
            for name in files:
                path = current_path / name
                if path.is_symlink() or path.suffix.casefold() != ".txt":
                    continue
                candidates.append(path)
    else:
        candidates = [
            path
            for path in root.iterdir()
            if path.is_file()
            and not path.is_symlink()
            and path.suffix.casefold() == ".txt"
        ]
    candidates.sort(key=lambda path: _natural_key(path.relative_to(root).as_posix()))
    return [
        InputFile(path=path, original_name=path.relative_to(root).as_posix())
        for path in candidates
    ]


def discover_inputs(values: Iterable[str], recursive: bool) -> list[InputFile]:
    discovered: list[InputFile] = []
    for raw_value in values:
        path = Path(raw_value)
        if path.is_symlink():
            raise UsageError(f"不接受显式符号链接输入：{path}")
        if path.is_dir():
            discovered.extend(_directory_txt_files(path, recursive))
        elif path.is_file() and path.suffix.casefold() == ".txt":
            discovered.append(InputFile(path=path, original_name=path.name))
        else:
            raise UsageError(f"输入不是有效 TXT 文件或目录：{path}")
    if not discovered:
        raise UsageError("没有发现 TXT 输入文件")
    names_by_key: dict[str, list[str]] = {}
    for item in discovered:
        names_by_key.setdefault(item.original_name.casefold(), []).append(
            item.original_name
        )
    duplicates = sorted(
        {
            name
            for values in names_by_key.values()
            if len(values) > 1
            for name in values
        }
    )
    if duplicates:
        raise UsageError(f"重复导出相对路径：{', '.join(duplicates)}")
    return discovered


def decode_txt(
    data: bytes,
    *,
    confidence_threshold: float,
    fallback_encoding: str,
) -> tuple[str, str, str, float, list[str]]:
    warnings: list[str] = []
    detected = ""
    confidence = 1.0
    encoding: str
    if data.startswith(codecs.BOM_UTF32_LE) or data.startswith(codecs.BOM_UTF32_BE):
        detected = encoding = "utf-32"
    elif data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        detected = encoding = "utf-16"
    elif data.startswith(codecs.BOM_UTF8):
        detected = encoding = "utf-8-sig"
    else:
        result = chardet.detect(data)
        detected = str(result.get("encoding") or "utf-8")
        confidence = float(result.get("confidence") or 0.0)
        normalized = detected.casefold().replace("_", "-")
        if normalized in {"gb2312", "gbk"}:
            encoding = "gb18030"
        elif normalized == "ascii":
            encoding = "utf-8"
        else:
            encoding = detected
        if confidence < confidence_threshold:
            warnings.append(
                f"编码探测置信度较低：{detected} ({confidence:.2f})"
            )
    try:
        text = data.decode(encoding, errors="strict")
        used = encoding
    except (LookupError, UnicodeDecodeError):
        try:
            text = data.decode(fallback_encoding, errors="strict")
            used = fallback_encoding
            warnings.append(f"首选编码失败，使用 fallback：{fallback_encoding}")
        except (LookupError, UnicodeDecodeError) as exc:
            raise ProjectError(
                f"无法使用 {encoding} 或 {fallback_encoding} 严格解码"
            ) from exc
    return text, detected, used, confidence, warnings


class TXTDocumentAdapter:
    adapter_id = "txt"
    version = "1"
    capabilities = frozenset({"import", "translated_export", "bilingual_export"})

    def import_sources(
        self,
        inputs: list[str],
        *,
        recursive: bool,
        config: dict[str, object],
    ) -> DocumentImport:
        discovered = discover_inputs(inputs, recursive)
        files: list[ImportedFile] = []
        warnings: list[str] = []
        input_config = config["input"]
        if not isinstance(input_config, dict):
            raise ConfigError("input 配置节无效")
        for item in discovered:
            text, detected, used, confidence, file_warnings = decode_txt(
                item.path.read_bytes(),
                confidence_threshold=float(
                    input_config["encoding_confidence_threshold"]
                ),
                fallback_encoding=str(input_config["fallback_encoding"]),
            )
            normalized = text.replace("\r\n", "\n").replace("\r", "\n")
            files.append(
                ImportedFile(
                    source_path=item.path,
                    original_name=item.original_name,
                    segments=tuple(normalized.split("\n")),
                    encoding_detected=detected,
                    encoding_used=used,
                    encoding_confidence=confidence,
                )
            )
            warnings.extend(
                f"{item.original_name}: {warning}"
                for warning in file_warnings
            )
        return DocumentImport(files=tuple(files), warnings=tuple(warnings))

    def export_sources(
        self,
        *,
        project: Path,
        staging_dir: Path,
        file: dict[str, object],
        segments: list[dict[str, object]],
        output_text: dict[str, str],
        bilingual: bool,
        output_encoding: str,
        opaque_state: dict[str, object] | None,
    ) -> list[Path]:
        del project, opaque_state
        lines: list[str] = []
        for segment in sorted(
            segments, key=lambda item: int(item["line_index"])
        ):
            if segment["is_empty"]:
                lines.append("")
            elif bilingual:
                lines.append(str(segment["source"]))
                lines.append(output_text[str(segment["segment_id"])])
            else:
                lines.append(output_text[str(segment["segment_id"])])
        relative = Path(str(file["original_name"]))
        try:
            payload = "\n".join(lines).encode(output_encoding, errors="strict")
        except UnicodeEncodeError as exc:
            raise IncompleteError(
                f"输出编码 {output_encoding} 无法表示 {relative}: {exc}"
            ) from exc
        destination = staging_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        return [relative]


def _global_adapter_id(config: dict[str, Any], app_root: Path) -> str:
    if "preset" not in config["llm"]:
        return str(config["llm"]["adapter"])
    preset_id = str(config["llm"]["preset"])
    return load_llm_preset(preset_path(app_root, preset_id)).adapter_id


def bundle_hash(app_root: Path = APP_ROOT) -> str:
    config = load_config(app_root / "config" / "config.toml")
    adapter_id = _global_adapter_id(config, app_root)
    adapter = load_json_adapter(
        app_root / "llm_adapters" / f"{adapter_id}.json"
    )
    if adapter.adapter_id != adapter_id:
        raise ConfigError(
            "全局 LLM Adapter 文件中的 adapter_id 与配置不一致"
        )
    paths = [app_root / "config" / "config.toml"] + [
        app_root / "prompts" / name for name in PROMPT_NAMES
    ] + [app_root / "llm_adapters" / f"{adapter_id}.json"]
    digest = hashlib.sha256()
    for path in paths:
        if not path.is_file():
            raise ConfigError(f"全局模板缺失：{path}")
        relative = path.relative_to(app_root).as_posix().encode()
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return f"sha256:{digest.hexdigest()}"


def _copy_bundle(source_root: Path, target: Path) -> None:
    shutil.copy2(source_root / "config" / "config.toml", target / "config.toml")
    prompt_target = target / "prompts"
    prompt_target.mkdir(parents=True, exist_ok=True)
    for name in PROMPT_NAMES:
        shutil.copy2(source_root / "prompts" / name, prompt_target / name)
    config = load_config(source_root / "config" / "config.toml")
    adapter_id = _global_adapter_id(config, source_root)
    adapter_source = source_root / "llm_adapters" / f"{adapter_id}.json"
    if not adapter_source.is_file():
        raise ConfigError(f"全局 LLM Adapter 缺失：{adapter_source}")
    adapter_target = target / "llm_adapters"
    adapter_target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(adapter_source, adapter_target / adapter_source.name)


def init_project(
    inputs: list[str],
    *,
    name: str,
    recursive: bool = False,
    document_adapter_id: str = "txt",
    empty: bool = False,
    dry_run: bool = False,
    app_root: Path = APP_ROOT,
    projects_root: Path | None = None,
) -> tuple[Path | None, dict[str, object]]:
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\0" in name
    ):
        raise UsageError("项目名不能为空，也不能包含路径分隔符")
    if empty == bool(inputs):
        raise UsageError("必须提供输入文件，或显式使用 --empty 创建空项目")
    projects_root = projects_root or app_root / "projects"
    global_config = load_config(app_root / "config" / "config.toml")
    global_hash = bundle_hash(app_root)
    from .plugins import get_document_adapter

    document_adapter = None
    imported = DocumentImport(files=(), warnings=())
    if not empty:
        document_adapter = get_document_adapter(document_adapter_id)
        if "import" not in document_adapter.capabilities:
            raise UsageError(
                f"Document Adapter 不支持导入：{document_adapter.adapter_id}"
            )
        imported = document_adapter.import_sources(
            inputs,
            recursive=recursive,
            config=global_config,
        )

    summary: dict[str, object] = {
        "project_name": name,
        "document_adapter": (
            document_adapter.adapter_id if document_adapter is not None else None
        ),
        "file_count": len(imported.files),
        "segment_count": sum(len(item.segments) for item in imported.files),
        "warnings": list(imported.warnings),
    }
    if dry_run:
        return None, summary

    projects_root.mkdir(parents=True, exist_ok=True)
    target = projects_root / name
    if target.exists():
        raise UsageError(f"项目已存在：{target}")
    project_id = new_record_id("PRJ")
    temp = Path(tempfile.mkdtemp(prefix=f".{name}.", dir=projects_root))
    try:
        _copy_bundle(app_root, temp)
        (temp / "input").mkdir()
        file_records: list[dict[str, object]] = []
        segment_records: list[dict[str, object]] = []
        for file_order, item in enumerate(imported.files, start=1):
            assert document_adapter is not None
            file_id = f"F{file_order:04d}"
            relative = Path(item.original_name)
            stored_name = relative.parent / f"{file_id}__{relative.name}"
            stored_path = temp / "input" / stored_name
            stored_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item.source_path, stored_path)

            segments = item.segments
            state_path = None
            if item.opaque_state is not None:
                state_path = (
                    Path("source")
                    / "adapters"
                    / document_adapter.adapter_id
                    / f"{file_id}.json"
                )
                atomic_write_json(
                    temp / state_path,
                    record_header(
                        "document_adapter_state",
                        project_id,
                        record_id=f"DOCUMENT-{file_id}",
                        adapter_id=document_adapter.adapter_id,
                        adapter_version=document_adapter.version,
                        file_id=file_id,
                        state=item.opaque_state,
                    ),
                )
            file_records.append(
                record_header(
                    "source_file",
                    project_id,
                    record_id=f"FILE-{file_id}",
                    file_id=file_id,
                    file_order=file_order,
                    original_name=item.original_name,
                    stored_name=stored_name.as_posix(),
                    encoding_detected=item.encoding_detected,
                    encoding_confidence=item.encoding_confidence,
                    encoding_used=item.encoding_used,
                    segment_count=len(segments),
                    document_adapter_id=document_adapter.adapter_id,
                    document_adapter_version=document_adapter.version,
                    document_adapter_state=(
                        state_path.as_posix() if state_path is not None else None
                    ),
                )
            )
            for line_index, source in enumerate(segments):
                segment_id = f"{file_id}-S{line_index + 1:06d}"
                segment_records.append(
                    record_header(
                        "source_segment",
                        project_id,
                        record_id=segment_id,
                        segment_id=segment_id,
                        file_id=file_id,
                        line_index=line_index,
                        source=source,
                        is_empty=source == "" or source.isspace(),
                    )
                )

        write_jsonl(temp / "source" / "files.jsonl", file_records)
        write_jsonl(temp / "source" / "segments.jsonl", segment_records)
        (temp / "terminology").mkdir(parents=True, exist_ok=True)
        (temp / "stages").mkdir(parents=True, exist_ok=True)
        (temp / "runs").mkdir(parents=True, exist_ok=True)
        (temp / "logs").mkdir(parents=True, exist_ok=True)
        (temp / "output").mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            temp / "terminology" / "overrides.json",
            record_header(
                "terminology_overrides",
                project_id,
                record_id="TERMINOLOGY-OVERRIDES",
                overrides=[],
            ),
        )
        atomic_write_json(
            temp / "project.json",
            record_header(
                "project",
                project_id,
                record_id=project_id,
                name=name,
                global_bundle_hash_seen=global_hash,
                file_count=len(file_records),
                segment_count=len(segment_records),
                next_file_sequence=len(file_records) + 1,
                status="active",
            ),
        )
        os.replace(temp, target)
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return target, summary


def _next_file_sequence(
    metadata: dict[str, Any], files: list[dict[str, Any]]
) -> int:
    configured = metadata.get("next_file_sequence")
    if isinstance(configured, int) and not isinstance(configured, bool) and configured > 0:
        return configured
    maximum = 0
    for file_record in files:
        match = re.fullmatch(r"F(\d+)", str(file_record.get("file_id", "")))
        if match:
            maximum = max(maximum, int(match.group(1)))
    return maximum + 1


def _running_run_ids(project: Path) -> list[str]:
    running: list[str] = []
    runs_dir = project / "runs"
    if not runs_dir.is_dir():
        return running
    for manifest_path in runs_dir.glob("*/manifest.json"):
        manifest = read_json(manifest_path)
        if manifest.get("status") == "running":
            running.append(str(manifest.get("run_id") or manifest_path.parent.name))
    return sorted(running)


def _source_records(
    project: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    metadata = read_json(project / "project.json")
    files = read_jsonl(project / "source" / "files.jsonl")
    return (
        metadata,
        _resolve_file_adapters(metadata, files),
        read_jsonl(project / "source" / "segments.jsonl"),
    )


def _resolve_file_adapters(
    metadata: dict[str, Any], files: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    legacy_id = str(metadata.get("document_adapter_id") or "txt")
    legacy_version = metadata.get("document_adapter_version")
    legacy_state = metadata.get("document_adapter_state")
    resolved: list[dict[str, Any]] = []
    for file_record in files:
        item = dict(file_record)
        item.setdefault("document_adapter_id", legacy_id)
        if "document_adapter_version" not in item:
            if legacy_version is not None:
                item["document_adapter_version"] = str(legacy_version)
            else:
                from .plugins import get_document_adapter

                item["document_adapter_version"] = get_document_adapter(
                    legacy_id
                ).version
        item.setdefault("document_adapter_state", legacy_state)
        resolved.append(item)
    return resolved


def _write_source_snapshot(
    root: Path,
    *,
    metadata: dict[str, Any],
    files: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    adapter_states: dict[Path, dict[str, Any]],
) -> None:
    write_jsonl(root / "source" / "files.jsonl", files)
    write_jsonl(root / "source" / "segments.jsonl", segments)
    atomic_write_json(root / "project.json", metadata)
    for state_path, state_record in adapter_states.items():
        atomic_write_json(root / state_path, state_record)


def _publish_source_snapshot(
    project: Path,
    staging: Path,
    *,
    state_paths: list[Path],
    removed_state_paths: list[Path],
) -> None:
    targets = [
        Path("source/files.jsonl"),
        Path("source/segments.jsonl"),
        Path("project.json"),
    ]
    targets.extend(state_paths)
    backup = staging / "backup"
    published: list[Path] = []
    removed_states: list[tuple[Path, Path]] = []
    try:
        for relative in targets:
            current = project / relative
            if current.exists():
                saved = backup / relative
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(current, saved)
            replacement = staging / relative
            current.parent.mkdir(parents=True, exist_ok=True)
            os.replace(replacement, current)
            published.append(relative)
        for old_state_path in removed_state_paths:
            current = project / old_state_path
            if current.exists():
                saved = backup / old_state_path
                saved.parent.mkdir(parents=True, exist_ok=True)
                os.replace(current, saved)
                removed_states.append((saved, old_state_path))
    except Exception:
        for relative in reversed(published):
            saved = backup / relative
            current = project / relative
            if saved.exists():
                os.replace(saved, current)
            elif current.exists():
                current.unlink()
        for saved, old_state_path in removed_states:
            destination = project / old_state_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(saved, destination)
        raise


def add_project_files(
    project: Path,
    inputs: list[str],
    *,
    recursive: bool = False,
    document_adapter_id: str | None = None,
) -> dict[str, object]:
    running = _running_run_ids(project)
    if running:
        raise UsageError(
            f"存在未完成 Run，不能添加文件：{', '.join(running)}"
        )
    metadata, files, segments = _source_records(project)
    config = load_config(project / "config.toml")
    from .plugins import get_document_adapter

    imports: list[tuple[DocumentAdapter, ImportedFile]] = []
    warnings: list[str] = []
    if document_adapter_id is not None:
        adapter = get_document_adapter(document_adapter_id)
        imported = adapter.import_sources(
            inputs, recursive=recursive, config=config
        )
        imports.extend((adapter, item) for item in imported.files)
        warnings.extend(imported.warnings)
    else:
        for raw_input in inputs:
            path = Path(raw_input)
            suffix = path.suffix.lower()
            if path.is_dir() or suffix == ".txt":
                adapter_id = "txt"
            elif suffix == ".epub":
                adapter_id = "epub"
            else:
                raise UsageError(
                    f"无法识别输入格式，请使用 --document-adapter：{raw_input}"
                )
            adapter = get_document_adapter(adapter_id)
            imported = adapter.import_sources(
                [raw_input], recursive=recursive, config=config
            )
            imports.extend((adapter, item) for item in imported.files)
            warnings.extend(imported.warnings)
    existing_names = {
        str(record["original_name"]).casefold() for record in files
    }
    duplicate_names: list[str] = []
    added_names: set[str] = set()
    for _, item in imports:
        normalized_name = item.original_name.casefold()
        if normalized_name in existing_names or normalized_name in added_names:
            duplicate_names.append(item.original_name)
        added_names.add(normalized_name)
    if duplicate_names:
        raise UsageError(f"活动文件已存在同名导出路径：{', '.join(duplicate_names)}")

    project_id = str(metadata["project_id"])
    next_sequence = _next_file_sequence(metadata, files)
    next_order = max((int(item["file_order"]) for item in files), default=0) + 1
    added_files: list[dict[str, Any]] = []
    added_segments: list[dict[str, Any]] = []
    staging = Path(tempfile.mkdtemp(prefix=".files-add.", dir=project))
    moved_inputs: list[Path] = []
    state_records: dict[Path, dict[str, Any]] = {}
    committed = False
    try:
        for offset, (adapter, item) in enumerate(imports):
            sequence = next_sequence + offset
            file_id = f"F{sequence:04d}"
            relative = Path(item.original_name)
            stored_name = relative.parent / f"{file_id}__{relative.name}"
            staged_input = staging / "input" / stored_name
            staged_input.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item.source_path, staged_input)
            state_path = None
            if item.opaque_state is not None:
                state_path = (
                    Path("source")
                    / "adapters"
                    / adapter.adapter_id
                    / f"{file_id}.json"
                )
                state_records[state_path] = record_header(
                    "document_adapter_state",
                    project_id,
                    record_id=f"DOCUMENT-{file_id}",
                    adapter_id=adapter.adapter_id,
                    adapter_version=adapter.version,
                    file_id=file_id,
                    state=item.opaque_state,
                )
            file_record = record_header(
                "source_file",
                project_id,
                record_id=f"FILE-{file_id}",
                file_id=file_id,
                file_order=next_order + offset,
                original_name=item.original_name,
                stored_name=stored_name.as_posix(),
                encoding_detected=item.encoding_detected,
                encoding_confidence=item.encoding_confidence,
                encoding_used=item.encoding_used,
                segment_count=len(item.segments),
                document_adapter_id=adapter.adapter_id,
                document_adapter_version=adapter.version,
                document_adapter_state=(
                    state_path.as_posix() if state_path is not None else None
                ),
            )
            added_files.append(file_record)
            for line_index, source in enumerate(item.segments):
                segment_id = f"{file_id}-S{line_index + 1:06d}"
                added_segments.append(
                    record_header(
                        "source_segment",
                        project_id,
                        record_id=segment_id,
                        segment_id=segment_id,
                        file_id=file_id,
                        line_index=line_index,
                        source=source,
                        is_empty=source == "" or source.isspace(),
                    )
                )
        new_files = [*files, *added_files]
        new_segments = [*segments, *added_segments]
        new_metadata = dict(metadata)
        new_metadata.update(
            file_count=len(new_files),
            segment_count=len(new_segments),
            next_file_sequence=next_sequence + len(added_files),
        )
        for key in (
            "document_adapter_id",
            "document_adapter_version",
            "document_adapter_state",
        ):
            new_metadata.pop(key, None)
        _write_source_snapshot(
            staging,
            metadata=new_metadata,
            files=new_files,
            segments=new_segments,
            adapter_states=state_records,
        )
        for file_record in added_files:
            relative = Path(str(file_record["stored_name"]))
            destination = project / "input" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging / "input" / relative, destination)
            moved_inputs.append(destination)
        _publish_source_snapshot(
            project,
            staging,
            state_paths=list(state_records),
            removed_state_paths=[],
        )
        committed = True
    except Exception:
        if not committed:
            for destination in moved_inputs:
                destination.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {
        "added_file_ids": [str(item["file_id"]) for item in added_files],
        "added_files": len(added_files),
        "added_segments": len(added_segments),
        "file_count": len(files) + len(added_files),
        "segment_count": len(segments) + len(added_segments),
        "warnings": warnings,
    }


def remove_project_files(
    project: Path,
    file_ids: list[str],
) -> dict[str, object]:
    if not file_ids:
        raise UsageError("必须选择至少一个文件")
    if len(set(file_ids)) != len(file_ids):
        raise UsageError("文件 ID 不能重复")
    running = _running_run_ids(project)
    if running:
        raise UsageError(
            f"存在未完成 Run，不能移除文件：{', '.join(running)}"
        )
    metadata, files, segments = _source_records(project)
    known = {str(item["file_id"]): item for item in files}
    unknown = [file_id for file_id in file_ids if file_id not in known]
    if unknown:
        raise UsageError(f"未知文件 ID：{', '.join(unknown)}")
    selected = set(file_ids)
    new_files = [item for item in files if str(item["file_id"]) not in selected]
    removed_segments = [
        item for item in segments if str(item["file_id"]) in selected
    ]
    new_segments = [
        item for item in segments if str(item["file_id"]) not in selected
    ]
    retained_state_paths = {
        str(item["document_adapter_state"])
        for item in new_files
        if item.get("document_adapter_state")
    }
    removed_state_paths = sorted(
        {
            Path(str(known[file_id]["document_adapter_state"]))
            for file_id in file_ids
            if known[file_id].get("document_adapter_state")
            and str(known[file_id]["document_adapter_state"])
            not in retained_state_paths
        }
    )
    new_metadata = dict(metadata)
    new_metadata.update(
        file_count=len(new_files),
        segment_count=len(new_segments),
        next_file_sequence=_next_file_sequence(metadata, files),
    )
    for key in (
        "document_adapter_id",
        "document_adapter_version",
        "document_adapter_state",
    ):
        new_metadata.pop(key, None)
    staging = Path(tempfile.mkdtemp(prefix=".files-remove.", dir=project))
    moved_inputs: list[tuple[Path, Path]] = []
    committed = False
    try:
        _write_source_snapshot(
            staging,
            metadata=new_metadata,
            files=new_files,
            segments=new_segments,
            adapter_states={},
        )
        for file_id in file_ids:
            relative = Path(str(known[file_id]["stored_name"]))
            source = project / "input" / relative
            if source.exists():
                held = staging / "removed-input" / relative
                held.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, held)
                moved_inputs.append((held, source))
        _publish_source_snapshot(
            project,
            staging,
            state_paths=[],
            removed_state_paths=removed_state_paths,
        )
        committed = True
    except Exception:
        if not committed:
            for held, source in moved_inputs:
                if held.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(held, source)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {
        "removed_file_ids": file_ids,
        "removed_files": len(file_ids),
        "removed_segments": len(removed_segments),
        "file_count": len(new_files),
        "segment_count": len(new_segments),
    }


def resolve_project(value: str, projects_root: Path = PROJECTS_ROOT) -> Path:
    direct = Path(value)
    path = direct if direct.is_dir() else projects_root / value
    if not (path / "project.json").is_file():
        raise ProjectError(f"项目不存在或无效：{value}")
    return path


def sync_global_templates(
    project: Path,
    *,
    dry_run: bool = False,
    app_root: Path = APP_ROOT,
    interactive: bool | None = None,
    choice: str | None = None,
) -> list[str]:
    metadata = read_json(project / "project.json")
    try:
        load_config(app_root / "config" / "config.toml")
        current_hash = bundle_hash(app_root)
    except ConfigError as exc:
        return [f"全局模板无效，继续使用项目副本：{exc}"]
    if metadata.get("global_bundle_hash_seen") == current_hash:
        return []
    global_config = load_config(app_root / "config" / "config.toml")
    adapter_id = _global_adapter_id(global_config, app_root)
    bundle_files = [Path("config.toml")] + [
        Path("prompts") / name for name in PROMPT_NAMES
    ] + [Path("llm_adapters") / f"{adapter_id}.json"]
    changed = []
    for relative in bundle_files:
        global_path = (
            app_root / "config" / "config.toml"
            if relative == Path("config.toml")
            else app_root / relative
        )
        project_path = project / relative
        if (
            not project_path.is_file()
            or project_path.read_bytes() != global_path.read_bytes()
        ):
            changed.append(relative.as_posix())
    warnings = [f"发现新的全局模板：{', '.join(changed) or '内容版本变化'}"]
    if dry_run:
        return warnings
    interactive = os.isatty(0) if interactive is None else interactive
    if not interactive:
        warnings.append("非交互环境保留项目副本；未更新 seen Hash")
        return warnings
    if choice is None:
        print(
            f"{warnings[0]}\n更新项目模板？[u]pdate/[k]eep: ",
            end="",
            file=sys.stderr,
            flush=True,
        )
        answer = input().strip().casefold()
        choice = "update" if answer in {"u", "update"} else "keep"
    if choice not in {"update", "keep"}:
        raise UsageError("模板选择必须是 update 或 keep")
    if choice == "update":
        load_config(app_root / "config" / "config.toml")
        timestamp = utc_now().replace(":", "").replace("-", "")
        backup = project / "snapshots" / "template_updates" / timestamp
        backup.mkdir(parents=True, exist_ok=False)
        shutil.copy2(project / "config.toml", backup / "config.toml")
        shutil.copytree(project / "prompts", backup / "prompts")
        if (project / "llm_adapters").exists():
            shutil.copytree(
                project / "llm_adapters", backup / "llm_adapters"
            )
        _copy_bundle(app_root, project)
        warnings.append(f"已更新项目模板；备份位于 {backup}")
    else:
        warnings.append("已保留项目模板")
    metadata["global_bundle_hash_seen"] = current_hash
    atomic_write_json(project / "project.json", metadata)
    return warnings


def load_source_files(
    project: Path, *, repair_tail: bool = True
) -> list[dict[str, object]]:
    metadata = read_json(project / "project.json")
    files = read_jsonl(
        project / "source" / "files.jsonl", repair_tail=repair_tail
    )
    return _resolve_file_adapters(metadata, files)


def load_segments(
    project: Path, *, repair_tail: bool = True
) -> list[dict[str, object]]:
    return read_jsonl(project / "source" / "segments.jsonl", repair_tail=repair_tail)
