from __future__ import annotations
from pathlib import Path
from typing import Any
from .documents import (
    DocumentExportJob,
    compact_emphasis_aozora,
    document_adapter_reads_version,
    publish_document_exports,
)
from .errors import (
    ExportError,
    UsageError,
)
from .execution import (
    classify_stage,
    load_stage_history,
)
from .logging_utils import get_logger
from .plugins import (
    get_document_adapter,
)
from .sqlite_storage import (
    read_json,
)

from .stage_runtime import (_project_context, _require_nonempty_segments, _restore_leading_whitespace)

def export_project(
    project: Path,
    export_stage: str,
    *,
    bilingual: bool,
    allow_missing: bool,
    output_format: str = "original",
    file_ids: list[str] | None = None,
) -> dict[str, Any]:
    if export_stage not in {"translated", "proofread", "polished"}:
        raise UsageError(f"不支持的导出阶段：{export_stage}")
    if output_format not in {"original", "txt"}:
        raise UsageError(f"不支持的导出格式：{output_format}")
    logger = get_logger("export")
    config, _, files, segments = _project_context(project)
    if file_ids is not None:
        if not file_ids:
            raise UsageError("导出文件范围不能为空")
        if len(file_ids) != len(set(file_ids)):
            raise UsageError("导出文件 ID 不能重复")
        known_file_ids = {str(item["file_id"]) for item in files}
        unknown = [
            file_id for file_id in file_ids if file_id not in known_file_ids
        ]
        if unknown:
            raise UsageError(f"未知文件 ID：{', '.join(unknown)}")
        selected_file_ids = set(file_ids)
        files = [
            item for item in files if str(item["file_id"]) in selected_file_ids
        ]
        segments = [
            item
            for item in segments
            if str(item["file_id"]) in selected_file_ids
        ]
    _require_nonempty_segments(segments)
    stage_name = {
        "translated": "translation",
        "proofread": "proofreading_applied",
        "polished": "polishing_applied",
    }[export_stage]
    histories = {
        stage: load_stage_history(project, stage)
        for stage in (
            "translation",
            "proofreading",
            "proofreading_applied",
            "polishing",
            "polishing_applied",
        )
    }
    primary = classify_stage(
        [],
        histories[stage_name],
        force=False,
    ).latest_completed
    translation = classify_stage(
        [], histories["translation"], force=False
    ).latest_completed
    proofread = classify_stage(
        [], histories["proofreading_applied"], force=False
    ).latest_completed
    records_by_id = {
        str(record["record_id"]): record
        for history in histories.values()
        for record in history
        if record.get("record_id")
    }

    def result_lineage(record: dict[str, Any]) -> list[dict[str, Any]]:
        lineage: list[dict[str, Any]] = []
        pending = [record]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            record_id = str(current.get("record_id", ""))
            if record_id in seen:
                continue
            seen.add(record_id)
            lineage.append(current)
            for key in ("base_result_id", "suggestion_result_id"):
                parent = records_by_id.get(str(current.get(key, "")))
                if parent is not None:
                    pending.append(parent)
        return lineage

    fallback_records: list[str] = []
    missing: list[str] = []
    output_text: dict[str, str] = {}
    validation_warnings = 0
    used_fingerprints: set[str] = set()
    for segment in segments:
        if segment["is_empty"]:
            continue
        segment_id = str(segment["segment_id"])
        record = primary.get(segment_id)
        if record is None and allow_missing:
            if export_stage == "polished":
                record = proofread.get(segment_id) or translation.get(segment_id)
            elif export_stage == "proofread":
                record = translation.get(segment_id)
            if record is None:
                output_text[segment_id] = str(segment["source"])
            else:
                output_text[segment_id] = str(record["text"])
            fallback_records.append(segment_id)
        elif record is None:
            missing.append(segment_id)
        else:
            output_text[segment_id] = str(record["text"])
        if segment_id in output_text:
            output_text[segment_id] = _restore_leading_whitespace(
                str(segment["source"]),
                output_text[segment_id],
            )
            if segment.get("_ruby_mode") in {
                "aozora",
                "short_xml",
                "compact",
            }:
                output_text[segment_id] = compact_emphasis_aozora(
                    output_text[segment_id]
                )
        if record is not None:
            lineage = result_lineage(record)
            if any(
                item.get("validation_status") == "warning" for item in lineage
            ):
                validation_warnings += 1
            used_fingerprints.update(
                str(item["stage_fingerprint"])
                for item in lineage
                if item.get("stage_fingerprint")
            )
    if missing:
        raise ExportError(
            f"导出缺少 {export_stage} 结果：{', '.join(missing[:10])}",
            reason="missing_stage_results",
            stage=export_stage,
            count=len(missing),
            segment_ids=missing[:10],
        )

    directory = (
        project / "output" / "bilingual" / export_stage
        if bilingual
        else project / "output" / export_stage
    )
    required_capability = (
        "bilingual_export" if bilingual else "translated_export"
    )
    segments_by_file: dict[str, list[dict[str, Any]]] = {}
    for segment in segments:
        segments_by_file.setdefault(str(segment["file_id"]), []).append(segment)
    jobs: list[DocumentExportJob] = []
    for file_record in files:
        source_adapter_id = str(file_record["document_adapter_id"])
        adapter_id = "txt" if output_format == "txt" else source_adapter_id
        adapter = get_document_adapter(adapter_id)
        if required_capability not in adapter.capabilities:
            raise ExportError(
                f"Document Adapter 不支持此导出模式：{adapter_id} "
                f"{required_capability}",
                reason="adapter_capability_missing",
                adapter_id=adapter_id,
                capability=required_capability,
            )
        opaque_state = None
        export_file = dict(file_record)
        if output_format == "txt":
            export_file["original_name"] = str(
                Path(str(file_record["original_name"])).with_suffix(".txt")
            )
        else:
            project_version = str(file_record["document_adapter_version"])
            if not document_adapter_reads_version(adapter, project_version):
                raise ExportError(
                    f"Document Adapter 版本不兼容：文件 "
                    f"{file_record['file_id']} 使用 {project_version}，"
                    f"当前 {adapter.version}",
                    reason="adapter_version_incompatible",
                    file_id=str(file_record["file_id"]),
                    adapter_id=adapter_id,
                    project_version=project_version,
                    current_version=adapter.version,
                )
            state_path = file_record.get("document_adapter_state")
            if state_path is not None:
                state_record = read_json(project, project / str(state_path))
                if (
                    state_record.get("adapter_id") != adapter_id
                    or str(state_record.get("adapter_version"))
                    != project_version
                    or state_record.get("file_id")
                    not in {None, file_record["file_id"]}
                    or not isinstance(state_record.get("state"), dict)
                ):
                    raise ExportError(
                        f"Document Adapter 状态损坏或版本不匹配："
                        f"{file_record['file_id']}",
                        reason="adapter_state_invalid",
                        file_id=str(file_record["file_id"]),
                        adapter_id=adapter_id,
                    )
                opaque_state = state_record["state"]
        jobs.append(
            DocumentExportJob(
                adapter=adapter,
                file=export_file,
                segments=segments_by_file[str(file_record["file_id"])],
                opaque_state=opaque_state,
            )
        )
    encoding = str(config["project"]["output_encoding"])
    written = publish_document_exports(
        jobs,
        project=project,
        directory=directory,
        output_text=output_text,
        bilingual=bilingual,
        output_encoding=encoding,
        target_language=str(config["project"]["target_language"]),
        target_language_tag=str(config["project"]["target_language_tag"]),
    )
    for path in written:
        logger.info("file written path=%s", path)
    logger.info(
        "export complete stage=%s files=%d fallback_segments=%d",
        export_stage,
        len(written),
        len(fallback_records),
    )
    return {
        "stage": export_stage,
        "bilingual": bilingual,
        "format": output_format,
        "selected_file_ids": [str(item["file_id"]) for item in files],
        "files": len(written),
        "written": written,
        "fallback_segments": fallback_records,
        "validation_warnings": validation_warnings,
        "mixed_fingerprints": len(used_fingerprints) > 1,
        "output_encoding": encoding,
    }
