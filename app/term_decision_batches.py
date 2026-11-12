from __future__ import annotations
from .async_utils import run_blocking

import re
import math
import json
import uuid
from copy import deepcopy
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .request_budget import resolve_request_budget, request_input_limit
from .config import load_project_config
from .documents import aozora_match_views
from .errors import RequestSizeError, StorageError, UsageError
from .execution import (
    estimate_tokens,
    estimate_messages,
    render_messages,
    run_bounded,
)
from .llm_client import LLMClient
from .sqlite_storage import (
    read_segment_sources,
    read_segments,
)
from .term_decision_protocol import (
    validation_feedback,
)
from .term_decision_rules import *
from .term_decision_rules import (
    _alias_violations,
    _group_violations,
    _relationship_violation_message,
    _relationship_violation_nodes,
    _relation_keys,
)
from .term_library import (
    normalize_term,
    term_normalization,
)

_TOKEN_SPLIT = re.compile(r"[\s・·･._—–\-]+")
STAGE = "terminology_decision"
EVIDENCE_SAMPLE_LIMIT = 5
EVIDENCE_CONTEXT_RADIUS = 60
REVIEW_EVIDENCE_SAMPLE_LIMIT = 10
REVIEW_EVIDENCE_CONTEXT_RADIUS = 2
RELATED_ANCHOR_LIMIT = 24
_PHASES = ("adjudication", "consistency")

__all__ = ['_evidence_excerpt', '_make_payload', '_pack_batches', '_related_anchors', '_relation_keys', '_request_batch', '_request_evidence', '_request_limits', 'collect_term_evidence', 'collect_term_review_evidence']


def _collect_segment_matches(
    segments: list[dict[str, Any]],
    forms_by_term: dict[str, list[tuple[str, str, str]]],
    spec: Any,
) -> dict[str, dict[str, Any]]:
    """Count matches while retaining only bounded review samples."""
    prefix_index: dict[str, set[str]] = {}
    single_index: dict[str, set[str]] = {}
    for normalized, forms in forms_by_term.items():
        for _, _, form in forms:
            target = single_index if len(form) == 1 else prefix_index
            key = form if len(form) == 1 else form[:2]
            target.setdefault(key, set()).add(normalized)

    matches = {
        normalized: {
            "hit_count": 0,
            "source_hit_count": 0,
            "alias_hit_counts": {
                value: 0 for kind, value, _ in forms if kind == "alias"
            },
            "boundary_samples": [],
            "fallback_samples": [],
            "sample_boundaries": set(),
        }
        for normalized, forms in forms_by_term.items()
    }
    for segment in segments:
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
            item = matches[normalized]
            item["hit_count"] += 1
            if any(kind == "source" for kind, _ in matched):
                item["source_hit_count"] += 1
            for kind, original in matched:
                if kind == "alias":
                    item["alias_hit_counts"][original] += 1
            boundary = (str(segment["file_id"]), str(segment["part_id"]))
            is_first_boundary_sample = boundary not in item["sample_boundaries"]
            should_sample = (
                is_first_boundary_sample
                and len(item["boundary_samples"]) < REVIEW_EVIDENCE_SAMPLE_LIMIT
            ) or (
                not is_first_boundary_sample
                and len(item["fallback_samples"]) < REVIEW_EVIDENCE_SAMPLE_LIMIT
            )
            if not should_sample:
                continue
            view_name, _ = _evidence_excerpt(
                views,
                matched,
                raw_views=raw_views,
                spec=spec,
            )
            sample = {
                "segment_id": str(segment["segment_id"]),
                "file_id": str(segment["file_id"]),
                "part_id": str(segment["part_id"]),
                "line_index": int(segment["line_index"]),
                "source": source,
                "is_empty": bool(segment["is_empty"]),
                "match_view": view_name,
                "matched_forms": [
                    {"kind": kind, "value": original}
                    for kind, original in matched
                ],
            }
            if is_first_boundary_sample:
                item["boundary_samples"].append(sample)
                item["sample_boundaries"].add(boundary)
            else:
                item["fallback_samples"].append(sample)
    return matches

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


def collect_term_review_evidence(
    project: Path,
    terms: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
    *,
    anchors: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Collect bounded full-Segment evidence for a later terminology review."""
    config = config or load_project_config(project)
    spec = term_normalization(config)
    if anchors:
        selected = {str(term["normalized"]): term for term in terms}
        for term in terms:
            for anchor in _related_anchors([term], anchors, spec):
                selected[str(anchor["normalized"])] = anchor
        terms = list(selected.values())
    forms_by_term = {
        str(term["normalized"]): _normalized_forms(term, spec) for term in terms
    }
    segments = read_segments(project)
    matches = _collect_segment_matches(segments, forms_by_term, spec)
    segment_index = {
        str(segment["segment_id"]): index for index, segment in enumerate(segments)
    }
    runs: list[tuple[int, int]] = []
    run_start = 0
    for index in range(1, len(segments) + 1):
        if index == len(segments) or (
            (
                str(segments[index]["file_id"]),
                str(segments[index]["part_id"]),
            )
            != (
                str(segments[run_start]["file_id"]),
                str(segments[run_start]["part_id"]),
            )
        ):
            runs.append((run_start, index - 1))
            run_start = index
    run_by_index = {
        index: (start, end)
        for start, end in runs
        for index in range(start, end + 1)
    }
    evidence: dict[str, dict[str, Any]] = {}
    for normalized, item in matches.items():
        evidence[normalized] = {
            "hit_count": item["hit_count"],
            "source_hit_count": item["source_hit_count"],
            "alias_hit_counts": item["alias_hit_counts"],
            "samples": [],
            "windows": [],
        }

    for normalized, item in matches.items():
        boundary_samples = item["boundary_samples"]
        selected = [
            *boundary_samples,
            *item["fallback_samples"][
                : REVIEW_EVIDENCE_SAMPLE_LIMIT - len(boundary_samples)
            ],
        ]
        ranges = []
        for sample in selected:
            index = segment_index[sample["segment_id"]]
            run_start, run_end = run_by_index[index]
            ranges.append(
                {
                    "start": max(run_start, index - REVIEW_EVIDENCE_CONTEXT_RADIUS),
                    "end": min(run_end, index + REVIEW_EVIDENCE_CONTEXT_RADIUS),
                    "hit_segment_id": sample["segment_id"],
                }
            )
        ranges.sort(key=lambda item: item["start"])
        merged: list[dict[str, Any]] = []
        for item in ranges:
            if merged and item["start"] <= merged[-1]["end"]:
                merged[-1]["end"] = max(merged[-1]["end"], item["end"])
                merged[-1]["hit_segment_ids"].append(item["hit_segment_id"])
            else:
                merged.append(
                    {
                        "start": item["start"],
                        "end": item["end"],
                        "hit_segment_ids": [item["hit_segment_id"]],
                    }
                )

        window_by_hit: dict[str, str] = {}
        for window_index, window in enumerate(merged, start=1):
            window_id = f"W{window_index:03d}"
            hit_ids = sorted(
                window["hit_segment_ids"], key=lambda value: segment_index[value]
            )
            window_by_hit.update({segment_id: window_id for segment_id in hit_ids})
            evidence[normalized]["windows"].append(
                {
                    "window_id": window_id,
                    "hit_segment_ids": hit_ids,
                    "segments": [
                        {
                            "segment_id": str(segment["segment_id"]),
                            "file_id": str(segment["file_id"]),
                            "part_id": str(segment["part_id"]),
                            "line_index": int(segment["line_index"]),
                            "source": str(segment["source"]),
                            "is_empty": bool(segment["is_empty"]),
                        }
                        for segment in segments[window["start"] : window["end"] + 1]
                    ],
                }
            )
        evidence[normalized]["samples"] = [
            {
                **sample,
                "window_id": window_by_hit[sample["segment_id"]],
            }
            for sample in selected
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
    *,
    include_windows: bool = False,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, str],
]:
    """Project evidence and share repeated source text within a request."""
    boundary_refs: dict[tuple[str, str], int] = {}
    segment_refs: dict[tuple[str, str, str], int] = {}
    source_refs: dict[str, int] = {}
    projected: dict[str, dict[str, Any]] = {}
    context_segments: dict[str, dict[str, Any]] = {}
    evidence_sources: dict[str, str] = {}

    def project_segment(segment: dict[str, Any]) -> tuple[int, int]:
        boundary = (str(segment["file_id"]), str(segment["part_id"]))
        boundary_ref = boundary_refs.setdefault(boundary, len(boundary_refs) + 1)
        segment_key = (*boundary, str(segment["segment_id"]))
        segment_ref = segment_refs.setdefault(segment_key, len(segment_refs) + 1)
        return boundary_ref, segment_ref

    for state in [*focus, *anchors]:
        normalized = str(state["normalized"])
        value = deepcopy(evidence[normalized])
        samples: list[dict[str, Any]] = []
        window_refs: dict[str, int] = {}
        windows = []
        if include_windows:
            for window in value.get("windows", []):
                window_id = str(window["window_id"])
                window_ref = window_refs.setdefault(window_id, len(window_refs) + 1)
                segments = []
                for segment in window["segments"]:
                    boundary_ref, segment_ref = project_segment(segment)
                    context_segments.setdefault(str(segment_ref), {
                        "boundary_ref": boundary_ref,
                        "source": segment["source"],
                        "is_empty": bool(segment["is_empty"]),
                    })
                    segments.append(segment_ref)
                hit_segment_refs = [
                    segment_refs[
                        (
                            str(segment["file_id"]),
                            str(segment["part_id"]),
                            str(segment["segment_id"]),
                        )
                    ]
                    for segment in window["segments"]
                    if str(segment["segment_id"]) in window["hit_segment_ids"]
                ]
                windows.append(
                    {
                        "window_ref": window_ref,
                        "hit_segment_refs": hit_segment_refs,
                        "segment_refs": segments,
                    }
                )
        for sample in value["samples"]:
            boundary_ref, segment_ref = project_segment(sample)
            sample_value = {
                "boundary_ref": boundary_ref,
                "match_view": sample["match_view"],
                "matched_forms": deepcopy(sample["matched_forms"]),
            }
            if include_windows:
                window_id = str(sample["window_id"])
                window_ref = window_refs.setdefault(window_id, len(window_refs) + 1)
                sample_value.update(
                    segment_ref=segment_ref,
                    window_ref=window_ref,
                )
            else:
                source = str(sample["source"])
                source_ref = source_refs.setdefault(source, len(source_refs) + 1)
                evidence_sources[str(source_ref)] = source
                sample_value["source_ref"] = source_ref
            samples.append(sample_value)
        value["samples"] = samples
        if include_windows:
            value["windows"] = windows
        else:
            value.pop("windows", None)
        projected[normalized] = value
    return projected, context_segments, evidence_sources

def _direct_group_anchors(
    focus: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    spec: Any,
) -> list[dict[str, Any]]:
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
    direct = []
    for item in candidates:
        item_forms = {
            normalize_term(str(value), spec)
            for value in [item["source"], *item.get("aliases", [])]
        }
        primary = item.get("group_primary")
        item_primary = (
            normalize_term(str(primary), spec) if primary is not None else None
        )
        if focus_primaries.intersection(item_forms) or (
            item_primary is not None and item_primary in focus_forms
        ):
            direct.append(item)
    return direct


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
        for item in _direct_group_anchors(focus, candidates, spec)
        if str(item["normalized"]) not in focus_ids
    ]
    direct_ids = {str(item["normalized"]) for item in direct}
    related = [
        item
        for item in candidates
        if str(item["normalized"]) not in focus_ids | direct_ids
        and related_to_focus(item)
    ]
    return [*direct, *related[: max(0, RELATED_ANCHOR_LIMIT - len(direct))]]

def _request_limits(config: dict[str, Any]) -> tuple[int, int, int, int]:
    budget = resolve_request_budget(config, "terminology_decision")
    hard_limit = request_input_limit(config, budget)
    return (budget.target_content_tokens, hard_limit,
            min(budget.max_input_tokens, config["llm"]["context_window_tokens"] - budget.context_reserve_tokens),
            config["execution"]["input_tokens_per_minute"])


def _decision_content_tokens(payload: dict[str, Any], factor: float) -> int:
    fields = ("normalized", "source", "category", "preferred_translation", "aliases", "group_primary", "disabled")
    terms = [{key: term[key] for key in fields if key in term} for term in payload["terms"]]
    for projected, term in zip(terms, payload["terms"], strict=True):
        if "conflicts" in term:
            projected["conflicts"] = {key: value for key, value in term["conflicts"].items() if key in ("categories", "preferred_translations", "alias_primaries", "group_claims")}
    return math.ceil(estimate_tokens(json.dumps(terms, ensure_ascii=False, separators=(",", ":"))) * factor)

def _make_payload(
    *,
    phase: str,
    target_language: str,
    focus: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
    evidence: dict[str, dict[str, Any]],
    conflicts: dict[str, dict[str, list[Any]]] | None = None,
) -> dict[str, Any]:
    include_disabled = phase in {"consistency", "final_review", "containment"}
    request_evidence, context_segments, evidence_sources = _request_evidence(
        focus,
        anchors,
        evidence,
        include_windows=phase == "final_review",
    )
    payload = {
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
    applied_descriptions: dict[str, str] = {}
    description_refs: dict[str, str] = {}
    for term in [*payload["terms"], *payload["anchors"]]:
        if "applied_description" not in term:
            continue
        text = term.pop("applied_description")
        ref = None
        if text is not None:
            ref = description_refs.setdefault(text, f"D{len(description_refs) + 1}")
            applied_descriptions[ref] = text
        term["applied_description_ref"] = ref
    if applied_descriptions:
        payload["applied_descriptions"] = applied_descriptions
    if evidence_sources:
        payload["evidence_sources"] = evidence_sources
    if phase == "final_review":
        payload["context_segments"] = context_segments
    return payload


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
    components: list[list[dict[str, Any]]] | None = None,
    feedback_builder: Callable[[set[str]], str] | None = None,
) -> tuple[list[tuple[list[dict[str, Any]], list[dict[str, Any]]]], int]:
    soft_target, hard_limit, context_limit, itpm_limit = _request_limits(config)
    token_factor = float(config["execution"]["token_safety_factor"])

    def measure(focus: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, int]:
        related = _related_anchors(focus, anchors, spec)
        payload = _make_payload(phase=phase, target_language=target_language, focus=focus,
                                anchors=related, evidence=evidence, conflicts=conflicts)
        if feedback_builder is not None:
            feedback = feedback_builder({str(item["normalized"]) for item in focus})
            if feedback:
                payload["validation_repair"] = feedback
        return related, estimate_messages(render_messages(prompt, payload), token_factor), _decision_content_tokens(payload, token_factor)

    batches: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []
    total = 0
    current: list[dict[str, Any]] = []
    components = _hard_components(states, spec) if components is None else components
    soft_rank = {str(state["normalized"]): index for index, state in enumerate(_ordered_states(states, spec))}
    components.sort(key=lambda component: (min(soft_rank[str(state["normalized"])] for state in component),
                                            tuple(str(state["normalized"]) for state in component)))
    for component in components:
        ordered = _ordered_states(component, spec)
        candidate = [*current, *ordered]
        related, estimate, content = measure(candidate)
        if current and (estimate > hard_limit or content > soft_target):
            previous_anchors, previous_estimate, _ = measure(current)
            batches.append((current, previous_anchors))
            total += previous_estimate
            current = []
            candidate = ordered
            related, estimate, content = measure(candidate)
        if estimate > hard_limit:
            sources = ", ".join(str(state["source"]) for state in candidate[:5])
            raise RequestSizeError(
                f"不可拆术语组件 [{sources}] 的完整术语证据和 Anchors 请求估算 {estimate} tokens，限制 {hard_limit} tokens",
                reason="itpm" if 0 < itpm_limit < context_limit else "context")
        current = candidate
    if current:
        related, estimate, _ = measure(current)
        batches.append((current, related))
        total += estimate
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
    previous_errors: list[dict[str, Any]] = []
    previous_signature: tuple[tuple[str, str | None], ...] | None = None
    parent_request_id: str | None = None
    last_request_id: str | None = None
    max_attempts = int(config["retry"]["format_max_attempts"])

    def accepted_state(normalized: str, decision: dict[str, Any]) -> dict[str, Any]:
        action = decision["action"]
        if action == "update":
            return decision["after"]
        if action == "disable":
            return {**known_states[normalized], "disabled": True}
        if action == "needs_review":
            return review_states[normalized]
        return known_states[normalized]

    def dependency_graph(
        candidate_states: dict[str, dict[str, Any]],
    ) -> dict[str, set[str]]:
        graph = _decision_dependency_graph(known_states, candidate_states, spec)
        candidate_focus = {
            normalized: candidate_states[normalized]
            for normalized in by_normalized
        }
        for states in (by_normalized, candidate_focus):
            for component in _hard_components(list(states.values()), spec):
                members = [str(item["normalized"]) for item in component]
                for left, right in zip(members, members[1:]):
                    graph.setdefault(left, set()).add(right)
                    graph.setdefault(right, set()).add(left)
        return graph

    for attempt in range(max_attempts + 1):
        signature = tuple(
            sorted(
                (str(error["code"]), error.get("normalized"))
                for error in previous_errors
            )
        )
        isolate = bool(attempt and signature and signature == previous_signature)
        if attempt == 0:
            retry_components = _hard_components(focus, spec)
        else:
            accepted_states = {normalized: accepted_state(normalized, decision) for normalized, decision in accepted.items()}
            retry_states = {**known_states, **accepted_states}
            retry_graph = dependency_graph(retry_states)
            for error in previous_errors:
                members = sorted(set(error.get("_dependency_component", [])) & set(by_normalized))
                for left, right in zip(members, members[1:]):
                    retry_graph.setdefault(left, set()).add(right)
                    retry_graph.setdefault(right, set()).add(left)
            retry_components = [
                [by_normalized[normalized] for normalized in sorted(component & unresolved)]
                for component in _dependency_components(retry_graph, unresolved)
                if component & unresolved
            ]
        round_accepted = dict(accepted)
        round_accepted_states = {
            normalized: accepted_state(normalized, decision)
            for normalized, decision in round_accepted.items()
        }
        round_parent_request_id = parent_request_id

        round_anchors = {str(item["normalized"]): item for item in anchors}
        round_anchors.update(round_accepted_states)
        packed_groups: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []
        for component_set in ([[component] for component in retry_components] if isolate else [retry_components]):
            packed, _ = await run_blocking(lambda: _pack_batches(
                [item for component in component_set for item in component], phase=phase,
                target_language=str(config["project"]["target_language"]), anchors=list(round_anchors.values()),
                evidence=evidence, prompt=prompt, config=config, spec=spec, conflicts=conflicts,
                components=component_set,
                feedback_builder=lambda target_ids: validation_feedback(previous_errors, target_ids, language=prompt_language)))
            packed_groups.extend(packed)
        pending_groups = [{str(item["normalized"]) for item in group} for group, _ in packed_groups]

        async def request_group(
            target_ids: set[str],
        ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], str]:
            request_focus = [
                by_normalized[normalized] for normalized in sorted(target_ids)
            ]
            anchor_states = {
                str(item["normalized"]): item
                for item in anchors
                if str(item["normalized"]) not in target_ids
            }
            anchor_states.update(
                (normalized, state)
                for normalized, state in round_accepted_states.items()
                if normalized not in target_ids
            )
            request_anchors = _related_anchors(
                request_focus, list(anchor_states.values()), spec
            )
            payload = (await run_blocking(lambda: _make_payload(
                phase=phase,
                target_language=str(config["project"]["target_language"]),
                focus=request_focus,
                anchors=request_anchors,
                evidence=evidence,
                conflicts=conflicts,
            )))
            feedback = await run_blocking(
                lambda: validation_feedback(
                    previous_errors, target_ids, language=prompt_language
                )
            )
            if feedback:
                payload["validation_repair"] = feedback
            messages = render_messages(prompt, payload)
            request_id = f"REQ-{uuid.uuid4().hex[:12].upper()}"
            last_request_id = request_id
            estimate = (await run_blocking(lambda: estimate_messages(
                messages, float(config["execution"]["token_safety_factor"])
            )))
            _, hard_limit, context_limit, itpm_limit = _request_limits(config)
            if estimate > hard_limit:
                raise RequestSizeError(
                    f"术语决策完整请求估算 {estimate} tokens，超过硬限制 {hard_limit} tokens",
                    reason="itpm" if 0 < itpm_limit < context_limit else "context",
                )
            response, request_id = await llm.chat(
                messages=messages,
                temperature=float(config["llm"]["temperature_terminology_decision"]),
                estimated_input_tokens=estimate,
                request_id=request_id,
                parent_request_id=round_parent_request_id,
            )
            (
                decisions,
                ignored_read_only,
                normalized_redundant,
                errors,
                batch_error,
            ) = (await run_blocking(lambda: _analyze_decisions(
                response.content,
                request_focus,
                visible_states=[*request_focus, *request_anchors],
                known_states={
                    **known_states,
                    **round_accepted_states,
                },
                read_only_terms=read_only_terms | set(round_accepted),
                prompt_language=prompt_language,
                review_states=review_states,
                spec=spec,
                phase=phase,
                conflicts=conflicts,
                descriptions={item["normalized"]: item["description"] for item in payload["terms"]},
            )))
            if ignored_read_only:
                llm.logger.warning(
                    "ignored read-only terminology decisions request=%s count=%d normalized=%s",
                    request_id,
                    len(ignored_read_only),
                    ",".join(dict.fromkeys(ignored_read_only[:10])),
                )
            unknown_records = [
                error for error in errors
                if error["code"] == "unknown_record" and not batch_error
            ]
            if unknown_records:
                llm.logger.warning(
                    "rejected unknown terminology decisions request=%s count=%d normalized=%s",
                    request_id,
                    len(unknown_records),
                    ",".join(
                        dict.fromkeys(
                            str(error.get("normalized"))
                            for error in unknown_records[:10]
                        )
                    ),
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
            return ({} if batch_error else decisions), errors, request_id

        group_results = await run_bounded(
            pending_groups,
            request_group,
            max_parallel=int(config["execution"]["max_parallel"]),
        )
        round_errors: list[dict[str, Any]] = []
        round_decisions: dict[str, dict[str, Any]] = {}
        failed_ids: set[str] = set()
        request_ids: list[str] = []
        for target_ids, (decisions, errors, request_id) in zip(
            pending_groups, group_results, strict=True
        ):
            request_ids.append(request_id)
            round_errors.extend(errors)
            round_decisions.update(decisions)
            failed_ids.update(
                str(error["normalized"])
                for error in errors
                if isinstance(error.get("normalized"), str)
                and str(error["normalized"]) in target_ids
            )
            for error in errors:
                failed_ids.update(
                    str(normalized)
                    for normalized in error.get("_dependency_component", [])
                )

        accepted.update(round_decisions)
        candidate_states = dict(known_states)
        for normalized, decision in accepted.items():
            candidate_states[normalized] = accepted_state(normalized, decision)
        expanded: set[str] = set()
        graph: dict[str, set[str]] | None = None
        if failed_ids:
            graph = dependency_graph(candidate_states)
            for component in _dependency_components(graph, failed_ids):
                expanded.update(component & set(by_normalized))
        if round_decisions and len(pending_groups) > 1:
            original_group_violations = set(_group_violations(known_states))
            violations = [
                *(
                    violation
                    for violation in _group_violations(candidate_states)
                    if violation not in original_group_violations
                ),
                *_alias_violations(known_states, candidate_states, spec),
            ]
            conflict_nodes = {
                normalized
                for violation in violations
                for normalized in _relationship_violation_nodes(violation)
            }
            if conflict_nodes:
                if graph is None:
                    graph = dependency_graph(candidate_states)
                for component in _dependency_components(graph, conflict_nodes):
                    affected = component & accepted.keys() & set(by_normalized)
                    if not affected:
                        continue
                    failed_ids.update(affected)
                    detail = "; ".join(
                        _relationship_violation_message(violation, prompt_language)
                        for violation in violations
                        if component.intersection(
                            _relationship_violation_nodes(violation)
                        )
                    )
                    round_errors.append({
                        "code": "invalid_relationship",
                        "message": detail,
                        "normalized": min(affected),
                        "_dependency_component": sorted(component),
                    })
                    expanded.update(affected)
        for normalized in expanded:
            accepted.pop(normalized, None)
        unresolved.difference_update(round_decisions)
        unresolved.update(expanded)
        if request_ids:
            last_request_id = request_ids[-1]
            parent_request_id = request_ids[-1]
        if not unresolved:
            return accepted
        previous_signature = signature
        previous_errors = round_errors
    detail = "；".join(error["message"] for error in previous_errors[:5])
    error = UsageError(f"术语决策格式修正重试耗尽：{detail}")
    error.params = {
        "reason": "format_retries_exhausted",
        "request_id": last_request_id,
    }
    raise error
