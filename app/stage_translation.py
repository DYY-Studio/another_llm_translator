from __future__ import annotations
import asyncio
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
import httpx
from .errors import (
    ContextLengthError,
    EmptyResponseSplitError,
)
from .decision import DecisionClient
from .term_library import (load_terms, term_normalization)
from .term_matching import _TermMatchCache
from .execution import (
    ChunkPlan,
    PreviousContextIndex,
    Scope,
    build_chunk_plans,
    classify_stage_states,
    contiguous_groups,
    segment_model_source,
    segment_model_text,
    select_scope,
    stage_fingerprint,
    stage_result_path,
)
from .llm_client import empty_response_split_scope, SlidingWindowLimiter
from .llm_response import TerminologyResponseMode, parse_jsonl_document
from .llm_keys import KeyPool
from .logging_utils import get_logger
from .plugins import (
    normalize_model_text,
)
from .sqlite_storage import (
    append_jsonl,
    latest_stage_states,
    read_content_summaries,
    record_header,
)
from .summary_provenance import digest, full_summary_context_usable
from .translation_validation import (
    TranslationValidationContext,
    validate_translation_text,
    validate_translation_response,
    SegmentAlignmentValidator,
)

from .stage_runtime import (StageRunState, _SegmentParseResult, _assemble_warnings, _create_or_continue_run, _document_prompt_requirement_helpers, _execute_stage_run, _frozen_run_options, _localized_request_loop, _project_context, _prompt_factory, _prompt_language, _replace_with_runtime_parts, _require_nonempty_segments, _restore_leading_whitespace, _resume_scope, _scope_record, _segment_model_payload_value, _split_oversized_preflight, _split_segment_source, _split_source_once, prompt_middle_digests, _FORMAT_CORRECTION)


def _summary_segment_id(item: dict[str, Any]) -> str:
    return str(item.get("_original_segment_id") or item["segment_id"])


def _has_hard_validation_findings(findings: list[dict[str, Any]]) -> bool:
    return any(
        str(item.get("severity", "error")) == "error" for item in findings
    )

def _parse_translation_items(
    content: str, expected_ids: list[str]
) -> _SegmentParseResult:
    document = parse_jsonl_document(content, record_type="segment")
    counts = Counter(
        item.get("id")
        for item in document.records
        if isinstance(item.get("id"), str)
    )
    expected = set(expected_ids)
    valid: dict[str, str] = {}
    errors: list[str] = list(document.errors)
    for item in document.records:
        segment_id = item.get("id")
        translation = item.get("translation")
        if segment_id not in expected:
            errors.append(f"未知 ID：{segment_id}")
            continue
        if counts[segment_id] != 1 or not isinstance(translation, str):
            errors.append(f"重复或字段错误：{segment_id}")
            continue
        valid[segment_id] = translation
    unresolved = [segment_id for segment_id in expected_ids if segment_id not in valid]
    return _SegmentParseResult(
        valid,
        unresolved,
        errors,
        document.complete and not errors,
        document.has_valid_end,
        counts == Counter(expected_ids),
    )

def _map_local_translation_response(
    content: str,
    id_map: dict[str, str],
) -> _SegmentParseResult:
    result = _parse_translation_items(content, list(id_map))
    return _SegmentParseResult(
        {id_map[local_id]: text for local_id, text in result.valid.items()},
        [id_map[local_id] for local_id in result.unresolved],
        result.errors,
        result.complete,
        result.has_valid_end,
        result.ids_complete,
    )


@dataclass(frozen=True, slots=True)
class _TranslationSummaryContext:
    part_first_ids: dict[tuple[str, str], str]
    previous_parts: dict[tuple[str, str], tuple[str, str] | None]
    full_text: dict[tuple[str, str], str]
    fragments: dict[
        tuple[str, str], tuple[tuple[int, str, str, frozenset[str]], ...]
    ]

    @classmethod
    def empty(cls) -> _TranslationSummaryContext:
        return cls({}, {}, {}, {})

    def for_items(self, items: list[dict[str, Any]]) -> tuple[list[str], str | None]:
        if not items:
            return [], None
        first = items[0]
        boundary = (str(first["file_id"]), str(first["part_id"]))
        if self.part_first_ids.get(boundary) == _summary_segment_id(first):
            previous = self.previous_parts.get(boundary)
            text = self.full_text.get(previous) if previous is not None else None
            return ([text], "previous_only") if text is not None else ([], None)

        first_line = int(first["line_index"])
        candidates = [
            item
            for item in self.fragments.get(boundary, ())
            if item[0] <= first_line
        ]
        if not candidates:
            return [], None
        latest_start = max(item[0] for item in candidates)
        latest = max(
            (item for item in candidates if item[0] == latest_start),
            key=lambda item: (item[1], item[2]),
        )
        current_ids = {_summary_segment_id(item) for item in items}
        summary_ids = latest[3]
        if not summary_ids.intersection(current_ids):
            relation = "previous_only"
        elif current_ids.issubset(summary_ids):
            relation = "contains_all_current"
        else:
            relation = "partial_overlap"
        return [latest[2]], relation


def _translation_summary_context(
    project: Path,
    segments: list[dict[str, Any]],
) -> _TranslationSummaryContext:
    file_order: dict[str, int] = {}
    for item in segments:
        file_order.setdefault(str(item["file_id"]), len(file_order))
    ordered = sorted(
        (item for item in segments if not item["is_empty"]),
        key=lambda item: (
            file_order[str(item["file_id"])],
            int(item["line_index"]),
            str(item["segment_id"]),
        ),
    )
    boundary_segments: dict[tuple[str, str], list[dict[str, Any]]] = {}
    part_order: list[tuple[str, str]] = []
    for item in ordered:
        boundary = (str(item["file_id"]), str(item["part_id"]))
        if boundary not in boundary_segments:
            boundary_segments[boundary] = []
            part_order.append(boundary)
        boundary_segments[boundary].append(item)

    current_by_id = {
        str(item["segment_id"]): item
        for item in ordered
    }
    valid_full: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    valid_fragments: dict[
        tuple[str, str], list[tuple[int, str, str, frozenset[str]]]
    ] = {}
    for summary in read_content_summaries(project, status="completed"):
        if summary.get("status") != "completed":
            continue
        text = summary.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        if bool(summary.get("source_changed")):
            continue
        kind = summary.get("kind")
        if kind not in {"full", "fragment"}:
            continue
        boundary = (str(summary.get("file_id", "")), str(summary.get("part_id", "")))
        current = boundary_segments.get(boundary)
        if kind == "full" and not full_summary_context_usable(summary, current or []):
            continue
        source_range = summary.get("source_range")
        values = source_range.get("segments") if isinstance(source_range, dict) else None
        if (
            not current
            or not isinstance(source_range, dict)
            or source_range.get("file_id") != boundary[0]
            or source_range.get("part_id") != boundary[1]
            or not isinstance(values, list)
            or not values
        ):
            continue

        stable_ids: list[str] = []
        valid_range = True
        for value in values:
            if not isinstance(value, dict):
                valid_range = False
                break
            stable_id = value.get("original_segment_id") or value.get("segment_id")
            if not isinstance(stable_id, str) or not stable_id:
                valid_range = False
                break
            item = current_by_id.get(stable_id)
            if item is None or (
                str(item["file_id"]), str(item["part_id"])
            ) != boundary:
                valid_range = False
                break
            if value.get(
                "original_source_digest", value.get("source_digest")
            ) != digest(str(item["source"])):
                valid_range = False
                break
            if value.get(
                "original_model_text_digest", value.get("model_text_digest")
            ) != digest(segment_model_source(item)):
                valid_range = False
                break
            stable_ids.append(stable_id)
        if not valid_range:
            continue

        current_ids = [str(item["segment_id"]) for item in current]
        unique_ids = list(dict.fromkeys(stable_ids))
        positions = [current_ids.index(stable_id) for stable_id in unique_ids]
        if positions != sorted(positions):
            continue
        if kind == "full" and unique_ids != current_ids:
            continue

        updated = str(
            summary.get("updated_at") or summary.get("created_at") or ""
        )
        record_id = str(summary.get("record_id", ""))
        if kind == "full":
            valid_full.setdefault(boundary, []).append((updated, record_id, text))
        else:
            start = min(int(current_by_id[stable_id]["line_index"]) for stable_id in unique_ids)
            valid_fragments.setdefault(boundary, []).append(
                (start, updated + "\x00" + record_id, text, frozenset(unique_ids))
            )

    full_text = {
        boundary: max(values, key=lambda item: (item[0], item[1]))[2]
        for boundary, values in valid_full.items()
    }
    fragments = {
        boundary: tuple(values)
        for boundary, values in valid_fragments.items()
    }
    part_first_ids = {
        boundary: str(values[0]["segment_id"])
        for boundary, values in boundary_segments.items()
    }
    previous_parts = {
        boundary: (part_order[index - 1] if index else None)
        for index, boundary in enumerate(part_order)
    }
    return _TranslationSummaryContext(
        part_first_ids=part_first_ids,
        previous_parts=previous_parts,
        full_text=full_text,
        fragments=fragments,
    )

async def run_translation(
    project: Path,
    scope: Scope,
    *,
    http_client: httpx.AsyncClient | None = None,
    limiter: SlidingWindowLimiter | KeyPool | None = None,
    resume_run_id: str | None = None,
    reuse_mixed_fingerprints: bool = False,
    prompt_language: str | None = None,
    on_progress: Callable[[int, int, int], None] | None = None,
    on_usage: Callable[[dict[str, Any] | None], None] | None = None,
    _draft_terminology: bool = False,
    _include_summaries: bool = False,
    _on_draft_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    run_stage = "terminology" if _draft_terminology else "translation"
    logger = get_logger(run_stage)
    preparation_started_at = time.perf_counter()
    scope, resume_arguments_ignored = _resume_scope(project, scope, resume_run_id)
    context_kwargs: dict[str, object] = {"stage": run_stage}
    frozen_run_options = _frozen_run_options(project, resume_run_id)
    if frozen_run_options is not None:
        context_kwargs["frozen_run_options"] = frozen_run_options
    if _draft_terminology:
        from .stage_terminology_draft import draft_run_context

        config, metadata, files, segments = draft_run_context(
            project, frozen_run_options, include_summaries=_include_summaries
        )
    else:
        config, metadata, files, segments = _project_context(project, **context_kwargs)
    logger.info(
        "stage preparation context ready elapsed=%.3fs files=%d segments=%d",
        time.perf_counter() - preparation_started_at,
        len(files),
        len(segments),
    )
    _require_nonempty_segments(segments)
    translation_validators = config["_translation_validator_instances"]
    language = _prompt_language(project, "translation", prompt_language)
    prompt_factory = _prompt_factory(project, "translation", language)
    prompt = prompt_factory(())
    requirements_for_items, prompt_partition_key = (
        _document_prompt_requirement_helpers(config, language)
    )
    prompt_for_items = lambda items: prompt_factory(requirements_for_items(items))
    library = load_terms(project)
    terms_revision = int(library["terms_revision"]) if library else None
    fingerprint = stage_fingerprint(
        config,
        "translation",
        prompt_middle_digests(project, "translation"),
        terms_revision=terms_revision,
    )
    if _draft_terminology:
        from .stage_terminology_draft import (
            DraftTerminologyScan,
            draft_translation_fingerprint,
        )
        from .stage_runtime import _prompt_language_for_stages

        language = _prompt_language_for_stages(
            project,
            prompt_language,
            ("terminology", "translation")
            + (("fragment_summary",) if _include_summaries else ()),
        )
        fingerprint = draft_translation_fingerprint(project, config, terms_revision)
    selected_segments = (
        [item for item in segments if not item["is_empty"]]
        if _draft_terminology and scope.force
        else select_scope(segments, files, scope)
    )
    active_segment_ids = [
        str(segment["segment_id"])
        for segment in segments
        if not segment["is_empty"]
    ]
    history_states = latest_stage_states(
        project,
        "translation",
        active_segment_ids,
    )
    selection = classify_stage_states(
        selected_segments,
        history_states,
        force=scope.force,
    )
    draft_scan = None
    translation_selection = selection
    if _draft_terminology:
        draft_scan = DraftTerminologyScan(
            project,
            metadata,
            config,
            scope,
            resume_run_id,
            language,
            selected_segments,
            {str(item["segment_id"]) for item in selection.reusable}
            if not scope.force
            else set(),
        )
        work_ids = {str(item["segment_id"]) for item in draft_scan.work}
        selection = replace(
            selection,
            work=tuple(draft_scan.work),
            reusable=tuple(
                item
                for item in selection.selected
                if str(item["segment_id"]) not in work_ids
            ),
        )
        prompt = draft_scan.prompt(
            list(selection.work)
            or [{"_draft_terms": True, "_draft_translation": True}],
            (),
        )
        prompt_for_items = lambda items: draft_scan.prompt(
            items, draft_scan.requirements(items)
        )
        prompt_partition_key = lambda item: (
            draft_scan.requirements([item]),
            bool(item.get("_draft_terms")),
            bool(item.get("_draft_translation")),
            bool(item.get("_draft_summary")),
            (item["file_id"], item["part_id"]) if _include_summaries else None,
        )
    logger.info(
        "stage preparation selection/history ready elapsed=%.3fs selected=%d requested=%d reusable=%d",
        time.perf_counter() - preparation_started_at,
        len(selection.selected),
        len(selection.work),
        len(selection.reusable),
    )
    usage: dict[str, Any] | None = None
    warnings = _assemble_warnings(
        stage="translation",
        resume_run_id=resume_run_id,
        resume_arguments_ignored=resume_arguments_ignored,
        resume_message=(
            f"续用 Run {resume_run_id} 的原始范围；本次使用当前 config 和 Prompt"
        ),
        config=config,
        fingerprint=fingerprint,
        existing_fingerprints=translation_selection.fingerprints,
        reusable_count=len(translation_selection.reusable),
        force=scope.force,
        reuse_allowed=reuse_mixed_fingerprints,
        dry_run=scope.dry_run,
        extra=(
            ["没有已发布术语库；本次翻译 terms_revision = null"]
            if library is None
            else []
        ),
    )
    if draft_scan is not None:
        warnings.extend(
            _assemble_warnings(
                stage="terminology",
                resume_run_id=resume_run_id,
                resume_arguments_ignored=resume_arguments_ignored,
                resume_message="续用原始术语粗翻范围",
                config=config,
                fingerprint=draft_scan.term_fingerprint,
                existing_fingerprints=draft_scan.term_fingerprints,
                reusable_count=len(draft_scan.term_done),
                force=scope.force,
                reuse_allowed=reuse_mixed_fingerprints,
                dry_run=scope.dry_run,
                extra=[],
            )
        )
    latest_text = {
        segment_id: str(record["text"])
        for segment_id, record in selection.latest_completed.items()
    }
    context_config = config["context"]["translation"]
    context_index = PreviousContextIndex(segments)
    summary_context_index = _TranslationSummaryContext.empty()
    if (
        context_config.get("previous_summaries", False)
        and "translation" not in config["chunking"]["cross_boundary_batching"]
    ):
        summary_context_index = _translation_summary_context(project, segments)
    term_match_cache = _TermMatchCache(
        library,
        term_normalization(config),
        int(config["terminology"]["max_terms_per_segment"]),
    )

    def payload_builder(items: list[dict[str, Any]]) -> dict[str, Any]:
        resolver = None
        if config["execution"]["scheduling_mode"] == "ordered_by_file":
            resolver = latest_text.get
        context = (
            context_index.previous(
                items[0],
                context_config["previous_segments"],
                target_resolver=resolver,
                target_transform=segment_model_text,
                source_key="model_source",
            )
            if context_config["enabled"]
            else []
        )
        if config["execution"]["scheduling_mode"] == "parallel":
            context = [item["source"] for item in context]
        summary_context, summary_context_relation = summary_context_index.for_items(items)
        payload = {
            "target_language": config["project"]["target_language"],
            "reference_context": context,
            "summary_context": summary_context,
            "summary_context_relation": summary_context_relation,
            "terms": _segment_model_payload_value(
                items[0], term_match_cache.for_items(items)
            ),
            "segments": [
                {
                    "id": item["segment_id"],
                    "source": segment_model_source(item),
                }
                for item in items
            ],
        }

        if draft_scan is not None:
            payload["response_mode"] = draft_scan.mode(items).value
            if items[0].get("_draft_terms") or items[0].get("_draft_summary"):
                payload["source_segments"] = (
                    [
                        {"id": str(index), "text": segment_model_source(item)}
                        for index, item in enumerate(items, 1)
                    ]
                    if items[0].get("_draft_translation")
                    or items[0].get("_draft_summary")
                    else [segment_model_source(item) for item in items]
                )
            if not items[0].get("_draft_translation"):
                del payload["segments"]
        return payload

    run_id, run_dir, continuation_index, fail_planning = _create_or_continue_run(
        project,
        run_stage,
        scope=scope,
        config=config,
        fingerprint=fingerprint,
        prompt=prompt,
        resume_run_id=resume_run_id,
        selected_count=len(selection.selected),
        requested_count=len(selection.work),
        reused_count=len(selection.reusable),
        details={
            "terms_revision": terms_revision,
            "scope": _scope_record(scope, force_all=_draft_terminology and scope.force),
            **(
                {
                    "include_draft_translation": True,
                    "include_summaries": _include_summaries,
                    "summary_selection": [
                        {"file_id": file_id, "part_id": part_id}
                        for file_id, part_id in sorted(draft_scan.summary_boundaries)
                    ],
                    "active_task_id": draft_scan.task_id,
                }
                if draft_scan
                else {}
            ),
            "prompt_language": language,
        },
        warnings=warnings,
    )

    if draft_scan is not None and not scope.dry_run:
        draft_scan.start(run_id)

    preflight = _split_oversized_preflight(
        selection.work,
        config=config,
        prompt=prompt,
        payload_builder=payload_builder,
        prompt_builder=prompt_for_items,
        fail_planning=fail_planning,
        make_probe=lambda segment, part: {
            **_split_segment_source(
                segment, f"{segment['segment_id']}-PROBE", part
            ),
        },
        split_part=lambda part: list(_split_source_once(part)),
        accept_part=lambda segment, part_id, part: _split_segment_source(
            segment, part_id, part
        ),
    )
    request_segments = preflight.request_segments
    part_original = preflight.part_original
    original_parts = preflight.original_parts
    preflight_failed = preflight.preflight_failed
    logger.info(
        "stage preparation preflight complete elapsed=%.3fs requested=%d failed=%d fast=%d exact=%d",
        time.perf_counter() - preparation_started_at,
        len(request_segments),
        len(preflight_failed),
        preflight.fast_checked,
        preflight.exact_checked,
    )

    if scope.dry_run:
        plans = build_chunk_plans(
            request_segments,
            all_segments=segments,
            config=config,
            stage=run_stage,
            prompt=prompt,
            payload_builder=payload_builder,
            prompt_builder=prompt_for_items,
            partition_key=prompt_partition_key,
        )
        logger.info(
            "stage plan selected=%d requested=%d reused=%d chunks=%d",
            len(selection.selected),
            len(selection.work),
            len(selection.reusable),
            len(plans),
        )
        return {
            "stage": "translation",
            "dry_run": True,
            "resume_run_id": resume_run_id,
            "selected": len(selection.selected),
            "requested": len(selection.work),
            "reused": len(selection.reusable),
            "chunks": len(plans),
            "estimated_input_tokens": sum(
                plan.estimated_input_tokens for plan in plans
            ),
            "warnings": warnings,
        }

    assert run_id is not None and run_dir is not None
    decision_clients = {
        preset_id: DecisionClient(definition, http_client=http_client, retry=config["retry"],
            debug_directory=run_dir if config["debug"]["enabled"] else None,
            project_id=metadata["project_id"], run_id=run_id, stage=run_stage)
        for preset_id, definition in config.get("_decision_preset_definitions", {}).items()
    }
    decision_bindings = config.get("_decision_validator_presets", {})
    decision_client = decision_clients.get(decision_bindings.get("preferred_term_usage"))
    alignment_options = config["validation"]["translation"]["alignment"]
    translation_validators = tuple(
        SegmentAlignmentValidator(decision_clients[decision_bindings["segment_alignment"]],
            confidence_threshold=alignment_options["confidence_threshold"],
            tail_segments=alignment_options["tail_segments"])
        if validator.validator_id == "segment_alignment" else validator
        for validator in translation_validators
    )

    result_path = stage_result_path(project, "translation")
    write_lock = asyncio.Lock()
    validation_pending: dict[str, dict[str, Any]] = {}
    advisory_repair_attempted: set[str] = set()
    failed_ids: set[str] = set()
    failure_counts: Counter[str] = Counter()
    if draft_scan is not None:
        draft_scan.failure_counts = failure_counts
    completed_ids: set[str] = set()
    by_id = {str(item["segment_id"]): item for item in segments}
    by_id.update(
        {str(item["segment_id"]): item for item in request_segments}
    )
    part_results: dict[str, dict[str, tuple[str, str]]] = {}

    def validation_context(
        segment_id: str, translation: str
    ) -> TranslationValidationContext:
        original_id = part_original.get(segment_id)
        item = by_id[original_id or segment_id]
        return TranslationValidationContext(
            source=str(item["source"]),
            translation=translation,
            terms=term_match_cache.validation_matches_for_item(item),
            decision=decision_client if segment_id not in part_original else None,
            decision_confidence_threshold=config["validation"]["translation"]["decision_confidence_threshold"],
            segment_id=original_id or segment_id,
            previous_source=tuple(entry["source"] for entry in context_index.previous(
                item, config["validation"]["translation"]["decision_previous_segments"],
            )) if config["validation"]["translation"]["decision_context_enabled"] else (),
        )

    def report_progress() -> None:
        if draft_scan is not None:
            if _on_draft_progress is not None:
                _on_draft_progress(draft_scan.progress())
            if on_progress is not None:
                on_progress(
                    len(draft_scan.completed()),
                    len(draft_scan.failed()),
                    len(selection.selected),
                )
            return
        if on_progress is not None:
            on_progress(
                len(selection.reusable) + len(completed_ids),
                len(failed_ids),
                len(selection.selected),
            )

    report_progress()

    async def save_completed(
        segment_id: str,
        text: str,
        request_id: str,
        *,
        validation_status: str = "passed",
        findings: list[dict[str, Any]] | None = None,
    ) -> None:
        text = _restore_leading_whitespace(
            str(by_id[segment_id]["source"]),
            text,
        )
        async with write_lock:
            append_jsonl(
                project,
                result_path,
                record_header(
                    "stage_result",
                    str(metadata["project_id"]),
                    stage="translation",
                    segment_id=segment_id,
                    status="completed",
                    text=text,
                    validation_status=validation_status,
                    validation_findings=findings or [],
                    stage_fingerprint=fingerprint,
                    terms_revision=terms_revision,
                    **(
                        {"generation_origin": "terminology_draft"} if draft_scan else {}
                    ),
                    run_id=run_id,
                    request_id=request_id,
                ),
            )
        completed_ids.add(segment_id)
        if draft_scan is not None:
            draft_scan.translation_done.add(segment_id)
            draft_scan.translation_failed.discard(segment_id)
        report_progress()
        latest_text[segment_id] = text

    async def save_failed(
        segment_id: str,
        request_id: str,
        error_class: str,
        message: str,
        *,
        candidate: str | None = None,
        findings: list[dict[str, Any]] | None = None,
    ) -> None:
        segment_id = part_original.get(segment_id, segment_id)
        if segment_id in failed_ids:
            return
        failure_counts[error_class] += 1
        async with write_lock:
            append_jsonl(
                project,
                result_path,
                record_header(
                    "stage_result",
                    str(metadata["project_id"]),
                    stage="translation",
                    segment_id=segment_id,
                    status="failed",
                    text=None,
                    candidate_text=candidate,
                    validation_findings=findings or [],
                    error_class=error_class,
                    error_message=message,
                    stage_fingerprint=fingerprint,
                    terms_revision=terms_revision,
                    **(
                        {"generation_origin": "terminology_draft"} if draft_scan else {}
                    ),
                    run_id=run_id,
                    request_id=request_id,
                ),
            )
        if draft_scan is not None:
            draft_scan.translation_failed.add(part_original.get(segment_id, segment_id))
        failed_ids.add(segment_id)
        report_progress()
        logger.warning(
            "segment failed segment=%s class=%s completed=%d failed=%d",
            segment_id,
            error_class,
            len(completed_ids),
            len(failed_ids),
        )

    def prepare_candidate(segment_id: str, text: Any) -> str:
        return normalize_model_text(files, by_id[segment_id], str(text), "translation")

    response_candidates: dict[str, tuple[str, str]] = {}
    response_groups: list[set[str]] = []

    async def accept_response(
        group: list[dict[str, Any]], candidates: dict[str, tuple[str, Any]], *, complete: bool = True,
    ) -> None:
        owners = {part_original.get(str(item["segment_id"]), str(item["segment_id"])) for item in group}
        # A split Segment connects candidate groups until its complete text is available.
        retained = []
        for existing in response_groups:
            if existing & owners:
                owners |= existing
            else:
                retained.append(existing)
        response_groups[:] = retained
        for item in group:
            segment_id = str(item["segment_id"])
            if segment_id not in candidates:
                continue
            request_id, text = candidates[segment_id]
            original_id = part_original.get(segment_id)
            if original_id is None:
                response_candidates[segment_id] = (text, request_id)
            else:
                part_results.setdefault(original_id, {})[segment_id] = (text, request_id)
                expected = original_parts[original_id]
                if all(part_id in part_results[original_id] for part_id in expected):
                    response_candidates[original_id] = (
                        "".join(part_results[original_id][part_id][0] for part_id in expected),
                        part_results[original_id][expected[-1]][1],
                    )
        if not complete or not owners <= response_candidates.keys():
            response_groups.append(owners)
            return
        ordered = sorted(owners, key=lambda segment_id: segment_order[segment_id])
        contexts = tuple(validation_context(segment_id, response_candidates[segment_id][0]) for segment_id in ordered)
        findings = await validate_translation_response(contexts, translation_validators)
        all_findings = [finding for values in findings.values() for finding in values]
        hard = _has_hard_validation_findings(all_findings)
        repairable = any(finding.get("repairable", True) for finding in all_findings)
        for segment_id in ordered:
            text, request_id = response_candidates.pop(segment_id)
            if findings:
                own = findings.get(segment_id)
                if not own:
                    own = [{"validator": "response_gate", "match_type": "validation_blocked",
                            "severity": "error" if hard else "advisory",
                            "start": 0 if hard else None, "end": len(text) if hard else None,
                            **({"matched_text": text} if hard else {}),
                            **({"repairable": False} if not repairable else {})}]
                validation_pending[segment_id] = {"segment": by_id[segment_id], "candidate": text,
                                                  "findings": own, "request_id": request_id}
            else:
                validation_pending.pop(segment_id, None)
                await save_completed(segment_id, text, request_id)

    segment_order = {str(item["segment_id"]): index for index, item in enumerate(segments)}

    state = StageRunState(
        project=project,
        stage=run_stage,
        config=config,
        metadata=metadata,
        segments=segments,
        prompt=prompt,
        fingerprint=fingerprint,
        resume_run_id=resume_run_id,
        warnings=warnings,
        run_id=run_id,
        run_dir=run_dir,
        decisions=tuple(decision_clients.values()),
        continuation_index=continuation_index,
        on_usage=on_usage,
        preparation_started_at=preparation_started_at,
    )

    async def save_external_error(
        expected: list[str], request_id: str, message: str
    ) -> None:
        for segment_id in expected:
            await save_failed(
                segment_id,
                request_id,
                "external_error",
                message,
            )

    async def process_once(
        chunk: ChunkPlan,
        initial_parent_request_id: str | None = None,
    ) -> None:
        group = list(chunk.segments)
        if draft_scan is not None:
            await draft_scan.process(
                group,
                state=state,
                payload_builder=payload_builder,
                prompt_builder=prompt_for_items,
                accept=accept_response,
                prepare_candidate=prepare_candidate,
                save_failed=save_failed,
                part_original=part_original,
                original_parts=original_parts,
                report_progress=report_progress,
                parent_request_id=initial_parent_request_id,
                by_id=by_id,
                record_context_failure=record_context_failure,
            )
            return
        exhausted = await _localized_request_loop(
            group,
            payload_builder=payload_builder,
            prompt=prompt_for_items(group),
            config=config,
            llm=state.llm,
            stage="translation",
            accept=None,
            accept_response=accept_response,
            prepare_candidate=prepare_candidate,
            save_error=save_external_error,
            parse=_map_local_translation_response,
            format_correction=_FORMAT_CORRECTION[language],
            prompt_language=language,
            by_id=by_id,
            segments=segments,
            prompt_partition_key=prompt_partition_key,
            logger=logger,
            initial_parent_request_id=initial_parent_request_id,
        )
        for segment_id in exhausted:
            await save_failed(
                segment_id,
                f"REQ-{uuid.uuid4().hex[:12].upper()}",
                "format_error",
                "格式修正次数耗尽",
            )

    async def repair_group(
        group: list[dict[str, Any]],
        subset: dict[str, dict[str, Any]],
        parent_request_id: str | None = None,
    ) -> None:
        try:
            exhausted = await _localized_request_loop(
                group,
                payload_builder=(
                    lambda items: payload_builder(
                        [
                            {
                                **item,
                                "_draft_terms": False,
                                "_draft_translation": True,
                                "_draft_summary": False,
                            }
                            for item in items
                        ]
                    )
                )
                if draft_scan
                else payload_builder,
                prompt=(
                    draft_scan.prompt_factories[
                        TerminologyResponseMode.TRANSLATION_ONLY
                    ](
                        draft_scan.requirements(
                            group, TerminologyResponseMode.TRANSLATION_ONLY
                        )
                    )
                    if draft_scan
                    else prompt_for_items(group)
                ),
                config=config,
                llm=state.llm,
                stage=run_stage,
                accept=None,
                accept_response=accept_response,
                prepare_candidate=prepare_candidate,
                save_error=save_external_error,
                parse=_map_local_translation_response,
                format_correction=_FORMAT_CORRECTION[language],
                prompt_language=language,
                by_id=by_id,
                segments=segments,
                prompt_partition_key=prompt_partition_key,
                logger=logger,
                initial_parent_request_id=parent_request_id,
                repair_candidates=subset,
            )
        except ContextLengthError as exc:
            with empty_response_split_scope(exc):
                if len(group) > 1:
                    midpoint = len(group) // 2
                    child_groups = (group[:midpoint], group[midpoint:])
                    for child_group in child_groups:
                        child_subset = {
                            str(item["segment_id"]): subset[str(item["segment_id"])]
                            for item in child_group
                        }
                        await repair_group(child_group, child_subset, exc.request_id)
                    return
                item = group[0]
                segment_id = str(item["segment_id"])
                if (
                    not config["chunking"]["allow_split_oversized_segment"]
                    or len(str(item["source"])) < 2
                ):
                    validation_pending[segment_id] = subset[segment_id]
                    return
                parts = _replace_with_runtime_parts(
                    item,
                    part_original=part_original,
                    original_parts=original_parts,
                    by_id=by_id,
                )
                candidate = str(subset[segment_id]["candidate"])
                left_length = round(
                    len(candidate)
                    * len(str(parts[0]["source"]))
                    / len(str(item["source"]))
                )
                candidate_parts = (candidate[:left_length], candidate[left_length:])
                for part, candidate_part in zip(parts, candidate_parts, strict=True):
                    part_id = str(part["segment_id"])
                    child_subset = {
                        part_id: {
                            "segment": part,
                            "candidate": candidate_part,
                            "findings": await validate_translation_text(
                                validation_context(part_id, candidate_part),
                                translation_validators,
                            ),
                            "request_id": subset[segment_id]["request_id"],
                        }
                    }
                    await repair_group([part], child_subset, exc.request_id)
                return
        for segment_id in exhausted:
            validation_pending[segment_id] = subset[segment_id]

    async def record_preflight_failure(
        failed: list[dict[str, Any]],
    ) -> None:
        if draft_scan is not None:
            draft_scan.fail_terms(
                failed,
                run_id,
                "PRECHECK",
                "context_error",
                "单 Segment 超过模型限制且内部拆分已关闭",
                part_original,
            )
        if draft_scan is not None:
            summary_items = [item for item in failed if item.get("_draft_summary")]
            for item in summary_items:
                draft_scan.record_summary(
                    [item],
                    state=state,
                    request_id="PRECHECK",
                    part_original=part_original,
                    original_parts=original_parts,
                    error="单 Segment 超过模型限制且内部拆分已关闭",
                    error_class="context_error",
                )
        for segment in failed:
            if draft_scan is not None and not segment.get("_draft_translation"):
                continue
            await save_failed(
                str(segment["segment_id"]),
                f"REQ-{uuid.uuid4().hex[:12].upper()}",
                "context_error",
                "单 Segment 超过模型限制且内部拆分已关闭",
            )

    async def record_context_failure(
        items: list[dict[str, Any]],
        error: ContextLengthError | None = None,
    ) -> None:
        message = (
            str(error) + "；当前范围不能继续拆分"
            if isinstance(error, EmptyResponseSplitError)
            else "模型报告上下文过长"
        )
        category = (
            "empty_response"
            if isinstance(error, EmptyResponseSplitError)
            else "context_error"
        )
        if draft_scan is not None:
            draft_scan.fail_terms(
                items,
                run_id,
                "CONTEXT",
                category,
                message,
                part_original,
            )
        if draft_scan is not None and items[0].get("_draft_summary"):
            draft_scan.record_summary(
                items,
                state=state,
                request_id="REQ-NONE",
                part_original=part_original,
                original_parts=original_parts,
                error=message,
                error_class=category,
            )
        if draft_scan is not None and not items[0].get("_draft_translation"):
            report_progress()
            return
        await save_failed(
            str(items[0]["segment_id"]),
            "REQ-NONE",
            category,
            message,
        )

    async def before_finalize() -> None:
        for owners in response_groups:
            for segment_id in owners:
                candidate = response_candidates.pop(segment_id, None)
                await save_failed(segment_id, candidate[1] if candidate else "REQ-NONE", "format_error",
                                  "整批译文未完整通过格式校验", candidate=candidate[0] if candidate else None)
        response_groups.clear()
        max_repairs = config["validation"]["translation"]["max_retry_attempts"]
        hard_repairs = 0
        while validation_pending:
            hard_pending = {
                segment_id: item
                for segment_id, item in validation_pending.items()
                if _has_hard_validation_findings(item["findings"])
            }
            if hard_pending:
                if hard_repairs >= max_repairs:
                    break
                hard_repairs += 1
                for segment_id in hard_pending:
                    validation_pending.pop(segment_id, None)
                groups = contiguous_groups(
                    (item["segment"] for item in hard_pending.values()),
                    all_segments=segments,
                    cross_boundary="translation"
                    in config["chunking"]["cross_boundary_batching"],
                    partition_key=prompt_partition_key,
                )
                logger.warning(
                    "validation repair attempt=%d segments=%d chunks=%d",
                    hard_repairs,
                    len(hard_pending),
                    len(groups),
                )
                for group in groups:
                    subset = {
                        str(item["segment_id"]): hard_pending[
                            str(item["segment_id"])
                        ]
                        for item in group
                    }
                    await repair_group(group, subset)
                continue

            advisory_pending = {
                segment_id: item
                for segment_id, item in validation_pending.items()
                if segment_id not in advisory_repair_attempted
                and any(finding.get("repairable", True) for finding in item["findings"])
            }
            if not advisory_pending:
                break
            for segment_id in advisory_pending:
                advisory_repair_attempted.add(segment_id)
                validation_pending.pop(segment_id, None)
            groups = contiguous_groups(
                (item["segment"] for item in advisory_pending.values()),
                all_segments=segments,
                cross_boundary="translation"
                in config["chunking"]["cross_boundary_batching"],
                partition_key=prompt_partition_key,
            )
            logger.warning(
                "advisory validation repair segments=%d chunks=%d",
                len(advisory_pending),
                len(groups),
            )
            for group in groups:
                subset = {
                    str(item["segment_id"]): advisory_pending[
                        str(item["segment_id"])
                    ]
                    for item in group
                }
                await repair_group(group, subset)
        exhausted_mode = config["validation"]["translation"]["exhausted_mode"]
        if exhausted_mode == "warning":
            pending_part_originals = {
                part_original[segment_id]
                for segment_id in validation_pending
                if segment_id in part_original
            }
            for original_id in pending_part_originals:
                expected = original_parts[original_id]
                combined_parts: list[str] = []
                request_id = "REQ-NONE"
                for part_id in expected:
                    if part_id in validation_pending:
                        item = validation_pending[part_id]
                        combined_parts.append(str(item["candidate"]))
                        request_id = str(item["request_id"])
                    elif part_id in part_results.get(original_id, {}):
                        text, request_id = part_results[original_id][part_id]
                        combined_parts.append(text)
                    else:
                        break
                else:
                    combined = "".join(combined_parts)
                    await save_completed(
                        original_id,
                        combined,
                        request_id,
                        validation_status="warning",
                        findings=await validate_translation_text(
                            validation_context(original_id, combined),
                            translation_validators,
                        ),
                    )
                    for part_id in expected:
                        validation_pending.pop(part_id, None)
        for segment_id, item in validation_pending.items():
            if (
                not _has_hard_validation_findings(item["findings"])
                or exhausted_mode == "warning"
            ):
                await save_completed(
                    segment_id,
                    item["candidate"],
                    item["request_id"],
                    validation_status="warning",
                    findings=item["findings"],
                )
            else:
                await save_failed(
                    segment_id,
                    item["request_id"],
                    "validation_error",
                    "翻译文字校验修复次数耗尽",
                    candidate=item["candidate"],
                    findings=item["findings"],
                )

        if draft_scan is not None:
            draft_scan.publish(run_id, segments)
            report_progress()

    def completed_count() -> int:
        if draft_scan is not None:
            return len(draft_scan.completed()) - (
                0 if resume_run_id else len(selection.reusable)
            )
        return (
            len(selection.reusable) + len(completed_ids)
            if resume_run_id
            else len(completed_ids)
        )

    try:
        usage = await _execute_stage_run(
            state,
            request_segments=request_segments,
            part_original=part_original,
            original_parts=original_parts,
            preflight_failed=preflight_failed,
            limiter=limiter,
            payload_builder=payload_builder,
            prompt_builder=prompt_for_items,
            prompt_partition_key=prompt_partition_key,
            process_once=process_once,
            record_preflight_failure=record_preflight_failure,
            record_context_failure=record_context_failure,
            before_finalize=before_finalize,
            completed_count=completed_count,
            failed_count=lambda: (
                len(draft_scan.failed()) if draft_scan else len(failed_ids)
            ),
            exception_completed=completed_count,
            exception_failed=lambda: (
                len(selection.work)
                - (
                    len(draft_scan.completed()) - len(selection.reusable)
                    if draft_scan
                    else len(completed_ids)
                )
            ),
            failure_counts=failure_counts,
            http_client=http_client,
            runtime_parts_kwargs={"by_id": by_id},
        )
    finally:
        if decision_clients:
            from .sqlite_storage import read_json, write_json
            path = run_dir / "manifest.json"
            manifest = read_json(project, path)
            manifest["decision_validation"] = [*manifest.get("decision_validation", []), *(record for client in decision_clients.values() for record in client.records)]
            write_json(project, path, manifest)
        if draft_scan is not None:
            draft_scan.finish_summary_run(run_id)
    failed_count = len(draft_scan.failed()) if draft_scan else len(failed_ids)
    logger.info(
        "run complete run=%s completed=%d failed=%d",
        run_id,
        len(completed_ids),
        failed_count,
    )
    return {
        "stage": run_stage,
        "run_id": run_id,
        **(
            {
                "include_draft_translation": True,
                "include_summaries": _include_summaries,
                "draft_progress": draft_scan.progress(),
            }
            if draft_scan
            else {}
        ),
        "selected": len(selection.selected),
        "requested": len(selection.work),
        "reused": len(selection.reusable),
        "completed": len(draft_scan.completed()) - len(selection.reusable)
        if draft_scan
        else len(completed_ids),
        "failed": failed_count,
        "failure_counts": dict(failure_counts),
        "last_attempt_failed": len(selection.last_attempt_failed),
        "warnings": warnings,
        "usage": usage,
    }
