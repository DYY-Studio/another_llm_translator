"""Human review, atomic application and guarded rollback of consistency drafts."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from .config import load_project_config
from .decision import DecisionClient
from .errors import UsageError
from .execution import stage_fingerprint
from .locking import project_write_lock
from .plugins import normalize_model_text
from .project import load_segments, load_source_files
from .repetition_consistency import current_draft, read_draft, segment_signature
from .sqlite_storage import (latest_stage_states, read_json, read_repetition_run,
                             record_header, utc_now, write_repetition_state)
from .stage_runtime import prompt_middle_digests
from .term_library import load_terms, term_normalization
from .term_matching import _TermMatchCache
from .translation_validation import TranslationValidationContext, validate_translation_text

_BEFORE_FIELDS = ("validation_status", "validation_findings", "stage_fingerprint", "terms_revision")


def _pending(project: Path, run_id: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = read_repetition_run(project, ("pending",))
    if manifest is None:
        raise UsageError("没有待审核的重复译文草案")
    if run_id is not None and manifest["run_id"] != run_id:
        raise UsageError("待审核草案已变化，请重新打开审核")
    return manifest, read_draft(project, manifest)


def _choices(draft: dict[str, Any], values: Any) -> dict[str, Any]:
    proposals = {p["id"]: p for g in draft["groups"] for p in g["proposals"]}
    if not isinstance(values, dict) or set(values) - proposals.keys():
        raise UsageError("草案审核选择含未知建议")
    for pid, value in values.items():
        if (not isinstance(value, dict) or set(value) != {"accepted", "target"}
            or type(value["accepted"]) is not bool or not isinstance(value["target"], str)
            or value["target"] not in proposals[pid]["members"]):
            raise UsageError("草案审核选择或目标译文无效")
    return values


def save_choices(project: Path, values: Any, *, run_id: str | None = None) -> dict[str, Any]:
    with project_write_lock(project):
        manifest, draft = _pending(project, run_id)
        values = _choices(draft, values)
        manifest["choices"] = {**manifest.get("choices", {}), **values}
        warnings = write_repetition_state(project, records=[], manifests=[manifest])
        return {"choices": values, "warnings": warnings}


def discard_draft(project: Path, *, confirm: bool, run_id: str | None = None) -> dict[str, Any]:
    if confirm is not True:
        raise UsageError("必须明确确认丢弃重复译文草案")
    with project_write_lock(project):
        manifest, _ = _pending(project, run_id)
        manifest["draft_status"] = "discarded"
        warnings = write_repetition_state(project, records=[], manifests=[manifest])
        return {"discarded": True, "warnings": warnings}


def _indices(segments: list[dict[str, Any]]) -> dict[str, tuple[list[dict[str, Any]], int]]:
    parts: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in segments:
        if not item["is_empty"]:
            parts[(item["file_id"], item["part_id"])].append(item)
    return {str(item["segment_id"]): (items, i) for items in parts.values() for i, item in enumerate(items)}


def _verify_groups(draft: dict[str, Any], groups: list[dict[str, Any]],
                   segments: list[dict[str, Any]], bases: dict[str, Any], indices: dict[str, Any]) -> None:
    by_id = {str(item["segment_id"]): item for item in segments}
    sources: dict[str, set[str]] = defaultdict(set)
    file_id = draft["scope"]["only_file"]
    for item in segments:
        if not item["is_empty"] and (file_id is None or item["file_id"] == file_id):
            sources[item["source"]].add(str(item["segment_id"]))
    for group in groups:
        if sources[group["source"]] != {o["segment_id"] for o in group["occurrences"]}:
            raise UsageError("重复组成员已变化，草案已过期")
        for occurrence in group["occurrences"]:
            sid = occurrence["segment_id"]
            item = by_id.get(sid)
            if (item is None or segment_signature(item) != occurrence["signature"]
                or bases.get(sid, {}).get("record_id") != occurrence["base_result_id"]):
                raise UsageError("重复组原文或译文基准已变化，草案已过期")
            neighbors, position = indices[sid]
            previous = [str(s["segment_id"]) for s in neighbors[max(0, position - 3):position]]
            following = [str(s["segment_id"]) for s in neighbors[position + 1:position + 4]]
            if previous != occurrence["previous"] or following != occurrence["next"]:
                raise UsageError("重复组上下文成员已变化，草案已过期")
            for nid in (*previous, *following):
                if segment_signature(by_id[nid]) != draft["evidence"][nid]["signature"]:
                    raise UsageError("重复组上下文已变化，草案已过期")


async def apply_draft(project: Path, *, confirm: bool, run_id: str | None = None,
                      expected_choices: dict[str, Any] | None = None) -> dict[str, Any]:
    if confirm is not True:
        raise UsageError("必须明确确认应用重复译文草案")
    with project_write_lock(project):
        manifest, draft = _pending(project, run_id)
        choices = _choices(draft, manifest.get("choices", {}))
        if expected_choices is not None and expected_choices != choices:
            raise UsageError("草案审核选择已变化，请重新打开审核")
        accepted = [(g, p, choices.get(p["id"], {"accepted": True, "target": p["target"]}))
                    for g in draft["groups"] for p in g["proposals"]
                    if choices.get(p["id"], {}).get("accepted", True)]
        groups = list({g["id"]: g for g, _, _ in accepted}.values())
        segments = load_segments(project)
        indices = _indices(segments)
        by_id = {str(s["segment_id"]): s for s in segments}
        ids = [o["segment_id"] for g in groups for o in g["occurrences"]]
        bases = {sid: state["completed"] for sid, state in latest_stage_states(project, "translation", ids).items()
                 if state["completed"] is not None} if ids else {}
        _verify_groups(draft, groups, segments, bases, indices)
        config = load_project_config(project, stage="translation")
        metadata = read_json(project, project / "project.json")
        files = load_source_files(project)
        library = load_terms(project)
        warnings: list[str] = []
        cache = _TermMatchCache(library, term_normalization(config), config["terminology"]["max_terms_per_segment"],
                               segments=segments, warnings=warnings)
        options = config["validation"]["translation"]
        binding = config.get("_decision_validator_presets", {}).get("preferred_term_usage")
        decision = DecisionClient(config["_decision_preset_definitions"][binding], retry=config["retry"]) if binding else None
        terms_revision = library["terms_revision"] if library else None
        fingerprint = stage_fingerprint(config, "translation", prompt_middle_digests(project, "translation"),
                                        terms_revision=terms_revision)
        records = []
        changes = []
        for group, proposal, choice in accepted:
            occurrences = {o["id"]: o for o in group["occurrences"]}
            target = group["translations"][occurrences[choice["target"]]["variant"]]
            for member in proposal["members"]:
                occurrence = occurrences[member]
                sid = occurrence["segment_id"]
                base = bases[sid]
                if base["text"] == target:
                    continue
                item = by_id[sid]
                text = normalize_model_text(files, item, target, "translation")
                if text != target:
                    raise UsageError("目标译文需要文档格式转换，不能直接统一，请人工编辑")
                neighbors, position = indices[sid]
                count = options["decision_previous_segments"] if options["decision_context_enabled"] else 0
                context = TranslationValidationContext(source=item["source"], translation=text,
                    terms=cache.validation_matches_for_item(item), decision=decision,
                    decision_prompts={key: value["content"] for key, value in config.get("_decision_prompt_definitions", {}).items()},
                    decision_confidence_threshold=options["decision_confidence_threshold"],
                    decision_warning_threshold=options["decision_warning_threshold"], segment_id=sid,
                    previous_source=tuple(s["source"] for s in neighbors[max(0, position - count):position]))
                decision_start = len(decision.records) if decision else 0
                findings = await validate_translation_text(context, config["_translation_validator_instances"])
                record = record_header("stage_result", metadata["project_id"], stage="translation", segment_id=sid,
                    status="completed", text=text, origin="repetition_consistency", run_id=manifest["run_id"],
                    request_id=None, stage_fingerprint=fingerprint, terms_revision=terms_revision,
                    validation_status="warning" if findings else "passed", validation_findings=findings,
                    decision_validation=decision.records[decision_start:] if decision else [])
                records.append(record)
                changes.append({"group": group["id"], "member": member, "after_result_id": record["record_id"],
                                "before_metadata": {**{key: base[key] for key in _BEFORE_FIELDS if key in base},
                                                    "restored_origin": base.get("restored_origin", base.get("origin"))}})
        manifest.update(draft_status="applied" if records else "rejected", applied_at=utc_now(), changes=changes)
        warnings.extend(write_repetition_state(project, records=records, manifests=[manifest]))
        return {"changed": len(records), "warnings": warnings,
                "validation_warning_count": sum(r["validation_status"] == "warning" for r in records)}


def rollback_draft(project: Path, *, confirm: bool, run_id: str | None = None) -> dict[str, Any]:
    if confirm is not True:
        raise UsageError("必须明确确认撤销重复译文统一")
    with project_write_lock(project):
        manifest = read_repetition_run(project, ("applied", "rolled_back"))
        if manifest is None or manifest["draft_status"] != "applied":
            raise UsageError("没有可撤销的最近一次重复译文应用")
        if run_id is not None and manifest["run_id"] != run_id:
            raise UsageError("最近一次应用已变化，请重新打开审核")
        draft = read_draft(project, manifest)
        groups = {g["id"]: g for g in draft["groups"]}
        occurrences = {g["id"]: {o["id"]: o for o in g["occurrences"]} for g in draft["groups"]}
        by_id = {str(s["segment_id"]): s for s in load_segments(project)}
        ids = [occurrences[c["group"]][c["member"]]["segment_id"] for c in manifest["changes"]]
        states = latest_stage_states(project, "translation", ids)
        records = []
        for change in manifest["changes"]:
            group = groups[change["group"]]
            occurrence = occurrences[change["group"]][change["member"]]
            sid = occurrence["segment_id"]
            item = by_id.get(sid)
            current = states.get(sid, {}).get("completed")
            if (item is None or segment_signature(item) != occurrence["signature"]
                or current is None or current["record_id"] != change["after_result_id"]):
                raise UsageError("受影响的原文或译文在应用后已有变化，不能撤销")
            records.append(record_header("stage_result", manifest["project_id"], stage="translation",
                segment_id=sid, status="completed", text=group["translations"][occurrence["variant"]],
                origin="repetition_rollback", run_id=manifest["run_id"], request_id=None,
                **change["before_metadata"]))
        manifest.update(draft_status="rolled_back", rolled_back_at=utc_now())
        warnings = write_repetition_state(project, records=records, manifests=[manifest])
        return {"changed": len(records), "warnings": warnings}


def review_state(project: Path) -> dict[str, Any]:
    manifest = read_repetition_run(project, ("applied", "rolled_back"))
    return {"draft": current_draft(project), "can_rollback": bool(manifest and manifest["draft_status"] == "applied"),
            "rollback_run_id": manifest["run_id"] if manifest and manifest["draft_status"] == "applied" else None}
