from __future__ import annotations
from copy import deepcopy
import csv
import io
import json
from pathlib import Path
from typing import Any
from .config import load_project_config
from .errors import (
    UsageError,
)
from .sqlite_storage import (
    atomic_write_text,
    read_json,
    read_jsonl,
    record_exists,
    record_header,
    write_json,
)
from .term_library import (_add_term_candidate, _apply_term_overrides, _build_term_rows, _seed_published_terms, _term_bucket, load_terms, normalize_term, term_normalization)
from .term_library import TermNormalization, validate_group_alias_overlaps, apply_group_alias_removals

TERM_CSV_FIELDS = (
    "source",
    "preferred_translation",
    "category",
    "description",
    "aliases_json",
    "disabled",
    "category_conflicts_json",
    "preferred_translation_conflicts_json",
    "group_primary",
)

def _exchange_term(
    value: Any, location: str, spec: TermNormalization
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UsageError(f"术语必须是对象：{location}")
    allowed = {
        "source",
        "preferred_translation",
        "category",
        "description",
        "aliases",
        "disabled",
        "conflicts",
        "group_primary",
    }
    unknown = set(value) - allowed
    if unknown:
        raise UsageError(
            f"术语包含未知字段：{location}: {', '.join(sorted(unknown))}"
        )
    source = value.get("source")
    if not isinstance(source, str) or not source.strip():
        raise UsageError(f"术语 source 不能为空：{location}")
    for key in ("preferred_translation", "category", "description"):
        field = value.get(key)
        if field is not None and not isinstance(field, str):
            raise UsageError(f"术语 {key} 必须是字符串或 null：{location}")
    aliases = value.get("aliases", [])
    if not isinstance(aliases, list) or not all(
        isinstance(alias, str) for alias in aliases
    ):
        raise UsageError(f"术语 aliases 必须是字符串数组：{location}")
    disabled = value.get("disabled", False)
    if not isinstance(disabled, bool):
        raise UsageError(f"术语 disabled 必须是布尔值：{location}")
    group_primary = value.get("group_primary")
    if group_primary is not None and (
        not isinstance(group_primary, str) or not group_primary.strip()
    ):
        raise UsageError(f"术语 group_primary 必须是非空字符串或 null：{location}")
    conflicts = value.get("conflicts", {})
    if not isinstance(conflicts, dict):
        raise UsageError(f"术语 conflicts 必须是对象：{location}")
    unknown_conflicts = set(conflicts) - {
        "categories",
        "preferred_translations",
    }
    if unknown_conflicts:
        raise UsageError(
            f"术语 conflicts 包含未知字段：{location}: "
            f"{', '.join(sorted(unknown_conflicts))}"
        )
    for key in ("categories", "preferred_translations"):
        candidates = conflicts.get(key, [])
        if not isinstance(candidates, list) or not all(
            isinstance(candidate, str) for candidate in candidates
        ):
            raise UsageError(f"术语冲突 {key} 必须是字符串数组：{location}")
    return {
        "source": source.strip(),
        "preferred_translation": (
            value["preferred_translation"].strip()
            if value.get("preferred_translation")
            else None
        ),
        "category": value["category"].strip() if value.get("category") else None,
        "description": (
            value["description"].strip() if value.get("description") else ""
        ),
        "_description_set": "description" in value,
        "aliases": [
            alias.strip()
            for alias in aliases
            if alias.strip()
            and normalize_term(alias, spec) != normalize_term(source, spec)
        ],
        "disabled": disabled,
        "group_primary": (
            normalize_term(group_primary, spec) if group_primary is not None else None
        ),
        "_group_primary_set": "group_primary" in value,
        "_group_primary_locked": "group_primary" in value,
        "conflicts": {
            "categories": [
                candidate.strip()
                for candidate in conflicts.get("categories", [])
                if candidate.strip()
            ],
            "preferred_translations": [
                candidate.strip()
                for candidate in conflicts.get("preferred_translations", [])
                if candidate.strip()
            ],
        },
    }

LEGACY_TERM_CSV_FIELDS = TERM_CSV_FIELDS[:-1]

def import_terms(
    project: Path,
    input_path: Path,
    *,
    dry_run: bool,
    overwrite: bool = False,
) -> dict[str, Any]:
    config = load_project_config(project)
    spec = term_normalization(config)
    imported = _load_term_exchange(input_path, spec)
    disabled_by_normalized: dict[str, bool] = {}
    merged_import: dict[str, dict[str, Any]] = {}
    for item in imported:
        normalized = normalize_term(item["source"], spec)
        previous_disabled = disabled_by_normalized.setdefault(
            normalized, item["disabled"]
        )
        if previous_disabled != item["disabled"]:
            raise UsageError(f"同一 normalized 术语的 disabled 冲突：{item['source']}")
        if not item["disabled"]:
            _add_term_candidate(merged_import, item, spec)

    library = load_terms(project)
    available = {
        str(item.get("normalized"))
        for item in (library or {}).get("terms", [])
        if item.get("normalized")
    } | set(merged_import)
    for normalized, item in merged_import.items():
        primary = item["group_primary"]
        if primary is not None and primary not in available:
            raise UsageError(
                f"导入术语组主不存在：{normalized} -> {primary}"
            )
    merged: dict[str, dict[str, Any]] = {}
    _seed_published_terms(merged, library, spec)
    for normalized, item in merged_import.items():
        if overwrite:
            existing = merged.get(normalized)
            replaced = deepcopy(item)
            if (
                existing is not None
                and not replaced["group_primary_set"]
                and existing["group_primary_set"]
            ):
                replaced["group_primary"] = existing["group_primary"]
                replaced["group_primary_set"] = True
                replaced["group_primary_locked"] = existing["group_primary_locked"]
            merged[normalized] = replaced
            continue
        target = merged.setdefault(normalized, _term_bucket())
        for key in (
            "sources",
            "categories",
            "translations",
            "aliases",
        ):
            target[key].extend(item[key])
        if item["group_primary_set"]:
            if (
                target["group_primary_set"]
                and target["group_primary"] != item["group_primary"]
            ):
                raise UsageError(f"导入术语组关系与现有关系冲突：{normalized}")
            target["group_primary"] = item["group_primary"]
            target["group_primary_set"] = True
            target["group_primary_locked"] = item["group_primary_locked"]

    overrides_path = project / "terminology" / "overrides.json"
    overrides_document = read_json(project, overrides_path)
    original_overrides = [
        dict(item) for item in overrides_document.get("overrides", [])
    ]
    overrides = {
        str(item["normalized"]): dict(item) for item in original_overrides
    }
    for item in imported:
        normalized = normalize_term(item["source"], spec)
        if not item["disabled"]:
            if overwrite and normalized in overrides:
                current_override = dict(overrides[normalized])
                current_override["source"] = item["source"]
                current_override["disabled"] = False
                current_override["preferred_translation"] = item["preferred_translation"]
                current_override["category"] = item["category"]
                current_override["aliases"] = list(item["aliases"])
                if item["_group_primary_set"]:
                    current_override["group_primary"] = item["group_primary"]
                overrides[normalized] = current_override
            continue
        current = overrides.get(normalized, {"normalized": normalized})
        overrides[normalized] = {
            **current,
            "source": current.get("source", item["source"]),
            "disabled": True,
        }
    _apply_term_overrides(merged, overrides)
    removals: dict[str, set[str]] = {}
    terms = _build_term_rows(
        merged,
        alias_policy=str(config["terminology"]["alias_primary_collision"]),
        spec=spec, alias_removals=removals,
    )
    affected = {normalize_term(item["source"], spec) for item in imported}
    affected.update(str(item["group_primary"]) for item in (library or {}).get("terms", [])
                    if item["normalized"] in affected and item.get("group_primary"))
    validate_group_alias_overlaps(terms, spec, affected=affected)
    apply_group_alias_removals(overrides, removals)
    overrides_list = [overrides[key] for key in sorted(overrides)]
    from .term_knowledge import add_unsourced, empty_knowledge, entity_members, evidence, project_descriptions, save_items, transfer_alias_evidence
    from .sqlite_storage import write_terminology_publication
    next_revision = int(library["terms_revision"]) + 1 if library else 1
    project_id = str(read_json(project, project / "project.json")["project_id"])
    library_record = record_header(
        "terminology_library", project_id, record_id=f"TERMS-{next_revision}", terms_revision=next_revision,
        published_run_id=library.get("published_run_id") if library else None,
        active_task_id=library.get("active_task_id") if library else None,
        terms=terms, origin="terms_import", knowledge=deepcopy(library["knowledge"]) if library else empty_knowledge(),
    )
    previous_entities = entity_members(library) if library else {}
    old_root_of = {
        member: old_root
        for old_root, old_members in previous_entities.items()
        for member in old_members
    }
    moved_owners = transfer_alias_evidence(library_record, spec)
    knowledge = library_record["knowledge"]
    active = {term["normalized"] for term in terms}
    pending = knowledge["pending_changes"]
    for key in pending.keys() - active:
        del pending[key]
    old_entities = deepcopy(knowledge["entities"])
    known = evidence(project, library_record)
    current = {t["normalized"]: t for t in (library or {}).get("terms", [])}
    overwritten_descriptions: dict[str, list[str]] = {}
    if overwrite:
        for item in imported:
            if item["disabled"] or not item["_description_set"]:
                continue
            owner = normalize_term(item["source"], spec)
            texts = overwritten_descriptions.setdefault(owner, [])
            text = item["description"]
            if text and text not in texts:
                texts.append(text)
        for owner, texts in overwritten_descriptions.items():
            keep_texts = set(texts)
            for key, frag in list(known.items()):
                if (
                    frag["owner"] == owner
                    and frag["state"] == "active"
                    and frag["source_range"] is None
                    and frag["text"] not in keep_texts
                ):
                    knowledge["fragments"][key]["state"] = "disabled"
                    frag["state"] = "disabled"
            for text in texts:
                has_valid_match = any(
                    frag["owner"] == owner
                    and frag["state"] == "active"
                    and frag["valid"]
                    and frag["text"] == text
                    for frag in known.values()
                )
                if not has_valid_match:
                    key = add_unsourced(knowledge, owner, text, "import")
                    known[key] = {
                        **knowledge["fragments"][key],
                        "id": key,
                        "source_range": None,
                        "valid": True,
                        "_positions": {},
                    }
    else:
        no_source = {
            (f["owner"], f["text"])
            for f in known.values()
            if f["state"] == "active" and f["source_range"] is None
        }
        for item in imported:
            owner = normalize_term(item["source"], spec)
            text = item["description"]
            if text and not item["disabled"]:
                if (owner, text) not in no_source and text != current.get(
                    owner, {}
                ).get("description"):
                    key = add_unsourced(knowledge, owner, text, "import")
                    no_source.add((owner, text))
                    known[key] = {
                        **knowledge["fragments"][key],
                        "id": key,
                        "source_range": None,
                        "valid": True,
                        "_positions": {},
                    }
    for root, members in entity_members(library_record).items():
        overwritten_members = {m for m in members if m in overwritten_descriptions}
        if overwritten_members:
            raw_items: list[dict[str, Any]] = []
            untouched_members = [m for m in members if m not in overwritten_members]
            if untouched_members:
                by_old_root: dict[str | None, list[str]] = {}
                for m in untouched_members:
                    old_r = old_root_of.get(m)
                    old_r = (
                        moved_owners.get(old_r, old_r)
                        if old_r is not None
                        else None
                    )
                    by_old_root.setdefault(old_r, []).append(m)
                for old_r, group_ums in by_old_root.items():
                    old_saved = old_entities.get(old_r) if old_r is not None else None
                    um_set = set(group_ums)
                    if old_saved is not None:
                        for item in old_saved.get("items", []):
                            raw_fids = item.get("fragment_ids", [])
                            if not raw_fids:
                                if old_r in um_set and str(item.get("text", "")).strip():
                                    raw_items.append(
                                        {
                                            "text": str(item["text"]).strip(),
                                            "fragment_ids": [],
                                        }
                                    )
                            else:
                                valid_fids = [
                                    fid
                                    for fid in raw_fids
                                    if fid in known
                                    and known[fid]["owner"] in um_set
                                    and known[fid]["state"] == "active"
                                    and known[fid]["valid"]
                                ]
                                if valid_fids and str(item.get("text", "")).strip():
                                    raw_items.append(
                                        {
                                            "text": str(item["text"]).strip(),
                                            "fragment_ids": valid_fids,
                                        }
                                    )
                    else:
                        for key, f in known.items():
                            if (
                                f["owner"] in um_set
                                and f["state"] == "active"
                                and f["valid"]
                                and str(f.get("text", "")).strip()
                            ):
                                raw_items.append(
                                    {
                                        "text": str(f["text"]).strip(),
                                        "fragment_ids": [key],
                                    }
                                )
            for m in members:
                if m not in overwritten_descriptions:
                    continue
                for target_text in overwritten_descriptions[m]:
                    fids = [
                        key
                        for key, f in known.items()
                        if f["owner"] == m
                        and f["state"] == "active"
                        and f["valid"]
                        and f["text"] == target_text
                    ]
                    raw_items.append({"text": target_text, "fragment_ids": fids})
            items_to_save: list[dict[str, Any]] = []
            by_text: dict[str, dict[str, Any]] = {}
            for item in raw_items:
                txt = item["text"]
                if txt in by_text:
                    existing_fids = by_text[txt]["fragment_ids"]
                    for fid in item["fragment_ids"]:
                        if fid not in existing_fids:
                            existing_fids.append(fid)
                else:
                    entry = {"text": txt, "fragment_ids": list(item["fragment_ids"])}
                    by_text[txt] = entry
                    items_to_save.append(entry)
            save_items(
                project,
                library_record,
                root,
                items_to_save,
                all_evidence=known,
            )
        elif root not in previous_entities and root not in old_entities:
            save_items(
                project,
                library_record,
                root,
                [
                    {"text": str(f["text"]).strip(), "fragment_ids": [key]}
                    for key, f in known.items()
                    if f["owner"] in members
                    and f["state"] == "active"
                    and f["valid"]
                    and str(f.get("text", "")).strip()
                ],
                all_evidence=known,
            )
    project_descriptions(project, library_record, all_evidence=known)
    changed = (terms != list((library or {}).get("terms", [])) or overrides_list != original_overrides
               or knowledge != (library or {}).get("knowledge"))
    summary = {
        "input": str(input_path), "format": input_path.suffix.casefold().removeprefix("."),
        "imported": len(imported), "changed": changed,
        "terms_revision": next_revision if changed else int(library["terms_revision"]),
        "dry_run": dry_run, "warnings": [],
    }
    if dry_run or not changed:
        return summary
    override_record = record_header("terminology_overrides", project_id, record_id="TERMINOLOGY-OVERRIDES", overrides=overrides_list, origin="terms_import")
    write_terminology_publication(project, terms=library_record, overrides=override_record)
    return summary

def _term_exchange_rows(
    project: Path,
    *,
    include_disabled: bool,
    source: str = "published",
) -> list[dict[str, Any]]:
    if source not in {"published", "scanned"}:
        raise UsageError("术语导出 source 必须是 published 或 scanned")
    if source == "scanned":
        config = load_project_config(project)
        spec = term_normalization(config)
        active_path = project / "terminology" / "active_task.json"
        active = read_json(project, active_path) if record_exists(project, active_path) else None
        if not active or active.get("status") != "active":
            return []
        task_id = str(active.get("active_task_id", ""))
        from .term_library import _ordered_term_candidates
        records = _ordered_term_candidates(project, task_id)
        merged: dict[str, dict[str, Any]] = {}
        descriptions: dict[str, list[str]] = {}
        for record in records:
            for candidate in record.get("terms", []):
                if isinstance(candidate, dict):
                    _add_term_candidate(merged, candidate, spec)
                    text = candidate.get("description", "")
                    values = descriptions.setdefault(normalize_term(candidate["source"], spec), [])
                    if text and text not in values:
                        values.append(text)
        overrides_document = read_json(project, project / "terminology" / "overrides.json")
        overrides = {
            str(item["normalized"]): dict(item)
            for item in overrides_document.get("overrides", [])
        }
        _apply_term_overrides(merged, overrides)
        alias_policy = str(config["terminology"]["alias_primary_collision"])
        candidates = _build_term_rows(merged, alias_policy=alias_policy, spec=spec)
        source_by_normalized = {
            str(term["normalized"]): str(term["source"]) for term in candidates
        }
        rows: list[dict[str, Any]] = []
        for term in candidates:
            normalized = normalize_term(str(term["source"]), spec)
            override = overrides.get(normalized, {})
            disabled = bool(override.get("disabled", False))
            if disabled and not include_disabled:
                continue
            rows.append(
                {
                    "source": override.get("source", term["source"]),
                    "preferred_translation": override.get(
                        "preferred_translation", term.get("preferred_translation")
                    ),
                    "category": override.get("category", term.get("category")),
                    "description": "\n".join(descriptions.get(normalized, [])),
                    "aliases": list(override.get("aliases", term.get("aliases", []))),
                    "disabled": disabled,
                    "group_primary": (
                        source_by_normalized.get(str(term.get("group_primary")))
                        if term.get("group_primary") is not None
                        else None
                    ),
                    "conflicts": {
                        "categories": list(term.get("conflicts", {}).get("categories", [])),
                        "preferred_translations": list(
                            term.get("conflicts", {}).get("preferred_translations", [])
                        ),
                    },
                }
            )
        return rows
    library = load_terms(project)
    current = {
        str(item["normalized"]): dict(item)
        for item in (library or {}).get("terms", [])
    }
    overrides_document = read_json(project, project / "terminology" / "overrides.json")
    overrides = {
        str(item["normalized"]): dict(item)
        for item in overrides_document.get("overrides", [])
    }
    rows: list[dict[str, Any]] = []
    source_by_normalized = {
        normalized: str(
            overrides.get(normalized, {}).get("source", term.get("source", normalized))
        )
        for normalized, term in current.items()
    }
    for normalized in sorted(set(current) | set(overrides)):
        term = current.get(normalized, {})
        override = overrides.get(normalized, {})
        disabled = bool(override.get("disabled", False))
        if disabled and not include_disabled:
            continue
        conflicts = term.get("conflicts", {})
        rows.append(
            {
                "source": override.get("source", term.get("source", normalized)),
                "preferred_translation": override.get(
                    "preferred_translation", term.get("preferred_translation")
                ),
                "category": override.get("category", term.get("category")),
                "description": (term["description"] if normalized in current else
                                "；".join(item["text"] for item in (library or {}).get("knowledge", {}).get("entities", {}).get(normalized, {}).get("items", []))),
                "aliases": list(override.get("aliases", term.get("aliases", []))),
                "disabled": disabled,
                "group_primary": (
                    source_by_normalized.get(str(term.get("group_primary")))
                    if term.get("group_primary") is not None
                    else None
                ),
                "conflicts": {
                    "categories": list(conflicts.get("categories", [])),
                    "preferred_translations": list(
                        conflicts.get("preferred_translations", [])
                    ),
                },
            }
        )
    return rows

def _load_term_exchange(
    path: Path, spec: TermNormalization
) -> list[dict[str, Any]]:
    suffix = path.suffix.casefold()
    try:
        content = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise UsageError(f"无法读取术语文件：{path}: {exc}") from exc
    if suffix == ".json":
        try:
            document = json.loads(content)
        except json.JSONDecodeError as exc:
            raise UsageError(f"术语 JSON 无效：{path}: {exc}") from exc
        if not isinstance(document, dict):
            raise UsageError("术语 JSON 顶层必须是对象")
        if set(document) != {"schema_version", "record_type", "terms"}:
            raise UsageError("术语 JSON 必须只包含 schema_version、record_type、terms")
        if (
            document.get("schema_version") not in {1, 2}
            or document.get("record_type") != "terminology_exchange"
        ):
            raise UsageError("不支持的术语交换格式版本")
        values = document.get("terms")
        if not isinstance(values, list):
            raise UsageError("术语 JSON 的 terms 必须是数组")
        rows = [
            _exchange_term(value, f"terms[{index}]", spec)
            for index, value in enumerate(values)
        ]
        if document.get("schema_version") == 1:
            for row in rows:
                row["group_primary"] = None
                row["_group_primary_set"] = False
                row["_group_primary_locked"] = False
        return rows
    if suffix != ".csv":
        raise UsageError("术语文件扩展名必须是 .json 或 .csv")
    try:
        reader = csv.DictReader(io.StringIO(content))
        fields = tuple(reader.fieldnames or ())
        if fields not in {TERM_CSV_FIELDS, LEGACY_TERM_CSV_FIELDS}:
            raise UsageError(
                "术语 CSV 表头必须是旧版或新版完整表头"
            )
        values = []
        for index, row in enumerate(reader, start=2):
            try:
                aliases = json.loads(row["aliases_json"] or "[]")
                category_conflicts = json.loads(
                    row["category_conflicts_json"] or "[]"
                )
                preferred_conflicts = json.loads(
                    row["preferred_translation_conflicts_json"] or "[]"
                )
            except json.JSONDecodeError as exc:
                raise UsageError(f"术语 CSV 数组字段无效：第 {index} 行") from exc
            disabled_text = (row["disabled"] or "").strip().casefold()
            if disabled_text not in {"true", "false"}:
                raise UsageError(f"术语 CSV disabled 必须是 true 或 false：第 {index} 行")
            values.append(
                _exchange_term(
                    {
                        "source": row["source"],
                        "preferred_translation": row["preferred_translation"] or None,
                        "category": row["category"] or None,
                        "description": row["description"] or "",
                        "aliases": aliases,
                        "disabled": disabled_text == "true",
                        "group_primary": (
                            row.get("group_primary") or None
                            if fields == TERM_CSV_FIELDS
                            else None
                        ),
                        "conflicts": {
                            "categories": category_conflicts,
                            "preferred_translations": preferred_conflicts,
                        },
                    },
                    f"第 {index} 行",
                    spec,
                )
            )
            if fields == LEGACY_TERM_CSV_FIELDS:
                values[-1]["_group_primary_set"] = False
                values[-1]["_group_primary_locked"] = False
        return values
    except csv.Error as exc:
        raise UsageError(f"术语 CSV 无效：{path}: {exc}") from exc

def export_terms(
    project: Path,
    output: Path,
    *,
    include_disabled: bool,
    source: str = "published",
) -> dict[str, Any]:
    rows = _term_exchange_rows(
        project,
        include_disabled=include_disabled,
        source=source,
    )
    if output.suffix.casefold() == ".json":
        atomic_write_text(
            output,
            json.dumps(
                {
                    "schema_version": 2,
                    "record_type": "terminology_exchange",
                    "terms": rows,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
    elif output.suffix.casefold() == ".csv":
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=TERM_CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "source": row["source"],
                    "preferred_translation": row["preferred_translation"] or "",
                    "category": row["category"] or "",
                    "description": row["description"] or "",
                    "aliases_json": json.dumps(
                        row["aliases"], ensure_ascii=False, separators=(",", ":")
                    ),
                    "disabled": "true" if row["disabled"] else "false",
                    "category_conflicts_json": json.dumps(
                        row["conflicts"]["categories"],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "preferred_translation_conflicts_json": json.dumps(
                        row["conflicts"]["preferred_translations"],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "group_primary": row.get("group_primary") or "",
                }
            )
        atomic_write_text(output, "\ufeff" + buffer.getvalue())
    else:
        raise UsageError("术语输出扩展名必须是 .json 或 .csv")
    return {
        "output": str(output),
        "format": output.suffix.casefold().removeprefix("."),
        "exported": len(rows),
        "warnings": ["术语文本交换不包含说明引用，完整结构请使用项目备份"],
        "include_disabled": include_disabled,
        "source": source,
    }
