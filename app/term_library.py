from __future__ import annotations
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from .config import load_project_config
from .errors import (
    UsageError,
)
from .sqlite_storage import (
    list_runs,
    read_json,
    read_jsonl,
    record_exists,
    record_header,
    write_json,
)

@dataclass(frozen=True)
class TermNormalization:
    form: str | None
    casefold: bool

def _build_term_rows(
    merged: dict[str, dict[str, Any]],
    *,
    alias_policy: str,
    spec: TermNormalization,
) -> list[dict[str, Any]]:
    _alias_primary_collisions(merged, policy=alias_policy, spec=spec)
    terms: list[dict[str, Any]] = []
    for index, (normalized, item) in enumerate(sorted(merged.items()), start=1):
        categories = sorted(set(item["categories"]))
        descriptions = sorted(set(item["descriptions"]))
        translations = sorted(set(item["translations"]))
        sources = sorted(set(item["sources"]), key=lambda text: (len(text), text))
        aliases = sorted(
            {
                alias
                for alias in item["aliases"]
                if normalize_term(alias, spec) != normalized
            }
        )
        terms.append(
            {
                "record_id": f"TERM-{index:06d}",
                "source": item["canonical_source"]
                or (sources[0] if sources else normalized),
                "normalized": normalized,
                "category": categories[0] if len(categories) == 1 else None,
                "description": "；".join(descriptions),
                "preferred_translation": (
                    translations[0] if len(translations) == 1 else None
                ),
                "aliases": aliases,
                "group_primary": item["group_primary"],
                "conflicts": {
                    "categories": categories if len(categories) > 1 else [],
                    "preferred_translations": (
                        translations if len(translations) > 1 else []
                    ),
                    "alias_primaries": sorted(
                        item["alias_conflicts"],
                        key=lambda value: (
                            value["alias"],
                            value["primary_source"],
                            value["reason"],
                        ),
                    ),
                    "group_claims": sorted(
                        item["group_claims"],
                        key=lambda value: (
                            value["entry"],
                            value["claimed_by"],
                            value["alias"],
                            value["reason"],
                        ),
                    ),
                },
            }
        )
    return terms

def _alias_primary_collisions(
    merged: dict[str, dict[str, Any]],
    *,
    policy: str,
    spec: TermNormalization,
) -> None:
    def add_claim(
        entry: str, claimed_by: str, alias: str, reason: str
    ) -> None:
        claim = {
            "entry": entry,
            "claimed_by": claimed_by,
            "alias": alias,
            "reason": reason,
        }
        for normalized in {entry, claimed_by}:
            if normalized in merged and claim not in merged[normalized]["group_claims"]:
                merged[normalized]["group_claims"].append(claim)

    for normalized, item in merged.items():
        primary = item["group_primary"]
        if primary is None:
            continue
        if primary == normalized or primary not in merged:
            raise UsageError(f"术语组主指针无效：{normalized} -> {primary}")
        if merged[primary]["group_primary"] is not None:
            raise UsageError(f"术语组主必须直接指向主条目：{normalized} -> {primary}")

    primary_sources = {
        normalized: sorted(
            set(item["sources"]), key=lambda text: (len(text), text)
        )[0]
        for normalized, item in merged.items()
        if item["sources"]
    }
    claims: dict[str, list[tuple[str, str]]] = {}
    for owner, item in merged.items():
        for alias in sorted(set(item["aliases"])):
            target = normalize_term(alias, spec)
            if target in merged and target != owner:
                claims.setdefault(target, []).append((owner, alias))
    if not claims:
        return

    parent = {
        target: owners[0][0]
        for target, owners in claims.items()
        if len({owner for owner, _ in owners}) == 1
    }
    cycle_nodes: set[str] = set()
    for node in parent:
        seen: list[str] = []
        current = node
        while current in parent:
            if current in seen:
                cycle_nodes.update(seen[seen.index(current) :])
                break
            seen.append(current)
            current = parent[current]

    unsafe_targets = {
        target for target, owners in claims.items() if len({o for o, _ in owners}) > 1
    } | cycle_nodes
    for target, owners in claims.items():
        for owner, alias in owners:
            owner_root = merged[owner]["group_primary"] or owner
            target_root = merged[target]["group_primary"] or target
            if owner_root == target_root:
                continue
            reason = (
                "multiple_owners"
                if len({value[0] for value in owners}) > 1
                else "cycle"
                if target in cycle_nodes or owner in cycle_nodes
                else "policy"
                if policy == "conflict"
                else "group_collision"
                if merged[target]["group_primary"] is not None
                or merged[target]["group_primary_locked"]
                or any(
                    value["group_primary"] == target
                    for value in merged.values()
                )
                else ""
            )
            if not reason and target not in unsafe_targets:
                merged[target]["group_primary"] = owner_root
                continue
            reason = reason or "group_collision"
            add_claim(target, owner, alias, reason)
            merged[owner]["alias_conflicts"].append(
                {
                    "alias": alias,
                    "primary_source": primary_sources.get(target, target),
                    "reason": reason,
                }
            )

    for normalized, item in merged.items():
        primary = item["group_primary"]
        if primary is not None and (
            primary not in merged or merged[primary]["group_primary"] is not None
        ):
            raise UsageError(f"术语组关系无法规范化：{normalized} -> {primary}")

def _term_bucket() -> dict[str, Any]:
    return {
        "sources": [],
        "categories": [],
        "descriptions": [],
        "translations": [],
        "aliases": [],
        "alias_conflicts": [],
        "group_primary": None,
        "group_primary_set": False,
        "group_primary_locked": False,
        "group_claims": [],
        "canonical_source": None,
    }

def _seed_published_terms(
    merged: dict[str, dict[str, Any]],
    library: dict[str, Any] | None,
    spec: TermNormalization,
) -> None:
    for term in (library or {}).get("terms", []):
        _add_term_candidate(merged, term, spec)
        description = term.get("description")
        if description and "；" in str(description):
            current = merged[normalize_term(str(term["source"]), spec)]
            current["descriptions"].remove(str(description))
            current["descriptions"].extend(
                part for part in str(description).split("；") if part
            )

def build_term_library_rows(
    project: Path,
    base_terms: list[dict[str, Any]],
    overrides: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    config = load_project_config(project)
    spec = term_normalization(config)
    merged: dict[str, dict[str, Any]] = {}
    _seed_published_terms(merged, {"terms": base_terms}, spec)
    _apply_term_overrides(merged, overrides)
    return _build_term_rows(
        merged,
        alias_policy=str(config["terminology"]["alias_primary_collision"]),
        spec=spec,
    )

def _apply_term_overrides(
    merged: dict[str, dict[str, Any]],
    overrides: dict[str, dict[str, Any]],
) -> None:
    for normalized, override in overrides.items():
        if override.get("disabled"):
            merged.pop(normalized, None)
            continue
        current = merged.setdefault(normalized, _term_bucket())
        if override.get("source"):
            current["sources"] = [override["source"]]
        for source_key, target_key in (
            ("category", "categories"),
            ("description", "descriptions"),
            ("preferred_translation", "translations"),
        ):
            if source_key in override:
                value = override.get(source_key)
                current[target_key] = [value] if value else []
        if "aliases" in override:
            current["aliases"] = list(override.get("aliases") or [])
        if "group_primary" in override:
            group_primary = override.get("group_primary")
            current["group_primary"] = (
                str(group_primary) if group_primary is not None else None
            )
            current["group_primary_set"] = True
            current["group_primary_locked"] = True

def normalize_term(value: str, spec: TermNormalization) -> str:
    if spec.form:
        value = unicodedata.normalize(spec.form, value)
    if spec.casefold:
        value = value.casefold()
    return value.strip()

def _merge_and_publish_terms(
    project: Path,
    *,
    task_id: str,
    project_id: str,
    published_run_id: str,
    active_status: str = "completed",
) -> dict[str, Any]:
    config = load_project_config(project)
    spec = term_normalization(config)
    previous = load_terms(project)
    candidates = [
        record
        for record in read_jsonl(
            project,
            project / "terminology" / "candidates.jsonl",
            task_id=task_id,
        )
    ]
    merged: dict[str, dict[str, Any]] = {}
    _seed_published_terms(merged, previous, spec)
    for record in candidates:
        for candidate in record.get("terms", []):
            _add_term_candidate(merged, candidate, spec)

    overrides_data = read_json(project, project / "terminology" / "overrides.json")
    overrides = {
        str(item["normalized"]): item for item in overrides_data.get("overrides", [])
    }
    _apply_term_overrides(merged, overrides)
    alias_policy = str(config["terminology"]["alias_primary_collision"])
    terms = _build_term_rows(merged, alias_policy=alias_policy, spec=spec)
    revision = int(previous["terms_revision"]) + 1 if previous else 1
    library = record_header(
        "terminology_library",
        project_id,
        record_id=f"TERMS-{revision}",
        terms_revision=revision,
        published_run_id=published_run_id,
        active_task_id=task_id,
        terms=terms,
    )
    write_json(project, project / "terminology" / "terms.json", library)
    active = read_json(project, project / "terminology" / "active_task.json")
    active["status"] = active_status
    active["terms_revision"] = revision
    write_json(project, project / "terminology" / "active_task.json", active)
    return library

def publish_partial_terms(project: Path) -> dict[str, Any]:
    """Publish current candidates without closing or deleting scan history."""
    if list_runs(project, stage="terminology", status="running"):
        raise UsageError("术语扫描仍在运行，结束 Run 后才能发布现有结果")
    active_path = project / "terminology" / "active_task.json"
    if not record_exists(project, active_path):
        raise UsageError("当前没有可发布的活动术语扫描")
    active = read_json(project, active_path)
    if active.get("status") != "active":
        raise UsageError("当前没有可发布的活动术语扫描")
    task_id = str(active.get("active_task_id", ""))
    candidates = [
        record
        for record in read_jsonl(
            project,
            project / "terminology" / "candidates.jsonl",
            task_id=task_id,
        )
        if record.get("terms")
    ]
    if not candidates:
        raise UsageError("当前活动扫描没有可发布的候选术语")
    config = load_project_config(project)
    spec = term_normalization(config)
    candidate_sources = {
        normalize_term(str(term.get("source")), spec)
        for record in candidates
        for term in record.get("terms", [])
        if isinstance(term, dict) and term.get("source")
    }
    metadata = read_json(project, project / "project.json")
    published_run_id = str(candidates[-1].get("run_id") or task_id)
    library = _merge_and_publish_terms(
        project,
        task_id=task_id,
        project_id=str(metadata["project_id"]),
        published_run_id=published_run_id,
        active_status="partial_published",
    )
    return {
        "published": True,
        "active_task_id": task_id,
        "terms_revision": library["terms_revision"],
        "published_terms": len(library.get("terms", [])),
        "scanned_terms": len(candidate_sources),
    }

def _add_term_candidate(
    merged: dict[str, dict[str, Any]],
    candidate: dict[str, Any],
    spec: TermNormalization,
) -> None:
    normalized = normalize_term(str(candidate["source"]), spec)
    current = merged.setdefault(normalized, _term_bucket())
    current["sources"].append(str(candidate["source"]))
    category = candidate.get("category")
    if category:
        current["categories"].append(str(category))
    current["categories"].extend(
        str(value)
        for value in candidate.get("conflicts", {}).get("categories", [])
        if value
    )
    description = candidate.get("description")
    if description:
        current["descriptions"].append(str(description))
    preferred = candidate.get("preferred_translation")
    if preferred:
        current["translations"].append(str(preferred))
    current["translations"].extend(
        str(value)
        for value in candidate.get("conflicts", {}).get(
            "preferred_translations", []
        )
        if value
    )
    current["aliases"].extend(
        str(alias) for alias in candidate.get("aliases", []) if alias
    )
    if candidate.get("_group_primary_set", "group_primary" in candidate):
        group_primary = candidate.get("group_primary")
        if group_primary is not None:
            group_primary = str(group_primary)
        if current["group_primary_set"] and current["group_primary"] != group_primary:
            raise UsageError(f"同一术语存在冲突的组主关系：{candidate['source']}")
        current["group_primary"] = group_primary
        current["group_primary_set"] = True
        current["group_primary_locked"] = current["group_primary_locked"] or bool(
            candidate.get("_group_primary_locked", False)
        )

def load_terms(project: Path) -> dict[str, Any] | None:
    path = project / "terminology" / "terms.json"
    return read_json(project, path) if record_exists(project, path) else None

def term_normalization(config: dict[str, Any]) -> TermNormalization:
    terminology = config["terminology"]
    return TermNormalization(
        form=terminology["unicode_normalization"] or None,
        casefold=terminology["case_insensitive"],
    )
