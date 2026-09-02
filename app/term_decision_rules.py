from __future__ import annotations
import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import Callable, Iterable
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
    run_bounded,
    unavailable_usage,
)
from .i18n import SUPPORTED_LANGUAGES, resolve_language
from .llm_keys import KeyPool
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
from .term_library import (
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

from .term_library import build_term_library_rows, normalize_term, term_normalization
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

_TOKEN_SPLIT = re.compile(r"[\s・·･._—–\-]+")

def _alias_violation_message(violation: _GroupViolation, language: str) -> str:
    kind, values = violation
    if kind == "alias_transfer":
        receiver, owner, alias = values
        return (f"alias {alias} remains owned by {owner} while added to {receiver}" if language == "en" else f"alias {alias} 已由 {owner} 保留却被新增到 {receiver}")
    if kind == "self_alias":
        return (f"term {values[0]} contains its own source as alias {values[1]}" if language == "en" else f"术语 {values[0]} 将自身原文 {values[1]} 作为 alias")
    if kind == "non_root_receiver":
        return (f"alias receiver {values[0]} is not a root term" if language == "en" else f"alias 接收方 {values[0]} 不是组根术语")
    if kind == "disabled_receiver":
        return (f"disabled term {values[0]} cannot receive alias {values[1]}" if language == "en" else f"已禁用术语 {values[0]} 不能接收 alias {values[1]}")
    if kind == "unknown_alias":
        return (f"alias {values[1]} has no known owner" if language == "en" else f"alias {values[1]} 没有已知所有者")
    if kind == "unknown_owner":
        return (f"alias {values[2]} points to missing owner {values[1]}" if language == "en" else f"alias {values[2]} 指向不存在的所有者 {values[1]}")
    return (f"term {values[0]} contains duplicate normalized aliases" if language == "en" else f"术语 {values[0]} 含有重复的规范化 alias")

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

__all__ = ['_alias_violations', '_analyze_decisions', '_conflicts_by_term', '_decision_dependency_graph', '_dependency_components', '_effective_conflicts', '_empty_conflicts', '_group_violation_message', '_group_violations', '_hard_components', '_has_conflicts', '_normalized_aliases', '_normalized_forms', '_nullable_string', '_ordered_states', '_payload_term', '_proposal_after_states', '_recover_invalid_relationship_components', '_relationship_violation_message', '_relationship_violation_nodes', '_term_conflicts', '_term_state', '_validate_accepted_relationship_conflicts', '_validate_accepted_scalar_conflicts', '_validate_final_states']

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

def _term_conflicts(term: dict[str, Any]) -> dict[str, list[Any]]:
    raw = term.get("conflicts") or {}
    if not isinstance(raw, dict):
        raise StorageError("术语 conflicts 必须是对象")
    conflicts: dict[str, list[Any]] = {}
    for field in _CONFLICT_FIELDS:
        values = raw.get(field, [])
        if not isinstance(values, list):
            raise StorageError(f"术语 conflicts.{field} 必须是数组")
        conflicts[field] = deepcopy(values)
    return conflicts

def _empty_conflicts() -> dict[str, list[Any]]:
    return {field: [] for field in _CONFLICT_FIELDS}

def _conflicts_by_term(
    terms: Iterable[dict[str, Any]],
) -> dict[str, dict[str, list[Any]]]:
    return {str(term["normalized"]): _term_conflicts(term) for term in terms}

def _has_conflicts(conflicts: dict[str, list[Any]]) -> bool:
    return any(conflicts[field] for field in _CONFLICT_FIELDS)

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
    conflicts: dict[str, dict[str, list[Any]]] | None = None,
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
    term_conflicts = (conflicts or {}).get(str(state["normalized"]))
    if term_conflicts is not None and _has_conflicts(term_conflicts):
        value["conflicts"] = deepcopy(term_conflicts)
    if include_disabled:
        value["disabled"] = bool(state["disabled"])
    prior_decision = state.get("_prior_decision")
    if include_disabled and isinstance(prior_decision, dict):
        value["prior_decision"] = {
            "action": prior_decision["action"],
            "reason": prior_decision["reason"],
        }
    return value

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
) -> list[_GroupViolation]:
    """Find newly introduced alias ownership that is not an explicit relation."""
    owners: dict[str, set[str]] = {}
    for normalized, state in original.items():
        values = [state.get("source", ""), *state.get("aliases", [])]
        for value in values:
            form = normalize_term(str(value), spec)
            if form:
                owners.setdefault(form, set()).add(normalized)

    violations: list[_GroupViolation] = []
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
    conflicts: dict[str, dict[str, list[Any]]] | None = None,
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
        {
            "code": "invalid_document",
            "message": message,
            "_document_error_code": error_code,
        }
        for message, error_code in zip(
            document.errors, document.error_codes, strict=True
        )
    ]
    batch_error = bool(document.errors)
    expected = {str(item["normalized"]): item for item in focus}
    conflicts = conflicts or {}
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
            term_conflicts = conflicts.get(normalized, {})
            scalar_conflicts = bool(
                term_conflicts.get("categories")
                or term_conflicts.get("preferred_translations")
            )
            if action == "keep" and scalar_conflicts and phase == "adjudication":
                reject(
                    "unresolved_conflict",
                    f"术语决策 keep 不能保留未裁决类别或推荐译名冲突：{normalized}",
                    value,
                )
                continue
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
                after[key] = parsed
        if patch_invalid:
            continue
        term_conflicts = conflicts.get(normalized, {})
        required_conflict_fields = {
            "category": term_conflicts.get("categories", []),
            "preferred_translation": term_conflicts.get("preferred_translations", []),
        }
        missing_conflict_fields = (
            [
                field
                for field, candidates in required_conflict_fields.items()
                if candidates and (field not in changes or after.get(field) is None)
            ]
            if phase == "adjudication"
            else []
        )
        if missing_conflict_fields:
            reject(
                "unresolved_conflict",
                "术语决策 update 必须明确解决冲突字段："
                f"{normalized}（{', '.join(missing_conflict_fields)}）",
                value,
            )
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

def _effective_conflicts(
    project: Path,
    states: dict[str, dict[str, Any]],
    source_conflicts: dict[str, dict[str, list[Any]]],
) -> dict[str, dict[str, list[Any]]]:
    rebuilt: dict[str, dict[str, list[Any]]] = {}
    if not _group_violations(states):
        active = [
            deepcopy(state) for state in states.values() if not state.get("disabled")
        ]
        rebuilt = {
            str(term["normalized"]): _term_conflicts(term)
            for term in build_term_library_rows(project, active, {})
        }
    result: dict[str, dict[str, list[Any]]] = {}
    for normalized, state in states.items():
        if state.get("disabled"):
            result[normalized] = _empty_conflicts()
            continue
        original = source_conflicts.get(normalized, _empty_conflicts())
        structural = rebuilt.get(normalized, original)
        result[normalized] = {
            "categories": (
                deepcopy(original["categories"])
                if state.get("category") is None
                else []
            ),
            "preferred_translations": (
                deepcopy(original["preferred_translations"])
                if state.get("preferred_translation") is None
                else []
            ),
            "alias_primaries": deepcopy(structural["alias_primaries"]),
            "group_claims": deepcopy(structural["group_claims"]),
        }
    return result

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

    owners: dict[str, set[str]] = {}
    for states in (original, final):
        for key, state in states.items():
            for value in [state["source"], *state.get("aliases", [])]:
                form = normalize_term(str(value), spec)
                if form:
                    owners.setdefault(form, set()).add(key)
    for values in owners.values():
        for left, right in pairwise(sorted(values)):
            connect(left, right)
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
    violation: _GroupViolation,
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

def _relationship_violation_message(violation: _GroupViolation, language: str) -> str:
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
    conflicts: dict[str, dict[str, list[Any]]] | None = None,
) -> None:
    violations: list[_GroupViolation] = [
        *_group_violations(final),
        *_alias_violations(original, final, spec),
    ]
    structural_conflicts = {
        normalized: value
        for normalized, value in (conflicts or {}).items()
        if value.get("alias_primaries") or value.get("group_claims")
    }
    if not violations and not structural_conflicts:
        return
    graph = _decision_dependency_graph(original, final, spec)
    invalid_nodes = {
        node
        for violation in violations
        for node in _relationship_violation_nodes(violation)
    } | set(structural_conflicts)
    for component in _dependency_components(graph, invalid_nodes):
        affected = sorted(component & decisions.keys())
        if not affected:
            continue
        details = [
            _relationship_violation_message(violation, language)
            for violation in violations
            if component.intersection(_relationship_violation_nodes(violation))
        ]
        details.extend(
            (
                f"{normalized} still has unresolved alias or group conflicts"
                if language == "en"
                else f"{normalized} 仍有未解决 alias 或组争用"
            )
            for normalized in sorted(component & structural_conflicts.keys())
        )
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

def _validate_accepted_scalar_conflicts(
    draft: dict[str, Any], after_states: dict[str, dict[str, Any]]
) -> None:
    source_library = draft.get("source_library")
    if not isinstance(source_library, dict):
        raise StorageError("术语决策草案缺少源术语库快照")
    source_terms = {
        str(term["normalized"]): term
        for term in source_library.get("terms", [])
        if isinstance(term, dict) and isinstance(term.get("normalized"), str)
    }
    for normalized, state in after_states.items():
        source = source_terms.get(normalized)
        if source is None:
            raise StorageError(f"术语决策草案源快照缺少术语：{normalized}")
        if state.get("disabled"):
            continue
        conflicts = _term_conflicts(source)
        unresolved = []
        if conflicts["categories"] and state.get("category") is None:
            unresolved.append("category")
        if (
            conflicts["preferred_translations"]
            and state.get("preferred_translation") is None
        ):
            unresolved.append("preferred_translation")
        if unresolved:
            raise UsageError(
                "术语决策草案未明确解决冲突字段："
                f"{normalized}（{', '.join(unresolved)}）；请重新生成"
            )

def _validate_accepted_relationship_conflicts(
    *,
    original_terms: list[dict[str, Any]],
    final_terms: list[dict[str, Any]],
    changed: set[str],
    spec: Any,
) -> None:
    original = {str(term["normalized"]): _term_state(term) for term in original_terms}
    final = {str(term["normalized"]): _term_state(term) for term in final_terms}
    final_conflicts = _conflicts_by_term(final_terms)
    conflict_nodes = {
        normalized
        for normalized, conflicts in final_conflicts.items()
        if conflicts["alias_primaries"] or conflicts["group_claims"]
    }
    if not conflict_nodes:
        return
    graph = _decision_dependency_graph(original, final, spec)
    affected: set[str] = set()
    for component in _dependency_components(graph, conflict_nodes):
        affected.update(component & changed)
    if affected:
        raise UsageError(
            "术语决策草案仍有未解决 alias 或组争用："
            + ", ".join(sorted(affected)[:10])
            + "；请重新生成"
        )
