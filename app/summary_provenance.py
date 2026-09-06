from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .execution import segment_model_source

STALE_STATUS = "stale_status"
SOURCE_CHANGED = "source_changed"
DEPENDENCY_CHANGED = "dependency_changed"
PROVENANCE_UNAVAILABLE = "provenance_unavailable"

_DEPENDENCY_KINDS = frozenset({"fragment", "reduction"})


@dataclass(frozen=True, slots=True)
class SummaryExpiryAssessment:
    expired: bool
    expiry_reason: str | None = None


def digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _dependency_for_artifact(artifact: dict[str, Any]) -> dict[str, str]:
    record_id = artifact.get("record_id")
    kind = artifact.get("kind")
    text = artifact.get("text")
    source_digest = artifact.get("source_digest")
    if (
        not isinstance(record_id, str)
        or not record_id
        or kind not in _DEPENDENCY_KINDS
        or not isinstance(text, str)
        or not isinstance(source_digest, str)
        or not source_digest
    ):
        raise ValueError("摘要依赖缺少可持久化的 record_id/kind/text/source_digest")
    return {
        "record_id": record_id,
        "kind": str(kind),
        "text_digest": digest(text),
        "source_digest": source_digest,
    }


def build_provenance(
    origin: str, children: Iterable[dict[str, Any]]
) -> tuple[dict[str, Any], str]:
    if not isinstance(origin, str) or not origin:
        raise ValueError("摘要 provenance origin 必须是非空字符串")
    values = list(children)
    if not values:
        raise ValueError("摘要 provenance 依赖不能为空")
    dependencies = [_dependency_for_artifact(item) for item in values]
    if len({item["record_id"] for item in dependencies}) != len(dependencies):
        raise ValueError("摘要 provenance 依赖不能重复")
    provenance = {
        "origin": origin,
        "artifact_ids": [item["record_id"] for item in dependencies],
        "source_ranges": [item["source_range"] for item in values],
        "dependencies": dependencies,
    }
    return provenance, digest(dependencies)


def _expired(reason: str) -> SummaryExpiryAssessment:
    return SummaryExpiryAssessment(expired=True, expiry_reason=reason)


def _current_segments_match(
    artifact: dict[str, Any], current_segments: list[dict[str, Any]]
) -> bool:
    current = [item for item in current_segments if not item.get("is_empty")]
    if not current:
        return False
    boundary = (artifact.get("file_id"), artifact.get("part_id"))
    if not all(isinstance(value, str) and value for value in boundary):
        return False
    if any(
        (item.get("file_id"), item.get("part_id")) != boundary for item in current
    ):
        return False
    current = sorted(
        current,
        key=lambda item: (int(item.get("line_index", 0)), str(item.get("segment_id", ""))),
    )
    current_by_id = {
        str(item.get("segment_id")): item
        for item in current
        if isinstance(item.get("segment_id"), str) and item.get("segment_id")
    }
    if len(current_by_id) != len(current):
        return False

    source_range = artifact.get("source_range")
    if not isinstance(source_range, dict):
        return False
    for key in ("file_id", "part_id"):
        if key in source_range and source_range.get(key) != boundary[0 if key == "file_id" else 1]:
            return False
    values = source_range.get("segments")
    if not isinstance(values, list) or not values or any(
        not isinstance(value, dict) for value in values
    ):
        return False
    stable_ids = [
        str(value.get("original_segment_id") or value.get("segment_id") or "")
        for value in values
    ]
    if not all(stable_ids):
        return False
    ordered_unique_ids = list(dict.fromkeys(stable_ids))
    if ordered_unique_ids != list(current_by_id):
        return False
    for value, stable_id in zip(values, stable_ids, strict=True):
        current_value = current_by_id.get(stable_id)
        if current_value is None:
            return False
        if value.get("original_source_digest", value.get("source_digest")) != digest(
            str(current_value.get("source", ""))
        ):
            return False
        if value.get(
            "original_model_text_digest", value.get("model_text_digest")
        ) != digest(segment_model_source(current_value)):
            return False
    return True


def _boundary(artifact: dict[str, Any]) -> tuple[str, str] | None:
    file_id = artifact.get("file_id")
    part_id = artifact.get("part_id")
    if not isinstance(file_id, str) or not file_id:
        return None
    if not isinstance(part_id, str) or not part_id:
        return None
    return file_id, part_id


def _artifact_map(
    artifact: dict[str, Any], summary_artifacts: Iterable[dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    values = [artifact, *summary_artifacts]
    by_id: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    for value in values:
        if not isinstance(value, dict):
            continue
        record_id = value.get("record_id")
        if not isinstance(record_id, str) or not record_id:
            continue
        if record_id in by_id and by_id[record_id] is not value:
            duplicates.add(record_id)
        by_id[record_id] = value
    return by_id, duplicates


def _legacy_adopted_fragment(
    artifact: dict[str, Any],
    provenance: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    duplicates: set[str],
    active_fragment_ids: set[str],
) -> SummaryExpiryAssessment | None:
    raw_ids = provenance.get("artifact_ids")
    source_ranges = provenance.get("source_ranges")
    if (
        not isinstance(raw_ids, list)
        or len(raw_ids) != 1
        or not isinstance(raw_ids[0], str)
        or not raw_ids[0]
        or not isinstance(source_ranges, list)
        or len(source_ranges) != 1
        or not isinstance(source_ranges[0], dict)
    ):
        return _expired(PROVENANCE_UNAVAILABLE)
    child_id = raw_ids[0]
    child = by_id.get(child_id)
    if child is None or child_id in duplicates:
        return _expired(PROVENANCE_UNAVAILABLE)
    if child.get("kind") != "fragment" or _boundary(child) != _boundary(artifact):
        return _expired(PROVENANCE_UNAVAILABLE)
    if child.get("status") != "completed" or bool(child.get("source_changed")):
        return _expired(DEPENDENCY_CHANGED)
    if child.get("text") is None or not isinstance(child.get("source_range"), dict):
        return _expired(PROVENANCE_UNAVAILABLE)
    if (
        artifact.get("text") != child.get("text")
        or artifact.get("source_range") != child.get("source_range")
        or source_ranges[0] != child.get("source_range")
        or active_fragment_ids != {child_id}
    ):
        return _expired(DEPENDENCY_CHANGED)
    return None


def _modern_provenance(
    artifact: dict[str, Any],
    provenance: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    duplicates: set[str],
) -> tuple[set[str] | None, str | None]:
    root_boundary = _boundary(artifact)
    if root_boundary is None:
        return None, PROVENANCE_UNAVAILABLE
    visiting: set[str] = set()

    def visit(node: dict[str, Any]) -> tuple[set[str] | None, str | None]:
        record_id = node.get("record_id")
        if not isinstance(record_id, str) or not record_id:
            return None, PROVENANCE_UNAVAILABLE
        if record_id in visiting:
            return None, PROVENANCE_UNAVAILABLE
        node_provenance = node.get("provenance")
        if not isinstance(node_provenance, dict):
            return None, PROVENANCE_UNAVAILABLE
        if not isinstance(node_provenance.get("origin"), str) or not node_provenance[
            "origin"
        ]:
            return None, PROVENANCE_UNAVAILABLE
        raw_ids = node_provenance.get("artifact_ids")
        source_ranges = node_provenance.get("source_ranges")
        dependencies = node_provenance.get("dependencies")
        if (
            not isinstance(raw_ids, list)
            or not raw_ids
            or any(not isinstance(value, str) or not value for value in raw_ids)
            or len(set(raw_ids)) != len(raw_ids)
            or not isinstance(source_ranges, list)
            or len(source_ranges) != len(raw_ids)
            or any(not isinstance(value, dict) for value in source_ranges)
            or not isinstance(dependencies, list)
            or len(dependencies) != len(raw_ids)
            or any(not isinstance(value, dict) for value in dependencies)
        ):
            return None, PROVENANCE_UNAVAILABLE
        if not isinstance(node.get("input_digest"), str) or node.get(
            "input_digest"
        ) != digest(dependencies):
            return None, PROVENANCE_UNAVAILABLE

        children: list[dict[str, Any]] = []
        for child_id, source_range, dependency in zip(
            raw_ids, source_ranges, dependencies, strict=True
        ):
            if child_id in duplicates:
                return None, PROVENANCE_UNAVAILABLE
            child = by_id.get(child_id)
            if child is None:
                return None, PROVENANCE_UNAVAILABLE
            if (
                dependency.get("record_id") != child_id
                or dependency.get("kind") not in _DEPENDENCY_KINDS
                or not isinstance(dependency.get("text_digest"), str)
                or not dependency["text_digest"]
                or not isinstance(dependency.get("source_digest"), str)
                or not dependency["source_digest"]
            ):
                return None, PROVENANCE_UNAVAILABLE
            if child.get("kind") != dependency["kind"]:
                return None, PROVENANCE_UNAVAILABLE
            if _boundary(child) != root_boundary:
                return None, PROVENANCE_UNAVAILABLE
            if source_range != child.get("source_range"):
                return None, DEPENDENCY_CHANGED
            if child.get("text") is None or not isinstance(
                child.get("source_digest"), str
            ):
                return None, PROVENANCE_UNAVAILABLE
            if dependency["text_digest"] != digest(str(child["text"])):
                return None, DEPENDENCY_CHANGED
            if dependency["source_digest"] != child["source_digest"]:
                return None, DEPENDENCY_CHANGED
            if child.get("status") != "completed" or bool(
                child.get("source_changed")
            ):
                return None, DEPENDENCY_CHANGED
            children.append(child)

        visiting.add(record_id)
        leaves: set[str] = set()
        for child in children:
            if child["kind"] == "fragment":
                leaves.add(str(child["record_id"]))
                continue
            child_leaves, reason = visit(child)
            if reason is not None or child_leaves is None:
                visiting.remove(record_id)
                return None, reason or PROVENANCE_UNAVAILABLE
            leaves.update(child_leaves)
        visiting.remove(record_id)
        return leaves, None

    return visit(artifact)


def assess_full_summary(
    artifact: dict[str, Any],
    current_segments: list[dict[str, Any]],
    summary_artifacts: list[dict[str, Any]],
) -> SummaryExpiryAssessment:
    if artifact.get("kind") != "full":
        return SummaryExpiryAssessment(expired=False)
    if artifact.get("status") == "stale":
        return _expired(STALE_STATUS)
    if bool(artifact.get("source_changed")):
        return _expired(SOURCE_CHANGED)
    if not _current_segments_match(artifact, current_segments):
        return _expired(SOURCE_CHANGED)

    provenance = artifact.get("provenance")
    if not isinstance(provenance, dict):
        return _expired(PROVENANCE_UNAVAILABLE)
    by_id, duplicates = _artifact_map(artifact, summary_artifacts)
    boundary = _boundary(artifact)
    active_fragment_ids = {
        str(item["record_id"])
        for item in summary_artifacts
        if isinstance(item, dict)
        and item.get("kind") == "fragment"
        and _boundary(item) == boundary
        and item.get("status") == "completed"
        and not bool(item.get("source_changed"))
        and isinstance(item.get("record_id"), str)
    }
    if provenance.get("origin") == "adopted_fragment" and "dependencies" not in provenance:
        legacy = _legacy_adopted_fragment(
            artifact, provenance, by_id, duplicates, active_fragment_ids
        )
        return legacy or SummaryExpiryAssessment(expired=False)

    leaf_ids, reason = _modern_provenance(artifact, provenance, by_id, duplicates)
    if reason is not None or leaf_ids is None:
        return _expired(reason or PROVENANCE_UNAVAILABLE)
    if leaf_ids != active_fragment_ids:
        return _expired(DEPENDENCY_CHANGED)
    return SummaryExpiryAssessment(expired=False)
