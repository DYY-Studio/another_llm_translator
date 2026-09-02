from __future__ import annotations

import asyncio

import hashlib

import json

import re


from collections.abc import Callable

from copy import deepcopy


from pathlib import Path

from typing import Any

import httpx

from .config import load_project_config


from .errors import StorageError, UsageError

from .execution import (
    Scope,
    combine_usage,
    continue_run,
    create_run,
    finalize_run,
    full_prompt,
    run_bounded,
    unavailable_usage,
)
from .llm_client import LLMClient, SlidingWindowLimiter

from .i18n import SUPPORTED_LANGUAGES, resolve_language

from .llm_keys import KeyPool

from .project import prompt_file

from .sqlite_storage import (
    atomic_write_json,
    read_json,
    record_header,
    utc_now,
    write_json,
)

from .term_library import (
    load_terms,
    term_normalization,
)

from .term_decision_rules import (
    _conflicts_by_term,
    _decision_dependency_graph, _dependency_components, _effective_conflicts,
    _empty_conflicts, _group_violations,
    _has_conflicts, _recover_invalid_relationship_components, _term_state,
    _validate_final_states,
)
from . import term_decision_batches as _batches
from . import term_decision_drafts as _drafts

from .term_decision_protocol import (
    DECISION_RULES_VERSION,
)

STAGE = "terminology_decision"

DRAFT_FILE = "terminology_decision_draft.json"

CHECKPOINT_FILE = "terminology_decision_checkpoint.json"

EVIDENCE_SAMPLE_LIMIT = 5

EVIDENCE_CONTEXT_RADIUS = 60

RELATED_ANCHOR_LIMIT = 24

_TOKEN_SPLIT = re.compile(r"[\s・·･._—–\-]+")

_STATE_FIELDS = (
    "category",
    "description",
    "preferred_translation",
    "aliases",
    "group_primary",
    "disabled",
)

_CONFLICT_FIELDS = (
    "categories",
    "preferred_translations",
    "alias_primaries",
    "group_claims",
)

_PHASES = ("adjudication", "consistency")

_GroupViolation = tuple[str, tuple[str, ...]]

def _prompt_language(project: Path, requested: str | None) -> str:
    language = requested or resolve_language()
    if language not in SUPPORTED_LANGUAGES:
        language = "zh-CN"
    if not (project / "prompts" / prompt_file(STAGE, language)).is_file():
        language = "zh-CN"
    return language

def _prompt(project: Path, language: str) -> dict[str, str]:
    path = project / "prompts" / prompt_file(STAGE, language)
    try:
        middle = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StorageError(f"无法读取 Prompt：{path.name}: {exc}") from exc
    return {
        phase: full_prompt(STAGE, middle, language, phase=phase) for phase in _PHASES
    }

def _prompt_snapshot(prompts: dict[str, str]) -> str:
    return "\n\n".join(
        f"===== terminology_decision/{phase} =====\n{prompts[phase]}"
        for phase in _PHASES
    )

def _alias_violation_message(violation: _GroupViolation, language: str) -> str:
    kind, values = violation
    if kind == "alias_transfer":
        receiver, owner, alias = values
        if language == "en":
            return f"alias {alias} remains owned by {owner} while added to {receiver}"
        return f"alias {alias} 已由 {owner} 保留却被新增到 {receiver}"
    if kind == "self_alias":
        if language == "en":
            return f"term {values[0]} contains its own source as alias {values[1]}"
        return f"术语 {values[0]} 将自身原文 {values[1]} 作为 alias"
    if kind == "non_root_receiver":
        if language == "en":
            return f"alias receiver {values[0]} is not a root term"
        return f"alias 接收方 {values[0]} 不是组根术语"
    if kind == "disabled_receiver":
        if language == "en":
            return f"disabled term {values[0]} cannot receive alias {values[1]}"
        return f"已禁用术语 {values[0]} 不能接收 alias {values[1]}"
    if kind == "unknown_alias":
        if language == "en":
            return f"alias {values[1]} has no known owner"
        return f"alias {values[1]} 没有已知所有者"
    if kind == "unknown_owner":
        if language == "en":
            return f"alias {values[2]} points to missing owner {values[1]}"
        return f"alias {values[2]} 指向不存在的所有者 {values[1]}"
    if language == "en":
        return f"term {values[0]} contains duplicate normalized aliases"
    return f"术语 {values[0]} 含有重复的规范化 alias"

def _checkpoint_path(project: Path, run_id: str) -> Path:
    return project / "runs" / run_id / CHECKPOINT_FILE

def decision_checkpoint_progress(project: Path, run_id: str) -> int:
    path = _checkpoint_path(project, run_id)
    if not path.is_file():
        return 0
    checkpoint = _read_checkpoint_file(path)
    phases = checkpoint.get("phases")
    if not isinstance(phases, dict):
        raise StorageError("术语决策检查点 phases 无效")
    completed = 0
    for phase in _PHASES:
        records = phases.get(phase)
        if not isinstance(records, dict):
            raise StorageError(f"术语决策检查点 {phase} 无效")
        completed += len(records)
    return completed

def decision_resume_compatibility(
    project: Path,
    run_id: str,
    *,
    source_terms_revision: int,
) -> tuple[bool, str | None]:
    try:
        manifest = read_json(
            project, project / "runs" / run_id / "manifest.json"
        )
    except StorageError:
        return False, "旧 Run 的 manifest 不可读"
    try:
        manifest_revision = int(manifest.get("source_terms_revision", -1))
    except (TypeError, ValueError):
        return False, "旧 Run 缺少有效的术语库 revision"
    if manifest_revision != source_terms_revision:
        return False, "术语库 revision 已变化，不能续用自动决策 Run"
    path = _checkpoint_path(project, run_id)
    if not path.is_file():
        return False, "旧 Run 缺少术语决策检查点规则版本"
    try:
        checkpoint = _read_checkpoint_file(path)
    except StorageError:
        return False, "旧 Run 的术语决策检查点不可读"
    if checkpoint.get("source_terms_revision") != source_terms_revision:
        return False, "术语库 revision 已变化，不能续用自动决策 Run"
    if checkpoint.get("decision_rules_version") != DECISION_RULES_VERSION:
        return False, "旧 Run 使用不兼容的术语决策输出协议"
    return True, None

def _read_checkpoint_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageError(f"无法读取术语决策检查点：{exc}") from exc
    if not isinstance(value, dict):
        raise StorageError("术语决策检查点必须是 JSON 对象")
    return value

def _new_checkpoint(project_id: str, run_id: str, revision: int) -> dict[str, Any]:
    return record_header(
        "terminology_decision_checkpoint",
        project_id,
        record_id=f"TERMINOLOGY-DECISION-CHECKPOINT-{run_id}",
        run_id=run_id,
        source_terms_revision=revision,
        decision_rules_version=DECISION_RULES_VERSION,
        phases={phase: {} for phase in _PHASES},
    )

def _load_checkpoint(
    project: Path,
    run_id: str,
    *,
    project_id: str,
    revision: int,
    known_terms: set[str],
) -> dict[str, Any]:
    path = _checkpoint_path(project, run_id)
    if not path.is_file():
        checkpoint = _new_checkpoint(project_id, run_id, revision)
        atomic_write_json(path, checkpoint)
        return checkpoint
    checkpoint = _read_checkpoint_file(path)
    if (
        checkpoint.get("record_type") != "terminology_decision_checkpoint"
        or checkpoint.get("project_id") != project_id
        or checkpoint.get("run_id") != run_id
        or checkpoint.get("source_terms_revision") != revision
    ):
        raise UsageError("术语决策检查点与当前 Run 或术语 revision 不一致")
    if checkpoint.get("decision_rules_version") != DECISION_RULES_VERSION:
        raise UsageError("术语决策检查点规则版本不兼容；请显式结束旧 Run 并强制新建")
    phases = checkpoint.get("phases")
    if not isinstance(phases, dict):
        raise StorageError("术语决策检查点 phases 无效")
    for phase in _PHASES:
        records = phases.get(phase)
        if not isinstance(records, dict):
            raise StorageError(f"术语决策检查点 {phase} 无效")
        unknown = set(map(str, records)) - known_terms
        if unknown:
            raise StorageError(
                "术语决策检查点包含未知术语：" + ", ".join(sorted(unknown)[:10])
            )
        if any(
            not isinstance(value, dict)
            or not isinstance(value.get("decision"), dict)
            or not isinstance(value.get("decision_fingerprint"), str)
            or not isinstance(value.get("model_fingerprint"), str)
            or not isinstance(value.get("prompt_fingerprint"), str)
            for value in records.values()
        ):
            raise StorageError(f"术语决策检查点 {phase} 记录无效")
    return checkpoint

def _checkpoint_decisions(
    checkpoint: dict[str, Any], phase: str
) -> dict[str, dict[str, Any]]:
    return {
        str(normalized): deepcopy(record["decision"])
        for normalized, record in checkpoint["phases"][phase].items()
    }

def _composite_fingerprint(checkpoint: dict[str, Any], field: str) -> str:
    values = {
        str(record[field])
        for phase in _PHASES
        for record in checkpoint["phases"][phase].values()
    }
    if not values:
        raise StorageError("术语决策检查点缺少批次指纹")
    if len(values) == 1:
        return values.pop()
    encoded = json.dumps(sorted(values), separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()

def _apply_tentative(
    states: dict[str, dict[str, Any]], decisions: dict[str, dict[str, Any]]
) -> None:
    for normalized, decision in decisions.items():
        if decision["action"] == "update":
            states[normalized] = deepcopy(decision["after"])
        elif decision["action"] == "disable":
            states[normalized] = {**states[normalized], "disabled": True}

def _consistency_states(
    original: dict[str, dict[str, Any]],
    tentative: dict[str, dict[str, Any]],
    decisions: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result = deepcopy(tentative)
    _apply_tentative(result, decisions)
    for normalized, decision in decisions.items():
        if decision["action"] == "needs_review":
            result[normalized] = deepcopy(original[normalized])
    return result

def _automatic_phase_two_anchors(
    eligible: list[dict[str, Any]],
    states: dict[str, dict[str, Any]],
    adjudication: dict[str, dict[str, Any]],
    consistency: dict[str, dict[str, Any]],
    conflicts: dict[str, dict[str, list[Any]]],
    spec: Any,
) -> list[dict[str, Any]]:
    violation_nodes = {
        node for violation in _group_violations(states) for node in violation[1]
    }
    blocked_relationship_nodes: set[str] = set()
    for component in _dependency_components(
        _decision_dependency_graph(states, states, spec), violation_nodes
    ):
        blocked_relationship_nodes.update(component)
    anchors: list[dict[str, Any]] = []
    for item in eligible:
        normalized = str(item["normalized"])
        first_action = str(adjudication[normalized]["action"])
        second = consistency.get(normalized)
        action = (
            str(second["action"])
            if second is not None and second["action"] != "keep"
            else first_action
        )
        state = states[normalized]
        if (
            action in {"keep", "update"}
            and not state.get("disabled")
            and normalized not in blocked_relationship_nodes
            and not _has_conflicts(conflicts.get(normalized, _empty_conflicts()))
        ):
            anchors.append(state)
    return anchors

def _merge_phase_decisions(
    *,
    original: dict[str, dict[str, Any]],
    tentative: dict[str, dict[str, Any]],
    final: dict[str, dict[str, Any]],
    adjudication: dict[str, dict[str, Any]],
    consistency: dict[str, dict[str, Any]],
    language: str,
) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for normalized, first in adjudication.items():
        second = consistency[normalized]
        if second["action"] == "keep":
            merged[normalized] = deepcopy(first)
            continue
        merged[normalized] = deepcopy(second)
        if second["action"] == "needs_review":
            continue
        first_changed = any(
            original[normalized][field] != tentative[normalized][field]
            for field in _STATE_FIELDS
        )
        second_changed = any(
            tentative[normalized][field] != final[normalized][field]
            for field in _STATE_FIELDS
        )
        if first_changed and second_changed:
            if language == "en":
                merged[normalized]["reason"] = (
                    f"Phase one: {first['reason']}; phase two: {second['reason']}"
                )
            else:
                merged[normalized]["reason"] = (
                    f"第一阶段：{first['reason']}；第二阶段：{second['reason']}"
                )
    return merged

def _proposal_id(values: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        values, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return "TDP-" + hashlib.sha256(encoded.encode()).hexdigest()[:16].upper()

def _merge_conflict_evidence(
    *snapshots: dict[str, dict[str, list[Any]]],
) -> dict[str, dict[str, list[Any]]]:
    merged: dict[str, dict[str, list[Any]]] = {}
    for snapshot in snapshots:
        for normalized, conflicts in snapshot.items():
            current = merged.setdefault(normalized, _empty_conflicts())
            for field in _CONFLICT_FIELDS:
                for value in conflicts[field]:
                    if value not in current[field]:
                        current[field].append(deepcopy(value))
    return merged

def _decision_fingerprint(
    config: dict[str, Any], prompts: dict[str, str], library: dict[str, Any]
) -> str:
    data = {
        "stage": STAGE,
        "rules_version": DECISION_RULES_VERSION,
        "target_language": config["project"]["target_language"],
        "model": config["llm"]["model"],
        "adapter_hash": config.get("_llm_adapter_hash"),
        "preset_hash": config.get("_llm_preset_hash"),
        "temperature": config["llm"]["temperature_terminology_decision"],
        "prompts": {
            phase: hashlib.sha256(prompts[phase].encode()).hexdigest()
            for phase in _PHASES
        },
        "terms_revision": library["terms_revision"],
        "terminology": config["terminology"],
        "terminology_decision": config["terminology_decision"],
    }
    encoded = json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()

def decision_plan(project: Path, prompt_language: str | None = None) -> dict[str, Any]:
    library = load_terms(project)
    if library is None or not library.get("terms"):
        raise UsageError("没有已发布术语库可供自动决策")
    config = load_project_config(project, stage=STAGE)
    overrides_document = read_json(project, project / "terminology" / "overrides.json")
    protected = {
        str(item["normalized"]) for item in overrides_document.get("overrides", [])
    }
    states = {
        str(item["normalized"]): _term_state(item) for item in library.get("terms", [])
    }
    source_conflicts = _conflicts_by_term(library.get("terms", []))
    eligible = [
        state
        for key, state in states.items()
        if key not in protected and not state["disabled"]
    ]
    if not eligible:
        raise UsageError("已发布术语全部受到人工 override 保护")
    evidence = _batches.collect_term_evidence(project, list(library.get("terms", [])), config)
    language = _prompt_language(project, prompt_language)
    prompts = _prompt(project, language)
    spec = term_normalization(config)
    protected_states = [states[key] for key in sorted(protected & set(states))]
    phase_one, phase_one_tokens = _batches._pack_batches(
        eligible,
        phase="adjudication",
        target_language=str(config["project"]["target_language"]),
        anchors=protected_states,
        evidence=evidence,
        prompt=prompts["adjudication"],
        config=config,
        spec=spec,
        conflicts=source_conflicts,
    )
    simulated_focus = [
        {
            **deepcopy(state),
            "_prior_decision": {"action": "keep", "reason": "dry-run"},
        }
        for state in eligible
    ]
    phase_two, phase_two_tokens = _batches._pack_batches(
        simulated_focus,
        phase="consistency",
        target_language=str(config["project"]["target_language"]),
        anchors=[*protected_states, *eligible],
        evidence=evidence,
        prompt=prompts["consistency"],
        config=config,
        spec=spec,
        conflicts=source_conflicts,
    )
    return {
        "library": library,
        "config": config,
        "overrides_document": overrides_document,
        "protected": protected,
        "states": states,
        "source_conflicts": source_conflicts,
        "eligible": eligible,
        "evidence": evidence,
        "language": language,
        "prompts": prompts,
        "spec": spec,
        "protected_states": protected_states,
        "phase_one": phase_one,
        "estimated_requests": len(phase_one) + len(phase_two),
        "estimated_input_tokens": phase_one_tokens + phase_two_tokens,
    }

async def run_terminology_decision(
    project: Path,
    *,
    dry_run: bool = False,
    replace_draft: bool = False,
    resume_run_id: str | None = None,
    prompt_language: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    limiter: SlidingWindowLimiter | KeyPool | None = None,
    on_progress: Callable[[int, int, int], None] | None = None,
    on_usage: Callable[[dict[str, Any] | None], None] | None = None,
) -> dict[str, Any]:
    existing = _drafts.current_decision_draft(project)
    if existing is not None and not replace_draft:
        raise UsageError("已有待处理术语决策草案；必须明确替换")
    plan = decision_plan(project, prompt_language)
    library = plan["library"]
    config = plan["config"]
    metadata = read_json(project, project / "project.json")
    overrides_document = plan["overrides_document"]
    protected = plan["protected"]
    states = plan["states"]
    source_conflicts = plan["source_conflicts"]
    eligible = plan["eligible"]
    evidence = plan["evidence"]
    language = plan["language"]
    prompts = plan["prompts"]
    fingerprint = _decision_fingerprint(config, prompts, library)
    model_fingerprint = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                {
                    "model": config["llm"]["model"],
                    "adapter": config.get("_llm_adapter_hash"),
                    "preset": config.get("_llm_preset_hash"),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
    )
    prompt_fingerprints = {
        phase: "sha256:" + hashlib.sha256(prompts[phase].encode()).hexdigest()
        for phase in _PHASES
    }
    prompt_snapshot = _prompt_snapshot(prompts)
    spec = plan["spec"]
    protected_states = plan["protected_states"]
    revision = int(library["terms_revision"])
    resumed_steps = 0
    if resume_run_id is not None:
        compatible, incompatibility_reason = decision_resume_compatibility(
            project,
            resume_run_id,
            source_terms_revision=revision,
        )
        if not compatible:
            raise UsageError(f"{incompatibility_reason}；请显式结束旧 Run 并强制新建")
        resumed_steps = decision_checkpoint_progress(project, resume_run_id)
    if dry_run:
        return {
            "stage": STAGE,
            "dry_run": True,
            "terms_revision": int(library["terms_revision"]),
            "eligible": len(eligible),
            "protected": len(protected_states),
            "estimated_requests": plan["estimated_requests"],
            "estimated_input_tokens": plan["estimated_input_tokens"],
            "resume_run_id": resume_run_id,
            "completed_steps": resumed_steps,
        }
    continuation_index = 0
    if resume_run_id is None:
        run_id, run_dir = create_run(
            project,
            config=config,
            stage=STAGE,
            fingerprint=fingerprint,
            prompt=prompt_snapshot,
            selected_count=len(eligible),
            requested_count=len(eligible),
            reused_count=0,
            details={
                "source_terms_revision": revision,
                "decision_status": "generating",
                "rejected_proposal_ids": [],
                "prompt_language": language,
            },
        )
    else:
        run_id, run_dir, continuation_index = continue_run(
            project,
            resume_run_id,
            config=config,
            stage=STAGE,
            fingerprint=fingerprint,
            prompt=prompt_snapshot,
            scope=Scope(),
            selected_count=len(eligible),
            requested_count=len(eligible),
            reused_count=0,
        )
    if limiter is None:
        execution = config["execution"]
        limiter = KeyPool(
            int(execution["requests_per_minute"]),
            int(execution["input_tokens_per_minute"]),
            int(execution["max_parallel"]),
            int(
                execution.get(
                    "max_parallel_per_key", execution["max_parallel"]
                )
            ),
        )
    eligible_terms = {str(item["normalized"]) for item in eligible}
    checkpoint = _load_checkpoint(
        project,
        run_id,
        project_id=str(metadata["project_id"]),
        revision=revision,
        known_terms=eligible_terms,
    )
    tentative = deepcopy(states)
    decisions = _checkpoint_decisions(checkpoint, "adjudication")
    _apply_tentative(tentative, decisions)
    final_decisions = _checkpoint_decisions(checkpoint, "consistency")
    if final_decisions and len(decisions) != len(eligible):
        raise StorageError("术语决策检查点在第一阶段完成前包含第二阶段结果")
    completed = len(decisions) + len(final_decisions)
    total = len(eligible) * 2
    usage_invoked = completed < total
    usage: dict[str, Any] | None = None
    active_llm: LLMClient | None = None

    def append_llm_warnings(manifest: dict[str, Any]) -> None:
        if active_llm is None or not active_llm.warnings:
            return
        existing = manifest.get("warnings", [])
        values = existing if isinstance(existing, list) else []
        manifest["warnings"] = list(dict.fromkeys([*values, *active_llm.warnings]))

    def record_resumable_interruption(
        current_usage: dict[str, Any] | None,
        error: BaseException,
    ) -> None:
        manifest = read_json(project, run_dir / "manifest.json")
        params = getattr(error, "params", {})
        reason = params.get("reason") if isinstance(params, dict) else None
        request_id = params.get("request_id") if isinstance(params, dict) else None
        if not isinstance(reason, str):
            reason = (
                "cancelled"
                if isinstance(error, asyncio.CancelledError)
                else "unexpected_error"
            )
        interruption = {
            "at": utc_now(),
            "error_code": getattr(error, "code", "cancelled"),
            "reason": reason,
            "completed_steps": completed,
            "total_steps": total,
        }
        if isinstance(request_id, str) and request_id.startswith("REQ-"):
            interruption["request_id"] = request_id
        manifest.update(
            status="running",
            decision_status="generating",
            completed_segment_count=completed,
            failed_segment_count=0,
            failure_counts={},
            completed_at=None,
            last_interruption=interruption,
        )
        if usage_invoked:
            previous_usage = manifest.get("usage")
            invocation_count = manifest.get("usage_invocation_count")
            if type(invocation_count) is int and invocation_count > 0:
                current_usage = combine_usage(previous_usage, current_usage)
            manifest.update(
                usage=current_usage or unavailable_usage(),
                usage_invocation_count=(
                    invocation_count + 1 if type(invocation_count) is int else 1
                ),
            )
        if active_llm is not None and active_llm._api_keys is not None:
            audits = manifest.setdefault("key_audits", [])
            if not isinstance(audits, list):
                audits = []
                manifest["key_audits"] = audits
            audits.append(
                active_llm.key_audit_summary(execution_index=continuation_index + 1)
            )
        append_llm_warnings(manifest)
        manifest.pop("proposal_count", None)
        manifest.pop("needs_review_count", None)
        write_json(project, run_dir / "manifest.json", manifest)

    try:
        async with LLMClient(
            config,
            limiter,
            run_dir=run_dir,
            project_id=str(metadata["project_id"]),
            run_id=run_id,
            stage=STAGE,
            client=http_client,
            on_usage=on_usage,
        ) as llm:
            active_llm = llm
            llm._prepare_keys()
            if on_progress:
                on_progress(completed, 0, total)
            remaining_phase_one = [
                item for item in eligible if str(item["normalized"]) not in decisions
            ]
            phase_one, _ = _batches._pack_batches(
                remaining_phase_one,
                phase="adjudication",
                target_language=str(config["project"]["target_language"]),
                anchors=protected_states,
                evidence=evidence,
                prompt=prompts["adjudication"],
                config=config,
                spec=spec,
                conflicts=source_conflicts,
            )

            async def adjudicate(
                batch: tuple[list[dict[str, Any]], list[dict[str, Any]]],
            ) -> None:
                focus, anchors = batch
                nonlocal completed
                result = await _batches._request_batch(
                    llm,
                    focus=focus,
                    anchors=anchors,
                    phase="adjudication",
                    prompt=prompts["adjudication"],
                    config=config,
                    evidence=evidence,
                    known_states=states,
                    read_only_terms={str(item["normalized"]) for item in anchors},
                    prompt_language=language,
                    review_states=states,
                    spec=spec,
                    conflicts=source_conflicts,
                )
                decisions.update(result)
                records = checkpoint["phases"]["adjudication"]
                for normalized, decision in result.items():
                    records[normalized] = {
                        "decision": deepcopy(decision),
                        "decision_fingerprint": fingerprint,
                        "model_fingerprint": model_fingerprint,
                        "prompt_fingerprint": prompt_fingerprints["adjudication"],
                    }
                atomic_write_json(_checkpoint_path(project, run_id), checkpoint)
                completed += len(focus)
                if on_progress:
                    on_progress(completed, 0, total)

            await run_bounded(
                phase_one,
                adjudicate,
                max_parallel=int(config["execution"]["max_parallel"]),
            )
            tentative = deepcopy(states)
            _apply_tentative(tentative, decisions)
            phase_two_state = _consistency_states(states, tentative, final_decisions)
            unresolved_phase_two_conflicts = _effective_conflicts(
                project, phase_two_state, source_conflicts
            )
            phase_two_conflicts = deepcopy(unresolved_phase_two_conflicts)
            for normalized, conflicts in phase_two_conflicts.items():
                source = source_conflicts[normalized]
                conflicts["categories"] = deepcopy(source["categories"])
                conflicts["preferred_translations"] = deepcopy(
                    source["preferred_translations"]
                )
            phase_two_focus = [
                {
                    **deepcopy(phase_two_state[item["normalized"]]),
                    "_prior_decision": deepcopy(decisions[item["normalized"]]),
                }
                for item in eligible
            ]
            phase_two_anchors = [
                *protected_states,
                *_automatic_phase_two_anchors(
                    eligible,
                    phase_two_state,
                    decisions,
                    final_decisions,
                    unresolved_phase_two_conflicts,
                    spec,
                ),
            ]
            remaining_phase_two = [
                item
                for item in phase_two_focus
                if str(item["normalized"]) not in final_decisions
            ]
            phase_two, _ = _batches._pack_batches(
                remaining_phase_two,
                phase="consistency",
                target_language=str(config["project"]["target_language"]),
                anchors=phase_two_anchors,
                evidence=evidence,
                prompt=prompts["consistency"],
                config=config,
                spec=spec,
                conflicts=phase_two_conflicts,
            )

            async def review_consistency(
                batch: tuple[list[dict[str, Any]], list[dict[str, Any]]],
            ) -> None:
                focus, anchors = batch
                nonlocal completed
                result = await _batches._request_batch(
                    llm,
                    focus=focus,
                    anchors=anchors,
                    phase="consistency",
                    prompt=prompts["consistency"],
                    config=config,
                    evidence=evidence,
                    known_states=phase_two_state,
                    read_only_terms={str(item["normalized"]) for item in anchors},
                    prompt_language=language,
                    review_states=states,
                    spec=spec,
                    conflicts=phase_two_conflicts,
                )
                final_decisions.update(result)
                records = checkpoint["phases"]["consistency"]
                for normalized, decision in result.items():
                    records[normalized] = {
                        "decision": deepcopy(decision),
                        "decision_fingerprint": fingerprint,
                        "model_fingerprint": model_fingerprint,
                        "prompt_fingerprint": prompt_fingerprints["consistency"],
                    }
                atomic_write_json(_checkpoint_path(project, run_id), checkpoint)
                completed += len(focus)
                if on_progress:
                    on_progress(completed, 0, total)

            await run_bounded(
                phase_two,
                review_consistency,
                max_parallel=int(config["execution"]["max_parallel"]),
            )
            final = _consistency_states(states, tentative, final_decisions)
            decisions = _merge_phase_decisions(
                original=states,
                tentative=tentative,
                final=final,
                adjudication=decisions,
                consistency=final_decisions,
                language=language,
            )
            usage = llm.usage_summary()
        _recover_invalid_relationship_components(
            original=states,
            final=final,
            decisions=decisions,
            language=language,
            spec=spec,
        )
        final_conflicts = _effective_conflicts(project, final, source_conflicts)
        _recover_invalid_relationship_components(
            original=states,
            final=final,
            decisions=decisions,
            language=language,
            spec=spec,
            conflicts=final_conflicts,
        )
        _validate_final_states(project, final, original=states, spec=spec)
        draft_conflicts = _merge_conflict_evidence(
            source_conflicts,
            phase_two_conflicts,
            final_conflicts,
        )
        draft = _drafts._build_draft(
            project_id=str(metadata["project_id"]),
            run_id=run_id,
            revision=int(library["terms_revision"]),
            original=states,
            final=final,
            decisions=decisions,
            protected=protected,
            evidence=evidence,
            conflicts=draft_conflicts,
            fingerprint=_composite_fingerprint(checkpoint, "decision_fingerprint"),
            source_library=deepcopy(library),
            source_overrides=deepcopy(overrides_document),
            model_fingerprint=_composite_fingerprint(checkpoint, "model_fingerprint"),
            prompt_fingerprint=_composite_fingerprint(checkpoint, "prompt_fingerprint"),
            spec=spec,
        )
        atomic_write_json(_drafts._draft_path(project, run_id), draft)
        manifest = read_json(project, run_dir / "manifest.json")
        manifest.update(
            decision_status="pending",
            proposal_count=len(draft["proposals"]),
            needs_review_count=len(draft["needs_review"]),
            protected_term_count=len(protected_states),
        )
        manifest.pop("last_interruption", None)
        append_llm_warnings(manifest)
        write_json(project, run_dir / "manifest.json", manifest)
        usage = finalize_run(
            project,
            run_dir,
            status="completed",
            completed=total,
            failed=0,
            warnings=(active_llm.warnings if active_llm is not None else []),
            usage=usage,
            usage_invoked=usage_invoked,
            key_audit=(
                active_llm.key_audit_summary(execution_index=continuation_index + 1)
                if active_llm is not None and active_llm._api_keys is not None
                else None
            ),
        )
        if existing is not None:
            old_run_id = str(existing["run_id"])
            old_path = project / "runs" / old_run_id / "manifest.json"
            old_manifest = read_json(project, old_path)
            old_manifest.update(
                decision_status="superseded",
                superseded_by_run_id=run_id,
            )
            write_json(project, old_path, old_manifest)
        return {
            "stage": STAGE,
            "run_id": run_id,
            "terms_revision": int(library["terms_revision"]),
            "eligible": len(eligible),
            "protected": len(protected_states),
            "proposals": len(draft["proposals"]),
            "needs_review": len(draft["needs_review"]),
            "completed": total,
            "failed": 0,
            "pending": 0,
            "usage": usage or unavailable_usage(),
        }
    except BaseException as exc:
        if active_llm is not None:
            usage = active_llm.usage_summary()
        record_resumable_interruption(usage, exc)
        raise
