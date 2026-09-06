from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import httpx

from .errors import (
    ContextLengthError,
    ExternalError,
    FatalExternalError,
    StorageError,
    UsageError,
)
from .execution import (
    ChunkPlan,
    PreviousContextIndex,
    Scope,
    _write_prompt_variants,
    build_chunk_plans,
    render_messages,
    segment_model_source,
    select_scope,
    stage_fingerprint,
)
from .llm_client import SlidingWindowLimiter
from .llm_keys import KeyPool
from .llm_response import (
    TerminologyResponseMode,
    parse_jsonl_document,
    parse_terminology_response,
)
from .logging_utils import get_logger
from .sqlite_storage import (
    append_jsonl,
    atomic_write_json,
    delete_content_summary_fragments,
    publish_content_summary_fulls,
    read_content_summaries,
    read_json,
    read_jsonl,
    read_summary_participation,
    record_exists,
    record_header,
    terminology_scan_state,
    utc_now,
    write_content_summary,
    write_json,
    write_summary_run,
)
from .stage_runtime import (
    _FORMAT_CORRECTION,
    StageRunState,
    _assemble_warnings,
    _create_or_continue_run,
    _document_prompt_requirement_helpers,
    _execute_stage_run,
    _project_context,
    _prompt_factory,
    _prompt_language_for_stages,
    _request_estimate,
    _require_nonempty_segments,
    _resume_scope,
    _scope_record,
    _split_oversized_preflight,
    _split_segment_source,
    _split_source_once,
    prompt_middle_digests,
)
from .term_library import _merge_and_publish_terms, load_terms

_SUMMARY_MODE_KEY = "_terminology_response_mode"


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _summary_participation(
    project: Path,
    selected: list[dict[str, Any]],
) -> set[tuple[str, str]]:
    available = {(str(item["file_id"]), str(item["part_id"])) for item in selected}
    return {
        (str(item["file_id"]), str(item["part_id"]))
        for item in read_summary_participation(project)
        if bool(item.get("selected"))
        and (str(item.get("file_id")), str(item.get("part_id"))) in available
    }


def _summary_covered_segments(
    project: Path,
    segments: list[dict[str, Any]],
    *,
    prompt_digests: Callable[[list[dict[str, Any]]], set[str]],
    model: str,
    target_language: str,
) -> set[str]:
    valid_slices: dict[str, dict[str, dict[str, Any]]] = {}
    artifacts = read_content_summaries(project, kind="fragment", status="completed")
    by_id = {str(item["segment_id"]): item for item in segments}
    for artifact in artifacts:
        if artifact.get("source_changed"):
            continue
        source_range = artifact.get("source_range")
        values = (
            source_range.get("segments") if isinstance(source_range, dict) else None
        )
        if not isinstance(values, list) or not values:
            continue
        if any(not isinstance(value, dict) for value in values):
            continue
        current = [
            by_id.get(str(value.get("original_segment_id", value.get("segment_id"))))
            for value in values
        ]
        if any(item is None for item in current):
            continue
        stable_values: list[dict[str, Any]] = []
        valid_values = True
        for value, item in zip(values, current, strict=True):
            assert item is not None
            stable_id = str(value.get("original_segment_id", value.get("segment_id")))
            if str(value.get("segment_id")) != stable_id:
                valid_values = False
                break
            source_value = str(value.get("source", ""))
            model_text = str(value.get("model_text", ""))
            model_text_digest = _digest(model_text)
            try:
                slice_index = int(value["slice_index"])
            except (KeyError, TypeError, ValueError):
                valid_values = False
                break
            expected_slice_id = (
                f"{stable_id}#slice-{slice_index:04d}-"
                f"{_digest(source_value)[7:15]}-{model_text_digest[7:15]}"
            )
            if (
                value.get("source_digest") != _digest(source_value)
                or value.get("original_source_digest") != _digest(str(item["source"]))
                or value.get("original_model_text_digest")
                != _digest(segment_model_source(item))
                or value.get("model_text_digest") != model_text_digest
                or value.get("slice_id") != expected_slice_id
            ):
                valid_values = False
                break
            stable_values.append(
                {
                    "segment_id": stable_id,
                    "original_segment_id": stable_id,
                    "slice_id": value["slice_id"],
                    "slice_index": slice_index,
                    "source": source_value,
                    "source_digest": value["source_digest"],
                    "original_source_digest": value["original_source_digest"],
                    "model_text": model_text,
                    "model_text_digest": model_text_digest,
                    "original_model_text_digest": value["original_model_text_digest"],
                }
            )
        if not valid_values:
            continue
        source_digest = _digest(stable_values)
        input_digest = _digest(
            [
                {"segment_id": value["segment_id"], "model_text": value["model_text"]}
                for value in stable_values
            ]
        )
        current_prompt_digests = prompt_digests(
            [item for item in current if item is not None]
        )
        artifact_prompt_digests = {
            str(artifact.get("prompt_digest")),
            str(artifact.get("fragment_prompt_digest")),
        }
        if (
            artifact.get("source_digest") == source_digest
            and artifact.get("input_digest") == input_digest
            and artifact_prompt_digests & current_prompt_digests
            and artifact.get("model") == model
            and artifact.get("target_language") == target_language
        ):
            for value in stable_values:
                valid_slices.setdefault(value["segment_id"], {})[
                    str(value["slice_id"])
                ] = value
    covered: set[str] = set()
    for segment_id, values in valid_slices.items():
        current = by_id.get(segment_id)
        if current is None:
            continue
        ordered = sorted(values.values(), key=lambda value: int(value["slice_index"]))
        if [int(value["slice_index"]) for value in ordered] != list(
            range(len(ordered))
        ):
            continue
        if "".join(str(value["source"]) for value in ordered) != str(current["source"]):
            continue
        covered.add(segment_id)
    return covered


def _validate_term_items(
    content: str,
) -> tuple[list[dict[str, Any]], list[str], bool]:
    document = parse_jsonl_document(content, record_type="term")
    terms: list[dict[str, Any]] = []
    errors = list(document.errors)
    for index, item in enumerate(document.records, start=1):
        item_errors: list[str] = []
        for key in ("source", "category"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                item_errors.append(f"术语记录 {index} 缺少有效 {key}")
        description = item.get("description")
        if description is not None and not isinstance(description, str):
            item_errors.append(f"术语记录 {index} 的 description 类型错误")
        preferred = item.get("preferred_translation")
        if preferred is not None and not isinstance(preferred, str):
            item_errors.append(f"术语记录 {index} 的 preferred_translation 类型错误")
        aliases = item.get("aliases", [])
        if not isinstance(aliases, list) or not all(
            isinstance(alias, str) for alias in aliases
        ):
            item_errors.append(f"术语记录 {index} 的 aliases 类型错误")
        if item_errors:
            errors.extend(item_errors)
            continue
        terms.append(
            {
                "source": item["source"].strip(),
                "category": item["category"].strip(),
                "description": description.strip() if description else None,
                "preferred_translation": preferred.strip() if preferred else None,
                "aliases": [alias.strip() for alias in aliases if alias.strip()],
            }
        )
    return terms, errors, document.complete and not errors


def _terminology_scan_selection(
    project: Path,
    segments: list[dict[str, Any]],
    files: list[dict[str, Any]],
    scope: Scope,
    task_id: str,
    *,
    force_all: bool = False,
) -> tuple[list[dict[str, Any]], set[str], set[str], set[str]]:
    selected = (
        [segment for segment in segments if not segment["is_empty"]]
        if force_all
        else select_scope(segments, files, scope)
    )
    selected_ids = {str(segment["segment_id"]) for segment in selected}
    completed_ids, fingerprints = terminology_scan_state(
        project,
        task_id,
        selected_ids,
    )
    return selected, selected_ids, completed_ids, fingerprints


async def run_terminology(
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
    include_summaries: bool = False,
) -> dict[str, Any]:
    logger = get_logger("terminology")
    preparation_started_at = time.perf_counter()
    scope, resume_arguments_ignored = _resume_scope(project, scope, resume_run_id)
    config, metadata, files, segments = _project_context(project, stage="terminology")
    logger.info(
        "stage preparation context ready elapsed=%.3fs files=%d segments=%d",
        time.perf_counter() - preparation_started_at,
        len(files),
        len(segments),
    )
    _require_nonempty_segments(segments)
    if (
        include_summaries
        and "terminology" in config["chunking"]["cross_boundary_batching"]
    ):
        raise UsageError(
            "include_summaries 要求关闭 chunking.cross_boundary_batching 中的 terminology"
        )
    mode_prompt_factories: dict[
        TerminologyResponseMode, Callable[[tuple[str, ...]], str]
    ] = {}
    fragment_prompt_factories: dict[
        str, Callable[[tuple[str, ...]], str]
    ] = {}
    mode_prompt_languages: dict[TerminologyResponseMode, str] = {}
    mode_requirement_helpers: dict[
        TerminologyResponseMode,
        tuple[
            Callable[[list[dict[str, Any]]], tuple[str, ...]],
            Callable[[dict[str, Any]], object],
        ],
    ] = {}
    prompt_configs: dict[str, dict[str, Any]] = {"terminology": config}

    def prompt_config_for_stage(stage: str) -> dict[str, Any]:
        stage_config = prompt_configs.get(stage)
        if stage_config is None:
            stage_config, _, _, _ = _project_context(project, stage=stage)
            prompt_configs[stage] = stage_config
        return stage_config

    def required_stages_for_mode(mode: TerminologyResponseMode) -> tuple[str, ...]:
        return (
            ("fragment_summary",)
            if mode is TerminologyResponseMode.SUMMARY_ONLY
            else ("terminology", "fragment_summary")
            if mode is TerminologyResponseMode.TERMS_AND_FRAGMENT_SUMMARY
            else ("terminology",)
        )

    def prompt_language_for_mode(mode: TerminologyResponseMode) -> str:
        language_for_mode = mode_prompt_languages.get(mode)
        if language_for_mode is None:
            language_for_mode = _prompt_language_for_stages(
                project,
                prompt_language,
                required_stages_for_mode(mode),
            )
            mode_prompt_languages[mode] = language_for_mode
        return language_for_mode

    def prompt_factory_for(
        mode: TerminologyResponseMode,
    ) -> Callable[[Iterable[str]], str]:
        factory = mode_prompt_factories.get(mode)
        if factory is None:
            factory = _prompt_factory(
                project,
                "terminology",
                prompt_language_for_mode(mode),
                response_mode=(
                    None if mode is TerminologyResponseMode.TERMS_ONLY else mode
                ),
            )
            mode_prompt_factories[mode] = factory
        return factory

    def fragment_prompt_factory_for(
        language: str,
    ) -> Callable[[Iterable[str]], str]:
        factory = fragment_prompt_factories.get(language)
        if factory is None:
            factory = _prompt_factory(
                project,
                "terminology",
                language,
                response_mode=TerminologyResponseMode.SUMMARY_ONLY,
            )
            fragment_prompt_factories[language] = factory
        return factory

    def requirement_helper_for(
        mode: TerminologyResponseMode,
    ) -> tuple[
        Callable[[list[dict[str, Any]]], tuple[str, ...]],
        Callable[[dict[str, Any]], object],
    ]:
        helper = mode_requirement_helpers.get(mode)
        if helper is None:
            language = prompt_language_for_mode(mode)
            if mode is TerminologyResponseMode.SUMMARY_ONLY:
                helper = _document_prompt_requirement_helpers(
                    prompt_config_for_stage("fragment_summary"), language
                )
            elif mode is TerminologyResponseMode.TERMS_AND_FRAGMENT_SUMMARY:
                terminology_helper = _document_prompt_requirement_helpers(
                    prompt_config_for_stage("terminology"), language
                )
                fragment_helper = _document_prompt_requirement_helpers(
                    prompt_config_for_stage("fragment_summary"), language
                )

                def requirements_for_joint(
                    items: list[dict[str, Any]],
                ) -> tuple[str, ...]:
                    values: list[str] = []
                    for requirement_helper in (
                        terminology_helper,
                        fragment_helper,
                    ):
                        for requirement in requirement_helper[0](items):
                            if requirement not in values:
                                values.append(requirement)
                    return tuple(values)

                def partition_key_for_joint(item: dict[str, Any]) -> object:
                    return (
                        terminology_helper[1](item),
                        fragment_helper[1](item),
                    )

                helper = (requirements_for_joint, partition_key_for_joint)
            else:
                helper = _document_prompt_requirement_helpers(
                    prompt_config_for_stage("terminology"), language
                )
            mode_requirement_helpers[mode] = helper
        return helper

    def prompt_requirements_for_mode(
        mode: TerminologyResponseMode,
    ) -> dict[str, dict[str, str]]:
        combined: dict[str, dict[str, str]] = {}
        for stage in required_stages_for_mode(mode):
            by_file = prompt_config_for_stage(stage).get(
                "_document_adapter_prompt_requirements", {}
            )
            if not isinstance(by_file, dict):
                raise StorageError("Document Adapter Prompt 要求索引无效")
            for file_id, language_requirements in by_file.items():
                if not isinstance(language_requirements, dict):
                    raise StorageError("Document Adapter Prompt 要求记录无效")
                target = combined.setdefault(str(file_id), {})
                for language, requirement in language_requirements.items():
                    if not isinstance(requirement, str) or not requirement:
                        continue
                    previous = target.get(str(language))
                    if previous is None:
                        target[str(language)] = requirement
                    elif requirement != previous:
                        target[str(language)] = f"{previous}\n{requirement}"
        return combined

    def mode_for_item(
        item: dict[str, Any],
        *,
        default: TerminologyResponseMode,
    ) -> TerminologyResponseMode:
        raw_mode = item.get(_SUMMARY_MODE_KEY, default)
        try:
            return TerminologyResponseMode(raw_mode)
        except (TypeError, ValueError) as exc:
            raise StorageError(f"术语请求缺少有效响应模式：{raw_mode}") from exc

    def requirements_for_mode(
        items: list[dict[str, Any]], mode: TerminologyResponseMode
    ) -> tuple[str, ...]:
        return requirement_helper_for(mode)[0](items)

    def prompt_for_items(items: list[dict[str, Any]]) -> str:
        return prompt_details_for_items(items)[2]

    def prompt_details_for_items(
        items: list[dict[str, Any]],
    ) -> tuple[TerminologyResponseMode, tuple[str, ...], str]:
        mode = mode_for_item(
            items[0], default=TerminologyResponseMode.TERMS_ONLY
        )
        requirements = requirements_for_mode(items, mode)
        return mode, requirements, prompt_factory_for(mode)(requirements)

    def prompt_language_for_items(items: list[dict[str, Any]]) -> str:
        mode = mode_for_item(
            items[0], default=TerminologyResponseMode.TERMS_ONLY
        )
        return prompt_language_for_mode(mode)

    def summary_prompt_digest_for(items: list[dict[str, Any]]) -> str:
        mode = mode_for_item(
            items[0], default=TerminologyResponseMode.SUMMARY_ONLY
        )
        if mode is TerminologyResponseMode.TERMS_ONLY:
            raise StorageError("术语概括请求不能使用 terms-only 模式")
        return _digest(prompt_factory_for(mode)(requirements_for_mode(items, mode)))

    def fragment_prompt_digest_for(items: list[dict[str, Any]]) -> str:
        mode = mode_for_item(
            items[0], default=TerminologyResponseMode.SUMMARY_ONLY
        )
        if mode is TerminologyResponseMode.TERMS_ONLY:
            raise StorageError("术语概括请求不能使用 terms-only 模式")
        if mode is TerminologyResponseMode.SUMMARY_ONLY:
            return summary_prompt_digest_for(items)
        return _digest(
            fragment_prompt_factory_for(prompt_language_for_mode(mode))(
                _document_prompt_requirement_helpers(
                    prompt_config_for_stage("fragment_summary"),
                    prompt_language_for_mode(mode),
                )[0](items)
            )
        )

    def summary_prompt_digests(items: list[dict[str, Any]]) -> set[str]:
        modes = {
            mode_for_item(item, default=TerminologyResponseMode.SUMMARY_ONLY)
            for item in items
        }
        if TerminologyResponseMode.TERMS_ONLY in modes:
            raise StorageError("术语概括请求不能使用 terms-only 模式")
        mode = (
            TerminologyResponseMode.TERMS_AND_FRAGMENT_SUMMARY
            if TerminologyResponseMode.TERMS_AND_FRAGMENT_SUMMARY in modes
            else TerminologyResponseMode.SUMMARY_ONLY
        )
        requirements = requirements_for_mode(items, mode)
        prompt_digest = _digest(prompt_factory_for(mode)(requirements))
        if mode is TerminologyResponseMode.SUMMARY_ONLY:
            return {prompt_digest}
        return {
            prompt_digest,
            _digest(
                fragment_prompt_factory_for(prompt_language_for_mode(mode))(
                    _document_prompt_requirement_helpers(
                        prompt_config_for_stage("fragment_summary"),
                        prompt_language_for_mode(mode),
                    )[0](items)
                )
            ),
        }

    def prompt_partition_key(item: dict[str, Any]) -> object:
        if not include_summaries:
            mode = TerminologyResponseMode.TERMS_ONLY
        else:
            mode = mode_for_item(
                item, default=TerminologyResponseMode.TERMS_ONLY
            )
        return (
            requirement_helper_for(mode)[1](item),
            item.get(_SUMMARY_MODE_KEY, TerminologyResponseMode.TERMS_ONLY.value),
            str(item["file_id"]),
            str(item["part_id"]),
        ) if include_summaries else requirement_helper_for(mode)[1](item)

    active_path = project / "terminology" / "active_task.json"
    active = (
        read_json(project, active_path) if record_exists(project, active_path) else None
    )
    published = load_terms(project)
    partial_published = bool(
        active is not None and active.get("status") == "partial_published"
    )

    def segment_needs_terms(segment: dict[str, Any]) -> bool:
        return scope.force or (
            not (include_summaries and partial_published)
            and str(segment["segment_id"]) not in completed_ids
        )

    resume_manifest = (
        read_json(project, project / "runs" / resume_run_id / "manifest.json")
        if resume_run_id is not None
        else None
    )
    create_task = resume_run_id is None and (
        scope.force or active is None or (partial_published and not include_summaries)
    )
    if resume_manifest is not None:
        task_id = str(resume_manifest.get("active_task_id", ""))
        if not task_id:
            raise StorageError(f"术语 Run 缺少 active_task_id：{resume_run_id}")
        if active is None or active.get("active_task_id") != task_id:
            raise StorageError(f"术语 Run 的 active task 不再可用：{resume_run_id}")
        saved_include_summaries = bool(resume_manifest.get("include_summaries", False))
        if saved_include_summaries != include_summaries:
            raise StorageError("续用 Run 的 include_summaries 与当前请求不一致")
    if create_task:
        task_id = f"TERM-TASK-{uuid.uuid4().hex[:10].upper()}"
    elif active and active.get("status") == "active":
        task_id = str(active["active_task_id"])
    else:
        task_id = str(active.get("active_task_id", "none")) if active else "none"

    # A forced terminology run rescans the full project and merges at publish.
    (
        selected,
        selected_ids,
        completed_ids,
        existing_fingerprints,
    ) = _terminology_scan_selection(
        project,
        segments,
        files,
        scope,
        task_id,
        force_all=scope.force,
    )
    term_work = (
        []
        if include_summaries and partial_published and not scope.force
        else [segment for segment in selected if segment_needs_terms(segment)]
    )
    summary_selection = (
        _summary_participation(project, selected) if include_summaries else set()
    )
    if include_summaries and scope.force and not scope.dry_run:
        delete_content_summary_fragments(project, summary_selection)
    if include_summaries and resume_manifest is not None:
        saved_selection = resume_manifest.get("summary_participation")
        if isinstance(saved_selection, list):
            summary_selection = {
                (str(item["file_id"]), str(item["part_id"]))
                for item in saved_selection
                if isinstance(item, dict)
                and bool(item.get("selected"))
                and isinstance(item.get("file_id"), str)
                and isinstance(item.get("part_id"), str)
                and (str(item["file_id"]), str(item["part_id"]))
                in {
                    (str(value["file_id"]), str(value["part_id"])) for value in selected
                }
            }

    summary_candidates = [
        {
            **segment,
            _SUMMARY_MODE_KEY: (
                TerminologyResponseMode.TERMS_AND_FRAGMENT_SUMMARY.value
                if segment_needs_terms(segment)
                else TerminologyResponseMode.SUMMARY_ONLY.value
            ),
        }
        for segment in selected
        if include_summaries
        and (str(segment["file_id"]), str(segment["part_id"])) in summary_selection
    ]
    covered_summary_ids = (
        _summary_covered_segments(
            project,
            summary_candidates,
            prompt_digests=summary_prompt_digests,
            model=str(config["llm"]["model"]),
            target_language=str(config["project"]["target_language"]),
        )
        if summary_candidates
        else set()
    )
    work: list[dict[str, Any]] = []
    required_modes: dict[str, set[str]] = {}
    required_prompt_modes: set[TerminologyResponseMode] = set()
    for segment in selected:
        segment_id = str(segment["segment_id"])
        boundary = (str(segment["file_id"]), str(segment["part_id"]))
        needs_terms = segment_needs_terms(segment)
        needs_summary = (
            include_summaries
            and boundary in summary_selection
            and segment_id not in covered_summary_ids
        )
        if not needs_terms and not needs_summary:
            continue
        if needs_terms and needs_summary:
            mode = TerminologyResponseMode.TERMS_AND_FRAGMENT_SUMMARY
        elif needs_terms:
            mode = TerminologyResponseMode.TERMS_ONLY
        else:
            mode = TerminologyResponseMode.SUMMARY_ONLY
        annotated = {**segment, _SUMMARY_MODE_KEY: mode.value}
        work.append(annotated)
        required_prompt_modes.add(mode)
        required_modes.setdefault(segment_id, set()).update(
            {"term"}
            if mode is TerminologyResponseMode.TERMS_ONLY
            else {"summary"}
            if mode is TerminologyResponseMode.SUMMARY_ONLY
            else {"term", "summary"}
        )
    logger.info(
        "stage preparation selection/history ready elapsed=%.3fs selected=%d requested=%d completed=%d",
        time.perf_counter() - preparation_started_at,
        len(selected),
        len(work),
        len(completed_ids),
    )
    prompt_mode = next(
        (
            mode
            for mode in (
                TerminologyResponseMode.TERMS_AND_FRAGMENT_SUMMARY,
                TerminologyResponseMode.TERMS_ONLY,
                TerminologyResponseMode.SUMMARY_ONLY,
            )
            if mode in required_prompt_modes
        ),
        None,
    )
    if prompt_mode is None:
        prompt = ""
        fingerprint_prompt_digests = (
            prompt_middle_digests(project, "terminology")
            if not include_summaries
            else {}
        )
    else:
        primary_requirements: tuple[str, ...] = ()
        if include_summaries:
            primary_items = [
                segment
                for segment in work
                if mode_for_item(
                    segment, default=TerminologyResponseMode.TERMS_ONLY
                )
                is prompt_mode
            ]
            if primary_items:
                primary_requirements = requirements_for_mode(
                    [primary_items[0]], prompt_mode
                )
        prompt = prompt_factory_for(prompt_mode)(primary_requirements)
        fingerprint_prompt_digests = (
            prompt_middle_digests(project, "terminology")
            if TerminologyResponseMode.TERMS_ONLY in required_prompt_modes
            or TerminologyResponseMode.TERMS_AND_FRAGMENT_SUMMARY
            in required_prompt_modes
            else {}
        )
    run_config = dict(config)
    run_config["_document_adapter_prompt_requirements"] = (
        prompt_requirements_for_mode(prompt_mode) if prompt_mode is not None else {}
    )
    fingerprint = stage_fingerprint(
        run_config if prompt_mode is TerminologyResponseMode.SUMMARY_ONLY else config,
        "terminology",
        fingerprint_prompt_digests,
    )

    if create_task:
        active = record_header(
            "terminology_task",
            str(metadata["project_id"]),
            record_id=task_id,
            active_task_id=task_id,
            status="active",
            initial_stage_fingerprint=fingerprint,
        )
        if not scope.dry_run:
            write_json(project, active_path, active)

    reopen_completed_task = (
        resume_run_id is None
        and not scope.force
        and bool(term_work)
        and active is not None
        and active.get("status") == "completed"
    )
    usage: dict[str, Any] | None = None
    warnings = _assemble_warnings(
        stage="terminology",
        resume_run_id=resume_run_id,
        resume_arguments_ignored=resume_arguments_ignored,
        resume_message=(
            f"续用 Run {resume_run_id} 的原始范围和术语任务；"
            "本次使用当前 config 和 Prompt"
        ),
        config=config,
        fingerprint=fingerprint,
        existing_fingerprints=(
            existing_fingerprints
            if any(
                mode
                in {
                    TerminologyResponseMode.TERMS_ONLY,
                    TerminologyResponseMode.TERMS_AND_FRAGMENT_SUMMARY,
                }
                for mode in required_prompt_modes
            )
            else set()
        ),
        reusable_count=len(selected_ids & completed_ids),
        force=scope.force,
        reuse_allowed=reuse_mixed_fingerprints,
        dry_run=scope.dry_run,
        extra=[],
    )
    context_config = config["context"]["terminology"]
    context_index = PreviousContextIndex(segments)

    def payload_builder(items: list[dict[str, Any]]) -> dict[str, Any]:
        raw_context = (
            context_index.previous(
                items[0],
                context_config["previous_segments"],
                source_key="model_source",
            )
            if context_config["enabled"]
            else []
        )
        payload = {
            "target_language": config["project"]["target_language"],
            "reference_context": [item["source"] for item in raw_context],
            "source_segments": [segment_model_source(item) for item in items],
        }
        if include_summaries:
            payload["source_refs"] = [str(index) for index, _ in enumerate(items, 1)]
        return payload

    run_id, run_dir, continuation_index, fail_planning = _create_or_continue_run(
        project,
        "terminology",
        scope=scope,
        config=run_config,
        fingerprint=fingerprint,
        prompt=(
            prompt
            if prompt_mode is not None and not include_summaries
            else None
        ),
        resume_run_id=resume_run_id,
        selected_count=len(selected),
        requested_count=len(work),
        reused_count=len(selected) - len(work),
        details={
            "active_task_id": task_id,
            "scope": _scope_record(scope, force_all=scope.force),
            "prompt_language": (
                prompt_language_for_mode(prompt_mode)
                if prompt_mode is not None
                else None
            ),
            "primary_mode": (
                prompt_mode.value if prompt_mode is not None else None
            ),
            "prompt_languages": {},
            "include_summaries": include_summaries,
            "summary_participation": [
                dict(item) for item in read_summary_participation(project)
            ],
            "terminology_modes": {
                segment_id: sorted(modes)
                for segment_id, modes in required_modes.items()
            },
        },
        warnings=warnings,
        prompt_variants=None,
        prompt_variant_requirements=None,
        primary_mode=prompt_mode.value if prompt_mode is not None else None,
    )

    if reopen_completed_task:
        active = {**active, "status": "active"}
        write_json(project, active_path, active)

    preflight = _split_oversized_preflight(
        work,
        config=config,
        prompt=prompt,
        payload_builder=payload_builder,
        prompt_builder=prompt_for_items,
        fail_planning=fail_planning,
        make_probe=lambda segment, part: _split_segment_source(
            segment, f"{segment['segment_id']}-PROBE", part
        ),
        split_part=lambda part: list(_split_source_once(part)),
        accept_part=lambda segment, part_id, part: _split_segment_source(
            segment, part_id, part
        ),
    )
    request_segments = preflight.request_segments
    part_original = preflight.part_original
    original_parts = preflight.original_parts
    preflight_failed = preflight.preflight_failed
    if include_summaries:
        actual_modes = {
            mode_for_item(
                item, default=TerminologyResponseMode.TERMS_ONLY
            )
            for item in request_segments
        }
        prompt_mode = next(
            (
                mode
                for mode in (
                    TerminologyResponseMode.TERMS_AND_FRAGMENT_SUMMARY,
                    TerminologyResponseMode.TERMS_ONLY,
                    TerminologyResponseMode.SUMMARY_ONLY,
                )
                if mode in actual_modes
            ),
            None,
        )
    logger.info(
        "stage preparation preflight complete elapsed=%.3fs requested=%d failed=%d fast=%d exact=%d",
        time.perf_counter() - preparation_started_at,
        len(request_segments),
        len(preflight_failed),
        preflight.fast_checked,
        preflight.exact_checked,
    )

    selected_by_id = {str(item["segment_id"]): item for item in selected}

    def summary_slice_provenance(item: dict[str, Any]) -> dict[str, Any]:
        request_segment_id = str(item["segment_id"])
        stable_id = part_original.get(request_segment_id, request_segment_id)
        source_item = selected_by_id.get(stable_id, item)
        source = str(item["source"])
        source_digest = _digest(source)
        model_text = segment_model_source(item)
        model_text_digest = _digest(model_text)
        expected = original_parts.get(stable_id, [request_segment_id])
        try:
            slice_index = expected.index(request_segment_id)
        except ValueError:
            raise StorageError(
                f"概括请求切片不在稳定 Segment 范围内：{request_segment_id}"
            ) from None
        return {
            "segment_id": stable_id,
            "original_segment_id": stable_id,
            "slice_id": (
                f"{stable_id}#slice-{slice_index:04d}-"
                f"{source_digest[7:15]}-{model_text_digest[7:15]}"
            ),
            "slice_index": slice_index,
            "source": source,
            "source_digest": source_digest,
            "original_source_digest": _digest(str(source_item["source"])),
            "model_text": model_text,
            "model_text_digest": model_text_digest,
            "original_model_text_digest": _digest(segment_model_source(source_item)),
        }

    summary_run_requests: dict[str, dict[str, Any]] = {}
    summary_prompt_modes_used: set[TerminologyResponseMode] = set()

    if scope.dry_run:
        plans = build_chunk_plans(
            request_segments,
            all_segments=segments,
            config=config,
            stage="terminology",
            prompt=prompt,
            payload_builder=payload_builder,
            prompt_builder=prompt_for_items,
            partition_key=prompt_partition_key,
        )
        logger.info(
            "stage plan selected=%d requested=%d reused=%d chunks=%d",
            len(selected),
            len(work),
            len(selected) - len(work),
            len(plans),
        )
        return {
            "stage": "terminology",
            "dry_run": True,
            "resume_run_id": resume_run_id,
            "active_task_id": task_id,
            "selected": len(selected),
            "requested": len(work),
            "chunks": len(plans),
            "estimated_input_tokens": sum(
                plan.estimated_input_tokens for plan in plans
            ),
            "warnings": warnings,
        }

    assert run_id is not None and run_dir is not None
    if include_summaries:
        snapshot_dir = (
            run_dir / "continuations" / f"{continuation_index:04d}"
            if continuation_index
            else run_dir
        )
        manifest = read_json(project, run_dir / "manifest.json")
        if continuation_index:
            continuations = manifest.get("continuations")
            if (
                not isinstance(continuations, list)
                or len(continuations) < continuation_index
                or not isinstance(continuations[continuation_index - 1], dict)
            ):
                raise StorageError(f"Run 缺少续作快照：{run_id}")
            primary_snapshot = continuations[continuation_index - 1]
        else:
            primary_snapshot = manifest
        primary_requirements = (
            prompt_requirements_for_mode(prompt_mode)
            if prompt_mode is not None
            else {}
        )
        primary_snapshot.pop("primary_mode", None)
        primary_snapshot.pop("prompt_language", None)
        if prompt_mode is not None:
            primary_snapshot["primary_mode"] = prompt_mode.value
            primary_snapshot["prompt_language"] = prompt_language_for_mode(
                prompt_mode
            )
        if not continuation_index:
            manifest["document_adapter_prompt_requirements"] = primary_requirements
        atomic_write_json(
            snapshot_dir / "document_adapter_prompt_requirements.json",
            primary_requirements,
        )
        write_json(project, run_dir / "manifest.json", manifest)
    write_lock = asyncio.Lock()

    def prompt_variant_name(
        mode: TerminologyResponseMode, requirements: tuple[str, ...]
    ) -> str:
        suffix = f"__requirements-{_digest(requirements)[7:23]}" if requirements else ""
        return f"{mode.value}{suffix}"

    async def record_prompt_variant(
        mode: TerminologyResponseMode,
        prompt_text: str,
        requirements: tuple[str, ...],
    ) -> None:
        if not include_summaries:
            return
        snapshot_dir = (
            run_dir / "continuations" / f"{continuation_index:04d}"
            if continuation_index
            else run_dir
        )
        name = prompt_variant_name(mode, requirements)
        async with write_lock:
            paths = _write_prompt_variants(
                snapshot_dir,
                {name: prompt_text},
                {name: requirements},
                primary_mode=(
                    prompt_mode.value if prompt_mode is not None else None
                ),
            )
            manifest = read_json(project, run_dir / "manifest.json")
            if continuation_index:
                continuations = manifest.get("continuations")
                if (
                    not isinstance(continuations, list)
                    or len(continuations) < continuation_index
                    or not isinstance(
                        continuations[continuation_index - 1], dict
                    )
                ):
                    raise StorageError(f"Run 缺少续作快照：{run_id}")
                target = continuations[continuation_index - 1]
            else:
                target = manifest
            variant_paths = target.setdefault("prompt_variants", {})
            languages = target.setdefault("prompt_languages", {})
            if not isinstance(variant_paths, dict) or not isinstance(languages, dict):
                raise StorageError(f"Run Prompt 快照元数据无效：{run_id}")
            variant_paths.update(paths)
            languages[mode.value] = prompt_language_for_mode(mode)
            write_json(project, run_dir / "manifest.json", manifest)
            if prompt_mode is mode:
                (snapshot_dir / "prompt.txt").write_text(
                    prompt_text, encoding="utf-8"
                )

    part_success: dict[str, set[str]] = {}
    failed_originals: set[str] = set()
    failure_counts: Counter[str] = Counter()
    completed_original_ids: set[str] = set()
    class_success_parts: dict[str, dict[str, set[str]]] = {
        "term": {},
        "summary": {},
    }
    class_failed_originals: dict[str, set[str]] = {"term": set(), "summary": set()}
    term_scan_recorded: set[str] = set()
    summary_run_record: dict[str, Any] | None = None

    if include_summaries:
        summary_request_segments = [
            item
            for item in request_segments
            if item.get(_SUMMARY_MODE_KEY)
            in {
                TerminologyResponseMode.TERMS_AND_FRAGMENT_SUMMARY.value,
                TerminologyResponseMode.SUMMARY_ONLY.value,
            }
        ]
        requested_summary_boundaries = sorted(
            {
                (str(item["file_id"]), str(item["part_id"]))
                for item in summary_request_segments
            }
        )
        if summary_request_segments:
            summary_run_record = record_header(
                "summary_run",
                str(metadata["project_id"]),
                record_id=run_id,
                run_id=run_id,
                mode="fragment",
                status="running",
                source_ranges=[],
                input_digest=_digest([]),
                prompt_digest=_digest([]),
                model=str(config["llm"]["model"]),
                target_language=str(config["project"]["target_language"]),
                prompt_languages={},
                include_summaries=True,
                selection=[
                    {"file_id": file_id, "part_id": part_id}
                    for file_id, part_id in requested_summary_boundaries
                ],
            )
            write_summary_run(project, summary_run_record)

    def persist_summary_run(status: str | None = None) -> None:
        nonlocal summary_run_record
        if summary_run_record is None:
            return
        entries: list[tuple[str, str, dict[str, Any]]] = []
        for item in summary_run_requests.values():
            mode = item.get(_SUMMARY_MODE_KEY)
            if mode not in {
                TerminologyResponseMode.TERMS_AND_FRAGMENT_SUMMARY.value,
                TerminologyResponseMode.SUMMARY_ONLY.value,
            }:
                continue
            provenance = summary_slice_provenance(item)
            entries.append((str(item["file_id"]), str(item["part_id"]), provenance))
        entries.sort(
            key=lambda entry: (
                entry[0],
                entry[1],
                int(entry[2]["slice_index"]),
                str(entry[2]["slice_id"]),
            )
        )
        ranges: list[dict[str, Any]] = []
        for file_id, part_id in sorted({(entry[0], entry[1]) for entry in entries}):
            ranges.append(
                {
                    "file_id": file_id,
                    "part_id": part_id,
                    "segments": [
                        provenance
                        for entry_file, entry_part, provenance in entries
                        if entry_file == file_id and entry_part == part_id
                    ],
                }
            )
        prompt_digests = sorted(
            {
                summary_prompt_digest_for([summary_run_requests[segment_id]])
                for segment_id in summary_run_requests
                if summary_run_requests[segment_id].get(_SUMMARY_MODE_KEY)
                in {
                    TerminologyResponseMode.TERMS_AND_FRAGMENT_SUMMARY.value,
                    TerminologyResponseMode.SUMMARY_ONLY.value,
                }
            }
        )
        summary_run_record = {
            **summary_run_record,
            "status": status or summary_run_record["status"],
            "source_ranges": ranges,
            "input_digest": _digest(
                [
                    {
                        "file_id": file_id,
                        "part_id": part_id,
                        **provenance,
                    }
                    for file_id, part_id, provenance in entries
                ]
            ),
            "prompt_digest": (
                prompt_digests[0]
                if len(prompt_digests) == 1
                else _digest(prompt_digests)
            ),
            "prompt_languages": {
                mode.value: mode_prompt_languages[mode]
                for mode in sorted(summary_prompt_modes_used, key=lambda value: value.value)
            },
            "target_language": str(config["project"]["target_language"]),
            "updated_at": utc_now(),
        }
        write_summary_run(project, summary_run_record)

    def observe_summary_request(items: list[dict[str, Any]]) -> None:
        if summary_run_record is None:
            return
        for item in items:
            if item.get(_SUMMARY_MODE_KEY) in {
                TerminologyResponseMode.TERMS_AND_FRAGMENT_SUMMARY.value,
                TerminologyResponseMode.SUMMARY_ONLY.value,
            }:
                summary_prompt_modes_used.add(
                    TerminologyResponseMode(str(item[_SUMMARY_MODE_KEY]))
                )
                summary_run_requests[str(item["segment_id"])] = dict(item)
        persist_summary_run()

    def observe_summary_split(
        items: list[dict[str, Any]], _groups: list[list[dict[str, Any]]]
    ) -> None:
        if summary_run_record is None:
            return
        for item in items:
            if item.get(_SUMMARY_MODE_KEY) in {
                TerminologyResponseMode.TERMS_AND_FRAGMENT_SUMMARY.value,
                TerminologyResponseMode.SUMMARY_ONLY.value,
            }:
                summary_run_requests.pop(str(item["segment_id"]), None)
        persist_summary_run()

    def update_summary_run_status(status: str) -> None:
        persist_summary_run(status=status)

    def original_id(segment: dict[str, Any]) -> str:
        return part_original.get(str(segment["segment_id"]), str(segment["segment_id"]))

    def expected_part_ids(segment_id: str) -> list[str]:
        return original_parts.get(segment_id, [segment_id])

    def mark_class_success(items: list[dict[str, Any]], result_class: str) -> list[str]:
        by_original = class_success_parts[result_class]
        for item in items:
            by_original.setdefault(original_id(item), set()).add(
                str(item["segment_id"])
            )
        completed: list[str] = []
        for item in items:
            owner = original_id(item)
            if set(expected_part_ids(owner)) <= by_original.get(owner, set()):
                if owner not in completed:
                    completed.append(owner)
                class_failed_originals[result_class].discard(owner)
        return completed

    def maybe_complete(original: str) -> None:
        required = required_modes.get(original, set())
        if not required:
            return
        if all(
            original in class_success_parts[result_class]
            and not class_failed_originals[result_class].__contains__(original)
            for result_class in required
        ):
            completed_original_ids.add(original)

    state = StageRunState(
        project=project,
        stage="terminology",
        config=config,
        metadata=metadata,
        segments=segments,
        prompt=prompt,
        fingerprint=fingerprint,
        resume_run_id=resume_run_id,
        warnings=warnings,
        run_id=run_id,
        run_dir=run_dir,
        continuation_index=continuation_index,
        on_usage=on_usage,
        preparation_started_at=preparation_started_at,
    )

    def report_progress() -> None:
        if on_progress is not None:
            on_progress(
                len(selected) - len(work) + len(completed_original_ids),
                len(failed_originals),
                len(selected),
            )

    report_progress()

    def summary_artifact(
        items: list[dict[str, Any]],
        *,
        status: str,
        run_request_id: str,
        text: str | None = None,
        refs: list[str] | None = None,
        error_class: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        values: list[dict[str, Any]] = []
        for item in items:
            values.append(summary_slice_provenance(item))
        boundaries = {(str(item["file_id"]), str(item["part_id"])) for item in items}
        if len(boundaries) != 1:
            raise StorageError("内容概括请求不能跨越 file_id/part_id 边界")
        file_id, part_id = next(iter(boundaries))
        source_digest = _digest(values)
        input_digest = _digest(
            [
                {"segment_id": value["segment_id"], "model_text": value["model_text"]}
                for value in values
            ]
        )
        prompt_digest = summary_prompt_digest_for(items)
        fragment_prompt_digest = fragment_prompt_digest_for(items)
        source_range = {
            "file_id": file_id,
            "part_id": part_id,
            "segment_ids": list(dict.fromkeys(value["segment_id"] for value in values)),
            "segments": values,
        }
        summary_id = (
            "SUMMARY-FRAGMENT-"
            + _digest(
                [
                    file_id,
                    part_id,
                    source_digest,
                    input_digest,
                    prompt_digest,
                    config["llm"]["model"],
                    config["project"]["target_language"],
                ]
            )[7:31].upper()
        )
        record = record_header(
            "content_summary",
            str(metadata["project_id"]),
            record_id=summary_id,
            kind="fragment",
            file_id=file_id,
            part_id=part_id,
            status=status,
            text=text,
            source_range=source_range,
            source_digest=source_digest,
            input_digest=input_digest,
            prompt_digest=prompt_digest,
            fragment_prompt_digest=fragment_prompt_digest,
            model=str(config["llm"]["model"]),
            target_language=str(config["project"]["target_language"]),
            run_id=run_id,
            refs=list(refs or []),
            request_id=run_request_id,
            error_class=error_class,
            error_message=error_message,
        )
        write_content_summary(project, record)
        if status == "completed":
            current_fragments = [
                item
                for item in read_content_summaries(
                    project,
                    file_id=file_id,
                    part_id=part_id,
                    kind="fragment",
                    status="completed",
                )
                if not bool(item.get("source_changed", False))
            ]
            current_segment_ids = {
                str(item["segment_id"])
                for item in segments
                if not item["is_empty"]
                and str(item["file_id"]) == file_id
                and str(item["part_id"]) == part_id
            }
            source_segment_ids = list(
                dict.fromkeys(
                    str(value.get("original_segment_id") or value.get("segment_id"))
                    for value in values
                    if value.get("original_segment_id") or value.get("segment_id")
                )
            )
            if (
                len(current_fragments) == 1
                and current_fragments[0].get("record_id") == record["record_id"]
                and set(source_segment_ids) == current_segment_ids
            ):
                full_record = {
                    **record,
                    "record_id": (
                        "SUMMARY-FULL-"
                        + _digest(
                            [
                                file_id,
                                part_id,
                                record["record_id"],
                                record.get("text"),
                            ]
                        )[7:31].upper()
                    ),
                    "kind": "full",
                    "refs": source_segment_ids,
                    "provenance": {
                        "origin": "adopted_fragment",
                        "artifact_ids": [str(record["record_id"])],
                        "source_ranges": [source_range],
                    },
                }
                publish_content_summary_fulls(project, [full_record])
        return record

    def mark_failed(
        items: list[dict[str, Any]],
        result_class: str,
        message: str,
        *,
        failure_category: str = "format_error",
        count_failure: bool = True,
    ) -> None:
        for item in items:
            owner = original_id(item)
            class_failed_originals[result_class].add(owner)
            failed_originals.add(owner)
            if count_failure:
                failure_counts[failure_category] += 1
            maybe_complete(owner)
        report_progress()

    async def process_summary_once(
        chunk: ChunkPlan,
        initial_parent_request_id: str | None = None,
    ) -> tuple[int, int]:
        unresolved = list(chunk.segments)
        parent_request_id = initial_parent_request_id
        response_mode = TerminologyResponseMode(str(unresolved[0][_SUMMARY_MODE_KEY]))
        failed_class = response_mode
        for format_attempt in range(config["retry"]["format_max_attempts"] + 1):
            for item in unresolved:
                item[_SUMMARY_MODE_KEY] = failed_class.value
            if failed_class is not TerminologyResponseMode.TERMS_ONLY:
                observe_summary_request(unresolved)
            payload = payload_builder(unresolved)
            if format_attempt:
                payload["format_correction"] = _FORMAT_CORRECTION[
                    prompt_language_for_items(unresolved)
                ]
            actual_mode, requirements, prompt_text = prompt_details_for_items(
                unresolved
            )
            messages = render_messages(prompt_text, payload)
            request_id = f"REQ-{uuid.uuid4().hex[:12].upper()}"
            estimated = _request_estimate(messages, config, request_id)
            await record_prompt_variant(actual_mode, prompt_text, requirements)
            try:
                response, _ = await state.llm.chat(
                    messages=messages,
                    temperature=config["llm"]["temperature_terminology"],
                    estimated_input_tokens=estimated,
                    request_id=request_id,
                    parent_request_id=parent_request_id,
                )
                source_refs = tuple(str(index) for index, _ in enumerate(unresolved, 1))
                parsed = parse_terminology_response(
                    response.content,
                    mode=failed_class,
                    source_refs=source_refs,
                    source_texts=tuple(
                        segment_model_source(item) for item in unresolved
                    ),
                )
            except FatalExternalError:
                raise
            except ContextLengthError as exc:
                if exc.segment_ids is None:
                    exc.segment_ids = tuple(
                        str(item["segment_id"]) for item in unresolved
                    )
                raise
            except ExternalError as exc:
                if failed_class is TerminologyResponseMode.TERMS_AND_FRAGMENT_SUMMARY:
                    mark_failed(
                        unresolved,
                        "term",
                        str(exc),
                        failure_category="external_error",
                    )
                    mark_failed(
                        unresolved,
                        "summary",
                        str(exc),
                        failure_category="external_error",
                        count_failure=False,
                    )
                    summary_artifact(
                        unresolved,
                        status="failed",
                        run_request_id=request_id,
                        error_class="external_error",
                        error_message=str(exc),
                    )
                else:
                    result_class = (
                        "summary"
                        if failed_class is TerminologyResponseMode.SUMMARY_ONLY
                        else "term"
                    )
                    mark_failed(
                        unresolved,
                        result_class,
                        str(exc),
                        failure_category="external_error",
                    )
                    if result_class == "summary":
                        summary_artifact(
                            unresolved,
                            status="failed",
                            run_request_id=request_id,
                            error_class="external_error",
                            error_message=str(exc),
                        )
                return 0, len(unresolved)

            envelope_ok = not parsed.global_error_codes
            term_ok = failed_class is TerminologyResponseMode.SUMMARY_ONLY or (
                envelope_ok and parsed.terms_complete
            )
            summary_ok = failed_class is TerminologyResponseMode.TERMS_ONLY or (
                envelope_ok and parsed.summary_complete
            )
            async with write_lock:
                if term_ok and parsed.terms:
                    append_jsonl(
                        project,
                        project / "terminology" / "candidates.jsonl",
                        record_header(
                            "terminology_candidates",
                            str(metadata["project_id"]),
                            stage="terminology",
                            status="completed",
                            run_id=run_id,
                            request_id=request_id,
                            active_task_id=task_id,
                            stage_fingerprint=fingerprint,
                            segment_ids=[item["segment_id"] for item in unresolved],
                            terms=list(parsed.terms),
                        ),
                    )
                if summary_ok and parsed.summaries:
                    items_by_ref = {
                        str(index): item
                        for index, item in enumerate(unresolved, start=1)
                    }
                    for summary in parsed.summaries:
                        summary_items = [
                            items_by_ref[ref]
                            for ref in sorted(summary["refs"], key=int)
                        ]
                        summary_artifact(
                            summary_items,
                            status="completed",
                            run_request_id=request_id,
                            text=str(summary["text"]),
                            refs=[
                                str(index)
                                for index in range(1, len(summary_items) + 1)
                            ],
                        )
                elif failed_class is not TerminologyResponseMode.TERMS_ONLY:
                    summary_artifact(
                        unresolved,
                        status="failed",
                        run_request_id=request_id,
                        error_class="format_error",
                        error_message="; ".join(parsed.errors[:3])
                        or "响应缺少有效 summary",
                    )
                if term_ok and failed_class is not TerminologyResponseMode.SUMMARY_ONLY:
                    for owner in mark_class_success(unresolved, "term"):
                        if owner not in term_scan_recorded:
                            append_jsonl(
                                project,
                                project / "terminology" / "scans.jsonl",
                                record_header(
                                    "terminology_scan",
                                    str(metadata["project_id"]),
                                    stage="terminology",
                                    segment_id=owner,
                                    status="completed",
                                    run_id=run_id,
                                    request_id=request_id,
                                    active_task_id=task_id,
                                    stage_fingerprint=fingerprint,
                                ),
                            )
                            term_scan_recorded.add(owner)
                        maybe_complete(owner)
                if (
                    summary_ok
                    and failed_class is not TerminologyResponseMode.TERMS_ONLY
                ):
                    for owner in mark_class_success(unresolved, "summary"):
                        maybe_complete(owner)
            if term_ok and summary_ok:
                return len(unresolved), 0
            if (
                not envelope_ok
                and response_mode is TerminologyResponseMode.TERMS_AND_FRAGMENT_SUMMARY
            ):
                next_class = "joint"
            else:
                next_class = "term" if not term_ok else "summary"
            if format_attempt >= config["retry"]["format_max_attempts"]:
                result_class = "term" if not term_ok else "summary"
                mark_failed(unresolved, result_class, "; ".join(parsed.errors[:3]))
                return 0, len(unresolved)
            failed_class = (
                response_mode
                if next_class == "joint"
                else TerminologyResponseMode.TERMS_ONLY
                if next_class == "term"
                else TerminologyResponseMode.SUMMARY_ONLY
            )
            parent_request_id = request_id
        return 0, len(unresolved)

    async def process_once(
        chunk: ChunkPlan,
        initial_parent_request_id: str | None = None,
    ) -> tuple[int, int]:
        if (
            str(
                chunk.segments[0].get(
                    _SUMMARY_MODE_KEY, TerminologyResponseMode.TERMS_ONLY.value
                )
            )
            != TerminologyResponseMode.TERMS_ONLY.value
        ):
            return await process_summary_once(chunk, initial_parent_request_id)
        unresolved = list(chunk.segments)
        parent_request_id = initial_parent_request_id
        parse_errors: list[str] = []
        for format_attempt in range(config["retry"]["format_max_attempts"] + 1):
            payload = payload_builder(unresolved)
            if format_attempt:
                payload["format_correction"] = _FORMAT_CORRECTION[
                    prompt_language_for_items(unresolved)
                ]
            actual_mode, requirements, prompt_text = prompt_details_for_items(
                unresolved
            )
            messages = render_messages(prompt_text, payload)
            request_id = f"REQ-{uuid.uuid4().hex[:12].upper()}"
            estimated = _request_estimate(messages, config, request_id)
            await record_prompt_variant(actual_mode, prompt_text, requirements)
            try:
                response, _ = await state.llm.chat(
                    messages=messages,
                    temperature=config["llm"]["temperature_terminology"],
                    estimated_input_tokens=estimated,
                    request_id=request_id,
                    parent_request_id=parent_request_id,
                )
                terms, parse_errors, response_complete = _validate_term_items(
                    response.content
                )
            except FatalExternalError:
                raise
            except ContextLengthError as exc:
                if exc.segment_ids is None:
                    exc.segment_ids = tuple(
                        str(segment["segment_id"]) for segment in unresolved
                    )
                raise
            except ExternalError as exc:
                async with write_lock:
                    for segment in unresolved:
                        segment_id = part_original.get(
                            str(segment["segment_id"]), str(segment["segment_id"])
                        )
                        if segment_id in failed_originals:
                            continue
                        failed_originals.add(segment_id)
                        failure_counts["external_error"] += 1
                        report_progress()
                        append_jsonl(
                            project,
                            project / "terminology" / "scans.jsonl",
                            record_header(
                                "terminology_scan",
                                str(metadata["project_id"]),
                                stage="terminology",
                                segment_id=segment_id,
                                status="failed",
                                run_id=run_id,
                                request_id=request_id,
                                active_task_id=task_id,
                                stage_fingerprint=fingerprint,
                                error_class=("external_error"),
                                error_message=str(exc),
                            ),
                        )
                return 0, len(
                    {
                        part_original.get(
                            str(segment["segment_id"]), str(segment["segment_id"])
                        )
                        for segment in unresolved
                    }
                )
            async with write_lock:
                if terms:
                    append_jsonl(
                        project,
                        project / "terminology" / "candidates.jsonl",
                        record_header(
                            "terminology_candidates",
                            str(metadata["project_id"]),
                            stage="terminology",
                            status="completed",
                            run_id=run_id,
                            request_id=request_id,
                            active_task_id=task_id,
                            stage_fingerprint=fingerprint,
                            segment_ids=[
                                segment["segment_id"] for segment in unresolved
                            ],
                            terms=terms,
                        ),
                    )
            if not response_complete:
                logger.warning(
                    "format correction request=%s attempt=%d errors=%d",
                    request_id,
                    format_attempt + 1,
                    len(parse_errors),
                )
                if format_attempt < config["retry"]["format_max_attempts"]:
                    parent_request_id = request_id
                    continue
                exc = ValueError("; ".join(parse_errors[:3]))
                async with write_lock:
                    for segment in unresolved:
                        segment_id = part_original.get(
                            str(segment["segment_id"]), str(segment["segment_id"])
                        )
                        if segment_id in failed_originals:
                            continue
                        failed_originals.add(segment_id)
                        failure_counts["format_error"] += 1
                        report_progress()
                        append_jsonl(
                            project,
                            project / "terminology" / "scans.jsonl",
                            record_header(
                                "terminology_scan",
                                str(metadata["project_id"]),
                                stage="terminology",
                                segment_id=segment_id,
                                status="failed",
                                run_id=run_id,
                                request_id=request_id,
                                active_task_id=task_id,
                                stage_fingerprint=fingerprint,
                                error_class="format_error",
                                error_message=str(exc),
                            ),
                        )
                return 0, len(
                    {
                        part_original.get(
                            str(segment["segment_id"]), str(segment["segment_id"])
                        )
                        for segment in unresolved
                    }
                )
            async with write_lock:
                completed_originals: list[str] = []
                for segment in unresolved:
                    request_segment_id = str(segment["segment_id"])
                    original_id = part_original.get(request_segment_id)
                    if original_id is None:
                        completed_originals.append(request_segment_id)
                        continue
                    part_success.setdefault(original_id, set()).add(request_segment_id)
                    if (
                        original_id not in failed_originals
                        and set(original_parts[original_id])
                        <= part_success[original_id]
                    ):
                        completed_originals.append(original_id)
                for segment_id in completed_originals:
                    append_jsonl(
                        project,
                        project / "terminology" / "scans.jsonl",
                        record_header(
                            "terminology_scan",
                            str(metadata["project_id"]),
                            stage="terminology",
                            segment_id=segment_id,
                            status="completed",
                            run_id=run_id,
                            request_id=request_id,
                            active_task_id=task_id,
                            stage_fingerprint=fingerprint,
                        ),
                    )
                    completed_original_ids.add(segment_id)
                report_progress()
            logger.info(
                "chunk complete chunk=%s completed=%d",
                chunk.chunk_id or "runtime",
                len(completed_originals),
            )
            return len(completed_originals), 0
        return 0, len(unresolved)

    async def record_preflight_failure(
        failed: list[dict[str, Any]],
    ) -> None:
        async with write_lock:
            for segment in failed:
                segment_id = str(segment["segment_id"])
                mode = TerminologyResponseMode(
                    str(
                        segment.get(
                            _SUMMARY_MODE_KEY, TerminologyResponseMode.TERMS_ONLY.value
                        )
                    )
                )
                if mode is not TerminologyResponseMode.TERMS_ONLY:
                    owner = original_id(segment)
                    class_failed_originals["summary"].add(owner)
                    summary_artifact(
                        [segment],
                        status="failed",
                        run_request_id=f"PRECHECK-{segment_id}",
                        error_class="context_error",
                        error_message="单 Segment 超过模型限制且内部拆分已关闭",
                    )
                    maybe_complete(owner)
                if mode is TerminologyResponseMode.SUMMARY_ONLY:
                    if segment_id not in failed_originals:
                        failed_originals.add(segment_id)
                        failure_counts["context_error"] += 1
                    report_progress()
                    continue
                if segment_id in failed_originals:
                    continue
                failed_originals.add(segment_id)
                failure_counts["context_error"] += 1
                report_progress()
                append_jsonl(
                    project,
                    project / "terminology" / "scans.jsonl",
                    record_header(
                        "terminology_scan",
                        str(metadata["project_id"]),
                        stage="terminology",
                        segment_id=segment_id,
                        status="failed",
                        run_id=run_id,
                        request_id=None,
                        active_task_id=task_id,
                        stage_fingerprint=fingerprint,
                        error_class="context_error",
                        error_message=("单 Segment 超过模型限制且内部拆分已关闭"),
                    ),
                )

    async def record_context_failure(
        items: list[dict[str, Any]],
    ) -> None:
        original_id = part_original.get(
            str(items[0]["segment_id"]), str(items[0]["segment_id"])
        )
        mode = TerminologyResponseMode(
            str(
                items[0].get(
                    _SUMMARY_MODE_KEY, TerminologyResponseMode.TERMS_ONLY.value
                )
            )
        )
        async with write_lock:
            if mode is not TerminologyResponseMode.TERMS_ONLY:
                class_failed_originals["summary"].add(original_id)
                summary_artifact(
                    items,
                    status="failed",
                    run_request_id=f"CONTEXT-{original_id}",
                    error_class="context_error",
                    error_message="模型报告上下文过长",
                )
                maybe_complete(original_id)
            if mode is TerminologyResponseMode.SUMMARY_ONLY:
                failed_originals.add(original_id)
                failure_counts["context_error"] += 1
                report_progress()
                return
            if original_id not in failed_originals:
                failed_originals.add(original_id)
                failure_counts["context_error"] += 1
                report_progress()
                append_jsonl(
                    project,
                    project / "terminology" / "scans.jsonl",
                    record_header(
                        "terminology_scan",
                        str(metadata["project_id"]),
                        stage="terminology",
                        segment_id=original_id,
                        status="failed",
                        run_id=run_id,
                        request_id=None,
                        active_task_id=task_id,
                        stage_fingerprint=fingerprint,
                        error_class="context_error",
                        error_message="模型报告上下文过长",
                    ),
                )

    published_now = False
    task_completed_ids: set[str] = set()

    async def before_finalize() -> None:
        nonlocal published, published_now, task_completed_ids
        all_nonempty = [segment for segment in segments if not segment["is_empty"]]
        task_scans = [
            record
            for record in read_jsonl(
                project,
                project / "terminology" / "scans.jsonl",
                task_id=task_id,
            )
        ]
        task_completed_ids = {
            str(record["segment_id"])
            for record in task_scans
            if record.get("status") == "completed"
        }
        if (
            (
                not include_summaries
                or any("term" in modes for modes in required_modes.values())
            )
            and active
            and active.get("status") == "active"
            and all(
                str(segment["segment_id"]) in task_completed_ids
                for segment in all_nonempty
            )
        ):
            published = _merge_and_publish_terms(
                project,
                task_id=task_id,
                project_id=str(metadata["project_id"]),
                published_run_id=run_id,
            )
            published_now = True

    def completed_count() -> int:
        if include_summaries:
            return len(selected) - len(work) + len(completed_original_ids)
        if resume_run_id:
            return sum(
                str(segment["segment_id"]) in task_completed_ids for segment in selected
            )
        return len(completed_original_ids)

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
            failed_count=lambda: len(failed_originals),
            exception_completed=lambda: (
                len(selected) - len(work) + len(completed_original_ids)
            ),
            exception_failed=lambda: len(work) - len(completed_original_ids),
            failure_counts=failure_counts,
            http_client=http_client,
            runtime_request_observer=observe_summary_request,
            runtime_split_observer=observe_summary_split,
        )
    except asyncio.CancelledError:
        update_summary_run_status("interrupted")
        raise
    except Exception:
        update_summary_run_status("failed")
        raise
    failed = len(failed_originals)
    all_nonempty = [segment for segment in segments if not segment["is_empty"]]
    if include_summaries:
        update_summary_run_status("completed" if failed == 0 else "failed")
    pending_result = (
        len(work) - len(completed_original_ids) - failed
        if include_summaries
        else len(all_nonempty) - len(task_completed_ids)
    )
    logger.info(
        "run complete run=%s completed=%d failed=%d pending=%d",
        run_id,
        len(completed_original_ids),
        failed,
        pending_result,
    )
    return {
        "stage": "terminology",
        "run_id": run_id,
        "active_task_id": task_id,
        "completed": len(completed_original_ids),
        "reused": len(selected) - len(work),
        "failed": failed,
        "failure_counts": dict(failure_counts),
        "pending": pending_result,
        "published": published_now,
        "terms_revision": published["terms_revision"] if published else None,
        "warnings": warnings,
        "usage": usage,
    }
