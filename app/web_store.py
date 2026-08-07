from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .config import load_project_config
from .documents import normalize_document_output
from .errors import UsageError
from .execution import stage_fingerprint, stage_result_path
from .locking import project_write_lock
from .project import load_source_files
from .sqlite_storage import (
    append_jsonl,
    get_segment,
    latest_stage_results,
    new_record_id,
    query_segment_neighbors,
    query_segments,
    read_json,
    read_jsonl,
    read_segment_sources,
    record_exists,
    record_header,
    segment_count,
    segment_ids,
    write_json,
)
from .stages import (
    build_term_library_rows,
    load_terms,
    match_terms,
    normalize_term,
    prompt_middle_digests,
    term_normalization,
    validate_translation_text,
)

REVIEW_STAGES = {"proofreading", "polishing"}


class WebStore:
    """Web 工作台使用的项目读写与视图存储。"""

    def __init__(self, project: Path):
        self.project = project
        self.config = load_project_config(project)
        self.metadata = read_json(project, project / "project.json")
        self.files = load_source_files(project)

    @property
    def project_id(self) -> str:
        return str(self.metadata["project_id"])

    def _history(
        self, stage: str, segment_ids_filter: list[str] | None = None
    ) -> dict[str, dict[str, Any]]:
        return {
            segment_id: record
            for segment_id, record in latest_stage_results(
                self.project, stage, segment_ids_filter
            ).items()
            if record.get("status") != "reset"
        }

    def _terms_revision(self) -> int | None:
        library = load_terms(self.project)
        return int(library["terms_revision"]) if library else None

    def _fingerprint(self, stage: str) -> str:
        if stage.endswith("_applied"):
            return stage_fingerprint(
                self.config,
                stage,
                None,
                apply_semantics={
                    "review_stage": stage.removesuffix("_applied"),
                    "allow_outdated_base": False,
                },
            )
        return stage_fingerprint(
            self.config,
            stage,
            prompt_middle_digests(self.project, stage),
            terms_revision=self._terms_revision(),
        )

    def _require_segment(self, segment_id: object) -> dict[str, Any]:
        if not isinstance(segment_id, str):
            raise UsageError(f"未知或空 Segment：{segment_id}")
        segment = get_segment(self.project, segment_id)
        if segment is None or segment.get("is_empty"):
            raise UsageError(f"未知或空 Segment：{segment_id}")
        return segment

    def _normalize_text(
        self, segment: dict[str, Any], text: str, stage: str
    ) -> str:
        file_record = next(
            item
            for item in self.files
            if str(item["file_id"]) == str(segment["file_id"])
        )
        from .plugins import get_document_adapter

        adapter = get_document_adapter(str(file_record["document_adapter_id"]))
        return normalize_document_output(
            adapter, segment=segment, text=text, stage=stage
        )

    def _base_results(
        self, stage: str, segment_ids_filter: list[str] | None = None
    ) -> dict[str, dict[str, Any]]:
        translations = self._history("translation", segment_ids_filter)
        if stage == "proofreading":
            return translations
        applied = self._history("proofreading_applied", segment_ids_filter)
        return {**translations, **applied}

    def _review_view(
        self,
        stage: str,
        segment_id: str,
        histories: dict[str, dict[str, dict[str, Any]]],
    ) -> dict[str, Any]:
        base = histories["translation"].get(segment_id)
        if stage == "polishing":
            base = histories["proofreading_applied"].get(segment_id) or base
        suggestion = histories[stage].get(segment_id)
        applied = histories[f"{stage}_applied"].get(segment_id)
        return {
            "base": self._result_view(base),
            "suggestion": self._result_view(suggestion),
            "applied": self._result_view(applied),
            "outdated": bool(
                base
                and suggestion
                and suggestion.get("base_result_id") != base.get("record_id")
            ),
            "applied_current": bool(
                suggestion
                and applied
                and applied.get("suggestion_result_id")
                == suggestion.get("record_id")
            ),
        }

    def _stage_errors(
        self, stage: str, segment_ids_filter: list[str] | None = None
    ) -> dict[str, dict[str, Any]]:
        latest = self._history(stage, segment_ids_filter)
        return {
            segment_id: {
                "error_class": str(record.get("error_class") or "stage_error"),
                "error_message": str(record.get("error_message") or "阶段请求失败")[:240],
                "run_id": record.get("run_id"),
                "request_id": record.get("request_id"),
                "created_at": record.get("created_at"),
            }
            for segment_id, record in latest.items()
            if record.get("status") == "failed"
        }

    def terminology_scan(self) -> dict[str, Any]:
        active_path = self.project / "terminology" / "active_task.json"
        active = (
            read_json(self.project, active_path)
            if record_exists(self.project, active_path)
            else None
        )
        base = {
            "active_task_id": None,
            "status": active.get("status", "none") if active else "none",
            "completed": 0,
            "failed": 0,
            "pending": segment_count(self.project),
            "candidate_count": 0,
            "candidate_records": 0,
            "failure_counts": {},
            "failed_segments": [],
            "failed_segments_truncated": False,
        }
        if not active or active.get("status") != "active":
            return base
        task_id = str(active.get("active_task_id", ""))
        base["active_task_id"] = task_id
        scans = [
            record
            for record in read_jsonl(
                self.project,
                self.project / "terminology" / "scans.jsonl",
                task_id=task_id,
            )
            if record.get("segment_id")
        ]
        latest: dict[str, dict[str, Any]] = {}
        for record in scans:
            latest[str(record["segment_id"])] = record
        completed = {
            segment_id
            for segment_id, record in latest.items()
            if record.get("status") == "completed"
        }
        failed_records = {
            segment_id: record
            for segment_id, record in latest.items()
            if record.get("status") == "failed"
        }
        counts = Counter(
            str(record.get("error_class") or "scan_error")
            for record in failed_records.values()
        )
        failed_segments = [
            {
                "segment_id": segment_id,
                "error_class": str(record.get("error_class") or "scan_error"),
                "error_message": str(record.get("error_message") or "术语扫描失败")[:240],
                "run_id": record.get("run_id"),
                "request_id": record.get("request_id"),
            }
            for segment_id, record in sorted(failed_records.items())
        ]
        candidate_records = [
            record
            for record in read_jsonl(
                self.project,
                self.project / "terminology" / "candidates.jsonl",
                task_id=task_id,
            )
        ]
        candidate_sources = {
            normalize_term(
                str(term.get("source")), term_normalization(self.config)
            )
            for record in candidate_records
            for term in record.get("terms", [])
            if isinstance(term, dict) and term.get("source")
        }
        base.update(
            {
                "completed": len(completed),
                "failed": len(failed_records),
                "pending": max(0, segment_count(self.project) - len(completed) - len(failed_records)),
                "candidate_count": len(candidate_sources),
                "candidate_records": len(candidate_records),
                "failure_counts": dict(counts),
                "failed_segments": failed_segments[:200],
                "failed_segments_truncated": len(failed_segments) > 200,
            }
        )
        return base

    def overview(
        self,
        *,
        offset: int = 0,
        limit: int = 100,
        file_id: str | None = None,
        status: str | None = None,
        search: str | None = None,
        stage: str = "translation",
    ) -> dict[str, Any]:
        if stage not in {"translation", "proofreading", "polishing"}:
            raise UsageError("overview stage 无效")
        if status not in {None, "completed", "failed", "pending", "warning"}:
            raise UsageError("overview status 无效")
        window = query_segments(
            self.project,
            offset=offset,
            limit=limit,
            file_id=file_id,
            status=status,
            search=search,
            stage=stage,
        )
        window_ids = [str(item["segment_id"]) for item in window]
        histories = {
            target: self._history(target, window_ids)
            for target in (
                "translation",
                "proofreading",
                "proofreading_applied",
                "polishing",
                "polishing_applied",
            )
        }
        stage_errors = {
            target: {
                segment_id: record
                for segment_id, record in self._stage_errors(target, window_ids).items()
                if segment_id in window_ids
            }
            for target in ("translation", "proofreading", "polishing")
        }
        files = [
            {
                "file_id": item["file_id"],
                "file_order": item["file_order"],
                "name": item["original_name"],
                "document_adapter_id": item["document_adapter_id"],
            }
            for item in sorted(self.files, key=lambda value: int(value["file_order"]))
        ]
        segments = []
        for item in window:
            segment_id = str(item["segment_id"])
            segments.append(
                {
                    "segment_id": segment_id,
                    "file_id": item["file_id"],
                    "part_id": item["part_id"],
                    "line_index": item["line_index"],
                    "source": item["source"],
                    "model_source": item.get("model_source"),
                    "format_count": len(item.get("_format_markers", [])),
                    "completed": {
                        stage: segment_id in history
                        for stage, history in histories.items()
                    },
                    "stage_errors": {
                        stage: stage_errors[stage].get(segment_id)
                        for stage in stage_errors
                        if segment_id in stage_errors[stage]
                    },
                    "translation": self._result_view(
                        histories["translation"].get(segment_id)
                    ),
                    "reviews": {
                        stage: self._review_view(stage, segment_id, histories)
                        for stage in sorted(REVIEW_STAGES)
                    },
                }
            )
        return {
            "name": self.metadata["name"],
            "path": str(self.project),
            "nonempty_segment_count": segment_count(self.project),
            "completed_segments": segment_count(
                self.project,
                file_id=file_id,
                status="completed",
                search=search,
                stage=stage,
            ),
            "total_segments": segment_count(
                self.project,
                file_id=file_id,
                status=status,
                search=search,
                stage=stage,
            ),
            "offset": offset,
            "limit": limit,
            "stage": stage,
            "files": files,
            "segments": segments,
        }

    def segment_index(
        self,
        *,
        file_id: str | None = None,
        status: str | None = None,
        search: str | None = None,
        stage: str = "translation",
    ) -> dict[str, Any]:
        if stage not in {"translation", "proofreading", "polishing"}:
            raise UsageError("segment index stage 无效")
        if status not in {None, "completed", "failed", "pending", "warning"}:
            raise UsageError("segment index status 无效")
        values = segment_ids(
            self.project,
            file_id=file_id,
            status=status,
            search=search,
            stage=stage,
        )
        return {"segment_ids": values, "total": len(values), "stage": stage}

    def segment_detail(self, segment_id: str) -> dict[str, Any]:
        segment = self._require_segment(segment_id)
        before_segments, after_segments = query_segment_neighbors(
            self.project,
            file_id=str(segment["file_id"]),
            part_id=str(segment["part_id"]),
            line_index=int(segment["line_index"]),
        )
        context_segments = [*before_segments, *after_segments]
        segment_filter = [
            segment_id,
            *(str(item["segment_id"]) for item in context_segments),
        ]
        histories = {
            stage: self._history(stage, segment_filter)
            for stage in (
                "translation",
                "proofreading",
                "proofreading_applied",
                "polishing",
                "polishing_applied",
            )
        }
        detail = self._segment_detail_view(segment, histories)
        detail["context"] = {
            "before": [
                self._segment_detail_view(item, histories)
                for item in before_segments
            ],
            "after": [
                self._segment_detail_view(item, histories)
                for item in after_segments
            ],
        }
        return detail

    def _segment_detail_view(
        self,
        segment: dict[str, Any],
        histories: dict[str, dict[str, dict[str, Any]]],
    ) -> dict[str, Any]:
        segment_id = str(segment["segment_id"])
        return {
            "segment_id": segment_id,
            "file_id": segment["file_id"],
            "part_id": segment["part_id"],
            "line_index": segment["line_index"],
            "source": segment["source"],
            "model_source": segment.get("model_source"),
            "format_count": len(segment.get("_format_markers", [])),
            "translation": self._result_view(
                histories["translation"].get(segment_id)
            ),
            "reviews": {
                stage: self._review_view(stage, segment_id, histories)
                for stage in sorted(REVIEW_STAGES)
            },
        }

    @staticmethod
    def _result_view(record: dict[str, Any] | None) -> dict[str, Any] | None:
        if record is None:
            return None
        return {
            key: record.get(key)
            for key in (
                "record_id",
                "text",
                "review_status",
                "suggested_text",
                "reason",
                "base_result_id",
                "validation_status",
                "created_at",
                "origin",
            )
        }

    def save_translation(self, payload: dict[str, Any]) -> dict[str, Any]:
        with project_write_lock(self.project):
            return self._save_translation(payload)

    def _save_translation(self, payload: dict[str, Any]) -> dict[str, Any]:
        segment_id = payload.get("segment_id")
        self._require_segment(segment_id)
        text = payload.get("text")
        if not isinstance(text, str):
            raise UsageError("译文必须是字符串")
        segment = self._require_segment(segment_id)
        text = self._normalize_text(segment, text, "translation")
        findings = validate_translation_text(
            text, self.config["validation"]["translation"]
        )
        record = record_header(
            "stage_result",
            self.project_id,
            stage="translation",
            segment_id=segment_id,
            status="completed",
            text=text,
            validation_status="warning" if findings else "passed",
            validation_findings=findings,
            stage_fingerprint=self._fingerprint("translation"),
            terms_revision=self._terms_revision(),
            run_id=None,
            request_id=None,
            origin="web",
        )
        append_jsonl(self.project, stage_result_path(self.project, "translation"), record)
        return self._result_view(record) or {}

    def save_review(self, payload: dict[str, Any]) -> dict[str, Any]:
        with project_write_lock(self.project):
            return self._save_review(payload)

    def _save_review(self, payload: dict[str, Any]) -> dict[str, Any]:
        stage = payload.get("stage")
        if stage not in REVIEW_STAGES:
            raise UsageError(f"不支持的建议阶段：{stage}")
        segment_id = payload.get("segment_id")
        self._require_segment(segment_id)
        base = self._base_results(stage, [str(segment_id)]).get(str(segment_id))
        if base is None:
            raise UsageError("当前 Segment 缺少可用基准结果")
        review_status = payload.get("review_status")
        if review_status not in {"accepted", "suggested"}:
            raise UsageError("建议状态必须是 accepted 或 suggested")
        suggested_text = payload.get("suggested_text")
        if review_status == "suggested":
            if not isinstance(suggested_text, str) or not suggested_text:
                raise UsageError("suggested 状态需要非空建议文本")
            suggested_text = self._normalize_text(
                self._require_segment(segment_id), suggested_text, str(stage)
            )
        else:
            suggested_text = None
        reason = payload.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise UsageError("原因必须是字符串或 null")
        suggestion = record_header(
            "stage_result",
            self.project_id,
            stage=stage,
            segment_id=segment_id,
            status="completed",
            review_status=review_status,
            suggested_text=suggested_text,
            reason=reason or None,
            base_result_id=base["record_id"],
            stage_fingerprint=self._fingerprint(stage),
            terms_revision=self._terms_revision(),
            run_id=None,
            request_id=None,
            origin="web",
        )
        append_jsonl(self.project, stage_result_path(self.project, stage), suggestion)
        applied = None
        if payload.get("apply"):
            text = (
                str(suggested_text)
                if review_status == "suggested"
                else str(base["text"])
            )
            applied_stage = f"{stage}_applied"
            applied = record_header(
                "stage_result",
                self.project_id,
                stage=applied_stage,
                segment_id=segment_id,
                status="completed",
                text=text,
                suggestion_result_id=suggestion["record_id"],
                base_result_id=base["record_id"],
                allowed_outdated_base=False,
                stage_fingerprint=self._fingerprint(applied_stage),
                run_id=None,
                request_id=None,
                origin="web",
            )
            append_jsonl(self.project, stage_result_path(self.project, applied_stage), applied)
        return {
            "suggestion": self._result_view(suggestion),
            "applied": self._result_view(applied),
        }

    def reset_results(self, payload: dict[str, Any]) -> dict[str, Any]:
        with project_write_lock(self.project):
            stage = payload.get("stage")
            if stage not in {"translation", *REVIEW_STAGES}:
                raise UsageError(f"不支持的重置阶段：{stage}")
            raw_ids = payload.get("segment_ids")
            if (
                not isinstance(raw_ids, list)
                or not raw_ids
                or not all(isinstance(value, str) for value in raw_ids)
            ):
                raise UsageError("segment_ids 必须是非空字符串数组")
            segment_ids = tuple(dict.fromkeys(raw_ids))
            for segment_id in segment_ids:
                self._require_segment(segment_id)
            stages = [str(stage)]
            if stage in REVIEW_STAGES:
                stages.append(f"{stage}_applied")
            batch_id = new_record_id("RESET")
            cleared_ids: set[str] = set()
            reset_records = 0
            for target_stage in stages:
                current = self._history(target_stage, list(segment_ids))
                for segment_id in segment_ids:
                    if segment_id not in current:
                        continue
                    append_jsonl(
                        self.project,
                        stage_result_path(self.project, target_stage),
                        record_header(
                            "stage_reset",
                            self.project_id,
                            stage=target_stage,
                            segment_id=segment_id,
                            status="reset",
                            reset_batch_id=batch_id,
                            origin="web",
                        ),
                    )
                    cleared_ids.add(segment_id)
                    reset_records += 1
            return {
                "stage": stage,
                "selected": len(segment_ids),
                "cleared": len(cleared_ids),
                "unchanged": len(segment_ids) - len(cleared_ids),
                "reset_records": reset_records,
                "reset_batch_id": batch_id if cleared_ids else None,
            }

    def terms(self) -> dict[str, Any]:
        library = load_terms(self.project)
        current = {
            str(item["normalized"]): dict(item)
            for item in (library or {}).get("terms", [])
        }
        overrides_document = read_json(self.project, self.project / "terminology" / "overrides.json")
        overrides = {
            str(item["normalized"]): dict(item)
            for item in overrides_document.get("overrides", [])
        }
        rows: list[dict[str, Any]] = []
        for normalized in sorted(set(current) | set(overrides)):
            term = current.get(normalized, {})
            override = overrides.get(normalized, {})
            raw_conflicts = term.get("conflicts", {})
            conflicts = {
                "categories": list(raw_conflicts.get("categories", [])),
                "preferred_translations": list(
                    raw_conflicts.get("preferred_translations", [])
                ),
                "alias_primaries": list(
                    raw_conflicts.get("alias_primaries", [])
                ),
            }
            disabled = bool(override.get("disabled", False))
            rows.append(
                {
                    "normalized": normalized,
                    "source": override.get("source", term.get("source", normalized)),
                    "category": override.get("category", term.get("category")),
                    "description": override.get(
                        "description", term.get("description")
                    ),
                    "preferred_translation": override.get(
                        "preferred_translation", term.get("preferred_translation")
                    ),
                    "aliases": override.get("aliases", term.get("aliases", [])),
                    "disabled": disabled,
                    "conflicts": conflicts,
                    "has_conflicts": not disabled
                    and bool(
                        conflicts["categories"]
                        or conflicts["preferred_translations"]
                        or conflicts["alias_primaries"]
                    ),
                }
            )
        rows.sort(
            key=lambda item: (
                not item["has_conflicts"],
                item["disabled"],
                item["normalized"],
            )
        )
        return {
            "terms_revision": (
                int(library["terms_revision"]) if library is not None else None
            ),
            "conflict_count": sum(bool(item["has_conflicts"]) for item in rows),
            "terms": rows,
            "scan": self.terminology_scan(),
        }

    def term_hits(
        self,
        normalized: str,
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        if offset < 0 or limit < 1 or limit > 500:
            raise UsageError("术语命中窗口参数无效")
        term = next(
            (
                item
                for item in self.terms()["terms"]
                if item["normalized"] == normalized
            ),
            None,
        )
        if term is None:
            raise UsageError(f"术语不存在：{normalized}")
        spec = term_normalization(self.config)
        library = {"terms": [term]}
        hits = [
            segment
            for segment in read_segment_sources(self.project)
            if match_terms(
                segment["source"], library=library, limit=1, spec=spec
            )
        ]
        return {
            "normalized": normalized,
            "source": term["source"],
            "total": len(hits),
            "offset": offset,
            "limit": limit,
            "hits": hits[offset : offset + limit],
        }

    @staticmethod
    def _term_value(payload: dict[str, Any], key: str) -> str | None:
        value = payload.get(key)
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise UsageError(f"{key} 必须是字符串")
        return value

    def save_term(self, payload: dict[str, Any]) -> dict[str, Any]:
        with project_write_lock(self.project):
            return self._save_term(payload)

    def _publish_terms(
        self,
        library: dict[str, Any] | None,
        current: dict[str, dict[str, Any]],
        overrides: dict[str, dict[str, Any]],
        *,
        origin: str,
    ) -> dict[str, Any]:
        override_record = record_header(
            "terminology_overrides",
            self.project_id,
            record_id="TERMINOLOGY-OVERRIDES",
            overrides=[overrides[key] for key in sorted(overrides)],
            origin=origin,
        )
        write_json(
            self.project,
            self.project / "terminology" / "overrides.json",
            override_record,
        )
        revision = int(library["terms_revision"]) + 1 if library else 1
        terms = build_term_library_rows(
            self.project,
            [current[key] for key in sorted(current)],
            overrides,
        )
        term_record = record_header(
            "terminology_library",
            self.project_id,
            record_id=f"TERMS-{revision}",
            terms_revision=revision,
            published_run_id=(
                library.get("published_run_id") if library is not None else None
            ),
            active_task_id=(
                library.get("active_task_id") if library is not None else None
            ),
            terms=terms,
            origin=origin,
        )
        write_json(
            self.project,
            self.project / "terminology" / "terms.json",
            term_record,
        )
        return self.terms()

    def remove_terms(self, payload: dict[str, Any]) -> dict[str, Any]:
        with project_write_lock(self.project):
            raw_values = payload.get("normalized")
            if (
                not isinstance(raw_values, list)
                or not raw_values
                or not all(isinstance(value, str) and value for value in raw_values)
            ):
                raise UsageError("normalized 必须是非空字符串数组")
            values = tuple(dict.fromkeys(raw_values))
            library = load_terms(self.project)
            current = {
                str(item["normalized"]): dict(item)
                for item in (library or {}).get("terms", [])
            }
            overrides_path = self.project / "terminology" / "overrides.json"
            overrides_document = read_json(self.project, overrides_path)
            overrides = {
                str(item["normalized"]): dict(item)
                for item in overrides_document.get("overrides", [])
            }
            unknown = [
                value
                for value in values
                if value not in current and value not in overrides
            ]
            if unknown:
                raise UsageError(f"未知术语：{', '.join(unknown[:10])}")
            changed = 0
            for normalized in values:
                override = overrides.get(
                    normalized,
                    {
                        "normalized": normalized,
                        "source": current.get(normalized, {}).get(
                            "source", normalized
                        ),
                    },
                )
                if override.get("disabled"):
                    continue
                overrides[normalized] = {**override, "disabled": True}
                current.pop(normalized, None)
                changed += 1
            if not changed:
                result = self.terms()
            else:
                result = self._publish_terms(
                    library,
                    current,
                    overrides,
                    origin="web",
                )
            result["removed"] = changed
            result["unchanged"] = len(values) - changed
            return result

    def delete_terms(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Remove terms and their disabled overrides so scans may rediscover them."""
        with project_write_lock(self.project):
            raw_values = payload.get("normalized")
            if (
                not isinstance(raw_values, list)
                or not raw_values
                or not all(isinstance(value, str) and value for value in raw_values)
            ):
                raise UsageError("normalized 必须是非空字符串数组")
            values = tuple(dict.fromkeys(raw_values))
            library = load_terms(self.project)
            current = {
                str(item["normalized"]): dict(item)
                for item in (library or {}).get("terms", [])
            }
            overrides_path = self.project / "terminology" / "overrides.json"
            overrides_document = read_json(self.project, overrides_path)
            overrides = {
                str(item["normalized"]): dict(item)
                for item in overrides_document.get("overrides", [])
            }
            unknown = [
                value
                for value in values
                if value not in current and value not in overrides
            ]
            if unknown:
                raise UsageError(f"未知术语：{', '.join(unknown[:10])}")
            deleted = 0
            for normalized in values:
                existed = normalized in current or normalized in overrides
                current.pop(normalized, None)
                overrides.pop(normalized, None)
                deleted += int(existed)
            if not deleted:
                result = self.terms()
            else:
                result = self._publish_terms(
                    library,
                    current,
                    overrides,
                    origin="web_permanent_delete",
                )
            result["deleted"] = deleted
            result["unchanged"] = len(values) - deleted
            return result

    def _save_term(self, payload: dict[str, Any]) -> dict[str, Any]:
        spec = term_normalization(self.config)
        source = payload.get("source")
        if not isinstance(source, str) or not source.strip():
            raise UsageError("术语 source 不能为空")
        normalized = normalize_term(source, spec)
        old_normalized = payload.get("old_normalized")
        if old_normalized is not None and not isinstance(old_normalized, str):
            raise UsageError("old_normalized 类型错误")
        aliases = payload.get("aliases", [])
        if not isinstance(aliases, list) or not all(
            isinstance(alias, str) for alias in aliases
        ):
            raise UsageError("aliases 必须是字符串数组")
        aliases = [
            alias.strip()
            for alias in aliases
            if alias.strip() and normalize_term(alias, spec) != normalized
        ]
        category = self._term_value(payload, "category")
        description = self._term_value(payload, "description")
        preferred = self._term_value(payload, "preferred_translation")
        disabled = bool(payload.get("disabled", False))

        library = load_terms(self.project)
        current = {
            str(item["normalized"]): dict(item)
            for item in (library or {}).get("terms", [])
        }
        overrides_path = self.project / "terminology" / "overrides.json"
        overrides_document = read_json(self.project, overrides_path)
        overrides = {
            str(item["normalized"]): dict(item)
            for item in overrides_document.get("overrides", [])
        }
        conflict_source = current.get(str(old_normalized or normalized), {}).get(
            "conflicts", {}
        )
        if (
            not disabled
            and conflict_source.get("categories")
            and not category
        ):
            raise UsageError("类别冲突尚未裁决")
        if (
            not disabled
            and conflict_source.get("preferred_translations")
            and not preferred
        ):
            raise UsageError("推荐译名冲突尚未裁决")
        if (
            normalized != old_normalized
            and normalized in set(current) | set(overrides)
            and not overrides.get(normalized, {}).get("disabled")
        ):
            raise UsageError(f"normalized source 已存在：{normalized}")
        if old_normalized and old_normalized != normalized:
            current.pop(old_normalized, None)
            old = overrides.get(old_normalized, {"normalized": old_normalized})
            overrides[old_normalized] = {**old, "disabled": True}

        override = {
            "normalized": normalized,
            "source": source,
            "category": category,
            "description": description,
            "preferred_translation": preferred,
            "aliases": aliases,
            "disabled": disabled,
        }
        overrides[normalized] = override
        if disabled:
            current.pop(normalized, None)
        else:
            current[normalized] = {
                "source": source,
                "normalized": normalized,
                "category": category,
                "description": description or "",
                "preferred_translation": preferred,
                "aliases": sorted(set(aliases)),
                "conflicts": {
                    "categories": [],
                    "preferred_translations": [],
                },
            }

        return self._publish_terms(
            library,
            current,
            overrides,
            origin="web",
        )
