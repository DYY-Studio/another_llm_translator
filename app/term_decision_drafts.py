from __future__ import annotations
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from .config import load_project_config
from .errors import StorageError, UsageError
from .sqlite_storage import (
    list_runs,
    read_json,
    record_header,
    utc_now,
    write_json,
    write_terminology_decision_state,
)
from .term_library import (
    build_term_library_rows,
    load_terms,
    term_normalization,
)
from .term_decision_protocol import (
    DECISION_RULES_VERSION,
)

from .term_decision_rules import *
from .term_decision_rules import _STATE_FIELDS
import hashlib
import json

def _proposal_id(values: list[dict[str, Any]]) -> str:
    encoded = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "TDP-" + hashlib.sha256(encoded.encode()).hexdigest()[:16].upper()
STAGE = "terminology_decision"
DRAFT_FILE = "terminology_decision_draft.json"

__all__ = ['_build_draft', '_decision_needs_review', '_draft_path', '_latest_applied_manifest', '_pending_manifest', 'apply_decision_draft', 'current_decision_draft', 'decision_review_state', 'discard_decision_draft', 'manual_review_state', 'rollback_decision', 'save_decision_rejections', 'set_manual_review_resolved']

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
    conflicts: dict[str, dict[str, list[Any]]],
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
                "conflicts": {key: deepcopy(conflicts[key]) for key in keys},
            }
        )
    needs_review = [
        {
            "normalized": key,
            "source": original[key]["source"],
            "reason": decision["reason"],
            "evidence": deepcopy(evidence[key]),
            "conflicts": deepcopy(conflicts[key]),
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
        decision_rules_version=DECISION_RULES_VERSION,
        decision_fingerprint=fingerprint,
        model_fingerprint=model_fingerprint,
        prompt_fingerprint=prompt_fingerprint,
        proposals=proposals,
        needs_review=needs_review,
        rejected_proposal_ids=[],
        source_library=source_library,
        source_overrides=source_overrides,
    )

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
    if draft.get("decision_rules_version") != DECISION_RULES_VERSION:
        raise UsageError("术语决策草案规则版本不兼容；请丢弃并重新生成")
    manifest_rejected = set(map(str, draft.get("rejected_proposal_ids", [])))
    requested_rejected = set(map(str, rejected_proposal_ids or []))
    known = {str(item["proposal_id"]) for item in draft["proposals"]}
    unknown = sorted(requested_rejected - known)
    if unknown:
        raise UsageError(f"未知术语决策建议：{', '.join(unknown[:10])}")
    rejected = manifest_rejected | requested_rejected
    after_states, accepted = _proposal_after_states(draft, rejected)
    _validate_accepted_scalar_conflicts(draft, after_states)
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
        override["group_primary"] = state.get("group_primary")
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
    _validate_accepted_relationship_conflicts(
        original_terms=list(library.get("terms", [])),
        final_terms=terms,
        changed=set(after_states),
        spec=term_normalization(load_project_config(project)),
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
