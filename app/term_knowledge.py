"""Source-backed entity explanations; scan candidates remain the evidence owner."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from .errors import UsageError
from .execution import segment_model_source
from .sqlite_storage import new_record_id, read_segments, read_terminology_candidates
from .summary_provenance import digest


def empty_knowledge() -> dict[str, Any]:
    return {"version": 1, "fragments": {}, "entities": {}, "published_candidates": []}


def persistent_library(library: dict[str, Any]) -> dict[str, Any]:
    """Keep runtime explanation projections out of storage and Run snapshots."""
    return {**library, "terms": [{key: value for key, value in term.items()
                                 if key not in {"entity_key", "knowledge_status", "description_items"}}
                                for term in library["terms"]]}


def navigation_fragment(fragment: dict[str, Any]) -> dict[str, Any]:
    value = {key: fragment[key] for key in ("id", "owner", "original_owner", "origin", "text", "state", "valid", "_positions")}
    if "source" in fragment:
        value["source"] = fragment["source"]
    value["source_range"] = [{key: r[key] for key in ("segment_id", "file_id", "part_id", "start", "end")}
                             for r in fragment["source_range"]] if fragment["source_range"] else None
    return value


def require_knowledge(library: dict[str, Any]) -> dict[str, Any]:
    knowledge = library.get("knowledge")
    if not isinstance(knowledge, dict) or knowledge.get("version") != 1:
        raise UsageError("术语库缺少有效的实体说明结构")
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


def _range_index(ranges: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_segment: dict[str, list[dict[str, Any]]] = {}
    for r in ranges:
        by_segment.setdefault(r["segment_id"], []).append(r)
    return by_segment


def _overlap(left: list[dict[str, Any]], right: dict[str, list[dict[str, Any]]]) -> bool:
    return any(a["source_digest"] != b["source_digest"]
               or (a["start"] < b["end"] and b["start"] < a["end"])
               for a in left for b in right.get(a["segment_id"], ()))


def evidence(project: Path, library: dict[str, Any]) -> dict[str, dict[str, Any]]:
    fragments = require_knowledge(library)["fragments"]
    ids = {f["candidate_id"] for f in fragments.values() if f.get("candidate_id")}
    records = {r["record_id"]: r for r in read_terminology_candidates(project, ids)}
    segments = {s["segment_id"]: s for s in read_segments(project)}
    positions = {key: i for i, key in enumerate(segments)}
    segment_digests = {}
    record_sources = {}
    result = {}
    for key, fragment in fragments.items():
        value = {**fragment, "id": key, "source_range": None, "valid": True, "_positions": {}}
        if fragment.get("candidate_id"):
            record = records.get(fragment["candidate_id"])
            if record is None:
                raise UsageError("实体说明引用的候选记录已缺失")
            value["source"] = record["terms"][fragment["term_index"]]["source"]
            value["text"] = record["terms"][fragment["term_index"]]["description"]
            value["source_range"] = record["source_range"]
            if fragment["candidate_id"] not in record_sources:
                for r in record["source_range"]:
                    segment_id = r["segment_id"]
                    if segment_id in segments and segment_id not in segment_digests:
                        segment = segments[segment_id]
                        segment_digests[segment_id] = (digest(str(segment["source"])), digest(segment_model_source(segment)))
                record_sources[fragment["candidate_id"]] = (
                    {r["segment_id"]: positions.get(r["segment_id"], -1) for r in record["source_range"]},
                    all(segment_digests.get(r["segment_id"]) == (r["source_digest"], r["model_text_digest"])
                        for r in record["source_range"]),
                )
            value["_positions"], value["valid"] = record_sources[fragment["candidate_id"]]
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
            coverage = _range_index(record["source_range"])
            for key, fragment in old.items():
                if fragment["source_range"] and _overlap(fragment["source_range"], coverage):
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
                    "original_owner": normalize_term(str(term["source"]), spec),
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


def _source_order(fragments: list[dict[str, Any]]) -> list[list[Any]]:
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
    member_names = set(members)
    fragments = [f for f in known.values() if f["owner"] in member_names]
    return _entity_view(library, root, members, known, fragments, require_current=require_current)


def _entity_view(
    library: dict[str, Any], root: str, members: list[str],
    known: dict[str, dict[str, Any]], fragments: list[dict[str, Any]], *,
    require_current: bool = False,
) -> dict[str, Any]:
    member_names = set(members)
    order = _source_order(fragments)
    rank = {tuple(r): index for index, r in enumerate(order)}
    fragments.sort(key=lambda f: (min((rank[tuple((r["segment_id"], r["start"], r["end"]))]
                                     for r in (f["source_range"] or [])), default=-1), f["id"]))
    saved = require_knowledge(library)["entities"].get(root)
    items = deepcopy(saved["items"]) if saved else []
    refs = {key for item in items for key in item["fragment_ids"]}
    referenced = [known[key] for key in refs if key in known]
    active_order = _source_order([f for f in fragments if f["state"] == "active"])
    previous_ranges = {tuple(r) for r in saved["source_order"]} if saved else set()
    current_ranges = {tuple(r) for r in active_order}
    stale = bool(saved) and (
        saved["members"] != members or len(referenced) != len(refs)
        or any(f["owner"] not in member_names or f["state"] != "active" or not f["valid"] for f in referenced)
        or [r for r in saved["source_order"] if tuple(r) in current_ranges]
        != [r for r in active_order if tuple(r) in previous_ranges]
    )
    version = _input_version(members, fragments)
    status = "stale" if stale else "pending" if not saved or saved["input_version"] != version else "current"
    if require_current and stale:
        raise UsageError(f"实体说明已过期，请更新后再执行：{root}")
    for item in items:
        item["sources"] = [known[key]["source_range"] for key in item["fragment_ids"] if key in known]
    return {"entity_key": root, "members": members, "status": status, "items": items,
            "fragments": fragments, "input_version": version,
            "source_order": active_order}


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
    knowledge["fragments"][key] = {"owner": owner, "original_owner": owner, "text": text, "origin": origin, "state": "active"}
    return key


def save_items(project: Path, library: dict[str, Any], normalized: str, items: Any, *,
               all_evidence: dict[str, dict[str, Any]] | None = None) -> None:
    known = all_evidence if all_evidence is not None else evidence(project, library)
    view = entity_view(project, library, normalized, all_evidence=known)
    clean = validate_items(view, items, allow_manual=True)
    knowledge = require_knowledge(library)
    for item in clean:
        if not item["fragment_ids"]:
            key = add_unsourced(knowledge, view["entity_key"], item["text"], "manual")
            item["fragment_ids"] = [key]
            known[key] = {**knowledge["fragments"][key], "id": key, "source_range": None, "valid": True, "_positions": {}}
    view = entity_view(project, library, normalized, all_evidence=known)
    knowledge["entities"][view["entity_key"]] = {
        "items": clean, "members": view["members"], "input_version": view["input_version"],
        "source_order": view["source_order"],
    }


def confirm_keep(project: Path, library: dict[str, Any], normalized: str, *,
                 all_evidence: dict[str, dict[str, Any]] | None = None) -> None:
    view = entity_view(project, library, normalized, require_current=True, all_evidence=all_evidence)
    saved = require_knowledge(library)["entities"].get(view["entity_key"])
    if saved is None:
        raise UsageError("尚无已应用说明，不能确认保持")
    saved["input_version"] = view["input_version"]
    saved["source_order"] = view["source_order"]


def project_descriptions(project: Path, library: dict[str, Any], *,
                         all_evidence: dict[str, dict[str, Any]] | None = None) -> None:
    known = all_evidence if all_evidence is not None else evidence(project, library)
    terms = {term["normalized"]: term for term in library["terms"]}
    groups = entity_members(library)
    roots = {member: root for root, members in groups.items() for member in members}
    fragments = {root: [] for root in groups}
    for fragment in known.values():
        root = roots.get(fragment["owner"])
        if root is not None:
            fragments[root].append(fragment)
    for root, members in groups.items():
        view = _entity_view(library, root, members, known, fragments[root])
        for member in members:
            term = terms[member]
            term["description"] = "；".join(item["text"] for item in view["items"])
            term["entity_key"] = root
            term["knowledge_status"] = view["status"]
            term["description_items"] = view["items"]


def compact_source_ranges(ranges: list[dict[str, Any]] | None, positions: dict[str, int]) -> list[dict[str, Any]] | None:
    groups: list[dict[str, Any]] = []
    previous = None
    for r in ranges or []:
        position = positions[r["segment_id"]]
        contiguous = previous is not None and (
            previous["file_id"] == r["file_id"] and previous["part_id"] == r["part_id"]
            and positions[previous["segment_id"]] + 1 == position
            and r["start"] == 0
            and previous["slice_source_digest"] == previous["source_digest"]
        )
        if not contiguous:
            groups.append({"file_id": r["file_id"], "part_id": r["part_id"],
                           "first": {"position": position, "start": r["start"]}})
        groups[-1]["last"] = {"position": position, "end": r["end"]}
        previous = r
    return groups or None


def knowledge_payload(views: list[dict[str, Any]], target_language: str) -> tuple[dict[str, Any], dict[str, tuple[str, str]]]:
    refs: dict[str, tuple[str, str]] = {}
    entities = []
    for view in views:
        fragments = []
        for f in view["fragments"]:
            if f["state"] != "active":
                continue
            key = f"K{len(refs) + 1}"
            refs[key] = (view["entity_key"], f["id"])
            fragments.append({"id": key, "text": f["text"], "owner": f["owner"],
                              "original_source": f.get("source", f["original_owner"]),
                              "origin": f["origin"], "source_range": compact_source_ranges(f["source_range"], f["_positions"])})
        entities.append({"entity": view["entity_key"], "members": view["members"],
                         "current_items": [{"text": item["text"]} for item in view["items"]],
                         "fragments": fragments})
    return {"phase": "knowledge", "target_language": target_language, "entities": entities}, refs


def pack_knowledge_batches(views: list[dict[str, Any]], prompt: str, config: dict[str, Any], *,
                           feedback: str = "") -> tuple[list[list[dict[str, Any]]], int, dict[str, str]]:
    """Pack complete entities, isolating invalid/oversized evidence without trimming."""
    from .execution import estimate_messages, render_messages
    from .term_decision_batches import _request_limits
    soft, hard, _, _ = _request_limits(config)
    overflow = config["terminology_decision"]["allow_soft_target_overflow"]
    batches, errors = [], {}
    current: list[dict[str, Any]] = []
    total = 0

    def estimate(group: list[dict[str, Any]]) -> int:
        payload, _ = knowledge_payload(group, config["project"]["target_language"])
        if feedback:
            payload["validation_feedback"] = feedback
        return estimate_messages(render_messages(prompt, payload), float(config["execution"]["token_safety_factor"]))

    def flush() -> None:
        nonlocal current, total
        if current:
            total += estimate(current)
            batches.append(current)
            current = []

    for view in views:
        active = [f for f in view["fragments"] if f["state"] == "active"]
        if any(not f["valid"] for f in active):
            errors[view["entity_key"]] = "实体证据已过期，请重新扫描或停用失效片段"
            continue
        if not active:
            continue
        size = estimate([view])
        if size > hard or (size > soft and not overflow):
            limit = hard if size > hard else soft
            errors[view["entity_key"]] = f"实体完整证据请求估算 {size} tokens，超过{'硬限制' if size > hard else '软预算'} {limit} tokens"
            continue
        if current and estimate([*current, view]) > min(soft, hard):
            flush()
        current.append(view)
        if size > soft:
            flush()
    flush()
    return batches, total, errors


def _knowledge_result(view: dict[str, Any], record: dict[str, Any], refs: dict[str, tuple[str, str]]) -> dict[str, Any]:
    if set(record) != {"type", "entity", "items", "reason"}:
        raise UsageError("实体说明响应字段错误")
    if not isinstance(record["reason"], str) or not record["reason"].strip():
        raise UsageError("实体说明响应缺少理由")
    if not isinstance(record["items"], list):
        raise UsageError("说明条目必须是数组")
    items = []
    for item in record["items"]:
        if not isinstance(item, dict) or set(item) != {"text", "refs"}:
            raise UsageError("说明条目必须包含 text 和 refs")
        if (not isinstance(item["refs"], list)
                or not all(isinstance(r, str) and r in refs for r in item["refs"])):
            raise UsageError("说明引用必须来自本次实体证据")
        items.append({"text": item["text"], "fragment_ids": [refs[r][1] for r in item["refs"]]})
    return {"items": validate_items(view, items), "reason": record["reason"], "input_version": view["input_version"]}


async def aggregate_batch(llm: Any, views: list[dict[str, Any]], prompt: str, config: dict[str, Any], *,
                          on_result: Callable[[dict[str, Any], dict[str, Any]], None]) -> dict[str, str]:
    """Persist valid entity results before retrying only unfinished complete inputs."""
    from .errors import AppError, FatalExternalError, StorageError
    from .execution import estimate_messages, render_messages
    from .llm_response import parse_jsonl_document
    pending = list(views)
    errors: dict[str, str] = {}
    feedback = ""
    parent_request_id = None
    for attempt in range(int(config["retry"]["format_max_attempts"]) + 1):
        batches, _, oversized = pack_knowledge_batches(pending, prompt, config, feedback=feedback)
        errors.update(oversized)
        retry = []
        for batch in batches:
            payload, refs = knowledge_payload(batch, config["project"]["target_language"])
            if feedback:
                payload["validation_feedback"] = feedback
            messages = render_messages(prompt, payload)
            estimate = estimate_messages(messages, float(config["execution"]["token_safety_factor"]))
            try:
                response, request_id = await llm.chat(
                    messages=messages, temperature=float(config["llm"]["temperature_terminology_decision"]),
                    estimated_input_tokens=estimate, request_id=new_record_id("REQ"), parent_request_id=parent_request_id,
                )
            except AppError as exc:
                if isinstance(exc, (FatalExternalError, StorageError)):
                    raise
                errors.update({v["entity_key"]: str(exc) for v in batch})
                continue
            parent_request_id = request_id
            document = parse_jsonl_document(response.content, record_type="entity_knowledge")
            requested = {v["entity_key"] for v in batch}
            if (document.errors or not document.complete
                    or any(not isinstance(r.get("entity"), str) or r["entity"] not in requested for r in document.records)):
                errors.update({key: "实体说明响应格式损坏、终止记录缺失或包含未知实体" for key in requested})
                retry.extend(batch)
                continue
            for view in batch:
                root = view["entity_key"]
                records = [r for r in document.records if r["entity"] == root]
                try:
                    if len(records) != 1:
                        raise UsageError("每个实体必须恰好返回一条 entity_knowledge")
                    result = _knowledge_result(view, records[0], refs)
                except UsageError as exc:
                    errors[root] = str(exc)
                    retry.append(view)
                    continue
                on_result(view, result)
                errors.pop(root, None)
        if not retry:
            break
        pending = retry
        feedback = "；".join(f'{v["entity_key"]}: {errors[v["entity_key"]]}' for v in pending)
    return errors


def update_entity(project: Path, payload: dict[str, Any]) -> dict[str, Any]:
    from .locking import project_write_lock
    from .term_library import load_terms
    from .sqlite_storage import read_json, write_terminology_publication
    with project_write_lock(project):
        library = load_terms(project)
        if library is None:
            raise UsageError("没有已发布术语库")
        if type(payload.get("expected_revision")) is not int or payload["expected_revision"] != library["terms_revision"]:
            raise UsageError("术语库版本已变化，请刷新后再编辑说明")
        normalized = payload.get("normalized")
        if not isinstance(normalized, str):
            raise UsageError("实体术语标识必须是字符串")
        known = evidence(project, library)
        view = entity_view(project, library, normalized, all_evidence=known)
        action = payload.get("action")
        fields = {"save_items": {"items"}, "confirm_keep": set(), "set_fragment_enabled": {"fragment_id", "enabled"}}
        if not isinstance(action, str) or action not in fields:
            raise UsageError("不支持的实体说明操作")
        if set(payload) - {"normalized", "expected_revision", "action"} - fields[action]:
            raise UsageError("实体说明操作包含不支持的字段")
        if action == "save_items":
            save_items(project, library, normalized, payload.get("items"), all_evidence=known)
        elif action == "confirm_keep":
            confirm_keep(project, library, normalized, all_evidence=known)
        elif action == "set_fragment_enabled":
            key = payload.get("fragment_id")
            if not isinstance(key, str) or key not in {f["id"] for f in view["fragments"]}:
                raise UsageError("片段不属于当前实体")
            if type(payload.get("enabled")) is not bool:
                raise UsageError("enabled 必须是布尔值")
            fragment = library["knowledge"]["fragments"][key]
            if fragment["state"] == "superseded":
                raise UsageError("已被重扫替换的片段不能恢复")
            fragment["state"] = "active" if payload["enabled"] else "disabled"
            known[key]["state"] = fragment["state"]
        else:
            raise UsageError("不支持的实体说明操作")
        library["terms_revision"] += 1
        library["record_id"] = f'TERMS-{library["terms_revision"]}'
        project_descriptions(project, library, all_evidence=known)
        overrides = read_json(project, project / "terminology" / "overrides.json")
        write_terminology_publication(project, terms=library, overrides=overrides)
        return library


def remap_owner(library: dict[str, Any], old: str, new: str) -> None:
    """Keep evidence identity when an explicit term rename/alias conversion moves it."""
    knowledge = require_knowledge(library)
    for fragment in knowledge["fragments"].values():
        if fragment["owner"] == old:
            fragment["owner"] = new
    if old in knowledge["entities"] and new not in knowledge["entities"]:
        knowledge["entities"][new] = knowledge["entities"].pop(old)


def transfer_alias_evidence(library: dict[str, Any], spec: Any) -> dict[str, str]:
    from .term_library import normalize_term
    active = {term["normalized"] for term in library["terms"]}
    aliases: dict[str, set[str]] = {}
    for term in library["terms"]:
        for alias in term.get("aliases", []):
            aliases.setdefault(normalize_term(alias, spec), set()).add(term["normalized"])
    moved = {}
    for owner in {f["owner"] for f in require_knowledge(library)["fragments"].values()} - active:
        receivers = aliases.get(owner, set())
        if len(receivers) == 1:
            new = next(iter(receivers))
            remap_owner(library, owner, new)
            moved[owner] = new
    return moved


def replacement_impacts(project: Path, segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from .term_library import load_terms
    library = load_terms(project)
    if library is None:
        return []
    coverage = _range_index(source_range(segments, {s["segment_id"]: s for s in segments}, {}))
    return [{"fragment_id": f["id"], "owner": f["owner"], "text": f["text"], "source_range": f["source_range"]}
            for f in evidence(project, library).values()
            if f["state"] == "active" and f["source_range"] and _overlap(f["source_range"], coverage)]
