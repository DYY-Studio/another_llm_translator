from __future__ import annotations
import re
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any
from .config import load_project_config
from .documents import aozora_match_views
from .errors import RequestSizeError, StorageError, UsageError
from .execution import (
    estimate_messages,
    render_messages,
)
from .llm_client import LLMClient
from .sqlite_storage import (
    read_segment_sources,
)
from .term_library import (
    normalize_term,
    term_normalization,
)
from .term_decision_protocol import (
    format_correction,
)

from .term_decision_rules import *
from .term_decision_rules import _relation_keys
import re
_TOKEN_SPLIT = re.compile(r"[\s・·･._—–\-]+")
STAGE = "terminology_decision"
EVIDENCE_SAMPLE_LIMIT = 5
EVIDENCE_CONTEXT_RADIUS = 60
RELATED_ANCHOR_LIMIT = 24
_PHASES = ("adjudication", "consistency")

__all__ = ['_compact_anchor_evidence', '_compact_anchors', '_evidence_excerpt', '_make_payload', '_pack_batches', '_related_anchors', '_relation_keys', '_request_batch', '_request_evidence', '_request_limits', 'collect_term_evidence']

def collect_term_evidence(
    project: Path,
    terms: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Count all source/alias hits in one Segment pass with existing match views."""
    config = config or load_project_config(project)
    spec = term_normalization(config)
    forms_by_term = {
        str(term["normalized"]): _normalized_forms(term, spec) for term in terms
    }
    prefix_index: dict[str, set[str]] = {}
    single_index: dict[str, set[str]] = {}
    for normalized, forms in forms_by_term.items():
        for _, _, form in forms:
            target = single_index if len(form) == 1 else prefix_index
            key = form if len(form) == 1 else form[:2]
            target.setdefault(key, set()).add(normalized)
    evidence = {
        normalized: {
            "hit_count": 0,
            "source_hit_count": 0,
            "alias_hit_counts": {
                value: 0 for kind, value, _ in forms if kind == "alias"
            },
            "samples": [],
        }
        for normalized, forms in forms_by_term.items()
    }
    sample_boundaries: dict[str, set[tuple[str, str]]] = {
        key: set() for key in evidence
    }
    boundary_samples: dict[str, list[dict[str, Any]]] = {key: [] for key in evidence}
    fallback_samples: dict[str, list[dict[str, Any]]] = {key: [] for key in evidence}
    for segment in read_segment_sources(project):
        source = str(segment["source"])
        raw_views = list(aozora_match_views(source))
        views = [normalize_term(value, spec) for value in raw_views]
        candidates: set[str] = set()
        for view in views:
            candidates.update(
                normalized
                for index in range(max(0, len(view) - 1))
                for normalized in prefix_index.get(view[index : index + 2], ())
            )
            candidates.update(
                normalized
                for character in view
                for normalized in single_index.get(character, ())
            )
        for normalized in candidates:
            matched: list[tuple[str, str]] = []
            for kind, original, form in forms_by_term[normalized]:
                if any(form in view for view in views):
                    matched.append((kind, original))
            if not matched:
                continue
            item = evidence[normalized]
            item["hit_count"] += 1
            if any(kind == "source" for kind, _ in matched):
                item["source_hit_count"] += 1
            for kind, original in matched:
                if kind == "alias":
                    item["alias_hit_counts"][original] += 1
            file_id = str(segment["file_id"])
            part_id = str(segment["part_id"])
            boundary = (file_id, part_id)
            is_first_boundary_sample = boundary not in sample_boundaries[normalized]
            should_sample = (
                is_first_boundary_sample
                and len(boundary_samples[normalized]) < EVIDENCE_SAMPLE_LIMIT
            ) or (
                not is_first_boundary_sample
                and len(fallback_samples[normalized]) < EVIDENCE_SAMPLE_LIMIT
            )
            if should_sample:
                view_name, excerpt = _evidence_excerpt(
                    views,
                    matched,
                    raw_views=raw_views,
                    spec=spec,
                )
                sample = {
                    "file_id": file_id,
                    "part_id": part_id,
                    "segment_id": str(segment["segment_id"]),
                    "source": excerpt,
                    "match_view": view_name,
                    "matched_forms": [
                        {"kind": kind, "value": original} for kind, original in matched
                    ],
                }
                if is_first_boundary_sample:
                    boundary_samples[normalized].append(sample)
                    sample_boundaries[normalized].add(boundary)
                else:
                    fallback_samples[normalized].append(sample)
    for normalized, item in evidence.items():
        primary = boundary_samples[normalized]
        item["samples"] = [
            *primary,
            *fallback_samples[normalized][: EVIDENCE_SAMPLE_LIMIT - len(primary)],
        ]
    return evidence

def _evidence_excerpt(
    views: list[str],
    matched: list[tuple[str, str]],
    *,
    raw_views: list[str],
    spec: Any,
) -> tuple[str, str]:
    """Return a bounded, labelled context excerpt around the first hit."""
    for index, view in enumerate(views):
        for _, original in matched:
            form = normalize_term(original, spec)
            position = view.find(form)
            if position < 0:
                continue
            start = max(0, position - EVIDENCE_CONTEXT_RADIUS)
            end = min(len(view), position + len(form) + EVIDENCE_CONTEXT_RADIUS)
            raw_view = raw_views[index]
            if len(raw_view) == len(view):
                excerpt = raw_view[start:end]
            else:
                excerpt = view[start:end]
            if len(raw_views) == 1:
                view_name = "source"
            elif index == 0:
                view_name = "aozora_base"
            else:
                view_name = "aozora_reading"
            return view_name, excerpt
    raise StorageError("术语决策证据摘录无法定位命中形式")

def _request_evidence(
    focus: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
    evidence: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Project durable locations to request-local content-boundary references."""
    boundary_refs: dict[tuple[str, str], int] = {}
    projected: dict[str, dict[str, Any]] = {}
    for state in [*focus, *anchors]:
        normalized = str(state["normalized"])
        value = deepcopy(evidence[normalized])
        samples = []
        for sample in value["samples"]:
            boundary = (str(sample["file_id"]), str(sample["part_id"]))
            boundary_ref = boundary_refs.setdefault(boundary, len(boundary_refs) + 1)
            samples.append(
                {
                    "boundary_ref": boundary_ref,
                    "source": sample["source"],
                    "match_view": sample["match_view"],
                    "matched_forms": deepcopy(sample["matched_forms"]),
                }
            )
        value["samples"] = samples
        projected[normalized] = value
    return projected

def _compact_anchor_evidence(
    evidence: dict[str, dict[str, Any]],
    anchors: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return payload evidence with anchor samples removed when requested."""
    if not any(item.get("_compact_evidence") for item in anchors):
        return evidence
    compacted = deepcopy(evidence)
    for anchor in anchors:
        normalized = str(anchor["normalized"])
        value = compacted.get(normalized)
        if isinstance(value, dict):
            value["samples"] = []
    return compacted

def _compact_anchors(anchors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**deepcopy(anchor), "_compact_evidence": True} for anchor in anchors]

def _related_anchors(
    focus: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    spec: Any,
) -> list[dict[str, Any]]:
    focus_ids = {str(item["normalized"]) for item in focus}
    focus_keys = {key for item in focus for key in _relation_keys(item, spec)}
    focus_forms = [
        normalize_term(str(value), spec)
        for item in focus
        for value in [item["source"], *item.get("aliases", [])]
    ]
    focus_primaries = {
        normalize_term(str(item["group_primary"]), spec)
        for item in focus
        if item.get("group_primary") is not None
    }

    def direct_group_relation(item: dict[str, Any]) -> bool:
        item_forms = {
            normalize_term(str(value), spec)
            for value in [item["source"], *item.get("aliases", [])]
        }
        primary = item.get("group_primary")
        item_primary = (
            normalize_term(str(primary), spec) if primary is not None else None
        )
        return bool(
            focus_primaries.intersection(item_forms)
            or (item_primary is not None and item_primary in focus_forms)
        )

    def related_to_focus(item: dict[str, Any]) -> bool:
        if focus_keys.intersection(_relation_keys(item, spec)):
            return True
        item_forms = [
            normalize_term(str(value), spec)
            for value in [item["source"], *item.get("aliases", [])]
        ]
        return any(
            min(len(left), len(right)) >= 2 and (left in right or right in left)
            for left in focus_forms
            for right in item_forms
        )

    direct = [
        item
        for item in candidates
        if str(item["normalized"]) not in focus_ids and direct_group_relation(item)
    ]
    direct_ids = {str(item["normalized"]) for item in direct}
    related = [
        item
        for item in candidates
        if str(item["normalized"]) not in focus_ids | direct_ids
        and related_to_focus(item)
    ]
    return [*direct, *related][:RELATED_ANCHOR_LIMIT]

def _request_limits(config: dict[str, Any]) -> tuple[int, int, int, int]:
    soft_target = int(config["chunking"]["target_chunk_input_tokens"])
    context_limit = int(config["llm"]["context_window_tokens"]) - int(
        config["llm"]["context_safety_margin_tokens"]
    )
    itpm_limit = int(config["execution"]["input_tokens_per_minute"])
    hard_limit = min(
        context_limit,
        itpm_limit if itpm_limit > 0 else 2**63 - 1,
    )
    return soft_target, hard_limit, context_limit, itpm_limit

def _make_payload(
    *,
    phase: str,
    target_language: str,
    focus: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
    evidence: dict[str, dict[str, Any]],
    conflicts: dict[str, dict[str, list[Any]]] | None = None,
) -> dict[str, Any]:
    include_disabled = phase == "consistency"
    request_evidence = _request_evidence(focus, anchors, evidence)
    return {
        "phase": phase,
        "target_language": target_language,
        "terms": [
            _payload_term(
                item,
                request_evidence,
                include_disabled=include_disabled,
                conflicts=conflicts,
            )
            for item in focus
        ],
        "anchors": [
            _payload_term(
                item,
                request_evidence,
                include_disabled=include_disabled,
                conflicts=conflicts,
            )
            for item in anchors
        ],
    }

def _pack_batches(
    states: list[dict[str, Any]],
    *,
    phase: str,
    target_language: str,
    anchors: list[dict[str, Any]],
    evidence: dict[str, dict[str, Any]],
    prompt: str,
    config: dict[str, Any],
    spec: Any,
    conflicts: dict[str, dict[str, list[Any]]] | None = None,
) -> tuple[list[tuple[list[dict[str, Any]], list[dict[str, Any]]]], int]:
    soft_target, hard_limit, context_limit, itpm_limit = _request_limits(config)
    decision_config = config["terminology_decision"]
    allow_soft_overflow = bool(decision_config["allow_soft_target_overflow"])
    anchor_overflow_mode = str(decision_config["anchor_overflow_mode"])
    token_factor = float(config["execution"]["token_safety_factor"])

    def estimate_for(
        focus: list[dict[str, Any]],
        anchor_list: list[dict[str, Any]],
    ) -> int:
        payload = _make_payload(
            phase=phase,
            target_language=target_language,
            focus=focus,
            anchors=anchor_list,
            evidence=_compact_anchor_evidence(evidence, anchor_list),
            conflicts=conflicts,
        )
        return estimate_messages(
            render_messages(prompt, payload),
            token_factor,
        )

    def overflow_reason() -> str:
        if hard_limit == itpm_limit and itpm_limit < context_limit:
            return "itpm"
        return "context"

    def fail_size(
        component: list[dict[str, Any]],
        estimate: int,
        *,
        limit: int,
        reason: str,
        detail: str,
    ) -> None:
        sources = ", ".join(str(state["source"]) for state in component[:5])
        raise RequestSizeError(
            f"不可拆术语组件 [{sources}] 的自动决策请求估算 {estimate} tokens，"
            f"限制 {limit} tokens；{detail}",
            reason=reason,
        )

    def prepare_component(
        focus: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int]:
        selected_anchors = _related_anchors(focus, anchors, spec)
        had_anchors = bool(selected_anchors)
        estimate = estimate_for(focus, selected_anchors)
        if estimate > hard_limit:
            if anchor_overflow_mode == "trim":
                while selected_anchors and estimate > hard_limit:
                    selected_anchors = selected_anchors[:-1]
                    estimate = estimate_for(focus, selected_anchors)
            elif anchor_overflow_mode == "compact":
                selected_anchors = _compact_anchors(selected_anchors)
                estimate = estimate_for(focus, selected_anchors)
            if estimate > hard_limit:
                detail = (
                    "无 Anchors 时完整术语及证据仍超过硬限制"
                    if not had_anchors
                    else "完整术语证据和 Anchors 仍超过硬限制"
                )
                fail_size(
                    focus,
                    estimate,
                    limit=hard_limit,
                    reason=overflow_reason(),
                    detail=f"{detail}，Anchor 超限策略为 {anchor_overflow_mode}",
                )
        if estimate > soft_target and not allow_soft_overflow:
            fail_size(
                focus,
                estimate,
                limit=soft_target,
                reason="context",
                detail="超过软目标且配置禁止继续执行",
            )
        return selected_anchors, estimate

    batches: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []
    total = 0
    current: list[dict[str, Any]] = []
    batch_limit = min(soft_target, hard_limit)
    components = _hard_components(states, spec)
    soft_rank = {
        str(state["normalized"]): index
        for index, state in enumerate(_ordered_states(states, spec))
    }
    components.sort(
        key=lambda component: (
            min(soft_rank[str(state["normalized"])] for state in component),
            tuple(str(state["normalized"]) for state in component),
        )
    )
    for component in components:
        ordered_component = _ordered_states(component, spec)
        proposed = [*current, *ordered_component]
        related = _related_anchors(proposed, anchors, spec)
        estimate = estimate_for(proposed, related)
        if current and estimate > batch_limit:
            previous_anchors, previous_estimate = prepare_component(current)
            batches.append((current, previous_anchors))
            total += previous_estimate
            current = ordered_component
            continue
        if not current and estimate > batch_limit:
            selected_anchors, single_estimate = prepare_component(ordered_component)
            batches.append((ordered_component, selected_anchors))
            total += single_estimate
            current = []
            continue
        current = proposed
    if current:
        selected_anchors, final_estimate = prepare_component(current)
        total += final_estimate
        batches.append((current, selected_anchors))
    return batches, total

async def _request_batch(
    llm: LLMClient,
    *,
    focus: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
    phase: str,
    prompt: str,
    config: dict[str, Any],
    evidence: dict[str, dict[str, Any]],
    known_states: dict[str, dict[str, Any]],
    read_only_terms: set[str],
    prompt_language: str,
    review_states: dict[str, dict[str, Any]],
    spec: Any,
    conflicts: dict[str, dict[str, list[Any]]] | None = None,
) -> dict[str, dict[str, Any]]:
    accepted: dict[str, dict[str, Any]] = {}
    unresolved = {str(item["normalized"]) for item in focus}
    by_normalized = {str(item["normalized"]): item for item in focus}
    component_by_term = {
        str(item["normalized"]): {str(member["normalized"]) for member in component}
        for component in _hard_components(focus, spec)
        for item in component
    }
    previous_errors: list[dict[str, Any]] = []
    previous_invalid: list[dict[str, Any]] = []
    previous_signature: tuple[tuple[str, str | None], ...] | None = None
    parent_request_id: str | None = None
    last_request_id: str | None = None
    max_attempts = int(config["retry"]["format_max_attempts"])
    for attempt in range(max_attempts + 1):
        signature = tuple(
            sorted(
                (str(error["code"]), error.get("normalized"))
                for error in previous_errors
            )
        )
        isolate = bool(attempt and signature and signature == previous_signature)
        if isolate:
            pending_groups = []
            seen_groups: set[tuple[str, ...]] = set()
            for normalized in sorted(unresolved):
                group = tuple(sorted(component_by_term[normalized] & unresolved))
                if group and group not in seen_groups:
                    seen_groups.add(group)
                    pending_groups.append(set(group))
        else:
            pending_groups = [set(unresolved)]
        round_errors: list[dict[str, Any]] = []
        round_invalid: list[dict[str, Any]] = []
        for target_ids in pending_groups:
            request_focus = [
                by_normalized[normalized] for normalized in sorted(target_ids)
            ]
            accepted_anchors = [
                accepted[normalized].get("after", known_states[normalized])
                for normalized in sorted(accepted)
            ]
            request_anchors = [*anchors, *accepted_anchors]
            payload = _make_payload(
                phase=phase,
                target_language=str(config["project"]["target_language"]),
                focus=request_focus,
                anchors=request_anchors,
                evidence=_compact_anchor_evidence(evidence, request_anchors),
                conflicts=conflicts,
            )
            if attempt:
                payload["format_correction"] = format_correction(
                    language=prompt_language,
                    errors=previous_errors,
                    previous_invalid_records=previous_invalid,
                    accepted_normalized=sorted(accepted),
                    target_normalized=sorted(target_ids),
                )
            messages = render_messages(prompt, payload)
            request_id = f"REQ-{uuid.uuid4().hex[:12].upper()}"
            last_request_id = request_id
            estimate = estimate_messages(
                messages, float(config["execution"]["token_safety_factor"])
            )
            response, _ = await llm.chat(
                messages=messages,
                temperature=float(config["llm"]["temperature_terminology_decision"]),
                estimated_input_tokens=estimate,
                request_id=request_id,
                parent_request_id=parent_request_id,
            )
            (
                decisions,
                ignored_read_only,
                normalized_redundant,
                errors,
                invalid_records,
                batch_error,
            ) = _analyze_decisions(
                response.content,
                request_focus,
                visible_states=[*request_focus, *request_anchors],
                known_states={
                    **known_states,
                    **{
                        normalized: decision.get("after", known_states[normalized])
                        for normalized, decision in accepted.items()
                    },
                },
                read_only_terms=read_only_terms | set(accepted),
                prompt_language=prompt_language,
                review_states=review_states,
                spec=spec,
                phase=phase,
                conflicts=conflicts,
            )
            if ignored_read_only:
                llm.logger.warning(
                    "ignored read-only terminology decisions request=%s count=%d normalized=%s",
                    request_id,
                    len(ignored_read_only),
                    ",".join(dict.fromkeys(ignored_read_only[:10])),
                )
            if normalized_redundant:
                llm.logger.warning(
                    "normalized redundant terminology fields request=%s count=%d "
                    "actions=%s normalized=%s",
                    request_id,
                    len(normalized_redundant),
                    ",".join(sorted({action for _, action in normalized_redundant})),
                    ",".join(
                        dict.fromkeys(
                            normalized for normalized, _ in normalized_redundant
                        )
                    )[:200],
                )
            if not batch_error:
                accepted.update(decisions)
                failed_ids = {
                    str(error["normalized"])
                    for error in errors
                    if isinstance(error.get("normalized"), str)
                }
                expanded = set().union(
                    *(component_by_term[normalized] for normalized in failed_ids),
                    set(),
                )
                for normalized in expanded:
                    accepted.pop(normalized, None)
                unresolved.difference_update(decisions)
                unresolved.update(expanded)
            round_errors.extend(errors)
            round_invalid.extend(invalid_records)
            parent_request_id = request_id
        if not unresolved:
            return accepted
        previous_signature = signature
        previous_errors = round_errors
        previous_invalid = round_invalid
    detail = "；".join(error["message"] for error in previous_errors[:5])
    error = UsageError(f"术语决策格式修正重试耗尽：{detail}")
    error.params = {
        "reason": "format_retries_exhausted",
        "request_id": last_request_id,
    }
    raise error
