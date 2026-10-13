"""Source-backed entity explanations; scan candidates remain the evidence owner."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from .errors import UsageError
from .execution import segment_model_source
from .sqlite_storage import new_record_id, read_segments, read_terminology_candidates
from .summary_provenance import digest


def empty_knowledge() -> dict[str, Any]:
    return {"version": 1, "fragments": {}, "entities": {}, "published_candidates": []}


def require_knowledge(library: dict[str, Any]) -> dict[str, Any]:
    knowledge = library.get("knowledge")
    if not isinstance(knowledge, dict) or knowledge.get("version") != 1:
        raise UsageError("旧术语库需要迁移：python scripts/migrate_term_knowledge.py PROJECT")
    return knowledge


def source_range(
    items: list[dict[str, Any]], originals: dict[str, dict[str, Any]],
    part_original: dict[str, str],
) -> list[dict[str, Any]]:
    values = []
    for item in items:
        stable = part_original.get(str(item["segment_id"]), str(item["segment_id"]))
        original = originals[stable]
        start = int(item.get("_source_offset", 0))
        values.append({
            "segment_id": stable, "file_id": original["file_id"],
            "part_id": original["part_id"], "start": start,
            "end": start + len(str(item["source"])),
            "source_digest": digest(str(original["source"])),
            "model_text_digest": digest(segment_model_source(original)),
            "slice_source_digest": digest(str(item["source"])),
            "slice_model_text_digest": digest(segment_model_source(item)),
        })
    return values


def _overlap(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> bool:
    return any(a["segment_id"] == b["segment_id"]
               and a["start"] < b["end"] and b["start"] < a["end"]
               for a in left for b in right)


def evidence(project: Path, library: dict[str, Any]) -> dict[str, dict[str, Any]]:
    fragments = require_knowledge(library)["fragments"]
    ids = {f["candidate_id"] for f in fragments.values() if f.get("candidate_id")}
    records = {r["record_id"]: r for r in read_terminology_candidates(project, ids)}
    segments = {s["segment_id"]: s for s in read_segments(project)}
    positions = {key: i for i, key in enumerate(segments)}
    result = {}
    for key, fragment in fragments.items():
        value = {**fragment, "id": key, "source_range": None, "valid": True}
        if fragment.get("candidate_id"):
            record = records.get(fragment["candidate_id"])
            if record is None:
                raise UsageError("实体说明引用的候选记录已缺失")
            value["text"] = record["terms"][fragment["term_index"]]["description"]
            value["source_range"] = record["source_range"]
            value["_positions"] = {r["segment_id"]: positions.get(r["segment_id"], -1) for r in record["source_range"]}
            value["valid"] = all(
                r["segment_id"] in segments
                and digest(str(segments[r["segment_id"]]["source"])) == r["source_digest"]
                and digest(segment_model_source(segments[r["segment_id"]])) == r["model_text_digest"]
                for r in value["source_range"]
            )
        result[key] = value
    return result


def publish_evidence(
    project: Path, library: dict[str, Any], candidates: list[dict[str, Any]],
    spec: Any, *, replace: bool = True,
) -> None:
    from .term_library import normalize_term
    knowledge = require_knowledge(library)
    seen = set(knowledge["published_candidates"])
    old = evidence(project, library)
    for record in candidates:
        candidate_id = record["record_id"]
        if candidate_id in seen:
            continue
        if not record.get("source_range"):
            raise UsageError("旧扫描缺少完整来源范围，请结束旧扫描并重新扫描")
        if replace:
            for key, fragment in old.items():
                if fragment["source_range"] and _overlap(fragment["source_range"], record["source_range"]):
                    # Retries/slices in one scan are observations, not replacement scans.
                    old_record = fragment.get("candidate_id")
                    same_task = next((r.get("active_task_id") for r in candidates
                                      if r["record_id"] == old_record), None) == record.get("active_task_id")
                    if not same_task:
                        knowledge["fragments"][key]["state"] = "superseded"
        for index, term in enumerate(record["terms"]):
            if term.get("description"):
                key = f"{candidate_id}:{index}"
                knowledge["fragments"][key] = {
                    "owner": normalize_term(str(term["source"]), spec),
                    "origin": "scan", "candidate_id": candidate_id,
                    "term_index": index, "state": "active",
                }
        knowledge["published_candidates"].append(candidate_id)
        seen.add(candidate_id)


def entity_members(library: dict[str, Any]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for term in library["terms"]:
        root = str(term.get("group_primary") or term["normalized"])
        groups.setdefault(root, []).append(str(term["normalized"]))
    return {key: sorted(values) for key, values in groups.items()}


def _input_version(members: list[str], fragments: list[dict[str, Any]]) -> str:
    return digest([members, [(f["id"], f["text"], f["source_range"])
                             for f in fragments if f["state"] == "active"]])


def _source_order(project: Path, fragments: list[dict[str, Any]]) -> list[list[Any]]:
    order = {key: index for f in fragments for key, index in f.get("_positions", {}).items()}
    ranges = {(r["segment_id"], r["start"], r["end"])
              for f in fragments for r in (f["source_range"] or [])}
    return [list(r) for r in sorted(ranges, key=lambda r: (order.get(r[0], -1), r[1], r[2]))]


def entity_view(
    project: Path, library: dict[str, Any], normalized: str, *, require_current: bool = False,
    all_evidence: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    groups = entity_members(library)
    root = next((key for key, members in groups.items() if normalized in members), None)
    if root is None:
        raise UsageError("实体术语不存在")
    members = groups[root]
    known = all_evidence if all_evidence is not None else evidence(project, library)
    fragments = [f for f in known.values() if f["owner"] in members]
    order = _source_order(project, fragments)
    rank = {tuple(r): index for index, r in enumerate(order)}
    fragments.sort(key=lambda f: (min((rank[tuple((r["segment_id"], r["start"], r["end"]))]
                                     for r in (f["source_range"] or [])), default=-1), f["id"]))
    saved = require_knowledge(library)["entities"].get(root)
    items = deepcopy(saved["items"]) if saved else []
    refs = {key for item in items for key in item["fragment_ids"]}
    referenced = [known[key] for key in refs if key in known]
    stale = bool(saved) and (
        saved["members"] != members or len(referenced) != len(refs)
        or any(f["owner"] not in members or f["state"] != "active" or not f["valid"] for f in referenced)
        or saved["source_order"] != _source_order(project, referenced)
    )
    version = _input_version(members, fragments)
    status = "stale" if stale else "pending" if not saved or saved["input_version"] != version else "current"
    if require_current and stale:
        raise UsageError(f"实体说明已过期，请更新后再执行：{root}")
    for item in items:
        item["sources"] = [known[key]["source_range"] for key in item["fragment_ids"] if key in known]
    return {"entity_key": root, "members": members, "status": status, "items": items,
            "fragments": fragments, "input_version": version,
            "source_order": _source_order(project, [f for f in fragments if f["state"] == "active"])}


def validate_items(view: dict[str, Any], items: Any, *, allow_manual: bool = False) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        raise UsageError("说明条目必须是数组")
    available = {f["id"] for f in view["fragments"] if f["state"] == "active" and f["valid"]}
    result = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {"text", "fragment_ids"}:
            raise UsageError("说明条目必须包含 text 和 fragment_ids")
        text, refs = item["text"], item["fragment_ids"]
        if not isinstance(text, str) or not text.strip():
            raise UsageError("说明文字不能为空")
        if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
            raise UsageError("说明引用必须是字符串数组")
        if (not refs and not allow_manual) or not set(refs) <= available:
            raise UsageError("说明引用缺失、无效或不属于当前实体")
        result.append({"text": text.strip(), "fragment_ids": list(dict.fromkeys(refs))})
    return result


def add_unsourced(knowledge: dict[str, Any], owner: str, text: str, origin: str) -> str:
    key = new_record_id("KNOW")
    knowledge["fragments"][key] = {"owner": owner, "text": text, "origin": origin, "state": "active"}
    return key


def save_items(project: Path, library: dict[str, Any], normalized: str, items: Any) -> None:
    view = entity_view(project, library, normalized)
    clean = validate_items(view, items, allow_manual=True)
    knowledge = require_knowledge(library)
    for item in clean:
        if not item["fragment_ids"]:
            item["fragment_ids"] = [add_unsourced(knowledge, view["entity_key"], item["text"], "manual")]
    view = entity_view(project, library, normalized)
    referenced = {key for item in clean for key in item["fragment_ids"]}
    knowledge["entities"][view["entity_key"]] = {
        "items": clean, "members": view["members"], "input_version": view["input_version"],
        "source_order": _source_order(project, [f for f in view["fragments"] if f["id"] in referenced]),
    }
    project_descriptions(project, library)


def confirm_keep(project: Path, library: dict[str, Any], normalized: str) -> None:
    view = entity_view(project, library, normalized, require_current=True)
    saved = require_knowledge(library)["entities"].get(view["entity_key"])
    if saved is None:
        raise UsageError("尚无已应用说明，不能确认保持")
    saved["input_version"] = view["input_version"]


def project_descriptions(project: Path, library: dict[str, Any]) -> None:
    known = evidence(project, library)
    for root, members in entity_members(library).items():
        view = entity_view(project, library, root, all_evidence=known)
        for term in library["terms"]:
            if term["normalized"] in members:
                term["description"] = "；".join(item["text"] for item in view["items"])
                term["entity_key"] = root
                term["knowledge_status"] = view["status"]
                term["description_items"] = view["items"]


def knowledge_payload(view: dict[str, Any], target_language: str) -> tuple[dict[str, Any], dict[str, str]]:
    active = [f for f in view["fragments"] if f["state"] == "active"]
    refs = {f"K{index}": f["id"] for index, f in enumerate(active, 1)}
    base = {
        "phase": "knowledge", "entity": view["entity_key"], "members": view["members"],
        "target_language": target_language,
        "current_items": [{"text": item["text"]} for item in view["items"]],
        "fragments": [{"id": key, "text": f["text"], "owner": f["owner"],
                       "origin": f["origin"], "source_range": [
                           {**{k:r[k] for k in ("segment_id","file_id","part_id","start","end")},
                            "position": f["_positions"][r["segment_id"]]} for r in f["source_range"]
                       ] if f["source_range"] else None}
                      for key, f in zip(refs, active, strict=True)],
    }
    return base, refs


async def aggregate_entity(llm: Any, view: dict[str, Any], prompt: str, config: dict[str, Any]) -> dict[str, Any]:
    """Aggregate one complete entity input, with checked request-local citations."""
    from .errors import RequestSizeError
    from .execution import estimate_messages, render_messages
    from .llm_response import parse_jsonl_document
    from .term_decision_batches import _request_limits
    active = [f for f in view["fragments"] if f["state"] == "active"]
    if any(not f["valid"] for f in active):
        raise UsageError("实体证据已过期，请重新扫描或停用失效片段")
    if not active:
        return {"items": [], "reason": "没有说明证据", "input_version": view["input_version"]}
    base, refs = knowledge_payload(view, config["project"]["target_language"])
    parent_request_id = None
    feedback = ""
    for attempt in range(int(config["retry"]["format_max_attempts"]) + 1):
        payload = {**base, **({"validation_feedback": feedback} if feedback else {})}
        messages = render_messages(prompt, payload)
        estimate = estimate_messages(messages, float(config["execution"]["token_safety_factor"]))
        _, hard, context, itpm = _request_limits(config)
        if estimate > hard:
            raise RequestSizeError(f"实体完整证据请求估算 {estimate} tokens，超过硬限制 {hard} tokens", reason="itpm" if 0 < itpm < context else "context")
        response, request_id = await llm.chat(
            messages=messages, temperature=float(config["llm"]["temperature_terminology_decision"]),
            estimated_input_tokens=estimate, request_id=new_record_id("REQ"), parent_request_id=parent_request_id,
        )
        try:
            document = parse_jsonl_document(response.content, record_type="entity_knowledge")
            if document.errors or not document.complete or len(document.records) != 1:
                raise UsageError("实体说明响应必须含一条完整 entity_knowledge 和 end")
            record = document.records[0]
            if set(record) != {"type", "entity", "items", "reason"} or record["entity"] != view["entity_key"]:
                raise UsageError("实体说明响应目标或字段错误")
            if not isinstance(record["reason"], str) or not record["reason"].strip():
                raise UsageError("实体说明响应缺少理由")
            if not isinstance(record["items"], list):
                raise UsageError("说明条目必须是数组")
            items = []
            for item in record["items"]:
                if not isinstance(item, dict) or set(item) != {"text", "refs"}:
                    raise UsageError("说明条目必须包含 text 和 refs")
                if not isinstance(item["refs"], list) or not item["refs"] or not all(isinstance(r, str) and r in refs for r in item["refs"]):
                    raise UsageError("说明引用必须来自本次实体证据")
                items.append({"text": item["text"], "fragment_ids": [refs[r] for r in item["refs"]]})
            clean = validate_items(view, items)
            return {"items": clean, "reason": record["reason"], "input_version": view["input_version"]}
        except UsageError as exc:
            if attempt >= int(config["retry"]["format_max_attempts"]):
                raise
            feedback = str(exc)
            parent_request_id = request_id
    raise AssertionError("unreachable")
