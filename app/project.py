from __future__ import annotations

import codecs
import hashlib
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import chardet

from .config import LLM_STAGES, load_config
from .documents import DocumentAdapter, DocumentImport, ImportedFile
from .errors import ConfigError, IncompleteError, ProjectError, UsageError
from .user_config import APP_ROOT, effective_path, user_root
from .sqlite_storage import (
    database_path,
    initialize as initialize_project_database,
    new_record_id,
    read_adapter_state,
    read_files as read_sqlite_files,
    read_project_meta,
    read_segments as read_sqlite_segments,
    record_header,
    replace_source,
    utc_now,
    write_json,
)

PROJECTS_ROOT = user_root() / "projects"
PROMPT_LANGUAGES = ("zh-CN", "en")


def prompt_file(stage: str, language: str) -> str:
    return f"{stage}.{language}.middle.txt"


PROMPT_NAMES = tuple(
    prompt_file(stage, language)
    for stage in LLM_STAGES
    for language in PROMPT_LANGUAGES
)
_TXT_EXTENSIONS = frozenset({".txt", ".text"})


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
                if path.is_symlink() or path.suffix.casefold() not in _TXT_EXTENSIONS:
                    continue
                candidates.append(path)
    else:
        candidates = [
            path
            for path in root.iterdir()
            if path.is_file()
            and not path.is_symlink()
            and path.suffix.casefold() in _TXT_EXTENSIONS
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
        elif path.is_file() and path.suffix.casefold() in _TXT_EXTENSIONS:
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


def _validated_original_name(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or path.name in {"", ".", ".."}
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise UsageError(f"输入相对路径无效：{value}")
    return path.as_posix()


def _auto_input_files(
    inputs: list[str],
    *,
    recursive: bool,
    original_names: list[str] | None,
) -> tuple[list[InputFile], list[str]]:
    if original_names is not None and len(original_names) != len(inputs):
        raise UsageError("输入文件与相对路径数量不一致")
    from .plugins import get_document_adapter_for_extension

    discovered: list[InputFile] = []
    warnings: list[str] = []
    for index, raw_input in enumerate(inputs):
        path = Path(raw_input)
        if path.is_symlink():
            raise UsageError(f"不接受显式符号链接输入：{path}")
        if path.is_file():
            adapter = get_document_adapter_for_extension(path.suffix)
            if "import" not in adapter.capabilities:
                raise UsageError(
                    f"Document Adapter 不支持导入：{adapter.adapter_id}"
                )
            name = (
                original_names[index]
                if original_names is not None
                else path.name
            )
            discovered.append(
                InputFile(path=path, original_name=_validated_original_name(name))
            )
            continue
        if not path.is_dir() or original_names is not None:
            raise UsageError(f"输入文件或目录不存在：{path}")
        candidates = path.rglob("*") if recursive else path.iterdir()
        ignored = 0
        directory_files: list[InputFile] = []
        for candidate in candidates:
            if candidate.is_symlink() or not candidate.is_file():
                continue
            try:
                adapter = get_document_adapter_for_extension(candidate.suffix)
                if "import" not in adapter.capabilities:
                    raise UsageError(
                        f"Document Adapter 不支持导入：{adapter.adapter_id}"
                    )
            except UsageError:
                ignored += 1
                continue
            directory_files.append(
                InputFile(
                    path=candidate,
                    original_name=_validated_original_name(
                        candidate.relative_to(path).as_posix()
                    ),
                )
            )
        directory_files.sort(key=lambda item: _natural_key(item.original_name))
        discovered.extend(directory_files)
        if ignored:
            warnings.append(f"{path}: 已忽略 {ignored} 个不支持的文件")
    if not discovered:
        raise UsageError("没有发现受支持的输入文件")
    names: set[str] = set()
    duplicates: list[str] = []
    for item in discovered:
        key = item.original_name.casefold()
        if key in names:
            duplicates.append(item.original_name)
        names.add(key)
    if duplicates:
        raise UsageError(f"重复导出相对路径：{', '.join(duplicates)}")
    return discovered, warnings


def _import_project_inputs(
    inputs: list[str],
    *,
    recursive: bool,
    config: dict[str, object],
    document_adapter_id: str | None,
    original_names: list[str] | None = None,
    adapter_options: dict[str, dict[str, str]] | None = None,
) -> tuple[list[tuple[DocumentAdapter, ImportedFile]], list[str]]:
    from .plugins import (
        get_document_adapter,
        get_document_adapter_for_extension,
        validate_document_import_options,
    )

    option_values = adapter_options or {}

    if document_adapter_id is not None:
        adapter = get_document_adapter(document_adapter_id)
        if "import" not in adapter.capabilities:
            raise UsageError(
                f"Document Adapter 不支持导入：{adapter.adapter_id}"
            )
        imported = adapter.import_sources(
            inputs,
            recursive=recursive,
            config=config,
            options=validate_document_import_options(
                adapter, option_values.get(adapter.adapter_id)
            ),
        )
        files = [_normalize_imported_file(item) for item in imported.files]
        if original_names is not None:
            if len(files) != len(original_names):
                raise UsageError("Adapter 返回文件数与输入相对路径数量不一致")
            files = [
                replace(item, original_name=_validated_original_name(name))
                for item, name in zip(files, original_names, strict=True)
            ]
        return [(adapter, item) for item in files], list(imported.warnings)

    discovered, warnings = _auto_input_files(
        inputs, recursive=recursive, original_names=original_names
    )
    values: list[tuple[DocumentAdapter, ImportedFile]] = []
    for source in discovered:
        adapter = get_document_adapter_for_extension(source.path.suffix)
        if "import" not in adapter.capabilities:
            raise UsageError(
                f"Document Adapter 不支持导入：{adapter.adapter_id}"
            )
        imported = adapter.import_sources(
            [str(source.path)],
            recursive=False,
            config=config,
            options=validate_document_import_options(
                adapter, option_values.get(adapter.adapter_id)
            ),
        )
        if len(imported.files) != 1:
            raise UsageError(
                f"按扩展名导入时 Adapter 必须返回一个 File：{adapter.adapter_id}"
            )
        values.append(
            (
                adapter,
                replace(
                    _normalize_imported_file(imported.files[0]),
                    original_name=source.original_name,
                ),
            )
        )
        warnings.extend(imported.warnings)
    return values, warnings


def _normalize_imported_file(item: ImportedFile) -> ImportedFile:
    parts = item.segment_part_ids
    if parts is None:
        parts = tuple("document" for _ in item.segments)
    elif (
        not isinstance(parts, tuple)
        or len(parts) != len(item.segments)
        or any(not isinstance(value, str) or not value for value in parts)
    ):
        raise UsageError(
            f"Document Adapter 返回的 segment_part_ids 无效：{item.original_name}"
        )
    model_sources = item.model_sources
    if model_sources is not None and (
        not isinstance(model_sources, tuple)
        or len(model_sources) != len(item.segments)
        or any(value is not None and not isinstance(value, str) for value in model_sources)
    ):
        raise UsageError(
            f"Document Adapter 返回的 model_sources 无效：{item.original_name}"
        )
    return replace(item, segment_part_ids=parts, model_sources=model_sources)


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
    extensions = _TXT_EXTENSIONS
    import_options = ()
    run_options = ()

    def import_sources(
        self,
        inputs: list[str],
        *,
        recursive: bool,
        config: dict[str, object],
        options: dict[str, str],
    ) -> DocumentImport:
        del options
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
            segments = tuple(normalized.split("\n"))
            files.append(
                ImportedFile(
                    source_path=item.path,
                    original_name=item.original_name,
                    segments=segments,
                    encoding_detected=detected,
                    encoding_used=used,
                    encoding_confidence=confidence,
                    segment_part_ids=tuple("document" for _ in segments),
                    model_sources=None,
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
        target_language: str,
        target_language_tag: str,
        opaque_state: dict[str, object] | None,
    ) -> list[Path]:
        del project, opaque_state, target_language, target_language_tag
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


BUNDLE_FILES = (Path("config.toml"),) + tuple(
    Path("prompts") / name for name in PROMPT_NAMES
)


def _bundle_source(app_root: Path) -> dict[Path, Path]:
    sources: dict[Path, Path] = {}
    for relative in BUNDLE_FILES:
        global_relative = (
            Path("config") / "config.toml"
            if relative == Path("config.toml")
            else relative
        )
        sources[relative] = effective_path(global_relative, builtin_root=app_root)
    return sources


def bundle_hash(app_root: Path = APP_ROOT) -> str:
    digest = hashlib.sha256()
    for relative, source in _bundle_source(app_root).items():
        if not source.is_file():
            raise ConfigError(f"全局模板缺失：{relative}")
        rel = relative.as_posix().encode()
        payload = source.read_bytes()
        digest.update(len(rel).to_bytes(4, "big"))
        digest.update(rel)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return f"sha256:{digest.hexdigest()}"


def _copy_bundle(source_root: Path, target: Path) -> None:
    for relative, source in _bundle_source(source_root).items():
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def init_project(
    inputs: list[str],
    *,
    name: str,
    recursive: bool = False,
    document_adapter_id: str | None = "txt",
    original_names: list[str] | None = None,
    adapter_options: dict[str, dict[str, str]] | None = None,
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
    projects_root = projects_root or user_root() / "projects"
    global_config = load_config(effective_path("config/config.toml", builtin_root=app_root))
    global_hash = bundle_hash(app_root)
    imports: list[tuple[DocumentAdapter, ImportedFile]] = []
    warnings: list[str] = []
    if not empty:
        imports, warnings = _import_project_inputs(
            inputs,
            recursive=recursive,
            config=global_config,
            document_adapter_id=document_adapter_id,
            original_names=original_names,
            adapter_options=adapter_options,
        )

    adapter_ids = {adapter.adapter_id for adapter, _ in imports}
    summary: dict[str, object] = {
        "project_name": name,
        "document_adapter": (
            next(iter(adapter_ids)) if len(adapter_ids) == 1 else None
        ),
        "file_count": len(imports),
        "segment_count": sum(len(item.segments) for _, item in imports),
        "warnings": warnings,
    }
    if dry_run:
        return None, summary

    try:
        projects_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UsageError(f"无法使用项目父目录：{projects_root}: {exc}") from exc
    target = projects_root / name
    if target.exists():
        raise UsageError(f"项目已存在：{target}")
    project_id = new_record_id("PRJ")
    try:
        temp = Path(tempfile.mkdtemp(prefix=f".{name}.", dir=projects_root))
    except OSError as exc:
        raise UsageError(f"无法写入项目父目录：{projects_root}: {exc}") from exc
    try:
        _copy_bundle(app_root, temp)
        initialize_project_database(temp)
        (temp / "input").mkdir()
        file_records: list[dict[str, object]] = []
        segment_records: list[dict[str, object]] = []
        adapter_state_records: list[dict[str, object]] = []
        for file_order, (document_adapter, item) in enumerate(imports, start=1):
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
                adapter_state_records.append(
                    record_header(
                        "document_adapter_state",
                        project_id,
                        record_id=f"DOCUMENT-{file_id}",
                        adapter_id=document_adapter.adapter_id,
                        adapter_version=document_adapter.version,
                        file_id=file_id,
                        state=item.opaque_state,
                    )
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
            part_ids = item.segment_part_ids
            if part_ids is None:
                raise ProjectError("导入文件缺少 segment_part_ids")
            model_sources = item.model_sources
            if model_sources is not None and len(model_sources) != len(segments):
                raise ProjectError("导入文件 model_sources 与 Segment 数量不一致")
            for line_index, source in enumerate(segments):
                segment_id = f"{file_id}-S{line_index + 1:06d}"
                fields: dict[str, object] = {
                    "segment_id": segment_id,
                    "file_id": file_id,
                    "line_index": line_index,
                    "part_id": part_ids[line_index],
                    "source": source,
                    "is_empty": source == "" or source.isspace(),
                }
                if model_sources is not None and model_sources[line_index] is not None:
                    fields["model_source"] = model_sources[line_index]
                segment_records.append(
                    record_header(
                        "source_segment",
                        project_id,
                        record_id=segment_id,
                        **fields,
                    )
                )

        (temp / "runs").mkdir(parents=True, exist_ok=True)
        (temp / "logs").mkdir(parents=True, exist_ok=True)
        (temp / "output").mkdir(parents=True, exist_ok=True)
        metadata = record_header(
            "project",
            project_id,
            record_id=project_id,
            name=name,
            global_bundle_hash_seen=global_hash,
            file_count=len(file_records),
            segment_count=len(segment_records),
            next_file_sequence=len(file_records) + 1,
            status="active",
        )
        replace_source(
            temp,
            file_records,
            segment_records,
            metadata,
            adapter_state_records,
        )
        write_json(
            temp,
            temp / "terminology" / "overrides.json",
            record_header(
                "terminology_overrides",
                project_id,
                record_id="TERMINOLOGY-OVERRIDES",
                overrides=[],
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
    from .sqlite_storage import list_runs

    return [str(item["run_id"]) for item in list_runs(project, status="running")]


def _source_records(
    project: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    metadata = read_project_meta(project)
    files = read_sqlite_files(project)
    return (
        metadata,
        _resolve_file_adapters(files),
        _load_segment_records(project, include_model_contract=False),
    )


def _resolve_file_adapters(
    files: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    for file_record in files:
        item = dict(file_record)
        if not isinstance(item.get("document_adapter_id"), str) or not item.get(
            "document_adapter_id"
        ):
            raise ProjectError("项目 File 缺少 Document Adapter；请重新创建项目")
        if not isinstance(item.get("document_adapter_version"), str) or not item.get(
            "document_adapter_version"
        ):
            raise ProjectError("项目 File 缺少 Document Adapter 版本；请重新创建项目")
        resolved.append(item)
    return resolved


def add_project_files(
    project: Path,
    inputs: list[str],
    *,
    recursive: bool = False,
    document_adapter_id: str | None = None,
    original_names: list[str] | None = None,
    adapter_options: dict[str, dict[str, str]] | None = None,
) -> dict[str, object]:
    running = _running_run_ids(project)
    if running:
        raise UsageError(
            f"存在未完成 Run，不能添加文件：{', '.join(running)}"
        )
    metadata, files, segments = _source_records(project)
    config = load_config(project / "config.toml")
    imports, warnings = _import_project_inputs(
        inputs,
        recursive=recursive,
        config=config,
        document_adapter_id=document_adapter_id,
        original_names=original_names,
        adapter_options=adapter_options,
    )
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
    state_records: list[dict[str, Any]] = []
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
                state_records.append(
                    record_header(
                        "document_adapter_state",
                        project_id,
                        record_id=f"DOCUMENT-{file_id}",
                        adapter_id=adapter.adapter_id,
                        adapter_version=adapter.version,
                        file_id=file_id,
                        state=item.opaque_state,
                    )
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
            part_ids = item.segment_part_ids
            if part_ids is None:
                raise ProjectError("导入文件缺少 segment_part_ids")
            model_sources = item.model_sources
            if model_sources is not None and len(model_sources) != len(item.segments):
                raise ProjectError("导入文件 model_sources 与 Segment 数量不一致")
            for line_index, source in enumerate(item.segments):
                segment_id = f"{file_id}-S{line_index + 1:06d}"
                fields: dict[str, object] = {
                    "segment_id": segment_id,
                    "file_id": file_id,
                    "line_index": line_index,
                    "part_id": part_ids[line_index],
                    "source": source,
                    "is_empty": source == "" or source.isspace(),
                }
                if model_sources is not None and model_sources[line_index] is not None:
                    fields["model_source"] = model_sources[line_index]
                added_segments.append(
                    record_header(
                        "source_segment",
                        project_id,
                        record_id=segment_id,
                        **fields,
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
        for file_record in added_files:
            relative = Path(str(file_record["stored_name"]))
            destination = project / "input" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging / "input" / relative, destination)
            moved_inputs.append(destination)
        retained_states = [
            state
            for existing in files
            if (state := read_adapter_state(project, str(existing["file_id"])))
            is not None
        ]
        replace_source(
            project,
            new_files,
            new_segments,
            new_metadata,
            [*retained_states, *state_records],
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
        for file_id in file_ids:
            relative = Path(str(known[file_id]["stored_name"]))
            source = project / "input" / relative
            if source.exists():
                held = staging / "removed-input" / relative
                held.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, held)
                moved_inputs.append((held, source))
        retained_states = [
            state
            for file_record in new_files
            if (state := read_adapter_state(project, str(file_record["file_id"])))
            is not None
        ]
        replace_source(
            project,
            new_files,
            new_segments,
            new_metadata,
            retained_states,
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


def delete_project(
    project: Path,
    *,
    protected_roots: Iterable[Path] = (),
) -> dict[str, object]:
    """Delete one complete, self-contained project directory."""
    root = project.resolve()
    if not database_path(root).is_file():
        raise ProjectError(f"项目不存在或无效：{project}")
    protected = {
        PROJECTS_ROOT.resolve(),
        APP_ROOT.resolve(),
        user_root().resolve(),
    }
    protected.update(Path(value).resolve() for value in protected_roots)
    if root in protected or root.parent == root:
        raise ProjectError("不能删除项目根目录")
    running = _running_run_ids(root)
    if running:
        raise UsageError(
            f"存在未完成 Run，不能删除项目：{', '.join(running)}"
        )
    metadata = read_project_meta(root)
    try:
        shutil.rmtree(root)
    except OSError as exc:
        raise ProjectError(f"删除项目失败：{root}: {exc}") from exc
    return {
        "deleted": True,
        "project_id": metadata.get("project_id"),
        "name": metadata.get("name"),
        "path": str(root),
    }


def resolve_project(value: str, projects_root: Path = PROJECTS_ROOT) -> Path:
    direct = Path(value)
    path = direct if direct.is_dir() else projects_root / value
    if not database_path(path).is_file():
        raise ProjectError(f"项目不存在或无效：{value}")
    return path.resolve()


def resolve_project_parent(
    value: str | Path, *, require_absolute: bool = False
) -> Path:
    path = Path(value).expanduser()
    if require_absolute and not path.is_absolute():
        raise UsageError("项目父目录必须是绝对路径")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise UsageError(f"项目父目录不存在或无法访问：{path}: {exc}") from exc
    if not resolved.is_dir():
        raise UsageError(f"项目父目录不是目录：{resolved}")
    if not os.access(resolved, os.W_OK | os.X_OK):
        raise UsageError(f"项目父目录不可写：{resolved}")
    return resolved


def sync_global_templates(
    project: Path,
    *,
    dry_run: bool = False,
    app_root: Path = APP_ROOT,
    interactive: bool | None = None,
    choice: str | None = None,
) -> list[str]:
    metadata = read_project_meta(project)
    try:
        load_config(effective_path("config/config.toml", builtin_root=app_root))
        current_hash = bundle_hash(app_root)
    except ConfigError as exc:
        return [f"全局模板无效，继续使用项目副本：{exc}"]
    if metadata.get("global_bundle_hash_seen") == current_hash:
        return []
    changed = []
    for relative, global_path in _bundle_source(app_root).items():
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
        load_config(effective_path("config/config.toml", builtin_root=app_root))
        timestamp = utc_now().replace(":", "").replace("-", "")
        backup = project / "snapshots" / "template_updates" / timestamp
        backup.mkdir(parents=True, exist_ok=False)
        shutil.copy2(project / "config.toml", backup / "config.toml")
        shutil.copytree(project / "prompts", backup / "prompts")
        _copy_bundle(app_root, project)
        warnings.append(f"已更新项目模板；备份位于 {backup}")
    else:
        warnings.append("已保留项目模板")
    metadata["global_bundle_hash_seen"] = current_hash
    write_json(project, project / "project.json", metadata)
    return warnings


def load_source_files(project: Path) -> list[dict[str, object]]:
    files = read_sqlite_files(project)
    return _resolve_file_adapters(files)


def load_segments(project: Path) -> list[dict[str, object]]:
    return _load_segment_records(project)


def _load_segment_records(
    project: Path,
    *,
    include_model_contract: bool = True,
) -> list[dict[str, object]]:
    segments = read_sqlite_segments(project)
    if any(
        not isinstance(segment.get("part_id"), str)
        or not segment["part_id"]
        for segment in segments
    ):
        raise ProjectError(
            "项目 Segment 缺少有效 part_id；请重新创建项目"
        )
    if not include_model_contract:
        return segments
    files = read_sqlite_files(project)
    state_by_file: dict[str, list[dict[str, Any]]] = {}
    for file_record in files:
        state_path = file_record.get("document_adapter_state")
        if not isinstance(state_path, str):
            continue
        state = read_adapter_state(project, str(file_record.get("file_id")))
        if not isinstance(state, dict):
            raise IncompleteError(
                f"Document Adapter 状态缺失：{file_record.get('file_id')}"
            )
        if isinstance(state.get("state"), dict):
            state = state["state"]
        locators = state.get("locators")
        if isinstance(locators, list):
            state_by_file[str(file_record.get("file_id"))] = locators
    by_file: dict[str, list[dict[str, Any]]] = {}
    for segment in segments:
        by_file.setdefault(str(segment.get("file_id")), []).append(segment)
    for file_id, items in by_file.items():
        locators = state_by_file.get(file_id)
        if locators is None or len(locators) != len(items):
            continue
        for segment, locator in zip(
            sorted(items, key=lambda value: int(value["line_index"])),
            locators,
            strict=True,
        ):
            if isinstance(locator, dict):
                slot = locator.get("slot")
                formats = slot.get("formats") if isinstance(slot, dict) else None
                if isinstance(formats, list):
                    segment["_format_markers"] = formats
    return segments
