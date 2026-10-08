"""Experimental terminology requests executed by the translation runner."""

from __future__ import annotations

import json
from collections import Counter
import uuid
from pathlib import Path
from typing import Any

from .errors import (
    ContextLengthError,
    ExternalError,
    FatalExternalError,
    IncompleteError,
    StorageError,
)
from .execution import (
    Scope,
    localize_request_ids,
    render_messages,
    segment_model_source,
    stage_fingerprint,
)
from .llm_response import (
    TerminologyResponseMode,
    _terminology_declaration_error,
    _validate_terminology_record,
    parse_jsonl_document,
    response_record_types,
    parse_terminology_response,
)
from .sqlite_storage import (
    append_jsonl,
    read_json,
    record_exists,
    record_header,
    latest_stage_states,
    terminology_scan_state,
    write_json,
    read_summary_participation,
    mark_content_summary_fragments_stale,
    write_summary_run,
)
from .stage_runtime import (
    _FORMAT_CORRECTION,
    _prompt_factory,
    _request_estimate,
    _project_context,
    prompt_preflight,
    prompt_middle_digests,
)
from .summary_provenance import digest, write_fragment_summary
from .term_library import _merge_and_publish_terms, load_terms


def draft_run_context(
    project: Path,
    frozen_run_options: dict[str, dict[str, str]] | None = None,
    include_summaries: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    config, metadata, files, segments = _project_context(
        project, stage="terminology", frozen_run_options=frozen_run_options
    )
    translation_config, _, _, _ = _project_context(
        project, stage="translation", frozen_run_options=frozen_run_options
    )
    config["_draft_prompt_requirements"] = {
        "term": {
            file_id: dict(values)
            for file_id, values in config[
                "_document_adapter_prompt_requirements"
            ].items()
        },
        "segment": translation_config["_document_adapter_prompt_requirements"],
    }
    requirements = config["_document_adapter_prompt_requirements"]
    for file_id, values in translation_config[
        "_document_adapter_prompt_requirements"
    ].items():
        target = requirements.setdefault(file_id, {})
        for language, value in values.items():
            target[language] = "\n".join(
                dict.fromkeys(item for item in (target.get(language), value) if item)
            )
    if include_summaries:
        summary_config, _, _, _ = _project_context(
            project, stage="fragment_summary", frozen_run_options=frozen_run_options
        )
        config["_fragment_prompt_requirements"] = summary_config[
            "_document_adapter_prompt_requirements"
        ]
        config["_draft_prompt_requirements"]["summary"] = config[
            "_fragment_prompt_requirements"
        ]
        for file_id, values in summary_config[
            "_document_adapter_prompt_requirements"
        ].items():
            target = requirements.setdefault(file_id, {})
            for language, value in values.items():
                target[language] = "\n".join(
                    dict.fromkeys(
                        item for item in (target.get(language), value) if item
                    )
                )
        config["chunking"]["cross_boundary_batching"] = [
            stage
            for stage in config["chunking"]["cross_boundary_batching"]
            if stage != "translation"
        ]
    config["_include_summaries"] = include_summaries
    config["llm"]["temperature_translation"] = config["llm"]["temperature_terminology"]
    config["context"]["translation"] = config["context"]["terminology"]
    config["_draft_translation"] = True
    return config, metadata, files, segments


def draft_scan_options(
    project: Path,
    language: str | None,
    include_summaries: bool = False,
    resume_run_id: str | None = None,
) -> dict[str, Any]:
    config, metadata, _, segments = draft_run_context(
        project, include_summaries=include_summaries
    )
    selected = [item for item in segments if not item["is_empty"]]
    ids = {str(item["segment_id"]) for item in selected}
    states = latest_stage_states(project, "translation", ids)
    translation_done = {
        key for key, state in states.items() if isinstance(state.get("completed"), dict)
    }
    # Construction resolves existing scan state without writing a task or Run.
    preflight = prompt_preflight(
        project,
        language,
        ("terminology", "translation")
        + (("fragment_summary",) if include_summaries else ()),
    )
    if not preflight["ok"]:
        return {"include_draft_translation": True, "draft_prompt_preflight": preflight}
    scan = DraftTerminologyScan(
        project,
        metadata,
        config,
        Scope(),
        resume_run_id,
        str(preflight["language"]),
        selected,
        translation_done,
    )
    library = load_terms(project)
    fingerprint = draft_translation_fingerprint(
        project, config, int(library["terms_revision"]) if library else None
    )
    mismatched = {
        key
        for key in translation_done
        if states[key]["completed"].get("stage_fingerprint") != fingerprint
    }
    if scan.term_fingerprints and scan.term_fingerprints != {scan.term_fingerprint}:
        mismatched.update(scan.term_done)
    completed = scan.completed()
    return {
        "include_draft_translation": True,
        "draft_prompt_preflight": preflight,
        "draft_progress": scan.progress(),
        "completed": len(completed),
        "pending": len(ids - completed),
        "mismatched_fingerprint_completed": len(mismatched),
        "current_fingerprint_completed": len(completed - mismatched),
    }


def draft_translation_fingerprint(
    project: Path, config: dict[str, Any], terms_revision: int | None
) -> str:
    prompts = {
        f"{stage}:{language}": value
        for stage in (
            ("terminology", "translation", "fragment_summary")
            if config.get("_include_summaries")
            else ("terminology", "translation")
        )
        for language, value in prompt_middle_digests(project, stage).items()
    }
    return stage_fingerprint(
        config, "translation", prompts, terms_revision=terms_revision
    )


class DraftTerminologyScan:
    """Keep scan results independent while reusing translation validation and storage."""

    def __init__(
        self,
        project: Path,
        metadata: dict[str, Any],
        config: dict[str, Any],
        scope: Scope,
        resume_run_id: str | None,
        language: str,
        selected: list[dict[str, Any]],
        translation_done: set[str],
    ) -> None:
        self.project = project
        self.project_id = str(metadata["project_id"])
        self.config = config
        self.language = language
        self.selected = {str(item["segment_id"]) for item in selected}
        self.selected_items = {str(item["segment_id"]): item for item in selected}
        self.translation_done = translation_done
        self.scope = scope
        self.summary_run: dict[str, Any] | None = None
        self.include_summaries = bool(config.get("_include_summaries"))
        self.summary_failed: set[str] = set()
        self.summary_part_done: dict[str, set[str]] = {}
        boundaries = (
            {
                (str(row["file_id"]), str(row["part_id"]))
                for row in read_summary_participation(project)
                if row["selected"]
            }
            if self.include_summaries
            else set()
        )
        if resume_run_id and self.include_summaries:
            saved = read_json(
                project, project / "runs" / resume_run_id / "manifest.json"
            )
            boundaries = {
                (str(row["file_id"]), str(row["part_id"]))
                for row in saved["summary_selection"]
            }
        self.summary_boundaries = boundaries
        self.summary_selected = {
            str(item["segment_id"])
            for item in selected
            if (str(item["file_id"]), str(item["part_id"])) in boundaries
        }
        self.fragment_factory = (
            _prompt_factory(project, "fragment_summary", language)
            if self.include_summaries
            else None
        )
        self.summary_done: set[str] = set()
        if self.summary_selected and not scope.force:
            from .stage_terminology import _summary_covered_segments

            self.summary_done = _summary_covered_segments(
                project,
                [
                    item
                    for item in selected
                    if str(item["segment_id"]) in self.summary_selected
                ],
                prompt_digests=lambda items: {digest(self.fragment_prompt(items))},
                model=str(config["llm"]["model"]),
                target_language=str(config["project"]["target_language"]),
            )
        self.translation_failed: set[str] = set()
        self.term_failed: set[str] = set()
        self.failure_counts: Counter[str] = Counter()
        self.part_done: dict[str, set[str]] = {}
        self.active_path = project / "terminology" / "active_task.json"
        active = (
            read_json(project, self.active_path)
            if record_exists(project, self.active_path)
            else None
        )
        create_task = resume_run_id is None and (
            scope.force or active is None or active.get("status") == "partial_published"
        )
        if resume_run_id:
            manifest = read_json(
                project, project / "runs" / resume_run_id / "manifest.json"
            )
            if bool(manifest.get("include_summaries", False)) != self.include_summaries:
                raise StorageError("续用 Run 必须保持原有概括选项")
            if not manifest.get("include_draft_translation"):
                raise StorageError("续用 Run 的粗翻选项与当前请求不一致")
            if active is None or active.get("active_task_id") != manifest.get(
                "active_task_id"
            ):
                raise StorageError("术语 Run 的 active task 不再可用")
        self.task_id = (
            f"TERM-TASK-{uuid.uuid4().hex[:10].upper()}"
            if create_task
            else str(active["active_task_id"])
        )
        self.term_done, self.term_fingerprints = terminology_scan_state(
            project, self.task_id, self.selected
        )
        self.term_fingerprint = stage_fingerprint(
            config,
            "terminology",
            {
                **prompt_middle_digests(project, "terminology"),
                **{
                    f"{stage}:{key}": value
                    for stage in (
                        ("translation", "fragment_summary")
                        if self.include_summaries
                        else ("translation",)
                    )
                    for key, value in prompt_middle_digests(project, stage).items()
                },
            },
        )
        self.active = (
            record_header(
                "terminology_task",
                self.project_id,
                record_id=self.task_id,
                active_task_id=self.task_id,
                status="active",
                initial_stage_fingerprint=self.term_fingerprint,
            )
            if create_task
            else active
        )
        self.work = []
        for item in selected:
            term = str(item["segment_id"]) not in self.term_done
            translation = str(item["segment_id"]) not in self.translation_done
            summary = (
                str(item["segment_id"]) in self.summary_selected - self.summary_done
            )
            if term or translation or summary:
                self.work.append(
                    {
                        **item,
                        "_draft_terms": term,
                        "_draft_translation": translation,
                        "_draft_summary": summary,
                    }
                )
        if any(item["_draft_terms"] for item in self.work):
            self.active = {**self.active, "status": "active"}
        self.prompt_factories = {
            mode: _prompt_factory(
                project,
                "terminology",
                language,
                response_mode=mode,
                require_term_declaration=True,
            )
            for mode in (
                TerminologyResponseMode.TERMS_ONLY,
                TerminologyResponseMode.TERMS_AND_TRANSLATION,
                TerminologyResponseMode.TRANSLATION_ONLY,
                TerminologyResponseMode.TERMS_AND_FRAGMENT_SUMMARY,
                TerminologyResponseMode.SUMMARY_ONLY,
                TerminologyResponseMode.TERMS_TRANSLATION_SUMMARY,
                TerminologyResponseMode.TRANSLATION_SUMMARY,
            )
            if self.include_summaries or "summary" not in response_record_types(mode)
        }

    def start(self, run_id: str) -> None:
        write_json(self.project, self.active_path, self.active)
        if self.include_summaries and any(
            item.get("_draft_summary") for item in self.work
        ):
            boundaries = sorted(
                {
                    (str(item["file_id"]), str(item["part_id"]))
                    for item in self.work
                    if item.get("_draft_summary")
                }
            )
            if self.scope.force:
                mark_content_summary_fragments_stale(self.project, set(boundaries))
            self.summary_run = record_header(
                "summary_run",
                self.project_id,
                record_id=run_id,
                run_id=run_id,
                mode="fragment",
                status="running",
                source_ranges=[],
                input_digest=digest([]),
                prompt_digest=digest([]),
                model=str(self.config["llm"]["model"]),
                target_language=str(self.config["project"]["target_language"]),
                include_summaries=True,
                include_draft_translation=True,
                selection=[
                    {"file_id": file_id, "part_id": part_id}
                    for file_id, part_id in boundaries
                ],
            )
            write_summary_run(self.project, self.summary_run)

    def finish_summary_run(self, run_id: str) -> None:
        if self.summary_run is None:
            return
        manifest = read_json(
            self.project, self.project / "runs" / run_id / "manifest.json"
        )
        self.summary_run.update(
            status=manifest["status"],
            usage=manifest.get("usage"),
            warnings=manifest.get("warnings", []),
        )
        write_summary_run(self.project, self.summary_run)

    @staticmethod
    def mode(items: list[dict[str, Any]]) -> TerminologyResponseMode:
        terms = bool(items[0].get("_draft_terms"))
        translation = bool(items[0].get("_draft_translation"))
        summary = bool(items[0].get("_draft_summary"))
        return {
            (True, True, True): TerminologyResponseMode.TERMS_TRANSLATION_SUMMARY,
            (False, True, True): TerminologyResponseMode.TRANSLATION_SUMMARY,
            (True, False, True): TerminologyResponseMode.TERMS_AND_FRAGMENT_SUMMARY,
            (False, False, True): TerminologyResponseMode.SUMMARY_ONLY,
            (True, True, False): TerminologyResponseMode.TERMS_AND_TRANSLATION,
            (True, False, False): TerminologyResponseMode.TERMS_ONLY,
            (False, True, False): TerminologyResponseMode.TRANSLATION_ONLY,
        }[terms, translation, summary]

    def prompt(self, items: list[dict[str, Any]], requirements: tuple[str, ...]) -> str:
        return self.prompt_factories[self.mode(items)](requirements)

    def requirements(
        self,
        items: list[dict[str, Any]],
        mode: TerminologyResponseMode | None = None,
    ) -> tuple[str, ...]:
        kinds = response_record_types(mode or self.mode(items))
        return tuple(
            dict.fromkeys(
                requirement
                for kind, by_file in self.config["_draft_prompt_requirements"].items()
                if kind in kinds
                for item in items
                if (
                    requirement := by_file.get(str(item["file_id"]), {}).get(
                        self.language
                    )
                )
            )
        )

    def progress(self) -> dict[str, dict[str, int]]:
        progress = {
            name: {
                "completed": len(done & self.selected),
                "failed": len(failed & self.selected),
                "total": len(self.selected),
            }
            for name, done, failed in (
                ("terminology", self.term_done, self.term_failed),
                ("translation", self.translation_done, self.translation_failed),
            )
        }

        if self.include_summaries:
            progress["content_summary"] = {
                "completed": len(self.summary_done),
                "failed": len(self.summary_failed),
                "total": len(self.summary_selected),
            }
        return progress

    def fragment_prompt(self, items: list[dict[str, Any]]) -> str:
        requirements = self.config["_fragment_prompt_requirements"]
        values = tuple(
            dict.fromkeys(
                requirements.get(str(item["file_id"]), {}).get(self.language, "")
                for item in items
            )
        )
        assert self.fragment_factory is not None
        return self.fragment_factory(values)

    def record_summary(
        self,
        items: list[dict[str, Any]],
        *,
        state: Any,
        request_id: str,
        part_original: dict[str, str],
        original_parts: dict[str, list[str]],
        text: str | None = None,
        error: str | None = None,
        error_class: str = "format_error",
        generated_prompt: str | None = None,
        response_mode: str | None = None,
    ) -> None:
        values = []
        for item in items:
            part_id = str(item["segment_id"])
            owner = part_original.get(part_id, part_id)
            source = str(item["source"])
            model_text = segment_model_source(item)
            original = self.selected_items[owner]
            index = original_parts.get(owner, [part_id]).index(part_id)
            values.append(
                {
                    "segment_id": owner,
                    "original_segment_id": owner,
                    "slice_id": f"{owner}#slice-{index:04d}-{digest(source)[7:15]}-{digest(model_text)[7:15]}",
                    "slice_index": index,
                    "source": source,
                    "source_digest": digest(source),
                    "original_source_digest": digest(str(original["source"])),
                    "model_text": model_text,
                    "model_text_digest": digest(model_text),
                    "original_model_text_digest": digest(
                        segment_model_source(original)
                    ),
                }
            )
        write_fragment_summary(
            self.project,
            project_id=self.project_id,
            config=self.config,
            items=items,
            values=values,
            run_id=state.run_id,
            request_id=request_id,
            prompt_digest=digest(
                generated_prompt
                if generated_prompt is not None
                else self.prompt(items, ())
            ),
            fragment_prompt_digest=digest(self.fragment_prompt(items)),
            segments=list(self.selected_items.values()),
            warnings=state.warnings,
            status="failed" if error else "completed",
            text=text,
            refs=[str(item["segment_id"]) for item in items],
            error_class=error_class if error else None,
            error_message=error,
        )
        if self.summary_run is not None:
            self.summary_run["source_ranges"].append(
                {
                    "file_id": str(items[0]["file_id"]),
                    "part_id": str(items[0]["part_id"]),
                    "segments": values,
                }
            )
            self.summary_run["input_digest"] = digest(self.summary_run["source_ranges"])
            self.summary_run["prompt_digest"] = digest(
                generated_prompt
                if generated_prompt is not None
                else self.prompt(items, ())
            )
            self.summary_run["prompt_languages"] = {
                self.mode(items).value: self.language
            }
            write_summary_run(self.project, self.summary_run)
        for item in items:
            part_id = str(item["segment_id"])
            owner = part_original.get(part_id, part_id)
            if error:
                if owner not in self.summary_failed:
                    self.failure_counts[error_class] += 1
                self.summary_failed.add(owner)
            else:
                self.summary_part_done.setdefault(owner, set()).add(part_id)
                if (
                    set(original_parts.get(owner, [part_id]))
                    <= self.summary_part_done[owner]
                ):
                    self.summary_done.add(owner)
                    self.summary_failed.discard(owner)

    def completed(self) -> set[str]:
        return (self.selected & self.term_done & self.translation_done) - (
            self.summary_selected - self.summary_done
        )

    def failed(self) -> set[str]:
        return self.selected & (
            self.term_failed | self.translation_failed | self.summary_failed
        )

    def record_terms(
        self,
        items: list[dict[str, Any]],
        terms: list[dict[str, Any]],
        run_id: str,
        request_id: str,
        part_original: dict[str, str],
        original_parts: dict[str, list[str]],
    ) -> None:
        if terms:
            append_jsonl(
                self.project,
                self.project / "terminology" / "candidates.jsonl",
                record_header(
                    "terminology_candidates",
                    self.project_id,
                    stage="terminology",
                    status="completed",
                    run_id=run_id,
                    request_id=request_id,
                    active_task_id=self.task_id,
                    stage_fingerprint=self.term_fingerprint,
                    segment_ids=[str(item["segment_id"]) for item in items],
                    terms=terms,
                ),
            )
        for item in items:
            part_id = str(item["segment_id"])
            owner = part_original.get(part_id, part_id)
            self.part_done.setdefault(owner, set()).add(part_id)
            if (
                owner in self.term_done
                or not set(original_parts.get(owner, [part_id]))
                <= self.part_done[owner]
            ):
                continue
            append_jsonl(
                self.project,
                self.project / "terminology" / "scans.jsonl",
                record_header(
                    "terminology_scan",
                    self.project_id,
                    stage="terminology",
                    segment_id=owner,
                    status="completed",
                    run_id=run_id,
                    request_id=request_id,
                    active_task_id=self.task_id,
                    stage_fingerprint=self.term_fingerprint,
                ),
            )
            self.term_done.add(owner)
            self.term_failed.discard(owner)

    def fail_terms(
        self,
        items: list[dict[str, Any]],
        run_id: str,
        request_id: str,
        category: str,
        message: str,
        part_original: dict[str, str],
    ) -> None:
        for item in items:
            if not item.get("_draft_terms"):
                continue
            owner = part_original.get(str(item["segment_id"]), str(item["segment_id"]))
            if owner in self.term_failed or owner in self.term_done:
                continue
            self.term_failed.add(owner)
            self.failure_counts[category] += 1
            append_jsonl(
                self.project,
                self.project / "terminology" / "scans.jsonl",
                record_header(
                    "terminology_scan",
                    self.project_id,
                    stage="terminology",
                    segment_id=owner,
                    status="failed",
                    run_id=run_id,
                    request_id=request_id,
                    active_task_id=self.task_id,
                    stage_fingerprint=self.term_fingerprint,
                    error_class=category,
                    error_message=message,
                ),
            )

    async def process(
        self,
        group: list[dict[str, Any]],
        *,
        state: Any,
        payload_builder: Any,
        prompt_builder: Any,
        accept: Any,
        save_failed: Any,
        part_original: dict[str, str],
        original_parts: dict[str, list[str]],
        report_progress: Any,
        parent_request_id: str | None,
    ) -> None:
        from .stage_translation import _map_local_translation_response

        queue = [(list(group), 0, parent_request_id)]
        while queue:
            pending, attempt, parent_request_id = queue.pop(0)
            mode = self.mode(pending)
            payload, id_map = localize_request_ids(payload_builder(pending), pending)
            if "summary" in response_record_types(mode):
                payload["source_refs"] = list(id_map)
            if attempt:
                payload["format_correction"] = _FORMAT_CORRECTION[self.language]
            generated_prompt = prompt_builder(pending)
            messages = render_messages(generated_prompt, payload)
            request_id = f"REQ-{uuid.uuid4().hex[:12].upper()}"
            try:
                response, request_id = await state.llm.chat(
                    messages=messages,
                    temperature=self.config["llm"]["temperature_terminology"],
                    estimated_input_tokens=_request_estimate(
                        messages, self.config, request_id
                    ),
                    request_id=request_id,
                    parent_request_id=parent_request_id,
                    segment_id_map=id_map,
                )
            except FatalExternalError:
                raise
            except ContextLengthError as exc:
                exc.segment_ids = tuple(str(item["segment_id"]) for item in pending)
                raise
            except ExternalError as exc:
                self.fail_terms(
                    pending,
                    state.run_id,
                    request_id,
                    "external_error",
                    str(exc),
                    part_original,
                )
                for item in pending:
                    if item["_draft_translation"]:
                        await save_failed(
                            str(item["segment_id"]),
                            request_id,
                            "external_error",
                            str(exc),
                        )
                summary_items = [item for item in pending if item.get("_draft_summary")]
                if summary_items:
                    self.record_summary(
                        summary_items,
                        state=state,
                        request_id=request_id,
                        part_original=part_original,
                        original_parts=original_parts,
                        error=str(exc),
                        error_class="external_error",
                    )
                report_progress()
                continue
            document = parse_jsonl_document(
                response.content,
                record_type=response_record_types(mode),
            )
            terms: list[dict[str, Any]] = []
            term_errors: list[str] = []
            declaration_error = (
                _terminology_declaration_error(document.records_by_type)
                if "term" in response_record_types(mode)
                else None
            )
            if "summary" in response_record_types(
                mode
            ) and "term" in response_record_types(mode):
                protocol = parse_terminology_response(
                    response.content, mode=mode, source_refs=tuple(id_map)
                )
                if "invalid_order" in protocol.global_error_codes:
                    declaration_error = ("invalid_order", "概括记录必须在术语记录之前")
            if declaration_error:
                term_errors.append(declaration_error[1])
            if "term" in response_record_types(mode):
                for record in document.records_by_type["term"]:
                    error, term = _validate_terminology_record(record)
                    if error:
                        term_errors.append(error)
                    elif term is not None:
                        terms.append(term)
                if document.complete and not term_errors:
                    self.record_terms(
                        pending,
                        terms,
                        state.run_id,
                        request_id,
                        part_original,
                        original_parts,
                    )
                    pending = [{**item, "_draft_terms": False} for item in pending]
            summary_errors: list[str] = []
            if "summary" in response_record_types(mode) and not declaration_error:
                summary_content = "\n".join(
                    json.dumps(row, ensure_ascii=False)
                    for row in document.records_by_type["summary"]
                )
                if document.has_valid_end:
                    summary_content += '\n{"type":"end"}'
                parsed_summary = parse_terminology_response(
                    summary_content,
                    mode=TerminologyResponseMode.SUMMARY_ONLY,
                    source_refs=tuple(id_map),
                )
                summary_errors.extend(parsed_summary.errors)
                if document.complete and parsed_summary.summary_complete:
                    for row in parsed_summary.summaries:
                        refs = {id_map[ref] for ref in row["refs"]}
                        self.record_summary(
                            [
                                item
                                for item in pending
                                if str(item["segment_id"]) in refs
                            ],
                            state=state,
                            request_id=request_id,
                            part_original=part_original,
                            original_parts=original_parts,
                            text=row["text"],
                            generated_prompt=generated_prompt,
                            response_mode=mode.value,
                        )
                    pending = [{**item, "_draft_summary": False} for item in pending]
            translation_unresolved: set[str] = set()
            if "segment" in response_record_types(mode) and not declaration_error:
                content = "\n".join(
                    json.dumps(record, ensure_ascii=False)
                    for record in document.records_by_type["segment"]
                )
                if document.has_valid_end:
                    content += '\n{"type":"end"}'
                parsed = _map_local_translation_response(content, id_map)
                valid = parsed.valid
                translation_unresolved = set(parsed.unresolved)
                if any(
                    code not in {"missing_end", "invalid_json"}
                    for code in document.error_codes
                ):
                    valid = {}
                    translation_unresolved = set(id_map.values())
                if (document.has_valid_end and not parsed.ids_complete) or (
                    self.config["retry"]["unresolved_retry_scope"] == "chunk"
                    and translation_unresolved
                ):
                    valid = {}
                    translation_unresolved = set(id_map.values())
                for segment_id, text in valid.items():
                    try:
                        await accept(segment_id, request_id, text)
                    except IncompleteError as exc:
                        translation_unresolved.add(segment_id)
                        term_errors.append(str(exc))
                pending = [
                    {
                        **item,
                        "_draft_translation": str(item["segment_id"])
                        in translation_unresolved,
                    }
                    for item in pending
                ]
            pending = [
                item
                for item in pending
                if item["_draft_terms"]
                or item["_draft_translation"]
                or item.get("_draft_summary")
            ]
            report_progress()
            if not pending:
                continue
            if attempt == self.config["retry"]["format_max_attempts"]:
                message = (
                    "; ".join([*document.errors, *term_errors, *summary_errors])
                    or "粗翻响应的 Segment ID 缺失、重复或字段错误"
                )
                self.fail_terms(
                    pending,
                    state.run_id,
                    request_id,
                    "format_error",
                    message,
                    part_original,
                )
                for item in pending:
                    if item["_draft_translation"]:
                        await save_failed(
                            str(item["segment_id"]), request_id, "format_error", message
                        )
                summary_items = [item for item in pending if item.get("_draft_summary")]
                if summary_items:
                    self.record_summary(
                        summary_items,
                        state=state,
                        request_id=request_id,
                        part_original=part_original,
                        original_parts=original_parts,
                        error=message,
                    )
                report_progress()
                continue
            groups: dict[tuple[bool, bool, bool], list[dict[str, Any]]] = {}
            for item in pending:
                groups.setdefault(
                    (
                        item["_draft_terms"],
                        item["_draft_translation"],
                        bool(item.get("_draft_summary")),
                    ),
                    [],
                ).append(item)
            queue.extend((items, attempt + 1, request_id) for items in groups.values())

    def publish(self, run_id: str, segments: list[dict[str, Any]]) -> None:
        if self.active.get("status") != "active":
            return
        all_ids = {str(item["segment_id"]) for item in segments if not item["is_empty"]}
        completed, _ = terminology_scan_state(self.project, self.task_id, all_ids)
        if all_ids <= completed:
            _merge_and_publish_terms(
                self.project,
                task_id=self.task_id,
                project_id=self.project_id,
                published_run_id=run_id,
            )
