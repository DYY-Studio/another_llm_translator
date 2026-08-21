from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable
from copy import deepcopy
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx

from .config import load_project_config
from .documents import aozora_match_views
from .errors import RequestSizeError, StorageError, UsageError
from .execution import (
    LLMClient,
    Scope,
    SlidingWindowLimiter,
    combine_usage,
    continue_run,
    create_run,
    estimate_messages,
    finalize_run,
    full_prompt,
    parse_jsonl_document,
    render_messages,
    unavailable_usage,
)
from .i18n import SUPPORTED_LANGUAGES, resolve_language
from .project import prompt_file
from .sqlite_storage import (
    atomic_write_json,
    list_runs,
    read_json,
    read_segment_sources,
    record_header,
    utc_now,
    write_json,
    write_terminology_decision_state,
)
from .stages import (
    build_term_library_rows,
    load_terms,
    normalize_term,
    term_normalization,
)
from .term_decision_protocol import (
    DECISION_ACTIONS,
    DECISION_RULES_VERSION,
    PATCH_FIELDS,
    SIMPLE_ACTION_KEYS,
    UPDATE_ACTION_KEYS,
    format_correction,
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

_PHASES = ("adjudication", "consistency")

_GroupViolation = tuple[str, tuple[str, ...]]
_AliasViolation = tuple[str, tuple[str, ...]]


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


def _term_state(
    term: dict[str, Any], *, disabled: bool | None = None
) -> dict[str, Any]:
    return {
        "normalized": str(term["normalized"]),
        "source": str(term["source"]),
        "category": term.get("category"),
        "description": term.get("description") or None,
        "preferred_translation": term.get("preferred_translation"),
        "aliases": [str(value) for value in term.get("aliases", [])],
        "group_primary": term.get("group_primary"),
        "disabled": bool(term.get("disabled", False)) if disabled is None else disabled,
    }


def _normalized_forms(term: dict[str, Any], spec: Any) -> list[tuple[str, str, str]]:
    values = [("source", str(term["source"]))]
    values.extend(("alias", str(value)) for value in term.get("aliases", []))
    result: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for kind, value in values:
        normalized = normalize_term(value, spec)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append((kind, value, normalized))
    return result


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
    sample_files: dict[str, set[str]] = {key: set() for key in evidence}
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
            samples = item["samples"]
            file_id = str(segment["file_id"])
            if (
                len(samples) < EVIDENCE_SAMPLE_LIMIT
                and file_id not in sample_files[normalized]
            ):
                view_name, excerpt = _evidence_excerpt(
                    views,
                    matched,
                    raw_views=raw_views,
                    source=source,
                    spec=spec,
                )
                samples.append(
                    {
                        "file_id": file_id,
                        "segment_id": str(segment["segment_id"]),
                        "source": excerpt,
                        "match_view": view_name,
                        "matched_forms": [
                            {"kind": kind, "value": original}
                            for kind, original in matched
                        ],
                    }
                )
                sample_files[normalized].add(file_id)
    return evidence


def _evidence_excerpt(
    views: list[str],
    matched: list[tuple[str, str]],
    *,
    raw_views: list[str],
    source: str,
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
    return "source", source[: EVIDENCE_CONTEXT_RADIUS * 2]


def _relation_keys(state: dict[str, Any], spec: Any) -> tuple[str, ...]:
    values = [state["source"], *state.get("aliases", [])]
    keys: set[str] = set()
    for raw in values:
        value = normalize_term(str(raw), spec)
        if not value:
            continue
        keys.add(f"whole:{value}")
        if len(value) >= 2:
            keys.add(f"prefix:{value[:2]}")
            keys.add(f"suffix:{value[-2:]}")
        for token in _TOKEN_SPLIT.split(value):
            if len(token) >= 2:
                keys.add(f"token:{token}")
    preferred = state.get("preferred_translation")
    if preferred:
        value = normalize_term(str(preferred), spec)
        if value:
            keys.add(f"translation:{value}")
            if len(value) >= 2:
                keys.add(f"translation-prefix:{value[:2]}")
                keys.add(f"translation-suffix:{value[-2:]}")
    primary = state.get("group_primary")
    if primary:
        keys.add(f"group:{primary}")
    return tuple(sorted(keys))


def _ordered_states(
    states: Iterable[dict[str, Any]], spec: Any
) -> list[dict[str, Any]]:
    return sorted(
        states,
        key=lambda state: (
            _relation_keys(state, spec)[:1],
            normalize_term(str(state["source"]), spec)[::-1],
            str(state["normalized"]),
        ),
    )


def _hard_components(
    states: list[dict[str, Any]], spec: Any
) -> list[list[dict[str, Any]]]:
    """Return indivisible groups formed by durable group and form ownership edges."""
    by_normalized = {str(state["normalized"]): state for state in states}
    edges = {normalized: set() for normalized in by_normalized}
    owners: dict[str, set[str]] = {}
    for normalized, state in by_normalized.items():
        primary = state.get("group_primary")
        if primary is not None and str(primary) in by_normalized:
            edges[normalized].add(str(primary))
            edges[str(primary)].add(normalized)
        for raw in [state["source"], *state.get("aliases", [])]:
            form = normalize_term(str(raw), spec)
            if form:
                owners.setdefault(form, set()).add(normalized)
    for form_owners in owners.values():
        ordered = sorted(form_owners)
        for left, right in pairwise(ordered):
            edges[left].add(right)
            edges[right].add(left)

    components: list[list[dict[str, Any]]] = []
    remaining = set(by_normalized)
    while remaining:
        first = min(remaining)
        pending = [first]
        component: set[str] = set()
        while pending:
            normalized = pending.pop()
            if normalized in component:
                continue
            component.add(normalized)
            pending.extend(sorted(edges[normalized] - component, reverse=True))
        remaining.difference_update(component)
        components.append([by_normalized[key] for key in sorted(component)])
    return components


def _payload_term(
    state: dict[str, Any],
    evidence: dict[str, dict[str, Any]],
    *,
    include_disabled: bool,
) -> dict[str, Any]:
    value = {
        key: deepcopy(state[key])
        for key in (
            "normalized",
            "source",
            "category",
            "description",
            "preferred_translation",
            "aliases",
            "group_primary",
        )
    } | {"evidence": deepcopy(evidence[state["normalized"]])}
    if include_disabled:
        value["disabled"] = bool(state["disabled"])
    prior_decision = state.get("_prior_decision")
    if include_disabled and isinstance(prior_decision, dict):
        value["prior_decision"] = {
            "action": prior_decision["action"],
            "reason": prior_decision["reason"],
        }
    return value


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
) -> dict[str, Any]:
    include_disabled = phase == "consistency"
    return {
        "phase": phase,
        "target_language": target_language,
        "terms": [
            _payload_term(item, evidence, include_disabled=include_disabled)
            for item in focus
        ],
        "anchors": [
            _payload_term(item, evidence, include_disabled=include_disabled)
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
        )
        return estimate_messages(
            render_messages(prompt, payload),
            token_factor,
        )

    def overflow_reason() -> str:
        if hard_limit == context_limit:
            return "context"
        if hard_limit == itpm_limit:
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


def _nullable_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise UsageError(f"术语决策 {field} 必须是字符串或 null")
    value = value.strip()
    return value or None


def _group_violations(states: dict[str, dict[str, Any]]) -> list[_GroupViolation]:
    active = {
        normalized: state
        for normalized, state in states.items()
        if not state.get("disabled")
    }
    violations: list[_GroupViolation] = []
    for normalized, state in sorted(active.items()):
        primary = state.get("group_primary")
        if primary is None:
            continue
        primary = str(primary)
        if primary == normalized:
            violations.append(("self", (normalized, primary)))
            continue
        target = states.get(primary)
        if target is None:
            violations.append(("missing", (normalized, primary)))
        elif target.get("disabled"):
            violations.append(("disabled", (normalized, primary)))
        elif target.get("group_primary") is not None:
            violations.append(("member", (normalized, primary)))

    cycles: set[tuple[str, ...]] = set()
    visited: set[str] = set()
    for start in sorted(active):
        if start in visited:
            continue
        path: list[str] = []
        positions: dict[str, int] = {}
        current = start
        while current in active and current not in visited:
            if current in positions:
                cycle = path[positions[current] :]
                if len(cycle) > 1:
                    first = min(range(len(cycle)), key=cycle.__getitem__)
                    cycles.add(tuple(cycle[first:] + cycle[:first]))
                break
            positions[current] = len(path)
            path.append(current)
            primary = active[current].get("group_primary")
            if primary is None:
                break
            current = str(primary)
        visited.update(path)
    violations.extend(("cycle", cycle) for cycle in sorted(cycles))
    return violations


def _group_violation_message(violation: _GroupViolation, language: str) -> str:
    kind, values = violation
    if kind == "cycle":
        edge = " -> ".join((*values, values[0]))
    else:
        edge = f"{values[0]} -> {values[1]}"
    if language == "en":
        labels = {
            "self": "self-referencing group pointer",
            "missing": "group pointer to a missing term",
            "disabled": "group pointer to a disabled term",
            "member": "group pointer to another member",
            "cycle": "group pointer cycle",
        }
        return f"{labels[kind]}: {edge}"
    labels = {
        "self": "术语组主自指",
        "missing": "术语组主不存在",
        "disabled": "术语组主已禁用",
        "member": "术语组成员指向另一成员",
        "cycle": "术语组关系循环",
    }
    return f"{labels[kind]}：{edge}"


def _normalized_aliases(state: dict[str, Any], spec: Any) -> dict[str, str]:
    """Map normalized alias forms to their first visible spelling."""
    result: dict[str, str] = {}
    for value in state.get("aliases", []):
        text = str(value)
        normalized = normalize_term(text, spec)
        if normalized:
            result.setdefault(normalized, text)
    return result


def _alias_violations(
    original: dict[str, dict[str, Any]],
    final: dict[str, dict[str, Any]],
    spec: Any,
) -> list[_AliasViolation]:
    """Find newly introduced alias ownership that is not an explicit relation."""
    owners: dict[str, set[str]] = {}
    for normalized, state in original.items():
        values = [state.get("source", ""), *state.get("aliases", [])]
        for value in values:
            form = normalize_term(str(value), spec)
            if form:
                owners.setdefault(form, set()).add(normalized)

    violations: list[_AliasViolation] = []
    for normalized, state in final.items():
        alias_forms = [
            normalize_term(str(value), spec) for value in state.get("aliases", [])
        ]
        aliases = _normalized_aliases(state, spec)
        original_state = original.get(normalized, {})
        original_alias_forms = [
            normalize_term(str(value), spec)
            for value in original_state.get("aliases", [])
        ]
        if len(alias_forms) != len(set(alias_forms)) and (
            len(original_alias_forms) == len(set(original_alias_forms))
            or alias_forms != original_alias_forms
        ):
            violations.append(("duplicate_alias", (normalized, normalized)))
        source_form = normalize_term(str(state.get("source", "")), spec)
        original_source_form = normalize_term(
            str(original_state.get("source", "")), spec
        )
        original_self_aliases = set(original_alias_forms) & {
            normalized,
            original_source_form,
        }
        for alias_form in aliases:
            if (alias_form == normalized or alias_form == source_form) and (
                alias_form not in original_self_aliases
                or alias_forms != original_alias_forms
            ):
                violations.append(("self_alias", (normalized, alias_form)))

        original_aliases = _normalized_aliases(original_state, spec)
        added = set(aliases) - set(original_aliases)
        if not added:
            continue
        if state.get("disabled"):
            violations.extend(
                ("disabled_receiver", (normalized, alias_form))
                for alias_form in sorted(added)
            )
            continue
        if state.get("group_primary") is not None:
            violations.extend(
                (
                    "non_root_receiver",
                    (normalized, str(state["group_primary"]), alias_form),
                )
                for alias_form in sorted(added)
            )
            continue
        for alias_form in sorted(added):
            alias_owners = owners.get(alias_form, set())
            if not alias_owners:
                violations.append(("unknown_alias", (normalized, alias_form)))
                continue
            for owner in sorted(alias_owners - {normalized}):
                owner_state = final.get(owner)
                if owner_state is None:
                    violations.append(
                        ("unknown_owner", (normalized, owner, alias_form))
                    )
                    continue
                owner_primary = owner_state.get("group_primary")
                same_root = (
                    owner_primary is not None and str(owner_primary) == normalized
                )
                released = alias_form not in _normalized_aliases(owner_state, spec)
                owner_source = normalize_term(str(owner_state.get("source", "")), spec)
                source_transfer = alias_form == owner_source
                if (
                    owner_state.get("disabled")
                    or (source_transfer and same_root)
                    or (not source_transfer and released)
                ):
                    continue
                violations.append(("alias_transfer", (normalized, owner, alias_form)))
    return violations


def _alias_violation_message(violation: _AliasViolation, language: str) -> str:
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


def _parse_decisions(
    content: str,
    focus: list[dict[str, Any]],
    *,
    visible_states: list[dict[str, Any]],
    known_states: dict[str, dict[str, Any]],
    read_only_terms: set[str],
    prompt_language: str = "zh-CN",
    review_states: dict[str, dict[str, Any]] | None = None,
    spec: Any | None = None,
    phase: str = "adjudication",
) -> tuple[dict[str, dict[str, Any]], list[str], list[tuple[str, str]]]:
    result = _analyze_decisions(
        content,
        focus,
        visible_states=visible_states,
        known_states=known_states,
        read_only_terms=read_only_terms,
        prompt_language=prompt_language,
        review_states=review_states,
        spec=spec,
        phase=phase,
    )
    if result[3]:
        raise UsageError("；".join(error["message"] for error in result[3][:10]))
    return result[0], result[1], result[2]


def _analyze_decisions(
    content: str,
    focus: list[dict[str, Any]],
    *,
    visible_states: list[dict[str, Any]],
    known_states: dict[str, dict[str, Any]],
    read_only_terms: set[str],
    prompt_language: str,
    review_states: dict[str, dict[str, Any]] | None,
    spec: Any | None,
    phase: str,
) -> tuple[
    dict[str, dict[str, Any]],
    list[str],
    list[tuple[str, str]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    bool,
]:
    document = parse_jsonl_document(content, record_type="decision")
    errors = [
        {"code": "invalid_document", "message": message} for message in document.errors
    ]
    batch_error = bool(document.errors)
    expected = {str(item["normalized"]): item for item in focus}
    decisions: dict[str, dict[str, Any]] = {}
    ignored_read_only: list[str] = []
    seen_targets: set[str] = set()
    normalized_redundant: list[tuple[str, str]] = []
    invalid_records: list[dict[str, Any]] = []
    raw_by_normalized: dict[str, dict[str, Any]] = {}
    visible_forms = {
        str(value)
        for state in visible_states
        for value in [state["source"], *state.get("aliases", [])]
    }
    visible_normalized = {str(state["normalized"]) for state in visible_states}

    def reject(code: str, message: str, value: dict[str, Any]) -> None:
        normalized = value.get("normalized")
        errors.append(
            {
                "code": code,
                "message": message,
                **({"normalized": normalized} if isinstance(normalized, str) else {}),
            }
        )
        invalid_records.append(deepcopy(value))

    for value in document.records:
        normalized = value.get("normalized")
        action = value.get("action")
        if not isinstance(normalized, str) or normalized not in expected:
            if isinstance(normalized, str) and normalized in read_only_terms:
                ignored_read_only.append(normalized)
                continue
            reject("unknown_record", f"未知术语决策 normalized：{normalized}", value)
            batch_error = True
            continue
        if normalized in seen_targets:
            reject("duplicate_record", f"术语决策重复：{normalized}", value)
            continue
        seen_targets.add(normalized)
        raw_by_normalized[normalized] = value
        if action not in DECISION_ACTIONS:
            reject("invalid_action", f"术语决策 action 无效：{normalized}", value)
            continue
        reason = value.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            reject(
                "invalid_reason",
                f"术语决策 reason 必须是非空字符串：{normalized}",
                value,
            )
            continue
        if action == "update":
            if set(value) != UPDATE_ACTION_KEYS:
                missing_keys = sorted(UPDATE_ACTION_KEYS - set(value))
                extra_keys = sorted(set(value) - UPDATE_ACTION_KEYS)
                details = []
                if missing_keys:
                    details.append(f"缺少字段 {', '.join(missing_keys)}")
                if extra_keys:
                    details.append(f"禁止字段 {', '.join(extra_keys)}")
                reject(
                    "invalid_fields",
                    f"{action} 决策字段无效：{normalized}（{'；'.join(details)}）",
                    value,
                )
                continue
        else:
            missing_keys = sorted(SIMPLE_ACTION_KEYS - set(value))
            extra_keys = set(value) - SIMPLE_ACTION_KEYS
            unknown_extra_keys = sorted(extra_keys - PATCH_FIELDS)
            details = []
            if missing_keys:
                details.append(f"缺少字段 {', '.join(missing_keys)}")
            if unknown_extra_keys:
                details.append(f"禁止字段 {', '.join(unknown_extra_keys)}")
            mismatched = sorted(
                key
                for key in extra_keys & PATCH_FIELDS
                if value[key] != expected[normalized].get(key)
            )
            if mismatched:
                details.append(f"冗余字段与当前状态不一致 {', '.join(mismatched)}")
            if details:
                reject(
                    "invalid_fields",
                    f"{action} 决策字段无效：{normalized}（{'；'.join(details)}）",
                    value,
                )
                continue
            if extra_keys:
                normalized_redundant.append((normalized, action))
            decisions[normalized] = {
                "action": action,
                "reason": reason.strip(),
            }
            continue
        changes = value.get("changes")
        if not isinstance(changes, dict) or not set(changes) <= PATCH_FIELDS:
            reject("invalid_patch", f"术语决策 changes 无效：{normalized}", value)
            continue
        prior = expected[normalized].get("_prior_decision")
        empty_allowed = (
            phase == "consistency"
            and isinstance(prior, dict)
            and prior.get("action") == "needs_review"
        ) or bool(expected[normalized].get("disabled"))
        if not changes and not empty_allowed:
            reject("empty_patch", f"术语决策 changes 不得为空：{normalized}", value)
            continue
        after = _term_state(expected[normalized])
        patch_invalid = False
        for key, patch_value in changes.items():
            if key == "aliases":
                if not isinstance(patch_value, list) or not all(
                    isinstance(alias, str) and alias.strip() for alias in patch_value
                ):
                    reject(
                        "invalid_aliases", f"术语决策 aliases 无效：{normalized}", value
                    )
                    patch_invalid = True
                    break
                aliases = [alias.strip() for alias in patch_value]
                if len(aliases) != len(set(aliases)):
                    reject(
                        "invalid_aliases", f"术语决策 aliases 重复：{normalized}", value
                    )
                    patch_invalid = True
                    break
                if any(alias not in visible_forms for alias in aliases):
                    reject(
                        "invisible_alias",
                        f"术语决策发明了源文 alias：{normalized}",
                        value,
                    )
                    patch_invalid = True
                    break
                after[key] = aliases
            elif key == "group_primary":
                if patch_value is not None and (
                    not isinstance(patch_value, str)
                    or patch_value not in visible_normalized
                ):
                    reject(
                        "invisible_group_primary",
                        f"术语决策 group_primary 无效：{normalized}",
                        value,
                    )
                    patch_invalid = True
                    break
                after[key] = patch_value
            else:
                try:
                    parsed = _nullable_string(patch_value, key)
                except UsageError as exc:
                    reject("invalid_patch_value", str(exc), value)
                    patch_invalid = True
                    break
                if key == "description" and parsed not in {
                    None,
                    expected[normalized].get("description") or None,
                }:
                    reject(
                        "invented_description",
                        f"术语决策不得新写 description：{normalized}",
                        value,
                    )
                    patch_invalid = True
                    break
                after[key] = parsed
        if patch_invalid:
            continue
        if changes and all(
            after[key] == expected[normalized].get(key) for key in changes
        ):
            reject("no_op_patch", f"术语决策 changes 未修改状态：{normalized}", value)
            continue
        aliases = after["aliases"]
        if spec is not None:
            alias_forms = [normalize_term(alias, spec) for alias in aliases]
            source_form = normalize_term(str(expected[normalized]["source"]), spec)
            if len(alias_forms) != len(set(alias_forms)):
                reject(
                    "invalid_aliases",
                    f"术语决策 aliases 规范化后重复：{normalized}",
                    value,
                )
                continue
            if any(
                alias_form in {normalized, source_form} for alias_form in alias_forms
            ):
                reject(
                    "self_alias",
                    f"术语决策 aliases 不得包含自身 source：{normalized}",
                    value,
                )
                continue
        decisions[normalized] = {
            "action": action,
            "reason": reason.strip(),
            "after": {**after, "disabled": False},
        }
    candidate_states = dict(known_states)
    for normalized, decision in decisions.items():
        if decision["action"] == "update":
            candidate_states[normalized] = decision["after"]
        elif decision["action"] == "disable":
            candidate_states[normalized] = {
                **candidate_states[normalized],
                "disabled": True,
            }
        elif decision["action"] == "needs_review" and review_states is not None:
            candidate_states[normalized] = review_states[normalized]
    focus_terms = set(expected)
    relationship_errors: list[tuple[str, str]] = []
    for violation in _group_violations(candidate_states):
        affected = focus_terms.intersection(violation[1])
        for normalized in affected:
            relationship_errors.append(
                (normalized, _group_violation_message(violation, prompt_language))
            )
    if spec is not None:
        visible_map = {
            str(state["normalized"]): known_states[str(state["normalized"])]
            for state in visible_states
            if str(state["normalized"]) in known_states
        }
        candidate_visible = {**visible_map}
        candidate_visible.update(
            (normalized, candidate_states[normalized]) for normalized in focus_terms
        )
        for violation in _alias_violations(visible_map, candidate_visible, spec):
            for normalized in focus_terms.intersection(violation[1][:2]):
                relationship_errors.append(
                    (normalized, _alias_violation_message(violation, prompt_language))
                )
    for normalized, message in relationship_errors:
        errors.append(
            {
                "code": "invalid_relationship",
                "message": message,
                "normalized": normalized,
            }
        )
        decisions.pop(normalized, None)
        if (
            normalized in raw_by_normalized
            and raw_by_normalized[normalized] not in invalid_records
        ):
            invalid_records.append(deepcopy(raw_by_normalized[normalized]))
    missing = sorted(set(expected) - seen_targets)
    if missing:
        errors.extend(
            {
                "code": "missing_record",
                "message": f"术语决策缺少记录：{normalized}",
                "normalized": normalized,
            }
            for normalized in missing
        )
    return (
        decisions,
        ignored_read_only,
        normalized_redundant,
        errors,
        invalid_records,
        batch_error,
    )


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


async def _dispatch_batches(
    batches: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]],
    worker: Callable[
        [list[dict[str, Any]], list[dict[str, Any]]],
        Awaitable[None],
    ],
    *,
    max_parallel: int,
) -> None:
    iterator = iter(batches)
    pending: set[asyncio.Task[None]] = set()

    def fill() -> None:
        while len(pending) < max_parallel:
            try:
                focus, anchors = next(iterator)
            except StopIteration:
                return
            pending.add(asyncio.create_task(worker(focus, anchors)))

    fill()
    try:
        while pending:
            done, still_pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            pending = still_pending
            try:
                for task in done:
                    task.result()
            except BaseException:
                pending.update(done)
                raise
            fill()
    except BaseException:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        raise


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


def decision_resume_compatibility(project: Path, run_id: str) -> tuple[bool, str | None]:
    path = _checkpoint_path(project, run_id)
    if not path.is_file():
        return False, "旧 Run 缺少术语决策检查点规则版本"
    try:
        checkpoint = _read_checkpoint_file(path)
    except StorageError:
        return False, "旧 Run 的术语决策检查点不可读"
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


def _decision_dependency_graph(
    original: dict[str, dict[str, Any]],
    final: dict[str, dict[str, Any]],
    spec: Any,
) -> dict[str, set[str]]:
    graph = {key: set() for key in original.keys() | final.keys()}

    def connect(left: str, right: str) -> None:
        graph.setdefault(left, set()).add(right)
        graph.setdefault(right, set()).add(left)

    for states in (original, final):
        for key, state in states.items():
            primary = state.get("group_primary")
            if primary is not None:
                connect(key, str(primary))

    original_owners: dict[str, set[str]] = {}
    for key, state in original.items():
        for value in [state["source"], *state.get("aliases", [])]:
            form = normalize_term(str(value), spec)
            if form:
                original_owners.setdefault(form, set()).add(key)
    for key, state in final.items():
        if key not in original:
            continue
        added = {
            normalize_term(str(value), spec) for value in state.get("aliases", [])
        } - {
            normalize_term(str(value), spec)
            for value in original[key].get("aliases", [])
        }
        for alias in added:
            for owner in original_owners.get(str(alias), set()):
                connect(key, owner)
    return graph


def _dependency_components(
    graph: dict[str, set[str]],
    starts: set[str],
    *,
    allowed: set[str] | None = None,
) -> list[set[str]]:
    remaining = set(starts if allowed is None else starts & allowed)
    components: list[set[str]] = []
    while remaining:
        component: set[str] = set()
        pending = [min(remaining)]
        while pending:
            current = pending.pop()
            if current in component or (allowed is not None and current not in allowed):
                continue
            component.add(current)
            neighbors = graph.get(current, set())
            if allowed is not None:
                neighbors = neighbors & allowed
            pending.extend(sorted(neighbors - component, reverse=True))
        remaining -= component
        components.append(component)
    return components


def _relationship_violation_nodes(
    violation: _GroupViolation | _AliasViolation,
) -> set[str]:
    kind, values = violation
    if kind in {
        "self_alias",
        "duplicate_alias",
        "disabled_receiver",
        "unknown_alias",
    }:
        return {values[0]}
    if kind in {"non_root_receiver", "alias_transfer", "unknown_owner"}:
        return set(values[:2])
    return set(values)


def _relationship_violation_message(
    violation: _GroupViolation | _AliasViolation, language: str
) -> str:
    kind = violation[0]
    if kind in {"self", "missing", "disabled", "member", "cycle"}:
        return _group_violation_message(violation, language)
    return _alias_violation_message(violation, language)


def _recover_invalid_relationship_components(
    *,
    original: dict[str, dict[str, Any]],
    final: dict[str, dict[str, Any]],
    decisions: dict[str, dict[str, Any]],
    language: str,
    spec: Any,
) -> None:
    violations: list[_GroupViolation | _AliasViolation] = [
        *_group_violations(final),
        *_alias_violations(original, final, spec),
    ]
    if not violations:
        return
    graph = _decision_dependency_graph(original, final, spec)
    invalid_nodes = {
        node
        for violation in violations
        for node in _relationship_violation_nodes(violation)
    }
    for component in _dependency_components(graph, invalid_nodes):
        affected = sorted(component & decisions.keys())
        if not affected:
            continue
        details = [
            _relationship_violation_message(violation, language)
            for violation in violations
            if component.intersection(_relationship_violation_nodes(violation))
        ]
        if language == "en":
            reason = (
                "Automatic terminology relationship validation failed; this dependency component was "
                "restored to its pre-run state and requires manual review: "
                + "; ".join(details)
            )
        else:
            reason = (
                "自动决策术语关系校验未通过；该依赖组件已恢复为决策前状态，请人工审查："
                + "；".join(details)
            )
        for normalized in affected:
            final[normalized] = deepcopy(original[normalized])
            decisions[normalized] = {
                "action": "needs_review",
                "reason": reason,
            }


def _proposal_id(values: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        values, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return "TDP-" + hashlib.sha256(encoded.encode()).hexdigest()[:16].upper()


def _build_draft(
    *,
    project_id: str,
    run_id: str,
    revision: int,
    original: dict[str, dict[str, Any]],
    final: dict[str, dict[str, Any]],
    decisions: dict[str, dict[str, Any]],
    protected: set[str],
    evidence: dict[str, dict[str, Any]],
    fingerprint: str,
    source_library: dict[str, Any],
    source_overrides: dict[str, Any],
    model_fingerprint: str,
    prompt_fingerprint: str,
    spec: Any,
) -> dict[str, Any]:
    changed = {
        key
        for key in final
        if key not in protected
        and any(original[key][field] != final[key][field] for field in _STATE_FIELDS)
    }
    graph = _decision_dependency_graph(original, final, spec)
    components = _dependency_components(graph, changed, allowed=changed)
    proposals: list[dict[str, Any]] = []
    for component in sorted(components, key=lambda value: sorted(value)):
        keys = sorted(component)
        before = [deepcopy(original[key]) for key in keys]
        after = [deepcopy(final[key]) for key in keys]
        fields = sorted(
            {
                field
                for key in keys
                for field in _STATE_FIELDS
                if original[key][field] != final[key][field]
            }
        )
        relationship = len(keys) > 1 or "group_primary" in fields
        proposals.append(
            {
                "proposal_id": _proposal_id([{"before": before, "after": after}]),
                "kind": "relationship" if relationship else "term_update",
                "normalized": keys,
                "before": before,
                "after": after,
                "changes": fields,
                "reason": "；".join(
                    dict.fromkeys(decisions[key]["reason"] for key in keys)
                ),
                "evidence": {key: deepcopy(evidence[key]) for key in keys},
            }
        )
    needs_review = [
        {
            "normalized": key,
            "source": original[key]["source"],
            "reason": decision["reason"],
            "evidence": deepcopy(evidence[key]),
        }
        for key, decision in sorted(decisions.items())
        if decision["action"] == "needs_review"
    ]
    return record_header(
        "terminology_decision_draft",
        project_id,
        record_id=f"TERMINOLOGY-DECISION-{run_id}",
        run_id=run_id,
        status="pending",
        source_terms_revision=revision,
        decision_fingerprint=fingerprint,
        model_fingerprint=model_fingerprint,
        prompt_fingerprint=prompt_fingerprint,
        proposals=proposals,
        needs_review=needs_review,
        rejected_proposal_ids=[],
        source_library=source_library,
        source_overrides=source_overrides,
    )


def _validate_final_states(
    project: Path,
    states: dict[str, dict[str, Any]],
    *,
    original: dict[str, dict[str, Any]],
    spec: Any,
) -> None:
    violations = _group_violations(states)
    violations.extend(_alias_violations(original, states, spec))
    if violations:
        raise UsageError(
            "术语决策生成非法术语关系："
            + "；".join(
                _relationship_violation_message(violation, "zh-CN")
                for violation in violations
            )
        )
    active = [deepcopy(state) for state in states.values() if not state.get("disabled")]
    built = build_term_library_rows(project, active, {})
    built_by_key = {str(item["normalized"]): item for item in built}
    expected_keys = {key for key, state in states.items() if not state.get("disabled")}
    if set(built_by_key) != expected_keys:
        raise UsageError("术语决策生成了不完整的最终术语集合")
    for normalized in expected_keys:
        expected_primary = states[normalized].get("group_primary")
        if built_by_key[normalized].get("group_primary") != expected_primary:
            raise UsageError(f"术语决策产生未声明的组关系：{normalized}")


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


def _draft_path(project: Path, run_id: str) -> Path:
    return project / "runs" / run_id / DRAFT_FILE


def current_decision_draft(project: Path) -> dict[str, Any] | None:
    for manifest in sorted(
        list_runs(project, stage=STAGE),
        key=lambda item: str(item.get("started_at", "")),
        reverse=True,
    ):
        if manifest.get("decision_status") != "pending":
            continue
        path = _draft_path(project, str(manifest["run_id"]))
        if not path.is_file():
            raise StorageError(f"术语决策 Run 缺少草案：{manifest['run_id']}")
        draft = json.loads(path.read_text(encoding="utf-8"))
        draft["rejected_proposal_ids"] = list(manifest.get("rejected_proposal_ids", []))
        return draft
    return None


def _decision_needs_review(
    project: Path, manifest: dict[str, Any]
) -> list[dict[str, Any]]:
    """Read the review items that belong to one completed decision Run."""
    path = _draft_path(project, str(manifest["run_id"]))
    if not path.is_file():
        raise StorageError(f"术语决策 Run 缺少草案：{manifest['run_id']}")
    try:
        draft = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageError(
            f"无法读取术语决策草案：{manifest['run_id']}: {exc}"
        ) from exc
    values = draft.get("needs_review", [])
    if not isinstance(values, list):
        raise StorageError(f"术语决策草案人工关注项格式无效：{manifest['run_id']}")
    resolved = {
        str(value)
        for value in manifest.get("manual_review_resolved_normalized", [])
        if isinstance(value, str)
    }
    result: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, dict) or not isinstance(value.get("normalized"), str):
            raise StorageError(f"术语决策草案人工关注项格式无效：{manifest['run_id']}")
        result.append(
            {
                **deepcopy(value),
                "run_id": str(manifest["run_id"]),
                "resolved": value["normalized"] in resolved,
            }
        )
    return result


def manual_review_state(project: Path) -> dict[str, Any]:
    """Return the active manual-review queue for the latest decision epoch."""
    latest: dict[str, dict[str, Any]] = {}
    for manifest in list_runs(project, stage=STAGE):
        status = manifest.get("decision_status")
        is_replacement = manifest.get("manual_review_replaces_previous") is True
        if status in {"applied", "rejected"}:
            for item in _decision_needs_review(project, manifest):
                normalized = str(item["normalized"])
                if normalized not in latest:
                    latest[normalized] = item
        # The marker is retained when the Run is rolled back.  In that case
        # its own items are no longer active, but older queues must not revive.
        if is_replacement:
            break
    items = [latest[key] for key in sorted(latest)]
    resolved = sum(1 for item in items if item["resolved"])
    return {
        "items": items,
        "total": len(items),
        "resolved": resolved,
        "remaining": len(items) - resolved,
    }


def set_manual_review_resolved(
    project: Path, *, run_id: str, normalized: str, resolved: bool
) -> dict[str, Any]:
    """Persist one review item's explicit handled state in its Run manifest."""
    manifest = next(
        (
            item
            for item in list_runs(project, stage=STAGE)
            if str(item.get("run_id")) == run_id
        ),
        None,
    )
    if manifest is None or manifest.get("decision_status") not in {
        "applied",
        "rejected",
    }:
        raise UsageError("该 Run 没有可处理的人工关注项")
    items = _decision_needs_review(project, manifest)
    known = {str(item["normalized"]) for item in items}
    if normalized not in known:
        raise UsageError(f"未知人工关注术语：{normalized}")
    active = {
        (str(item["run_id"]), str(item["normalized"]))
        for item in manual_review_state(project)["items"]
    }
    if (run_id, normalized) not in active:
        raise UsageError("该人工关注项已被更新的术语决策队列取代")
    values = {
        str(value)
        for value in manifest.get("manual_review_resolved_normalized", [])
        if isinstance(value, str)
    }
    if resolved:
        values.add(normalized)
    else:
        values.discard(normalized)
    manifest["manual_review_resolved_normalized"] = sorted(values)
    write_json(project, project / "runs" / run_id / "manifest.json", manifest)
    return manual_review_state(project)


def decision_review_state(project: Path) -> dict[str, Any]:
    draft = current_decision_draft(project)
    applied = _latest_applied_manifest(project)
    library = load_terms(project)
    rollback = None
    if (
        applied is not None
        and library is not None
        and int(applied.get("applied_terms_revision", -1))
        == int(library["terms_revision"])
    ):
        rollback = {
            "run_id": str(applied["run_id"]),
            "applied_terms_revision": int(applied["applied_terms_revision"]),
        }
    return {
        "draft": draft,
        "rollback": rollback,
        "manual_review": manual_review_state(project),
    }


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
    eligible = [
        state
        for key, state in states.items()
        if key not in protected and not state["disabled"]
    ]
    if not eligible:
        raise UsageError("已发布术语全部受到人工 override 保护")
    evidence = collect_term_evidence(project, list(library.get("terms", [])), config)
    language = _prompt_language(project, prompt_language)
    prompts = _prompt(project, language)
    spec = term_normalization(config)
    protected_states = [states[key] for key in sorted(protected & set(states))]
    phase_one, phase_one_tokens = _pack_batches(
        eligible,
        phase="adjudication",
        target_language=str(config["project"]["target_language"]),
        anchors=protected_states,
        evidence=evidence,
        prompt=prompts["adjudication"],
        config=config,
        spec=spec,
    )
    simulated_focus = [
        {
            **deepcopy(state),
            "_prior_decision": {"action": "keep", "reason": "dry-run"},
        }
        for state in eligible
    ]
    phase_two, phase_two_tokens = _pack_batches(
        simulated_focus,
        phase="consistency",
        target_language=str(config["project"]["target_language"]),
        anchors=[*protected_states, *eligible],
        evidence=evidence,
        prompt=prompts["consistency"],
        config=config,
        spec=spec,
    )
    return {
        "library": library,
        "config": config,
        "overrides_document": overrides_document,
        "protected": protected,
        "states": states,
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
    on_progress: Callable[[int, int, int], None] | None = None,
    on_usage: Callable[[dict[str, Any] | None], None] | None = None,
) -> dict[str, Any]:
    existing = current_decision_draft(project)
    if existing is not None and not replace_draft:
        raise UsageError("已有待处理术语决策草案；必须明确替换")
    plan = decision_plan(project, prompt_language)
    library = plan["library"]
    config = plan["config"]
    metadata = read_json(project, project / "project.json")
    overrides_document = plan["overrides_document"]
    protected = plan["protected"]
    states = plan["states"]
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
        resume_manifest = read_json(
            project, project / "runs" / resume_run_id / "manifest.json"
        )
        if int(resume_manifest.get("source_terms_revision", -1)) != revision:
            raise UsageError("术语库 revision 已变化，不能续用自动决策 Run")
        compatible, incompatibility_reason = decision_resume_compatibility(
            project, resume_run_id
        )
        if not compatible:
            raise UsageError(
                f"{incompatibility_reason}；请显式结束旧 Run 并强制新建"
            )
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
        run_id, run_dir, _continuation_index = continue_run(
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
    limiter = SlidingWindowLimiter(
        int(config["execution"]["requests_per_minute"]),
        int(config["execution"]["input_tokens_per_minute"]),
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
            if on_progress:
                on_progress(completed, 0, total)
            remaining_phase_one = [
                item for item in eligible if str(item["normalized"]) not in decisions
            ]
            phase_one, _ = _pack_batches(
                remaining_phase_one,
                phase="adjudication",
                target_language=str(config["project"]["target_language"]),
                anchors=protected_states,
                evidence=evidence,
                prompt=prompts["adjudication"],
                config=config,
                spec=spec,
            )

            async def adjudicate(
                focus: list[dict[str, Any]], anchors: list[dict[str, Any]]
            ) -> None:
                nonlocal completed
                result = await _request_batch(
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

            await _dispatch_batches(
                phase_one,
                adjudicate,
                max_parallel=int(config["execution"]["max_parallel"]),
            )
            tentative = deepcopy(states)
            _apply_tentative(tentative, decisions)
            phase_two_state = _consistency_states(states, tentative, final_decisions)
            phase_two_focus = [
                {
                    **deepcopy(phase_two_state[item["normalized"]]),
                    "_prior_decision": deepcopy(decisions[item["normalized"]]),
                }
                for item in eligible
            ]
            phase_two_anchors = [
                *protected_states,
                *[phase_two_state[item["normalized"]] for item in eligible],
            ]
            remaining_phase_two = [
                item
                for item in phase_two_focus
                if str(item["normalized"]) not in final_decisions
            ]
            phase_two, _ = _pack_batches(
                remaining_phase_two,
                phase="consistency",
                target_language=str(config["project"]["target_language"]),
                anchors=phase_two_anchors,
                evidence=evidence,
                prompt=prompts["consistency"],
                config=config,
                spec=spec,
            )

            async def review_consistency(
                focus: list[dict[str, Any]], anchors: list[dict[str, Any]]
            ) -> None:
                nonlocal completed
                result = await _request_batch(
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

            await _dispatch_batches(
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
        _validate_final_states(project, final, original=states, spec=spec)
        draft = _build_draft(
            project_id=str(metadata["project_id"]),
            run_id=run_id,
            revision=int(library["terms_revision"]),
            original=states,
            final=final,
            decisions=decisions,
            protected=protected,
            evidence=evidence,
            fingerprint=_composite_fingerprint(checkpoint, "decision_fingerprint"),
            source_library=deepcopy(library),
            source_overrides=deepcopy(overrides_document),
            model_fingerprint=_composite_fingerprint(checkpoint, "model_fingerprint"),
            prompt_fingerprint=_composite_fingerprint(checkpoint, "prompt_fingerprint"),
            spec=spec,
        )
        atomic_write_json(_draft_path(project, run_id), draft)
        manifest = read_json(project, run_dir / "manifest.json")
        manifest.update(
            decision_status="pending",
            proposal_count=len(draft["proposals"]),
            needs_review_count=len(draft["needs_review"]),
            protected_term_count=len(protected_states),
        )
        manifest.pop("last_interruption", None)
        write_json(project, run_dir / "manifest.json", manifest)
        usage = finalize_run(
            project,
            run_dir,
            status="completed",
            completed=total,
            failed=0,
            usage=usage,
            usage_invoked=usage_invoked,
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
    except asyncio.CancelledError as exc:
        if "llm" in locals():
            usage = llm.usage_summary()
        record_resumable_interruption(usage, exc)
        raise
    except Exception as exc:
        if "llm" in locals():
            usage = llm.usage_summary()
        record_resumable_interruption(usage, exc)
        raise


def _pending_manifest(project: Path, run_id: str) -> dict[str, Any]:
    manifest = read_json(project, project / "runs" / run_id / "manifest.json")
    if manifest.get("stage") != STAGE or manifest.get("decision_status") != "pending":
        raise UsageError("术语决策草案不再处于待审核状态")
    return manifest


def save_decision_rejections(project: Path, proposal_ids: list[str]) -> dict[str, Any]:
    draft = current_decision_draft(project)
    if draft is None:
        raise UsageError("没有待处理术语决策草案")
    library = load_terms(project)
    if library is None or int(library["terms_revision"]) != int(
        draft["source_terms_revision"]
    ):
        raise UsageError("术语库已变化，当前决策草案已过期")
    if not isinstance(proposal_ids, list) or not all(
        isinstance(value, str) and value for value in proposal_ids
    ):
        raise UsageError("rejected_proposal_ids 必须是字符串数组")
    known = {str(item["proposal_id"]) for item in draft["proposals"]}
    rejected = list(dict.fromkeys(proposal_ids))
    unknown = sorted(set(rejected) - known)
    if unknown:
        raise UsageError(f"未知术语决策建议：{', '.join(unknown[:10])}")
    run_id = str(draft["run_id"])
    path = project / "runs" / run_id / "manifest.json"
    manifest = _pending_manifest(project, run_id)
    manifest["rejected_proposal_ids"] = rejected
    write_json(project, path, manifest)
    draft["rejected_proposal_ids"] = rejected
    return draft


def discard_decision_draft(project: Path, *, confirm: bool) -> dict[str, Any]:
    if confirm is not True:
        raise UsageError("必须明确确认丢弃术语决策草案")
    draft = current_decision_draft(project)
    if draft is None:
        raise UsageError("没有待处理术语决策草案")
    run_id = str(draft["run_id"])
    path = project / "runs" / run_id / "manifest.json"
    manifest = _pending_manifest(project, run_id)
    manifest.update(decision_status="discarded", discarded_at=utc_now())
    write_json(project, path, manifest)
    return {"discarded": True, "run_id": run_id}


def _proposal_after_states(
    draft: dict[str, Any], rejected: set[str]
) -> tuple[dict[str, dict[str, Any]], int]:
    values: dict[str, dict[str, Any]] = {}
    accepted = 0
    for proposal in draft["proposals"]:
        if str(proposal["proposal_id"]) in rejected:
            continue
        accepted += 1
        for state in proposal["after"]:
            normalized = str(state["normalized"])
            if normalized in values:
                raise StorageError(f"术语决策建议重复修改：{normalized}")
            values[normalized] = deepcopy(state)
    return values, accepted


def apply_decision_draft(
    project: Path,
    *,
    confirm_all: bool,
    rejected_proposal_ids: list[str] | None = None,
) -> dict[str, Any]:
    if confirm_all is not True:
        raise UsageError("必须使用 --all 或界面确认应用术语决策")
    draft = current_decision_draft(project)
    if draft is None:
        raise UsageError("没有待处理术语决策草案")
    library = load_terms(project)
    if library is None or int(library["terms_revision"]) != int(
        draft["source_terms_revision"]
    ):
        raise UsageError("术语库已变化，当前决策草案已过期")
    manifest_rejected = set(map(str, draft.get("rejected_proposal_ids", [])))
    requested_rejected = set(map(str, rejected_proposal_ids or []))
    known = {str(item["proposal_id"]) for item in draft["proposals"]}
    unknown = sorted(requested_rejected - known)
    if unknown:
        raise UsageError(f"未知术语决策建议：{', '.join(unknown[:10])}")
    rejected = manifest_rejected | requested_rejected
    after_states, accepted = _proposal_after_states(draft, rejected)
    run_id = str(draft["run_id"])
    manifest_path = project / "runs" / run_id / "manifest.json"
    manifest = _pending_manifest(project, run_id)
    if accepted == 0:
        manifest.update(
            decision_status="rejected",
            rejected_proposal_ids=sorted(rejected),
            applied_proposal_count=0,
            manual_review_resolved_normalized=[],
            manual_review_replaces_previous=True,
            applied_at=utc_now(),
        )
        write_json(project, manifest_path, manifest)
        return {
            "run_id": run_id,
            "applied": 0,
            "rejected": len(rejected),
            "terms_revision": int(library["terms_revision"]),
        }
    overrides_document = read_json(project, project / "terminology" / "overrides.json")
    overrides = {
        str(item["normalized"]): deepcopy(item)
        for item in overrides_document.get("overrides", [])
    }
    protected = set(overrides)
    if protected.intersection(after_states):
        raise UsageError("术语决策试图修改受保护的人工 override")
    current = {
        str(item["normalized"]): deepcopy(item) for item in library.get("terms", [])
    }
    for normalized, state in after_states.items():
        if normalized not in current:
            raise UsageError(f"术语决策目标已不存在：{normalized}")
        override = {
            "normalized": normalized,
            "source": state["source"],
            "category": state.get("category"),
            "description": state.get("description"),
            "preferred_translation": state.get("preferred_translation"),
            "aliases": list(state.get("aliases", [])),
            "disabled": bool(state.get("disabled")),
        }
        if state.get("group_primary") is not None:
            override["group_primary"] = state["group_primary"]
        else:
            override["group_primary"] = None
        overrides[normalized] = override
        if override["disabled"]:
            current.pop(normalized, None)
        else:
            current[normalized] = {
                **current[normalized],
                **deepcopy(state),
                "conflicts": {
                    "categories": [],
                    "preferred_translations": [],
                    "alias_primaries": [],
                    "group_claims": [],
                },
            }
    terms = build_term_library_rows(
        project,
        [current[key] for key in sorted(current)],
        overrides,
    )
    revision = int(library["terms_revision"]) + 1
    term_record = record_header(
        "terminology_library",
        str(draft["project_id"]),
        record_id=f"TERMS-{revision}",
        terms_revision=revision,
        published_run_id=library.get("published_run_id"),
        active_task_id=library.get("active_task_id"),
        decision_run_id=run_id,
        origin="terminology_decision",
        terms=terms,
    )
    override_record = record_header(
        "terminology_overrides",
        str(draft["project_id"]),
        record_id="TERMINOLOGY-OVERRIDES",
        origin="terminology_decision",
        overrides=[overrides[key] for key in sorted(overrides)],
    )
    manifest.update(
        decision_status="applied",
        rejected_proposal_ids=sorted(rejected),
        applied_proposal_count=accepted,
        manual_review_resolved_normalized=[],
        manual_review_replaces_previous=True,
        applied_terms_revision=revision,
        applied_at=utc_now(),
    )
    write_terminology_decision_state(
        project,
        terms=term_record,
        overrides=override_record,
        run_manifest=manifest,
    )
    return {
        "run_id": run_id,
        "applied": accepted,
        "rejected": len(rejected),
        "terms_revision": revision,
    }


def _latest_applied_manifest(project: Path) -> dict[str, Any] | None:
    for manifest in sorted(
        list_runs(project, stage=STAGE),
        key=lambda item: str(item.get("started_at", "")),
        reverse=True,
    ):
        if manifest.get("decision_status") == "applied":
            return manifest
    return None


def rollback_decision(project: Path, *, confirm: bool) -> dict[str, Any]:
    if confirm is not True:
        raise UsageError("必须明确确认撤销术语决策")
    manifest = _latest_applied_manifest(project)
    if manifest is None:
        raise UsageError("没有可撤销的已应用术语决策")
    run_id = str(manifest["run_id"])
    draft_path = _draft_path(project, run_id)
    if not draft_path.is_file():
        raise StorageError(f"术语决策 Run 缺少撤销快照：{run_id}")
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    library = load_terms(project)
    if library is None or int(library["terms_revision"]) != int(
        manifest["applied_terms_revision"]
    ):
        raise UsageError("术语库在应用后已有变化，不能撤销自动决策")
    source_library = deepcopy(draft["source_library"])
    source_overrides = deepcopy(draft["source_overrides"])
    revision = int(library["terms_revision"]) + 1
    source_library.update(
        record_id=f"TERMS-{revision}",
        terms_revision=revision,
        origin="terminology_decision_rollback",
        rollback_run_id=run_id,
        created_at=utc_now(),
    )
    source_overrides.update(
        origin="terminology_decision_rollback",
        created_at=utc_now(),
    )
    manifest.update(
        decision_status="rolled_back",
        rollback_terms_revision=revision,
        rolled_back_at=utc_now(),
    )
    write_terminology_decision_state(
        project,
        terms=source_library,
        overrides=source_overrides,
        run_manifest=manifest,
    )
    return {
        "run_id": run_id,
        "rolled_back": True,
        "terms_revision": revision,
    }
