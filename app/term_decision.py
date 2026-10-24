from __future__ import annotations
from .async_utils import run_blocking, blocking_scope

import asyncio

import hashlib

import json

import re


from collections.abc import Callable

from copy import deepcopy


from pathlib import Path

from typing import Any

import httpx

from .prompt_resources import ProjectPrompts
from .config import load_config, load_project_config


from .errors import AppError, StorageError, UsageError, app_error_payload

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
    _conflicts_by_term, _containment_pairs,
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
_FINAL_REVIEW_PHASE = "final_review"


def _phase_names(final_review: bool = False) -> tuple[str, ...]:
    return (*_PHASES, _FINAL_REVIEW_PHASE, "containment", "knowledge") if final_review else (*_PHASES, "containment", "knowledge")

_GroupViolation = tuple[str, tuple[str, ...]]

def _prompt_language(project: Path, requested: str | None) -> str:
    language = requested or resolve_language()
    if language not in SUPPORTED_LANGUAGES:
        language = "zh-CN"
    if not ProjectPrompts(project).path(STAGE, language).is_file():
        language = "zh-CN"
    return language

def _prompt(
    project: Path, language: str, *, final_review: bool = False, knowledge_only: bool = False
) -> dict[str, str]:
    prompts = {}
    resources = ProjectPrompts(project)
    for resource in (("terminology_knowledge",) if knowledge_only else (STAGE, "terminology_knowledge")):
        path = resources.path(resource, language)
        try:
            middle = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StorageError(f"无法读取 Prompt：{path.name}: {exc}") from exc
        if resource == "terminology_knowledge":
            prompts["knowledge"] = full_prompt(resource, middle, language)
        else:
            prompts.update({phase: full_prompt(STAGE, middle, language, phase=phase)
                            for phase in _phase_names(final_review) if phase != "knowledge"})
    return prompts

def _prompt_snapshot(prompts: dict[str, str]) -> str:
    return "\n\n".join(
        f"===== terminology_decision/{phase} =====\n{prompts[phase]}"
        for phase in prompts
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
    for phase, records in phases.items():
        if not isinstance(records, dict):
            raise StorageError(f"术语决策检查点 {phase} 无效")
        completed += len(records)
    return completed

def decision_resume_compatibility(
    project: Path,
    run_id: str,
    *,
    source_terms_revision: int,
    final_review: bool = False,
    knowledge_only: bool = False,
    review_draft_run_id: str | None = None,
) -> tuple[bool, str | None]:
    from .execution import run_protocol_is_current
    if not run_protocol_is_current(project, run_id):
        return False, "旧 Run 协议不兼容，请结束旧 Run 并新建"
    try:
        manifest = read_json(
            project, project / "runs" / run_id / "manifest.json"
        )
    except StorageError:
        return False, "旧 Run 的 manifest 不可读"
    if manifest.get("review_draft_run_id") != review_draft_run_id:
        return False, "旧 Run 的草案终审对象与当前运行不一致"
    if manifest.get("status") != "running":
        return False, "旧 Run 已结束，不能续跑；请新建 Run"
    try:
        manifest_revision = int(manifest.get("source_terms_revision", -1))
    except (TypeError, ValueError):
        return False, "旧 Run 缺少有效的术语库 revision"
    if manifest_revision != source_terms_revision:
        return False, "术语库 revision 已变化，不能续用自动决策 Run"
    if manifest.get("decision_status") not in {"generating", "pending"}:
        return False, "旧 Run 的草案已处理，不能续用自动决策 Run"
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
    if bool(checkpoint.get("knowledge_only", False)) != knowledge_only:
        return False, "旧 Run 的实体说明执行模式与当前运行不一致"
    if bool(checkpoint.get("final_review", False)) != final_review:
        return False, "旧 Run 的终审选项与当前运行不一致"
    previous_generation = load_config(project / "runs" / run_id / "config.toml")["terminology_decision"]["description_generation"]
    current_generation = load_config(project / "config.toml")["terminology_decision"]["description_generation"]
    if previous_generation != current_generation:
        return False, "旧 Run 的实体说明生成方式与当前设置不一致"
    return True, None

def _read_checkpoint_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageError(f"无法读取术语决策检查点：{exc}") from exc
    if not isinstance(value, dict):
        raise StorageError("术语决策检查点必须是 JSON 对象")
    return value

def _new_checkpoint(
    project_id: str,
    run_id: str,
    revision: int,
    *,
    final_review: bool = False,
    knowledge_only: bool = False,
) -> dict[str, Any]:
    return record_header(
        "terminology_decision_checkpoint",
        project_id,
        record_id=f"TERMINOLOGY-DECISION-CHECKPOINT-{run_id}",
        run_id=run_id,
        source_terms_revision=revision,
        decision_rules_version=DECISION_RULES_VERSION,
        final_review=final_review,
        knowledge_only=knowledge_only,
        phases={phase: {} for phase in _phase_names(final_review)},
    )

def _load_checkpoint(
    project: Path,
    run_id: str,
    *,
    project_id: str,
    revision: int,
    known_terms: set[str],
    knowledge_terms: set[str] | None = None,
    final_review: bool = False,
    knowledge_only: bool = False,
) -> dict[str, Any]:
    path = _checkpoint_path(project, run_id)
    if not path.is_file():
        checkpoint = _new_checkpoint(
            project_id, run_id, revision, final_review=final_review, knowledge_only=knowledge_only
        )
        atomic_write_json(path, checkpoint, compact=True)
        return checkpoint
    checkpoint = _read_checkpoint_file(path)
    if (
        checkpoint.get("record_type") != "terminology_decision_checkpoint"
        or checkpoint.get("project_id") != project_id
        or checkpoint.get("run_id") != run_id
        or checkpoint.get("source_terms_revision") != revision
    ):
        raise UsageError("术语决策检查点与当前 Run 或术语 revision 不一致")
    if bool(checkpoint.get("knowledge_only", False)) != knowledge_only:
        raise UsageError("术语决策检查点实体说明执行模式与当前 Run 不一致")
    if bool(checkpoint.get("final_review", False)) != final_review:
        raise UsageError("术语决策检查点终审选项与当前 Run 不一致")
    if checkpoint.get("decision_rules_version") != DECISION_RULES_VERSION:
        raise UsageError("术语决策检查点规则版本不兼容；请显式结束旧 Run 并强制新建")
    phases = checkpoint.get("phases")
    if not isinstance(phases, dict):
        raise StorageError("术语决策检查点 phases 无效")
    expected_phases = set(_phase_names(final_review))
    if set(phases) != expected_phases:
        raise UsageError("术语决策检查点 phase 选项与当前运行不一致")
    for phase in _phase_names(final_review):
        records = phases.get(phase)
        if not isinstance(records, dict):
            raise StorageError(f"术语决策检查点 {phase} 无效")
        unknown = set(map(str, records)) - (knowledge_terms if phase == "knowledge" and knowledge_terms is not None else known_terms)
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
        for records in checkpoint["phases"].values()
        for record in records.values()
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


def _final_review_base_states(
    tentative: dict[str, dict[str, Any]],
    consistency: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Keep accepted phase-one state while leaving consistency reviews pending."""
    result = deepcopy(tentative)
    _apply_tentative(result, consistency)
    return result


def _final_review_targets(
    *,
    eligible: list[dict[str, Any]],
    protected: set[str],
    original: dict[str, dict[str, Any]],
    final: dict[str, dict[str, Any]],
    decisions: dict[str, dict[str, Any]],
    spec: Any,
) -> list[dict[str, Any]]:
    pending = {
        normalized
        for normalized, decision in decisions.items()
        if decision.get("action") == "needs_review"
    }
    if not pending:
        return []
    graph = _decision_dependency_graph(original, final, spec)
    eligible_ids = {
        str(item["normalized"]) for item in eligible
    } - protected
    target_ids = set().union(
        *(
            component & eligible_ids
            for component in _dependency_components(graph, pending)
        ),
    )
    return [
        {
            **deepcopy(final[str(item["normalized"])]),
            "_prior_decision": deepcopy(decisions[str(item["normalized"])]),
        }
        for item in eligible
        if str(item["normalized"]) in target_ids
    ]


def _merge_final_review_decisions(
    decisions: dict[str, dict[str, Any]],
    review_decisions: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    merged = deepcopy(decisions)
    for normalized, decision in review_decisions.items():
        merged[normalized] = deepcopy(decision)
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
    config: dict[str, Any],
    prompts: dict[str, str],
    library: dict[str, Any],
    *,
    final_review: bool = False,
    knowledge_only: bool = False,
    review_draft_run_id: str | None = None,
) -> str:
    data = {
        "stage": STAGE,
        "rules_version": DECISION_RULES_VERSION,
        "final_review": final_review,
        **({"review_draft_run_id": review_draft_run_id} if review_draft_run_id is not None else {}),
        "knowledge_only": knowledge_only,
        "target_language": config["project"]["target_language"],
        "model": config["llm"]["model"],
        "adapter_hash": config.get("_llm_adapter_hash"),
        "preset_hash": config.get("_llm_preset_hash"),
        "temperature": config["llm"]["temperature_terminology_decision"] if config["llm"]["send_temperature"] else None,
        "prompts": {
            phase: hashlib.sha256(prompts[phase].encode()).hexdigest()
            for phase in prompts
        },
        "terms_revision": library["terms_revision"],
        "terminology": config["terminology"],
        "terminology_decision": config["terminology_decision"],
    }
    encoded = json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()

def decision_plan(
    project: Path,
    prompt_language: str | None = None,
    *,
    final_review: bool = False,
    knowledge_only: bool = False,
    review_draft_run_id: str | None = None,
    prepare_requests: bool = True,
    estimate_usage: bool = True,
) -> dict[str, Any]:
    if knowledge_only and final_review:
        raise UsageError("仅实体说明模式不能同时启用术语终审")
    library = load_terms(project, resolve_knowledge=False)
    if library is None or not library.get("terms"):
        raise UsageError("没有已发布术语库可供自动决策")
    config = load_project_config(project, stage=STAGE)
    overrides_document = read_json(project, project / "terminology" / "overrides.json")
    protected = {
        str(item["normalized"]) for item in overrides_document.get("overrides", [])
    }
    source_conflicts = _conflicts_by_term(library.get("terms", []))
    from .term_knowledge import entity_members, evidence as knowledge_evidence, project_descriptions, applied_descriptions
    known_fragments = {}
    knowledge_views = []
    if prepare_requests:
        known_fragments = knowledge_evidence(project, library)
        views = project_descriptions(project, library, all_evidence=known_fragments)
        generation = config["terminology_decision"]["description_generation"]
        knowledge_views = [view for view in views.values()
                           if knowledge_only or view["status"] != "current" or view["description_generation"] != generation]
        knowledge_pending = len(knowledge_views)
    else:
        applied_descriptions(library)
        # Name decisions can change entity membership. Resolve their knowledge
        # targets at the actual phase boundary, rather than during UI/queue inspection.
        knowledge_pending = len(entity_members(library)) if knowledge_only else 0
    states = {str(item["normalized"]): _term_state(item) for item in library.get("terms", [])}
    source_draft = None
    if review_draft_run_id is not None:
        if not final_review or knowledge_only:
            raise UsageError("草案终审必须启用终审，且不能使用仅实体说明模式")
        source_draft = _drafts.current_decision_draft(project)
        if source_draft is None or source_draft["run_id"] != review_draft_run_id:
            raise UsageError("术语决策草案已变化，请刷新后重试")
        if source_draft["source_terms_revision"] != library["terms_revision"]:
            raise UsageError("术语库已变化，当前决策草案已过期")
        if source_draft.get("decision_rules_version") != DECISION_RULES_VERSION:
            raise UsageError("当前决策草案规则版本不一致，请重新生成草案")
        if source_draft.get("knowledge_only") or not source_draft["needs_review"]:
            raise UsageError("当前草案没有可终审的术语待审项")
    eligible = [state for key, state in states.items() if not knowledge_only and key not in protected and not state["disabled"]]
    if not eligible and not knowledge_pending:
        raise UsageError("已发布术语全部受到人工 override 保护")
    evidence = ({} if knowledge_only or not prepare_requests else _batches.collect_term_evidence(
        project, list(library.get("terms", [])), config
    ))
    if not knowledge_only and prepare_requests:
        descriptions: dict[str, dict[str, list[str]]] = {}
        for fragment in known_fragments.values():
            if fragment["state"] != "active" or not fragment["valid"]:
                continue
            source = fragment.get("source", fragment["original_owner"])
            descriptions.setdefault(fragment["owner"], {}).setdefault(source, []).append(fragment["text"])
        for key, item in evidence.items():
            item["description"] = "\n\n".join(
                f"[{source}]\n" + "\n\n".join(texts)
                for source, texts in descriptions.get(key, {}).items()
            ) or None
    language = _prompt_language(project, prompt_language)
    prompts = _prompt(project, language, final_review=final_review, knowledge_only=knowledge_only)
    spec = term_normalization(config)
    protected_states = [states[key] for key in sorted(protected & set(states))]
    phase_one, phase_two = [], []
    phase_one_tokens = phase_two_tokens = 0
    if source_draft is None and not knowledge_only and prepare_requests and estimate_usage:
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
    if estimate_usage and source_draft is None:
        if not knowledge_only:
            simulated_focus = [
                {**deepcopy(state), "_prior_decision": {"action": "keep", "reason": "estimate"}}
                for state in eligible
            ]
            phase_two, phase_two_tokens = _batches._pack_batches(
                simulated_focus, phase="consistency",
                target_language=str(config["project"]["target_language"]),
                anchors=[*protected_states, *eligible], evidence=evidence,
                prompt=prompts["consistency"], config=config, spec=spec, conflicts=source_conflicts,
            )
        from .term_knowledge import pack_knowledge_batches
        knowledge_batches, knowledge_tokens, _ = pack_knowledge_batches(knowledge_views, prompts["knowledge"], config)
    else:
        knowledge_batches, knowledge_tokens = [], 0
    review_focus = []
    if source_draft is not None:
        draft_final, draft_decisions, _ = _draft_review_states(states, source_draft)
        review_focus = _final_review_targets(
            eligible=eligible, protected=protected, original=states,
            final=draft_final, decisions=draft_decisions, spec=spec,
        )
        if not review_focus:
            raise UsageError("当前草案没有可终审的术语待审项")
        if prepare_requests:
            for key, item in _batches.collect_term_review_evidence(project, review_focus, config).items():
                evidence[key].update(item)
        knowledge_batches, knowledge_tokens = [], 0
        if estimate_usage:
            phase_one, phase_one_tokens = _batches._pack_batches(
                review_focus, phase="final_review",
                target_language=str(config["project"]["target_language"]),
                anchors=[state for key, state in draft_final.items()
                         if key not in {item["normalized"] for item in review_focus}],
                evidence=evidence, prompt=prompts["final_review"], config=config,
                spec=spec, conflicts=source_conflicts,
            )
    return {
        "library": library,
        "config": config,
        "overrides_document": overrides_document,
        "protected": protected,
        "states": states,
        "source_conflicts": source_conflicts,
        "eligible": eligible,
        "final_review": final_review,
        "knowledge_only": knowledge_only,
        "source_draft": source_draft,
        "evidence": evidence,
        "language": language,
        "prompts": prompts,
        "spec": spec,
        "protected_states": protected_states,
        "phase_one": phase_one,
        "requests_prepared": prepare_requests,
        "estimated_requests": len(phase_one) + len(phase_two) + len(knowledge_batches) if estimate_usage else None,
        "estimated_input_tokens": phase_one_tokens + phase_two_tokens + knowledge_tokens if estimate_usage else None,
        "total_steps": len(review_focus) if source_draft is not None else len(eligible) * 2 + knowledge_pending,
        "knowledge_pending": knowledge_pending,
    }


def _draft_review_states(
    states: dict[str, dict[str, Any]], draft: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    final = deepcopy(states)
    decisions = {key: {"action": "keep", "reason": "已有草案决定"} for key in states}
    updates = {}
    for proposal in draft["proposals"]:
        for state in proposal["after"]:
            key = state["normalized"]
            final[key] = deepcopy(state)
            decisions[key] = {"action": "update", "after": deepcopy(state), "reason": proposal["reason"]}
        for root, aggregate in proposal.get("knowledge_after", {}).items():
            updates[root] = {
                "aggregate": deepcopy(aggregate),
                "members": [key for key in proposal["normalized"]
                            if (final[key].get("group_primary") or key) == root],
                "reason": proposal["reason"],
                "owner_moves": deepcopy(proposal.get("knowledge_owner_moves", {})),
            }
    for item in draft["needs_review"]:
        decisions[item["normalized"]] = {"action": "needs_review", "reason": item["reason"]}
    return final, decisions, updates

@blocking_scope
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
    plan: dict[str, Any] | None = None,
    final_review: bool = False,
    knowledge_only: bool = False,
    review_draft_run_id: str | None = None,
) -> dict[str, Any]:
    existing = (await run_blocking(lambda: _drafts._read_pending_draft(project)))
    if existing is not None and existing["run_id"] != resume_run_id and not replace_draft and review_draft_run_id is None:
        raise UsageError("已有待处理术语决策草案；必须明确替换")
    if plan is None or not plan.get("requests_prepared", True):
        plan = (await run_blocking(lambda: decision_plan(project, prompt_language, final_review=final_review, knowledge_only=knowledge_only, review_draft_run_id=review_draft_run_id, estimate_usage=dry_run)))
    elif bool(plan.get("final_review", False)) != final_review:
        raise UsageError("术语决策计划的终审选项与当前运行不一致")
    if bool(plan.get("knowledge_only", False)) != knowledge_only:
        raise UsageError("术语决策计划的实体说明执行模式与当前运行不一致")
    source_draft = plan.get("source_draft")
    library = plan["library"]
    config = plan["config"]
    metadata = (await run_blocking(lambda: read_json(project, project / "project.json")))
    overrides_document = plan["overrides_document"]
    protected = plan["protected"]
    states = plan["states"]
    source_conflicts = plan["source_conflicts"]
    eligible = plan["eligible"]
    evidence = plan["evidence"]
    language = plan["language"]
    prompts = plan["prompts"]
    fingerprint = _decision_fingerprint(
        config, prompts, library, final_review=final_review, knowledge_only=knowledge_only, review_draft_run_id=review_draft_run_id
    )
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
        for phase in prompts
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
            final_review=final_review,
            knowledge_only=knowledge_only,
            review_draft_run_id=review_draft_run_id,
        )
        if not compatible:
            raise UsageError(f"{incompatibility_reason}；请显式结束旧 Run 并强制新建")
        resumed_steps = decision_checkpoint_progress(project, resume_run_id)
    if dry_run:
        return {
            "stage": STAGE,
            "dry_run": True,
            "knowledge_only": knowledge_only,
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
        run_id, run_dir = (await run_blocking(lambda: create_run(
            project,
            config=config,
            stage=STAGE,
            fingerprint=fingerprint,
            prompt=prompt_snapshot,
            selected_count=plan["total_steps"] if source_draft is not None else plan["knowledge_pending"] if knowledge_only else len(eligible),
            requested_count=plan["total_steps"] if source_draft is not None else plan["knowledge_pending"] if knowledge_only else len(eligible),
            reused_count=0,
            details={
                "source_terms_revision": revision,
                "decision_status": "generating",
                "rejected_proposal_ids": [],
                "prompt_language": language,
                "final_review": final_review,
                "knowledge_only": knowledge_only,
                "review_draft_run_id": review_draft_run_id,
            },
        )))
    else:
        run_id, run_dir, continuation_index = (await run_blocking(lambda: continue_run(
            project,
            resume_run_id,
            config=config,
            stage=STAGE,
            fingerprint=fingerprint,
            prompt=prompt_snapshot,
            scope=Scope(),
            selected_count=plan["total_steps"] if source_draft is not None else plan["knowledge_pending"] if knowledge_only else len(eligible),
            requested_count=plan["total_steps"] if source_draft is not None else plan["knowledge_pending"] if knowledge_only else len(eligible),
            reused_count=0,
        )))
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
    checkpoint = (await run_blocking(lambda: _load_checkpoint(
        project,
        run_id,
        project_id=str(metadata["project_id"]),
        revision=revision,
        known_terms=eligible_terms,
        knowledge_terms=set(states),
        final_review=final_review,
        knowledge_only=knowledge_only,
    )))
    tentative = deepcopy(states)
    decisions = _checkpoint_decisions(checkpoint, "adjudication")
    _apply_tentative(tentative, decisions)
    consistency_decisions = _checkpoint_decisions(checkpoint, "consistency")
    review_decisions = (
        _checkpoint_decisions(checkpoint, _FINAL_REVIEW_PHASE)
        if final_review
        else {}
    )
    if consistency_decisions and len(decisions) != len(eligible):
        raise StorageError("术语决策检查点在第一阶段完成前包含第二阶段结果")
    containment_decisions = _checkpoint_decisions(checkpoint, "containment")
    completed = len(decisions) + len(consistency_decisions) + len(review_decisions) + len(containment_decisions) + len(checkpoint["phases"]["knowledge"])
    initial_steps = 0 if source_draft is not None else len(eligible) * 2
    total = initial_steps + len(containment_decisions) + len(checkpoint["phases"]["knowledge"])
    if knowledge_only:
        total = plan["total_steps"]
    final_review_target_count = 0
    containment_components: list[set[str]] = []
    usage_invoked = completed < total
    usage: dict[str, Any] | None = None
    active_llm: LLMClient | None = None

    review_base: dict[str, dict[str, Any]] | None = None
    containment_base: dict[str, dict[str, Any]] | None = None
    review_target_ids: set[str] = set()
    final_responses_complete = False
    knowledge_updates: dict[str, Any] = {}
    if source_draft is not None:
        _, _, knowledge_updates = _draft_review_states(states, source_draft)
    knowledge_errors: dict[str, str] = {}
    cleanup_warnings: list[str] = []

    def save_draft(failure: AppError | None = None) -> dict[str, Any]:
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
            reviewed_components=containment_components,
            knowledge_updates=knowledge_updates,
            knowledge_errors=knowledge_errors,
            knowledge_only=knowledge_only,
        )
        if source_draft is not None:
            affected = review_target_ids | {item["normalized"] for item in containment_focus}
            preserved = [deepcopy(item) for item in source_draft["proposals"]
                         if not set(item["normalized"]) & affected]
            draft["proposals"] = preserved + [item for item in draft["proposals"]
                                             if set(item["normalized"]) & affected]
            rejected_terms = {key for item in source_draft["proposals"]
                              if item["proposal_id"] in source_draft["rejected_proposal_ids"]
                              for key in item["normalized"]}
            draft["rejected_proposal_ids"] = [item["proposal_id"] for item in draft["proposals"]
                                               if set(item["normalized"]) & rejected_terms]
        atomic_write_json(_drafts._draft_path(project, run_id), draft, compact=True)
        manifest = read_json(project, run_dir / "manifest.json")
        manifest.update(
            decision_status="pending",
            rejected_proposal_ids=[value for value in (draft["rejected_proposal_ids"] if source_draft is not None else manifest.get("rejected_proposal_ids", []))
                                   if value in {proposal["proposal_id"] for proposal in draft["proposals"]}],
            proposal_count=len(draft["proposals"]),
            needs_review_count=len(draft["needs_review"]),
            protected_term_count=len(protected_states),
            final_review=final_review,
            final_review_target_count=final_review_target_count,
            total_steps=total,
        )
        if failure is None:
            manifest.pop("final_review_error", None)
        else:
            manifest["final_review_error"] = app_error_payload(failure)
        write_json(project, run_dir / "manifest.json", manifest)
        if existing is not None and existing["run_id"] != run_id:
            old_path = project / "runs" / str(existing["run_id"]) / "manifest.json"
            old_manifest = read_json(project, old_path)
            old_manifest.update(decision_status="superseded", superseded_by_run_id=run_id)
            if "final_review_error" in old_manifest:
                old_manifest.update(status="completed", completed_at=utc_now())
            cleanup_warnings.extend(write_json(project, old_path, old_manifest))
            if cleanup_warnings:
                manifest["warnings"] = list(dict.fromkeys([*manifest.get("warnings", []), *cleanup_warnings]))
                write_json(project, run_dir / "manifest.json", manifest)
        return draft

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
            decision_status=("pending" if _drafts._draft_path(project, run_id).is_file() else "generating"),
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
            if plan["total_steps"] > 0:
                await llm.prepare_keys()
            if on_progress:
                on_progress(completed, 0, total)
            final = deepcopy(states)
            phase_two_conflicts = {}
            containment_focus = []
            final_conflicts = source_conflicts
            if not knowledge_only:
                if source_draft is not None:
                    final, decisions, _ = _draft_review_states(states, source_draft)
                else:
                    remaining_phase_one = [
                        item for item in eligible if str(item["normalized"]) not in decisions
                    ]
                    phase_one, _ = (await run_blocking(lambda: _batches._pack_batches(
                        remaining_phase_one,
                        phase="adjudication",
                        target_language=str(config["project"]["target_language"]),
                        anchors=protected_states,
                        evidence=evidence,
                        prompt=prompts["adjudication"],
                        config=config,
                        spec=spec,
                        conflicts=source_conflicts,
                    )))

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
                        def commit_result() -> None:
                            nonlocal completed
                            decisions.update(result)
                            records = checkpoint["phases"]["adjudication"]
                            for normalized, decision in result.items():
                                records[normalized] = {
                                    "decision": deepcopy(decision),
                                    "decision_fingerprint": fingerprint,
                                    "model_fingerprint": model_fingerprint,
                                    "prompt_fingerprint": prompt_fingerprints["adjudication"],
                                }
                            (atomic_write_json(_checkpoint_path(project, run_id), checkpoint, compact=True))
                            completed += len(focus)
                        await run_blocking(commit_result)
                        if on_progress:
                            on_progress(completed, 0, total)

                    await run_bounded(
                        phase_one,
                        adjudicate,
                        max_parallel=int(config["execution"]["max_parallel"]),
                    )
                    tentative = deepcopy(states)
                    _apply_tentative(tentative, decisions)
                    phase_two_state = (await run_blocking(lambda: _consistency_states(
                        states, tentative, consistency_decisions
                    )))
                    unresolved_phase_two_conflicts = (await run_blocking(lambda: _effective_conflicts(
                        project, phase_two_state, source_conflicts
                    )))
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
                            consistency_decisions,
                            unresolved_phase_two_conflicts,
                            spec,
                        ),
                    ]
                    remaining_phase_two = [
                        item
                        for item in phase_two_focus
                        if str(item["normalized"]) not in consistency_decisions
                    ]
                    phase_two, _ = (await run_blocking(lambda: _batches._pack_batches(
                        remaining_phase_two,
                        phase="consistency",
                        target_language=str(config["project"]["target_language"]),
                        anchors=phase_two_anchors,
                        evidence=evidence,
                        prompt=prompts["consistency"],
                        config=config,
                        spec=spec,
                        conflicts=phase_two_conflicts,
                    )))

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
                        def commit_result() -> None:
                            nonlocal completed
                            consistency_decisions.update(result)
                            records = checkpoint["phases"]["consistency"]
                            for normalized, decision in result.items():
                                records[normalized] = {
                                    "decision": deepcopy(decision),
                                    "decision_fingerprint": fingerprint,
                                    "model_fingerprint": model_fingerprint,
                                    "prompt_fingerprint": prompt_fingerprints["consistency"],
                                }
                            (atomic_write_json(_checkpoint_path(project, run_id), checkpoint, compact=True))
                            completed += len(focus)
                        await run_blocking(commit_result)
                        if on_progress:
                            on_progress(completed, 0, total)

                    await run_bounded(
                        phase_two,
                        review_consistency,
                        max_parallel=int(config["execution"]["max_parallel"]),
                    )
                    phase_two_final = (await run_blocking(lambda: _consistency_states(
                        states, tentative, consistency_decisions
                    )))
                    final = (
                        (await run_blocking(lambda: _final_review_base_states(tentative, consistency_decisions)))
                        if final_review
                        else phase_two_final
                    )
                    decisions = _merge_phase_decisions(
                        original=states,
                        tentative=tentative,
                        final=final,
                        adjudication=decisions,
                        consistency=consistency_decisions,
                        language=language,
                    )
                if final_review:
                    review_base = deepcopy(final)
                    (await run_blocking(lambda: _recover_invalid_relationship_components(
                        original=states,
                        final=final,
                        decisions=decisions,
                        language=language,
                        spec=spec,
                    )))
                    review_conflicts = (await run_blocking(lambda: _effective_conflicts(
                        project, final, source_conflicts
                    )))
                    (await run_blocking(lambda: _recover_invalid_relationship_components(
                        original=states,
                        final=final,
                        decisions=decisions,
                        language=language,
                        spec=spec,
                        conflicts=review_conflicts,
                    )))
                    review_conflicts = (await run_blocking(lambda: _effective_conflicts(
                        project, final, source_conflicts
                    )))
                    review_base = deepcopy(final)
                    review_focus = (await run_blocking(lambda: _final_review_targets(
                        eligible=eligible,
                        protected=protected,
                        original=states,
                        final=final,
                        decisions=decisions,
                        spec=spec,
                    )))
                    review_target_ids = {
                        str(item["normalized"]) for item in review_focus
                    }
                    unexpected_review = set(review_decisions) - review_target_ids
                    if unexpected_review:
                        raise StorageError(
                            "术语终审检查点包含非当前待审术语："
                            + ", ".join(sorted(unexpected_review)[:10])
                        )
                    final_review_target_count = len(review_focus)
                    if review_focus:
                        review_evidence = (await run_blocking(lambda: _batches.collect_term_review_evidence(
                            project, review_focus, config
                        )))
                        for key, item in review_evidence.items():
                            evidence[key].update(item)
                    total = initial_steps + final_review_target_count + len(containment_decisions)
                    usage_invoked = completed < total
                    manifest = (await run_blocking(lambda: read_json(project, run_dir / "manifest.json")))
                    manifest.update(
                        final_review_target_count=final_review_target_count,
                        total_steps=total,
                    )
                    (await run_blocking(lambda: write_json(project, run_dir / "manifest.json", manifest)))
                    if final_review_target_count and on_progress:
                        on_progress(completed, 0, total)
                    remaining_review = [
                        item
                        for item in review_focus
                        if str(item["normalized"]) not in review_decisions
                    ]
                    phase_three = (
                        (await run_blocking(lambda: _batches._pack_batches(
                            remaining_review,
                            phase=_FINAL_REVIEW_PHASE,
                            target_language=str(config["project"]["target_language"]),
                            anchors=([state for key, state in final.items() if key not in review_target_ids]
                                     if source_draft is not None else protected_states),
                            evidence=evidence,
                            prompt=prompts[_FINAL_REVIEW_PHASE],
                            config=config,
                            spec=spec,
                            conflicts=review_conflicts,
                        )))
                        if remaining_review
                        else ([], 0)
                    )[0]

                    async def review_final(
                        batch: tuple[list[dict[str, Any]], list[dict[str, Any]]],
                    ) -> None:
                        focus, anchors = batch
                        nonlocal completed
                        result = await _batches._request_batch(
                            llm,
                            focus=focus,
                            anchors=anchors,
                            phase=_FINAL_REVIEW_PHASE,
                            prompt=prompts[_FINAL_REVIEW_PHASE],
                            config=config,
                            evidence=evidence,
                            known_states=final,
                            read_only_terms={
                                str(item["normalized"]) for item in anchors
                            },
                            prompt_language=language,
                            review_states=states,
                            spec=spec,
                            conflicts=review_conflicts,
                        )
                        def commit_result() -> None:
                            nonlocal completed
                            review_decisions.update(result)
                            records = checkpoint["phases"][_FINAL_REVIEW_PHASE]
                            for normalized, decision in result.items():
                                records[normalized] = {
                                    "decision": deepcopy(decision),
                                    "decision_fingerprint": fingerprint,
                                    "model_fingerprint": model_fingerprint,
                                    "prompt_fingerprint": prompt_fingerprints[
                                        _FINAL_REVIEW_PHASE
                                    ],
                                }
                            (atomic_write_json(_checkpoint_path(project, run_id), checkpoint, compact=True))
                            completed += len(focus)
                        await run_blocking(commit_result)
                        if on_progress:
                            on_progress(completed, 0, total)

                    await run_bounded(
                        phase_three,
                        review_final,
                        max_parallel=int(config["execution"]["max_parallel"]),
                    )
                    final = deepcopy(final)
                    _apply_tentative(final, review_decisions)
                    decisions = _merge_final_review_decisions(
                        decisions, review_decisions
                    )
                    if source_draft is not None:
                        graph = _decision_dependency_graph(states, final, spec)
                        review_target_ids = set().union(*_dependency_components(graph, review_target_ids))
                # Review only suspect combinations, using the completed states rather than phase-one anchors.
                containment_base = deepcopy(final)
                pairs = _containment_pairs(containment_base, spec)
                graph = (await run_blocking(lambda: _decision_dependency_graph(states, containment_base, spec)))
                for left, right in pairs:
                    graph[left].add(right)
                    graph[right].add(left)
                containment_components = (await run_blocking(lambda: _dependency_components(graph, {key for pair in pairs for key in pair})))
                containment_focus = [
                    {**deepcopy(containment_base[key]), "_containment_peers": sorted(component)}
                    for component in containment_components for key in sorted(component)
                    if key not in protected and key in eligible_terms
                    and decisions[key]["action"] != "needs_review" and not containment_base[key].get("disabled")
                ]
                if source_draft is not None:
                    containment_components = [component for component in containment_components
                                              if component & review_target_ids]
                    affected = set().union(*containment_components) if containment_components else set()
                    containment_focus = [item for item in containment_focus if item["normalized"] in affected]
                target_ids = {str(item["normalized"]) for item in containment_focus}
                if set(containment_decisions) - target_ids:
                    raise StorageError("包含关系复核检查点包含非当前目标术语")
                total = initial_steps + final_review_target_count + len(containment_focus) + len(checkpoint["phases"]["knowledge"])
                usage_invoked = usage_invoked or completed < total
                remaining = [item for item in containment_focus if item["normalized"] not in containment_decisions]
                if containment_focus:
                    manifest = (await run_blocking(lambda: read_json(project, run_dir / "manifest.json")))
                    manifest.update(containment_target_count=len(containment_focus), total_steps=total)
                    (await run_blocking(lambda: write_json(project, run_dir / "manifest.json", manifest)))
                    if on_progress:
                        on_progress(completed, 0, total)
                containment_anchors = [state for key, state in containment_base.items()
                                       if key not in target_ids and not state.get("disabled")]
                containment_conflicts = (await run_blocking(lambda: _effective_conflicts(project, containment_base, source_conflicts)))
                batches = (await run_blocking(lambda: _batches._pack_batches(
                    remaining, phase="containment", target_language=str(config["project"]["target_language"]),
                    anchors=containment_anchors, evidence=evidence, prompt=prompts["containment"],
                    config=config, spec=spec, conflicts=containment_conflicts,
                )))[0] if remaining else []

                async def review_containment(batch: tuple[list[dict[str, Any]], list[dict[str, Any]]]) -> None:
                    focus, anchors = batch
                    nonlocal completed
                    result = await _batches._request_batch(
                        llm, focus=focus, anchors=anchors, phase="containment", prompt=prompts["containment"],
                        config=config, evidence=evidence, known_states=containment_base,
                        read_only_terms={str(item["normalized"]) for item in anchors},
                        prompt_language=language, review_states=states, spec=spec, conflicts=containment_conflicts,
                    )
                    def commit_result() -> None:
                        nonlocal completed
                        containment_decisions.update(result)
                        for normalized, decision in result.items():
                            checkpoint["phases"]["containment"][normalized] = {
                                "decision": deepcopy(decision), "decision_fingerprint": fingerprint,
                                "model_fingerprint": model_fingerprint,
                                "prompt_fingerprint": prompt_fingerprints["containment"],
                            }
                        (atomic_write_json(_checkpoint_path(project, run_id), checkpoint, compact=True))
                        completed += len(focus)
                    await run_blocking(commit_result)
                    if on_progress:
                        on_progress(completed, 0, total)

                await run_bounded(batches, review_containment, max_parallel=int(config["execution"]["max_parallel"]))
                final_responses_complete = True
                _apply_tentative(final, containment_decisions)
                for key, decision in containment_decisions.items():
                    if decision["action"] != "keep":
                        decisions[key] = deepcopy(decision)
                    else:
                        decisions[key]["reason"] += "; " + decision["reason"]
                # A repair must not create a fresh inconsistency elsewhere. Existing pairs may be
                # explicitly kept as independent translations; containment is not a hard invariant.
                unresolved_pairs = [
                    pair for pair in _containment_pairs(final, spec)
                    if pair not in pairs
                ]
                for left, right in unresolved_pairs:
                    graph[left].add(right)
                    graph[right].add(left)
                    for key in (left, right):
                        if key in eligible_terms:
                            decisions[key] = {"action": "needs_review", "reason": (
                                "Final terminology still has an unconfirmed containment spelling conflict."
                                if language == "en" else "最终术语仍存在未确认的长短术语译名冲突。"
                            )}
                containment_components = (await run_blocking(lambda: _dependency_components(graph, {key for pair in [*pairs, *unresolved_pairs] for key in pair})))
                for component in containment_components:
                    unresolved = [key for key in component if decisions.get(key, {}).get("action") == "needs_review"]
                    if unresolved:
                        reason = "；".join(dict.fromkeys(decisions[key]["reason"] for key in unresolved))
                        for key in component & eligible_terms:
                            final[key] = deepcopy(containment_base[key])
                            decisions[key] = {"action": "needs_review", "reason": reason}
                if not final_review:
                    (await run_blocking(lambda: _recover_invalid_relationship_components(
                        original=states,
                        final=final,
                        decisions=decisions,
                        language=language,
                        spec=spec,
                    )))
                final_conflicts = (await run_blocking(lambda: _effective_conflicts(project, final, source_conflicts)))
                if final_review:
                    unresolved = sorted(
                        normalized
                        for normalized, conflicts in final_conflicts.items()
                        if not final[normalized].get("disabled") and _has_conflicts(conflicts)
                    )
                    if unresolved:
                        raise UsageError(
                            "术语自动终审后仍有未解决冲突："
                            + ", ".join(unresolved[:10])
                        )
                    if any(
                        decision.get("action") == "needs_review" and not any(normalized in component for component in containment_components)
                        for normalized, decision in decisions.items()
                    ):
                        raise UsageError("术语自动终审后仍存在 needs_review 决策")
                else:
                    (await run_blocking(lambda: _recover_invalid_relationship_components(
                        original=states,
                        final=final,
                        decisions=decisions,
                        language=language,
                        spec=spec,
                        conflicts=final_conflicts,
                    )))
                (await run_blocking(lambda: _validate_final_states(project, final, original=states, spec=spec)))
            from .term_knowledge import aggregate_batch, pack_knowledge_batches, entity_members, entity_view, save_items, transfer_alias_evidence, evidence as knowledge_evidence
            def prepare_knowledge():
                knowledge_library = deepcopy(library)
                for root, update in knowledge_updates.items():
                    knowledge_library["knowledge"]["entities"][root] = deepcopy(update["aggregate"])
                knowledge_library["terms"] = [deepcopy(state) for state in final.values() if not state.get("disabled")]
                owner_moves = {} if knowledge_only else transfer_alias_evidence(knowledge_library, spec)
                known_fragments = knowledge_evidence(project, knowledge_library)
                views = [entity_view(project, knowledge_library, root, all_evidence=known_fragments) for root in entity_members(knowledge_library)]
                return knowledge_library, owner_moves, known_fragments, views

            knowledge_library, owner_moves, known_fragments, views = await run_blocking(prepare_knowledge)
            unresolved_names = {key for key, decision in decisions.items() if decision["action"] == "needs_review"}
            generation = config["terminology_decision"]["description_generation"]
            targets = [view for view in views
                       if (knowledge_only or view["status"] != "current" or view["description_generation"] != generation)
                       and not set(view["members"]) & unresolved_names]
            if source_draft is not None:
                affected = review_target_ids | {item["normalized"] for item in containment_focus}
                targets = [view for view in targets if set(view["members"]) & affected]
            total = initial_steps + final_review_target_count + len(containment_focus) + len(targets)
            cached = checkpoint["phases"]["knowledge"]
            already_cached = set(cached)

            event_loop = asyncio.get_running_loop()

            def accept_knowledge(view: dict[str, Any], result: dict[str, Any]) -> None:
                nonlocal completed
                root = view["entity_key"]
                save_items(project, knowledge_library, root, result["items"], all_evidence=known_fragments,
                           description_generation=generation)
                knowledge_updates[root] = {"aggregate": deepcopy(knowledge_library["knowledge"]["entities"][root]),
                                           "members": view["members"], "reason": result["reason"],
                                           "owner_moves": {old: new for old, new in owner_moves.items() if new in view["members"]}}
                if root not in already_cached:
                    result["source_order"] = view["source_order"]
                    cached[root] = {"decision": deepcopy(result), "decision_fingerprint": fingerprint,
                                    "model_fingerprint": model_fingerprint, "prompt_fingerprint": prompt_fingerprints["knowledge"]}
                    atomic_write_json(_checkpoint_path(project, run_id), checkpoint, compact=True)
                    completed += 1
                if on_progress:
                    event_loop.call_soon_threadsafe(on_progress, completed, len(knowledge_errors), total)

            remaining_views = []
            for view in targets:
                root = view["entity_key"]
                if root in cached:
                    result = deepcopy(cached[root]["decision"])
                    if result["input_version"] != view["input_version"] or result["source_order"] != view["source_order"]:
                        knowledge_errors[root] = "实体说明检查点输入已变化，请重新生成"
                    elif any(f["state"] == "active" and not f["valid"] for f in view["fragments"]):
                        knowledge_errors[root] = "实体证据已过期，请重新扫描或停用失效片段"
                    else:
                        await run_blocking(accept_knowledge, view, result)
                elif not any(f["state"] == "active" for f in view["fragments"]):
                    await run_blocking(accept_knowledge, view, {"items": [], "reason": "没有说明证据", "input_version": view["input_version"]})
                else:
                    remaining_views.append(view)
            knowledge_batches, _, packing_errors = (await run_blocking(lambda: pack_knowledge_batches(remaining_views, prompts["knowledge"], config)))
            knowledge_errors.update(packing_errors)
            usage_invoked = usage_invoked or bool(knowledge_batches)

            async def aggregate_knowledge(batch: list[dict[str, Any]]) -> None:
                errors = await aggregate_batch(llm, batch, prompts["knowledge"], config, on_result=accept_knowledge)
                knowledge_errors.update(errors)
                if on_progress:
                    event_loop.call_soon_threadsafe(on_progress, completed, len(knowledge_errors), total)

            await run_bounded(knowledge_batches, aggregate_knowledge, max_parallel=int(config["execution"]["max_parallel"]))
            if knowledge_errors:
                raise UsageError("实体说明聚合失败：" + "；".join(f"{root}: {error}" for root, error in knowledge_errors.items()))
            usage = llm.usage_summary()
        draft = (await run_blocking(lambda: save_draft()))
        manifest = (await run_blocking(lambda: read_json(project, run_dir / "manifest.json")))
        manifest.pop("last_interruption", None)
        append_llm_warnings(manifest)
        (await run_blocking(lambda: write_json(project, run_dir / "manifest.json", manifest)))
        usage = (await run_blocking(lambda: finalize_run(
            project,
            run_dir,
            status="completed",
            completed=total,
            failed=0,
            warnings=[*(active_llm.warnings if active_llm is not None else []), *cleanup_warnings],
            usage=usage,
            usage_invoked=usage_invoked,
            key_audit=(
                active_llm.key_audit_summary(execution_index=continuation_index + 1)
                if active_llm is not None and active_llm._api_keys is not None
                else None
            ),
        )))
        return {
            "stage": STAGE,
            "run_id": run_id,
            "terms_revision": int(library["terms_revision"]),
            "eligible": len(eligible),
            "protected": len(protected_states),
            "knowledge_only": knowledge_only,
            "proposals": len(draft["proposals"]),
            "needs_review": len(draft["needs_review"]),
            "completed": total,
            "failed": 0,
            "pending": 0,
            "usage": usage or unavailable_usage(),
            "warnings": cleanup_warnings,
        }
    except BaseException as exc:
        if active_llm is not None:
            usage = active_llm.usage_summary()
        (await run_blocking(lambda: record_resumable_interruption(usage, exc)))
        if source_draft is not None:
            raise
        if knowledge_errors and completed and isinstance(exc, AppError):
            (await run_blocking(lambda: save_draft(exc)))
        elif final_review and review_base is not None and isinstance(exc, AppError) and not isinstance(exc, StorageError):
            # Rebuild from completed dependency components, never partial relationship patches.
            if not final_responses_complete:
                final = deepcopy(review_base)
                for phase_base, results, targets, reviewed in (
                    (review_base, review_decisions, review_target_ids, []),
                    (containment_base, containment_decisions, set(containment_decisions) | {
                        str(item["normalized"]) for item in containment_focus
                    } if containment_base is not None else set(), containment_components),
                ):
                    if phase_base is None:
                        continue
                    candidate = deepcopy(phase_base)
                    _apply_tentative(candidate, results)
                    graph = (await run_blocking(lambda: _decision_dependency_graph(phase_base, candidate, spec)))
                    unreviewed_pairs = (
                        [pair for pair in _containment_pairs(candidate, spec) if pair not in pairs]
                        if phase_base is containment_base else []
                    )
                    for left, right in unreviewed_pairs:
                        graph[left].add(right)
                        graph[right].add(left)
                    for component in reviewed:
                        keys = sorted(component)
                        for left, right in zip(keys, keys[1:]):
                            graph[left].add(right)
                            graph[right].add(left)
                    for component in (await run_blocking(lambda: _dependency_components(graph, targets))):
                        complete = all(key in results and results[key]["action"] != "needs_review"
                                       for key in component & targets) and not any(
                                       left in component for left, _ in unreviewed_pairs
                                   )
                        for key in component & decisions.keys():
                            if complete:
                                final[key] = deepcopy(candidate[key])
                                if key in results and results[key]["action"] != "keep":
                                    decisions[key] = deepcopy(results[key])
                                elif key in results and decisions[key]["action"] == "needs_review":
                                    decisions[key] = deepcopy(results[key])
                            else:
                                final[key] = deepcopy(phase_base[key])
                                decisions[key] = {"action": "needs_review", "reason": str(exc)}
            final_conflicts = (await run_blocking(lambda: _effective_conflicts(project, final, source_conflicts)))
            (await run_blocking(lambda: _recover_invalid_relationship_components(
                original=states, final=final, decisions=decisions, language=language,
                spec=spec, conflicts=final_conflicts,
            )))
            final_conflicts = (await run_blocking(lambda: _effective_conflicts(project, final, source_conflicts)))
            unresolved = {key for key, conflicts in final_conflicts.items() if _has_conflicts(conflicts)}
            graph = (await run_blocking(lambda: _decision_dependency_graph(states, final, spec)))
            for component in (await run_blocking(lambda: _dependency_components(graph, unresolved))):
                for key in component & decisions.keys():
                    final[key] = deepcopy(states[key])
                    decisions[key] = {"action": "needs_review", "reason": str(exc)}
            final_conflicts = (await run_blocking(lambda: _effective_conflicts(project, final, source_conflicts)))
            (await run_blocking(lambda: _validate_final_states(project, final, original=states, spec=spec)))
            (await run_blocking(lambda: save_draft(exc)))
        raise
