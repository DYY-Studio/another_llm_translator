from __future__ import annotations
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
from .term_library import TermNormalization

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
        target = merged.setdefault(normalized, _term_bucket())
        for key in (
            "sources",
            "categories",
            "descriptions",
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
        if not item["disabled"]:
            continue
        normalized = normalize_term(item["source"], spec)
        current = overrides.get(normalized, {"normalized": normalized})
        overrides[normalized] = {
            **current,
            "source": current.get("source", item["source"]),
            "disabled": True,
        }
    _apply_term_overrides(merged, overrides)
    terms = _build_term_rows(
        merged,
        alias_policy=str(config["terminology"]["alias_primary_collision"]),
        spec=spec,
    )
    overrides_list = [overrides[key] for key in sorted(overrides)]
    existing_terms = list((library or {}).get("terms", []))
    changed = terms != existing_terms or overrides_list != original_overrides
    next_revision = int(library["terms_revision"]) + 1 if library else 1
    summary = {
        "input": str(input_path),
        "format": input_path.suffix.casefold().removeprefix("."),
        "imported": len(imported),
        "changed": changed,
        "terms_revision": next_revision if changed else (
            int(library["terms_revision"]) if library else None
        ),
        "dry_run": dry_run,
        "warnings": [],
    }
    if dry_run or not changed:
        return summary
    override_record = record_header(
        "terminology_overrides",
        str(read_json(project, project / "project.json")["project_id"]),
        record_id="TERMINOLOGY-OVERRIDES",
        overrides=overrides_list,
        origin="terms_import",
    )
    library_record = record_header(
        "terminology_library",
        str(read_json(project, project / "project.json")["project_id"]),
        record_id=f"TERMS-{next_revision}",
        terms_revision=next_revision,
        published_run_id=library.get("published_run_id") if library else None,
        active_task_id=library.get("active_task_id") if library else None,
        terms=terms,
        origin="terms_import",
    )
    write_json(project, overrides_path, override_record)
    write_json(project, project / "terminology" / "terms.json", library_record)
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
        records = [
            record
            for record in read_jsonl(
                project,
                project / "terminology" / "candidates.jsonl",
                task_id=task_id,
            )
        ]
        merged: dict[str, dict[str, Any]] = {}
        for record in records:
            for candidate in record.get("terms", []):
                if isinstance(candidate, dict):
                    _add_term_candidate(merged, candidate, spec)
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
                    "description": override.get("description", term.get("description", "")),
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
                "description": override.get(
                    "description", term.get("description", "")
                ),
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
        "include_disabled": include_disabled,
        "source": source,
    }
