from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .config import load_project_config
from .errors import TermGroupError, UsageError
from .execution import stage_fingerprint, stage_result_path
from .locking import project_write_lock
from .plugins import normalize_model_text
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
        text = normalize_model_text(self.files, segment, text, "translation")
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
            suggested_text = normalize_model_text(
                self.files,
                self._require_segment(segment_id),
                suggested_text,
                str(stage),
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
                "group_claims": list(raw_conflicts.get("group_claims", [])),
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
                    "group_primary": (
                        None
                        if disabled
                        else override.get("group_primary", term.get("group_primary"))
                    ),
                    "disabled": disabled,
                    "conflicts": conflicts,
                    "has_conflicts": not disabled
                    and bool(
                        conflicts["categories"]
                        or conflicts["preferred_translations"]
                        or conflicts["alias_primaries"]
                        or conflicts["group_claims"]
                    ),
                }
            )
        by_normalized = {item["normalized"]: item for item in rows}

        def group_root(item: dict[str, Any]) -> str:
            primary = item["group_primary"]
            return (
                primary
                if primary is not None and primary in by_normalized
                else item["normalized"]
            )

        group_conflicts: dict[str, bool] = {}
        for item in rows:
            root = group_root(item)
            group_conflicts[root] = group_conflicts.get(root, False) or bool(
                item["has_conflicts"]
            )

        rows.sort(
            key=lambda item: (
                not group_conflicts[group_root(item)],
                item["disabled"],
                group_root(item),
                item["normalized"] != group_root(item),
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

    @staticmethod
    def _term_forms(term: dict[str, Any]) -> list[tuple[str, str]]:
        return [
            ("source", str(term["source"])),
            *[("alias", str(alias)) for alias in term.get("aliases", [])],
        ]

    @staticmethod
    def _term_group_root(
        term: dict[str, Any], by_normalized: dict[str, dict[str, Any]]
    ) -> str:
        primary = term.get("group_primary")
        return (
            str(primary)
            if primary and str(primary) in by_normalized
            else str(term["normalized"])
        )

    def _related_term_rows(
        self,
        rows: list[dict[str, Any]],
        selected: dict[str, Any],
        *,
        exclude_same_group: bool = True,
    ) -> list[dict[str, Any]]:
        """Find deterministic source/alias containment candidates."""
        spec = term_normalization(self.config)
        by_normalized = {str(item["normalized"]): item for item in rows}
        selected_root = self._term_group_root(selected, by_normalized)
        roots = {
            str(item["normalized"]): self._term_group_root(item, by_normalized)
            for item in rows
        }
        component_sizes: Counter[str] = Counter(roots.values())
        components: dict[str, list[dict[str, Any]]] = {}
        for item in rows:
            components.setdefault(roots[str(item["normalized"])], []).append(item)
        selected_component = components[selected_root]
        selected_has_claim = any(
            item.get("conflicts", {}).get("group_claims")
            for item in selected_component
        )

        selected_forms = [
            (kind, value, normalize_term(value, spec))
            for kind, value in self._term_forms(selected)
        ]
        selected_forms = [item for item in selected_forms if item[2]]
        candidates: list[dict[str, Any]] = []
        for candidate in rows:
            if candidate.get("disabled"):
                continue
            candidate_normalized = str(candidate["normalized"])
            if candidate_normalized == str(selected["normalized"]):
                continue
            candidate_root = roots[candidate_normalized]
            if exclude_same_group and candidate_root == selected_root:
                continue
            candidate_component = components[candidate_root]
            candidate_has_claim = any(
                item.get("conflicts", {}).get("group_claims")
                for item in candidate_component
            )
            best: tuple[Any, ...] | None = None
            best_match: dict[str, Any] | None = None
            candidate_forms = [
                (kind, value, normalize_term(value, spec))
                for kind, value in self._term_forms(candidate)
            ]
            candidate_forms = [item for item in candidate_forms if item[2]]
            for selected_kind, selected_value, selected_normalized in selected_forms:
                for candidate_kind, candidate_value, candidate_normalized_value in candidate_forms:
                    if selected_normalized == candidate_normalized_value:
                        continue
                    if (
                        len(selected_normalized) >= 2
                        and selected_normalized in candidate_normalized_value
                    ):
                        relation = "contains_selected"
                        contained_length = len(selected_normalized)
                        length_delta = len(candidate_normalized_value) - contained_length
                    elif (
                        len(candidate_normalized_value) >= 2
                        and candidate_normalized_value in selected_normalized
                    ):
                        relation = "contained_by_selected"
                        contained_length = len(candidate_normalized_value)
                        length_delta = len(selected_normalized) - contained_length
                    else:
                        continue
                    sort_key = (
                        0 if relation == "contains_selected" else 1,
                        int(selected_kind != "source" or candidate_kind != "source"),
                        length_delta,
                        -contained_length,
                        candidate_normalized,
                    )
                    if best is None or sort_key < best:
                        best = sort_key
                        best_match = {
                            "relation": relation,
                            "selected_match": selected_value,
                            "selected_match_type": selected_kind,
                            "related_match": candidate_value,
                            "related_match_type": candidate_kind,
                        }
            if best is None or best_match is None:
                continue
            blocked_reason: str | None = None
            if selected_has_claim or candidate_has_claim:
                blocked_reason = "group_claim"
            elif (
                component_sizes[selected_root] > 1
                and component_sizes[candidate_root] > 1
            ):
                blocked_reason = "cross_group"
            candidates.append(
                {
                    "normalized": candidate_normalized,
                    "source": candidate["source"],
                    "preferred_translation": candidate.get("preferred_translation"),
                    "group_primary": candidate.get("group_primary"),
                    "group_root_normalized": candidate_root,
                    "group_root_source": by_normalized[candidate_root]["source"],
                    "group_size": component_sizes[candidate_root],
                    "disabled": False,
                    "has_conflicts": bool(candidate.get("has_conflicts")),
                    "can_group": blocked_reason is None,
                    "can_convert_alias": blocked_reason is None,
                    "blocked_reason": blocked_reason,
                    "sort_key": best,
                    **best_match,
                }
            )
        candidates.sort(key=lambda item: item.pop("sort_key"))
        return candidates

    def related_terms(
        self, normalized: str, *, limit: int = 20
    ) -> dict[str, Any]:
        if not normalized:
            raise UsageError("术语推荐查询必须提供 normalized")
        if limit < 1 or limit > 100:
            raise UsageError("术语推荐数量参数无效")
        rows = self.terms()["terms"]
        selected = next(
            (item for item in rows if item["normalized"] == normalized), None
        )
        if selected is None or selected.get("disabled"):
            raise UsageError(f"术语不存在或已移除：{normalized}")
        return {
            "normalized": normalized,
            "related": self._related_term_rows(rows, selected)[:limit],
        }

    def group_related_terms(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("confirm") is not True:
            raise UsageError("必须明确确认建立术语组")
        normalized = payload.get("normalized")
        related_normalized = payload.get("related_normalized")
        primary_normalized = payload.get("primary_normalized")
        if not all(
            isinstance(value, str) and value
            for value in (normalized, related_normalized, primary_normalized)
        ):
            raise UsageError("normalized、related_normalized 和 primary_normalized 必须是非空字符串")
        with project_write_lock(self.project):
            rows = self.terms()["terms"]
            by_normalized = {str(item["normalized"]): item for item in rows}
            selected = by_normalized.get(normalized)
            candidate = by_normalized.get(related_normalized)
            if selected is None or candidate is None or selected.get("disabled") or candidate.get("disabled"):
                raise TermGroupError(
                    "相关推荐条目不存在或已移除",
                    reason="stale_recommendation",
                    normalized=normalized,
                    related_normalized=related_normalized,
                )
            related = next(
                (
                    item
                    for item in self._related_term_rows(
                        rows, selected, exclude_same_group=False
                    )
                    if item["normalized"] == related_normalized
                ),
                None,
            )
            if related is None:
                raise TermGroupError(
                    "相关推荐关系已失效",
                    reason="stale_recommendation",
                    normalized=normalized,
                    related_normalized=related_normalized,
                )
            if not related["can_group"]:
                raise TermGroupError(
                    "相关推荐条目不能快捷加入术语组",
                    reason=(
                        "group_collision"
                        if related["blocked_reason"] == "cross_group"
                        else str(related["blocked_reason"] or "group_collision")
                    ),
                    normalized=normalized,
                    related_normalized=related_normalized,
                )
            roots = {
                str(item["normalized"]): self._term_group_root(item, by_normalized)
                for item in rows
            }
            selected_root = roots[normalized]
            related_root = roots[related_normalized]
            if selected_root == related_root:
                raise TermGroupError(
                    "两个术语已经属于同一术语组",
                    reason="same_group",
                    normalized=normalized,
                    related_normalized=related_normalized,
                )
            selected_component = {
                key for key, root in roots.items() if root == selected_root
            }
            related_component = {
                key for key, root in roots.items() if root == related_root
            }
            if any(
                by_normalized[key].get("conflicts", {}).get("group_claims")
                for key in selected_component | related_component
            ):
                raise TermGroupError(
                    "术语组仍有未裁决争用",
                    reason="group_claim",
                    normalized=normalized,
                    related_normalized=related_normalized,
                )
            if len(selected_component) > 1 and len(related_component) > 1:
                raise TermGroupError(
                    "不能快捷合并两个已有术语组",
                    reason="group_collision",
                    normalized=normalized,
                    related_normalized=related_normalized,
                )
            allowed_primaries = {selected_root, related_root}
            if primary_normalized not in allowed_primaries:
                raise TermGroupError(
                    "组主必须是当前条目、相关推荐条目或已有组主",
                    reason="invalid_primary",
                    normalized=normalized,
                    related_normalized=related_normalized,
                )
            component = selected_component | related_component
            library = load_terms(self.project)
            current = {
                str(item["normalized"]): dict(item)
                for item in (library or {}).get("terms", [])
            }
            overrides_document = read_json(
                self.project, self.project / "terminology" / "overrides.json"
            )
            overrides = {
                str(item["normalized"]): dict(item)
                for item in overrides_document.get("overrides", [])
            }
            for key in component:
                primary = None if key == primary_normalized else primary_normalized
                if key not in current:
                    raise TermGroupError(
                        "术语组成员不在当前发布库中",
                        reason="missing_entry",
                        normalized=key,
                    )
                current[key]["group_primary"] = primary
                override = overrides.get(
                    key,
                    {"normalized": key, "source": current[key].get("source", key)},
                )
                overrides[key] = {**override, "group_primary": primary}
            result = self._publish_terms(
                library, current, overrides, origin="web_group_related"
            )
            result["group_primary"] = primary_normalized
            return result

    def convert_related_to_alias(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("confirm") is not True:
            raise UsageError("必须明确确认将术语转为别名")
        normalized = payload.get("normalized")
        related_normalized = payload.get("related_normalized")
        if not isinstance(normalized, str) or not normalized:
            raise UsageError("normalized 必须是非空字符串")
        if not isinstance(related_normalized, str) or not related_normalized:
            raise UsageError("related_normalized 必须是非空字符串")
        with project_write_lock(self.project):
            rows = self.terms()["terms"]
            by_normalized = {str(item["normalized"]): item for item in rows}
            selected = by_normalized.get(normalized)
            candidate = by_normalized.get(related_normalized)
            if selected is None or candidate is None or selected.get("disabled") or candidate.get("disabled"):
                raise TermGroupError(
                    "相关推荐条目不存在或已移除",
                    reason="stale_recommendation",
                    normalized=normalized,
                    related_normalized=related_normalized,
                )
            related = next(
                (
                    item
                    for item in self._related_term_rows(rows, selected)
                    if item["normalized"] == related_normalized
                ),
                None,
            )
            if related is None:
                raise TermGroupError(
                    "相关推荐关系已失效",
                    reason="stale_recommendation",
                    normalized=normalized,
                    related_normalized=related_normalized,
                )
            roots = {
                str(item["normalized"]): self._term_group_root(item, by_normalized)
                for item in rows
            }
            selected_root = roots[normalized]
            selected_component = {
                key for key, root in roots.items() if root == selected_root
            }
            candidate_component = {
                key for key, root in roots.items() if root == roots[related_normalized]
            }
            if candidate.get("group_primary") is not None or len(candidate_component) > 1:
                raise TermGroupError(
                    "有成员的术语不能快捷转为别名",
                    reason="candidate_has_members",
                    normalized=related_normalized,
                )
            if any(
                by_normalized[key].get("conflicts", {}).get("group_claims")
                for key in selected_component | candidate_component
            ):
                raise TermGroupError(
                    "术语组仍有未裁决争用",
                    reason="group_claim",
                    normalized=normalized,
                    related_normalized=related_normalized,
                )
            spec = term_normalization(self.config)
            existing = [selected["source"], *selected.get("aliases", [])]
            existing_normalized = {normalize_term(value, spec) for value in existing}
            group_source_normalized = {
                normalize_term(by_normalized[key]["source"], spec)
                for key in selected_component
            }
            active_external_sources = {
                normalize_term(item["source"], spec): str(item["normalized"])
                for item in rows
                if not item.get("disabled")
                and str(item["normalized"]) not in selected_component
                and str(item["normalized"]) != related_normalized
            }
            additions: list[str] = []
            seen = set(existing_normalized)
            for value in [candidate["source"], *candidate.get("aliases", [])]:
                normalized_value = normalize_term(value, spec)
                if not normalized_value or normalized_value in seen or normalized_value in group_source_normalized:
                    continue
                if normalized_value in active_external_sources:
                    raise TermGroupError(
                        "待转移 alias 与其他启用主条目冲突",
                        reason="alias_collision",
                        normalized=normalized,
                        related_normalized=related_normalized,
                        alias=value,
                        claimed_by=active_external_sources[normalized_value],
                    )
                seen.add(normalized_value)
                additions.append(str(value))
            library = load_terms(self.project)
            current = {
                str(item["normalized"]): dict(item)
                for item in (library or {}).get("terms", [])
            }
            overrides_document = read_json(
                self.project, self.project / "terminology" / "overrides.json"
            )
            overrides = {
                str(item["normalized"]): dict(item)
                for item in overrides_document.get("overrides", [])
            }
            target_override = overrides.get(
                normalized,
                {"normalized": normalized, "source": selected["source"]},
            )
            target_aliases = [*selected.get("aliases", []), *additions]
            overrides[normalized] = {**target_override, "aliases": target_aliases}
            current[normalized]["aliases"] = target_aliases
            candidate_override = overrides.get(
                related_normalized,
                {"normalized": related_normalized, "source": candidate["source"]},
            )
            candidate_override = {**candidate_override, "disabled": True}
            candidate_override.pop("group_primary", None)
            overrides[related_normalized] = candidate_override
            current.pop(related_normalized, None)
            result = self._publish_terms(
                library, current, overrides, origin="web_convert_related_alias"
            )
            result["converted"] = related_normalized
            result["aliases_added"] = additions
            return result

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
        isolated_term = dict(term)
        isolated_term["group_primary"] = None
        isolated_conflicts = dict(term.get("conflicts") or {})
        isolated_conflicts["group_claims"] = []
        isolated_term["conflicts"] = isolated_conflicts
        library = {"terms": [isolated_term]}
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
            self.project / "terminology" / "overrides.json",
            override_record,
        )
        write_json(
            self.project,
            self.project / "terminology" / "terms.json",
            term_record,
        )
        return self.terms()

    def _terms_edit_prepare(
        self, payload: dict[str, Any]
    ) -> tuple[
        tuple[str, ...],
        dict[str, Any] | None,
        dict[str, dict[str, Any]],
        dict[str, dict[str, Any]],
    ]:
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
        return values, library, current, overrides

    def remove_terms(self, payload: dict[str, Any]) -> dict[str, Any]:
        with project_write_lock(self.project):
            values, library, current, overrides = self._terms_edit_prepare(payload)
            self._reject_group_primary_removal(values, current)
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
                overrides[normalized].pop("group_primary", None)
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
            values, library, current, overrides = self._terms_edit_prepare(payload)
            self._reject_group_primary_removal(values, current)
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

    @staticmethod
    def _reject_group_primary_removal(
        values: tuple[str, ...], current: dict[str, dict[str, Any]]
    ) -> None:
        for normalized in values:
            members = [
                key
                for key, term in current.items()
                if term.get("group_primary") == normalized
            ]
            if members:
                raise TermGroupError(
                    "组主仍有成员，不能移除或删除",
                    reason="primary_has_members",
                    normalized=normalized,
                    members=members,
                )

    def materialize_term(self, payload: dict[str, Any]) -> dict[str, Any]:
        with project_write_lock(self.project):
            normalized = payload.get("normalized")
            alias = payload.get("alias")
            if not isinstance(normalized, str) or not normalized:
                raise UsageError("normalized 必须是非空字符串")
            if not isinstance(alias, str) or not alias.strip():
                raise UsageError("alias 必须是非空字符串")
            spec = term_normalization(self.config)
            rows = {item["normalized"]: item for item in self.terms()["terms"]}
            owner = rows.get(normalized)
            if owner is None or owner["disabled"]:
                raise UsageError(f"术语不存在或已移除：{normalized}")
            alias_value = next(
                (
                    value
                    for value in owner["aliases"]
                    if normalize_term(value, spec) == normalize_term(alias, spec)
                ),
                None,
            )
            if alias_value is None:
                raise UsageError("alias 不属于指定术语")
            target_normalized = normalize_term(alias_value, spec)
            primary = owner.get("group_primary") or normalized
            target = rows.get(target_normalized)
            if target is not None:
                if target["disabled"]:
                    raise TermGroupError(
                        "目标条目已移除，不能加入术语组",
                        reason="target_disabled",
                        normalized=target_normalized,
                    )
                target_primary = target.get("group_primary") or target_normalized
                target_has_members = any(
                    item.get("group_primary") == target_normalized
                    for item in rows.values()
                )
                if target_primary != primary and (
                    target.get("group_primary") is not None or target_has_members
                ):
                    raise TermGroupError(
                        "目标条目属于其他术语组",
                        reason="cross_group",
                        normalized=target_normalized,
                        group_primary=target_primary,
                    )

            library = load_terms(self.project)
            current = {
                str(item["normalized"]): dict(item)
                for item in (library or {}).get("terms", [])
            }
            overrides_document = read_json(
                self.project, self.project / "terminology" / "overrides.json"
            )
            overrides = {
                str(item["normalized"]): dict(item)
                for item in overrides_document.get("overrides", [])
            }
            owner_override = overrides.get(
                normalized,
                {"normalized": normalized, "source": owner["source"]},
            )
            owner_aliases = [
                value
                for value in owner["aliases"]
                if normalize_term(value, spec) != target_normalized
            ]
            overrides[normalized] = {**owner_override, "aliases": owner_aliases}
            if target is None:
                target_source = alias_value
                current[target_normalized] = {
                    "source": target_source,
                    "normalized": target_normalized,
                    "category": None,
                    "description": "",
                    "preferred_translation": None,
                    "aliases": [],
                    "group_primary": primary,
                    "conflicts": {},
                }
                overrides[target_normalized] = {
                    "normalized": target_normalized,
                    "source": target_source,
                    "category": None,
                    "description": None,
                    "preferred_translation": None,
                    "aliases": [],
                    "group_primary": primary,
                    "disabled": False,
                }
            elif target_primary != primary:
                current[target_normalized]["group_primary"] = primary
                target_override = overrides.get(
                    target_normalized,
                    {"normalized": target_normalized, "source": target["source"]},
                )
                overrides[target_normalized] = {
                    **target_override,
                    "group_primary": primary,
                }
            result = self._publish_terms(
                library, current, overrides, origin="web_materialize_term"
            )
            result["materialized"] = target_normalized
            return result

    def set_term_primary(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("confirm") is not True:
            raise UsageError("必须明确确认更换术语组主")
        with project_write_lock(self.project):
            normalized = payload.get("normalized")
            if not isinstance(normalized, str) or not normalized:
                raise UsageError("normalized 必须是非空字符串")
            library = load_terms(self.project)
            current = {
                str(item["normalized"]): dict(item)
                for item in (library or {}).get("terms", [])
            }
            if normalized not in current:
                raise UsageError(f"术语不存在：{normalized}")
            component = {normalized}
            changed = True
            while changed:
                changed = False
                roots = {
                    current[key].get("group_primary") or key
                    for key in component
                }
                for key, term in current.items():
                    claims = term.get("conflicts", {}).get("group_claims", [])
                    related = {
                        str(value.get("entry")) for value in claims
                    } | {str(value.get("claimed_by")) for value in claims}
                    if (
                        key in roots
                        or term.get("group_primary") in roots
                        or component & related
                    ) and key not in component:
                        component.add(key)
                        changed = True
            if len(component) == 1 and current[normalized].get("group_primary") is None:
                return self.terms()
            overrides_document = read_json(
                self.project, self.project / "terminology" / "overrides.json"
            )
            overrides = {
                str(item["normalized"]): dict(item)
                for item in overrides_document.get("overrides", [])
            }
            for key in component:
                primary = None if key == normalized else normalized
                current[key]["group_primary"] = primary
                override = overrides.get(
                    key,
                    {"normalized": key, "source": current[key].get("source", key)},
                )
                overrides[key] = {**override, "group_primary": primary}
            return self._publish_terms(
                library, current, overrides, origin="web_set_term_primary"
            )

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
        original_normalized = str(old_normalized or normalized)
        existing_group_primary = current.get(original_normalized, {}).get(
            "group_primary"
        )
        if disabled:
            self._reject_group_primary_removal((original_normalized,), current)
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
            if current.get(old_normalized, {}).get("group_primary") is None:
                for member_key, member in current.items():
                    if member.get("group_primary") != old_normalized:
                        continue
                    member["group_primary"] = normalized
                    member_override = overrides.get(
                        member_key,
                        {"normalized": member_key, "source": member.get("source", member_key)},
                    )
                    overrides[member_key] = {
                        **member_override,
                        "group_primary": normalized,
                    }
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
        if existing_group_primary is not None and not disabled:
            override["group_primary"] = existing_group_primary
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
                "group_primary": existing_group_primary,
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
