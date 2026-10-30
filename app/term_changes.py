"""Unreviewed scan differences, referencing the original candidate records."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import UsageError
from .sqlite_storage import read_terminology_candidates


def require_no_decision_draft(project: Path) -> None:
    from .term_decision_drafts import current_decision_draft
    if current_decision_draft(project) is not None:
        raise UsageError("存在待审术语决策草案，请先应用或丢弃，再扫描或发布术语")


def accumulate_changes(
    library: dict[str, Any], previous: dict[str, Any] | None,
    candidates: list[dict[str, Any]], spec: Any,
) -> None:
    from .term_library import normalize_term
    knowledge = library["knowledge"]
    pending = knowledge["pending_changes"]
    seen = set(knowledge["published_candidates"])
    current = {term["normalized"]: term for term in (previous or {}).get("terms", [])}
    active = {term["normalized"] for term in library["terms"]}
    for record in candidates:
        if record["record_id"] in seen:
            continue
        for index, candidate in enumerate(record["terms"]):
            key = normalize_term(candidate["source"], spec)
            if key not in active:
                continue
            old = current.get(key)
            changed = old is None or any(
                candidate.get(field) and candidate[field] != old.get(field)
                for field in ("preferred_translation", "category")
            ) or bool(set(candidate.get("aliases", [])) - set(old.get("aliases", [])))
            if not changed:
                continue
            item = pending.setdefault(key, {"kind": "new" if old is None else "changed", "candidate_refs": []})
            ref = [record["record_id"], index]
            if ref not in item["candidate_refs"]:
                item["candidate_refs"].append(ref)
    for key in pending.keys() - active:
        del pending[key]


def change_details(project: Path, library: dict[str, Any]) -> dict[str, Any]:
    pending = library["knowledge"]["pending_changes"]
    ids = {ref[0] for item in pending.values() for ref in item["candidate_refs"]}
    records = {record["record_id"]: record for record in read_terminology_candidates(project, ids)} if ids else {}
    result = {}
    for key, item in pending.items():
        grouped = {}
        locations = {}
        for record_id, index in item["candidate_refs"]:
            term = records[record_id]["terms"][index]
            signature = (term["source"], term.get("preferred_translation"), term.get("category"), tuple(sorted(term.get("aliases", []))))
            candidate = grouped.setdefault(signature, {
                "source": term["source"], "preferred_translation": term.get("preferred_translation"),
                "category": term.get("category"), "aliases": term.get("aliases", []),
                "candidate_id": record_id, "source_range": [],
            })
            for source in records[record_id]["source_range"]:
                location = {name: source[name] for name in ("segment_id", "file_id", "part_id", "start", "end")}
                identity = tuple(location.values())
                seen = locations.setdefault(signature, set())
                if identity not in seen:
                    candidate["source_range"].append(location)
                    seen.add(identity)
        result[key] = {"kind": item["kind"], "candidates": list(grouped.values())}
    return result


def decision_targets(library: dict[str, Any], changes: dict[str, Any], spec: Any, *, all_terms: bool) -> set[str]:
    from copy import deepcopy
    from .term_decision_rules import _decision_dependency_graph, _dependency_components, _term_state
    from .term_decision_batches import _related_anchors
    states = {term["normalized"]: _term_state(term) for term in library["terms"]}
    if all_terms:
        return set(states)
    targets = set(library["knowledge"]["pending_changes"])
    targets.update(term["normalized"] for term in library["terms"] if any(term.get("conflicts", {}).values()))
    targets &= states.keys()
    if not targets:
        return targets
    incoming = deepcopy(states)
    for key, change in changes.items():
        if key in incoming:
            incoming[key]["aliases"] = list(set(incoming[key]["aliases"]) | {
                alias for candidate in change["candidates"] for alias in candidate.get("aliases", [])})
    # Existing related-anchor selection is bounded; only one expansion is made.
    targets.update(item["normalized"] for item in _related_anchors(
        [incoming[key] for key in sorted(targets)], list(states.values()), spec))
    graph = _decision_dependency_graph(states, incoming, spec)
    return set().union(*_dependency_components(graph, targets)) & states.keys()


def publication_invalidates_undo(project: Path) -> bool:
    from .term_decision_drafts import _latest_applied_manifest
    from .term_library import load_terms
    library = load_terms(project, resolve_knowledge=False)
    applied = _latest_applied_manifest(project)
    return bool(library and applied and applied.get("applied_terms_revision") == library["terms_revision"])
