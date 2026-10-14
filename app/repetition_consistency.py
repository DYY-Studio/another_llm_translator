"""Exact repeated-Segment discovery and model-generated consistency drafts."""
from __future__ import annotations

import asyncio
import hashlib
import json
import unicodedata
from collections import defaultdict
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx

from . import config as config_module
from .config import load_project_config
from .decision import DecisionClient, DecisionQuestion, decision_preset_path, load_decision_preset
from .decision_prompt import get_declaration, resolve_prompt
from .stage_runtime import _prompt, _prompt_language
from .errors import ExternalError, StorageError, UsageError
from .execution import (Scope, combine_usage, create_run, estimate_messages, finalize_run,
                        render_messages, run_bounded, select_scope, unavailable_usage)
from .llm_client import LLMClient, SlidingWindowLimiter
from .llm_keys import KeyPool
from .project import load_segments, load_source_files
from .sqlite_storage import (atomic_write_json, atomic_write_text, latest_stage_states, read_json,
                             read_repetition_run, utc_now)
from .sqlite_storage import write_repetition_state
from .term_library import load_terms, term_normalization
from .term_matching import _TermMatchCache
from .user_config import effective_path

STAGE = "repetition_consistency"
DRAFT_FILE = "repetition_draft.json"


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def draft_path(project: Path, run_id: str) -> Path:
    return project / "runs" / run_id / DRAFT_FILE


def read_draft(project: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    try:
        draft = json.loads(draft_path(project, manifest["run_id"]).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StorageError("无法读取重复译文审查草案") from exc
    if not isinstance(draft, dict) or draft.get("run_id") != manifest["run_id"]:
        raise StorageError("重复译文审查草案身份无效")
    for group in draft["groups"]:
        if group["screening"] == "skip" and not group["proposals"]:
            members = [o["id"] for o in group["occurrences"]]
            group["proposals"] = [{"id": f"{group['id']}-manual", "members": members,
                "target": members[0], "apply": members, "default_accepted": False, "reason": "", "manual": True}]
    return draft


def current_draft(project: Path) -> dict[str, Any] | None:
    manifest = read_repetition_run(project, ("pending",))
    if manifest is None:
        return None
    return {**read_draft(project, manifest), "choices": manifest.get("choices", {})}


def repetition_plan(project: Path, scope: Scope, *, prompt_language: str | None = None, exclude_punctuation_only: bool = True) -> dict[str, Any]:
    scope.validate()
    if scope.from_file or scope.only_segment or scope.segment_ids is not None or scope.force or scope.dry_run:
        raise UsageError("重复译文审查只支持整个项目或单个 File")
    config = load_project_config(project, stage="translation")
    language = _prompt_language(project, STAGE, prompt_language)
    prompt = _prompt(project, STAGE, language)
    screen_content, _ = resolve_prompt(get_declaration("repetition_screening"), project)
    screen = DecisionQuestion("value", screen_content["instructions"], screen_content["criteria"])
    config["_decision_prompt_definitions"]["repetition_screening"] = {
        "choice_ids": list(screen.choices), "content": screen_content}
    preset_id = config["decision"]["preset"]
    if not preset_id:
        raise UsageError("重复译文审查需要配置 Decision Preset")
    preset = load_decision_preset(effective_path(
        str(decision_preset_path(Path(), preset_id)), builtin_root=config_module.APP_ROOT))
    if preset["preset_id"] != preset_id:
        raise UsageError("Decision Preset ID 与配置不一致")
    segments = load_segments(project)
    files = load_source_files(project)
    file_indices = {f["file_id"]: i for i, f in enumerate(files, 1)}
    file_names = {f["file_id"]: f["original_name"] for f in files}
    if scope.only_file and scope.only_file not in {f["file_id"] for f in files}:
        raise UsageError("未知 File")
    selected = select_scope(segments, files, scope)
    states = latest_stage_states(project, "translation", [str(s["segment_id"]) for s in selected])
    bases = {sid: state["completed"] for sid, state in states.items() if state["completed"] is not None}
    if any(str(s["segment_id"]) not in bases for s in selected):
        raise UsageError("所选范围必须已有完整翻译")
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in selected:
        by_source[str(s["source"])].append(s)
    candidates = [items for items in by_source.values() if len(items) > 1
                  and len({bases[str(s["segment_id"])]["text"] for s in items}) > 1]
    excluded_punctuation_groups = 0
    if exclude_punctuation_only:
        retained = []
        for items in candidates:
            variants = {bases[str(s["segment_id"])]["text"] for s in items}
            bodies = {"".join(c for c in text if not unicodedata.category(c).startswith("P")) for text in variants}
            if len(bodies) == 1:
                excluded_punctuation_groups += 1
            else:
                retained.append(items)
        candidates = retained
    by_part: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for s in segments if candidates else []:
        if not s["is_empty"]:
            by_part[(s["file_id"], s["part_id"])].append(s)
    positions = {str(s["segment_id"]): (items, i)
                 for items in by_part.values() for i, s in enumerate(items)}
    library = load_terms(project) if candidates else None
    terms_revision = library["terms_revision"] if library else None
    warnings: list[str] = []
    term_cache = _TermMatchCache(library, term_normalization(config),
        int(config["terminology"]["max_terms_per_segment"]), segments=segments, warnings=warnings) if candidates else None
    terms: dict[str, dict[str, Any]] = {}
    term_ids: dict[str, str] = {}
    evidence: dict[str, dict[str, Any]] = {}
    sources: dict[str, str] = {}
    groups = []
    input_tokens = 0
    estimated_requests = 0
    decision = DecisionClient(preset)
    llm_limit = config["llm"]["context_window_tokens"] - config["llm"]["context_safety_margin_tokens"]
    decision_limit = preset["context_window_tokens"] - preset["context_safety_margin_tokens"]
    for number, items in enumerate(candidates, 1):
        variants = list(dict.fromkeys(bases[str(s["segment_id"])]["text"] for s in items))
        variant_indices = {text: i for i, text in enumerate(variants)}
        occurrences = []
        for i, s in enumerate(items, 1):
            sid = str(s["segment_id"])
            neighbors, position = positions[sid]
            context = [*neighbors[max(0, position - 3):position], *neighbors[position + 1:position + 4]]
            context_ids = []
            for neighbor in context:
                nid = str(neighbor["segment_id"])
                context_ids.append(nid)
                if nid not in evidence:
                    source_key = digest(neighbor["source"])
                    sources.setdefault(source_key, str(neighbor["source"]))
                    evidence[nid] = {"source_key": source_key, "signature": segment_signature(neighbor)}
            matched_ids = []
            local_items = [*neighbors[max(0, position - 3):position], s, *neighbors[position + 1:position + 4]]
            for term in term_cache.for_items(local_items):
                aliases = term.pop("aliases")
                relations = []
                if "entity_knowledge" in term:
                    relations = [item.pop("source_relation") for item in term["entity_knowledge"]["items"]]
                key = digest(term)
                tid = term_ids.get(key)
                if tid is None:
                    tid = str(len(terms) + 1)
                    term_ids[key] = tid
                    terms[tid] = term
                matched_ids.append({"id": tid, "aliases": aliases, "source_relations": relations})
            occurrences.append({"id": str(i), "terms": matched_ids, "segment_id": sid,
                "file_index": file_indices[s["file_id"]],
                "file_id": s["file_id"], "part_id": s["part_id"], "line_index": s["line_index"],
                "signature": segment_signature(s), "base_result_id": bases[sid]["record_id"],
                "origin": bases[sid].get("restored_origin", bases[sid].get("origin")), "variant": variant_indices[bases[sid]["text"]],
                "previous": context_ids[:min(3, position)], "next": context_ids[min(3, position):]})
        group = {"id": str(number), "source": str(items[0]["source"]),
                 "files": {str(file_indices[s["file_id"]]): file_names[s["file_id"]] for s in items},
                 "translations": variants, "occurrences": occurrences}
        _, screening_tokens = decision.request_plan(screening_payload(group), [screen])
        messages = render_messages(prompt, comparison_payload(group, evidence, sources, config["project"]["target_language"], terms))
        comparison_tokens = estimate_messages(messages, config["execution"]["token_safety_factor"])
        itpm = config["execution"]["input_tokens_per_minute"]
        group["over_budget"] = screening_tokens > decision_limit or comparison_tokens >= llm_limit or (itpm > 0 and comparison_tokens > itpm)
        if not group["over_budget"]:
            input_tokens += screening_tokens + comparison_tokens
            estimated_requests += 2
        groups.append(group)
    selected_count = sum(len(g["occurrences"]) for g in groups)
    fingerprint = digest({"exclude_punctuation_only": exclude_punctuation_only, "groups": groups, "evidence": evidence, "terms": terms, "terms_revision": terms_revision, "llm": config["_llm_preset_hash"],
                          "temperature": config["llm"]["temperature_translation"], "retry": config["retry"],
                          "decision": preset, "target_language": config["project"]["target_language"], "prompt": prompt,
                          "screen": screen_content})
    return {"exclude_punctuation_only": exclude_punctuation_only, "excluded_punctuation_groups": excluded_punctuation_groups, "config": config, "prompt": prompt, "language": language, "screen": screen, "terms": terms, "terms_revision": terms_revision, "warnings": warnings, "decision_preset": preset, "groups": groups, "evidence": evidence, "sources": sources,
            "selected": selected_count, "fingerprint": fingerprint, "estimated_requests": estimated_requests,
            "estimated_input_tokens": input_tokens}


def segment_signature(segment: dict[str, Any]) -> str:
    return digest([segment["source"], segment.get("model_source"), segment["file_id"],
                   segment["part_id"], segment["line_index"]])


def screening_payload(group: dict[str, Any]) -> dict[str, Any]:
    counts = [0] * len(group["translations"])
    for occurrence in group["occurrences"]:
        counts[occurrence["variant"]] += 1
    return {"source": group["source"], "translations": group["translations"], "counts": counts}


def comparison_payload(group: dict[str, Any], evidence: dict[str, Any], sources: dict[str, str], language: str, terms: dict[str, Any]) -> dict[str, Any]:
    context_ids = dict.fromkeys(n for o in group["occurrences"] for n in (*o["previous"], *o["next"]))
    source_keys = dict.fromkeys(evidence[sid]["source_key"] for sid in context_ids)
    source_ids = {key: str(i) for i, key in enumerate(source_keys, 1)}
    short = {sid: source_ids[evidence[sid]["source_key"]] for sid in context_ids}
    used_terms = dict.fromkeys(ref["id"] for o in group["occurrences"] for ref in o["terms"])
    short_terms = {tid: str(i) for i, tid in enumerate(used_terms, 1)}
    return {"source": group["source"], "translations": group["translations"], "target_language": language,
            "files": group["files"],
            "terms": {short_terms[tid]: terms[tid] for tid in used_terms},
            "context": {source_ids[key]: sources[key] for key in source_keys},
            "occurrences": [{"id": o["id"], "file": o["file_index"], "part": o["part_id"],
                             "position": o["line_index"], "variant": o["variant"], "terms": [{**ref, "id": short_terms[ref["id"]]} for ref in o["terms"]],
                             "previous": [short[sid] for sid in o["previous"]],
                             "next": [short[sid] for sid in o["next"]]} for o in group["occurrences"]]}


def parse_comparison(content: str, group: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(content)
        if not isinstance(value, dict) or set(value) != {"groups", "keep", "uncertain"}:
            raise ValueError("字段无效")
        if any(not isinstance(value[key], list) for key in value):
            raise ValueError("分组必须是数组")
        members = [*value["keep"], *value["uncertain"]]
        proposals = []
        for i, item in enumerate(value["groups"], 1):
            if (not isinstance(item, dict) or set(item) != {"members", "target", "reason", "apply"}
                or not isinstance(item["members"], list) or len(item["members"]) < 2
                or not isinstance(item["target"], str) or item["target"] not in item["members"]
                or not isinstance(item["reason"], str) or not item["reason"].strip()):
                raise ValueError("统一子组无效")
            if (not isinstance(item["apply"], list) or any(not isinstance(sid, str) for sid in item["apply"])
                or len(set(item["apply"])) != len(item["apply"]) or set(item["apply"]) - set(item["members"])):
                raise ValueError("应用成员无效")
            members.extend(item["members"])
            proposals.append({"id": f"{group['id']}-{i}", **item})
        expected = {o["id"] for o in group["occurrences"]}
        if any(not isinstance(sid, str) for sid in members) or len(members) != len(expected) or set(members) != expected:
            raise ValueError("未知、重复或缺少成员")
        return {"proposals": proposals, "keep": value["keep"], "uncertain": value["uncertain"]}
    except (ValueError, TypeError) as exc:
        raise ExternalError(f"重复译文审查响应无效：{exc}") from exc


async def run_repetition(project: Path, scope: Scope, *, prompt_language: str | None = None, exclude_punctuation_only: bool = True, replace_draft: bool = False,
                         http_client: httpx.AsyncClient | None = None,
                         limiter: SlidingWindowLimiter | KeyPool | None = None,
                         on_progress: Callable[[int, int, int], None] | None = None,
                         on_usage: Callable[[dict[str, Any] | None], None] | None = None,
                         prepared_plan: dict[str, Any] | None = None) -> dict[str, Any]:
    old = read_repetition_run(project, ("pending",))
    if old and not replace_draft:
        raise UsageError("已有待审核草案，必须明确选择替换")
    plan = prepared_plan if prepared_plan is not None else repetition_plan(project, scope, prompt_language=prompt_language, exclude_punctuation_only=exclude_punctuation_only)
    config = plan["config"]
    prompt, screen = plan["prompt"], plan["screen"]
    total = plan["selected"]
    metadata = read_json(project, project / "project.json")
    run_id, run_dir = create_run(project, config=config, stage=STAGE, fingerprint=plan["fingerprint"],
        prompt=prompt, selected_count=total, requested_count=total, reused_count=0,
        details={"exclude_punctuation_only": plan["exclude_punctuation_only"], "excluded_punctuation_groups": plan["excluded_punctuation_groups"], "prompt_language": plan["language"], "terms_revision": plan["terms_revision"], "draft_status": "generating", "scope": {"only_file": scope.only_file}, "choices": {}})
    atomic_write_text(run_dir / "repetition_terms.json", json.dumps({
        "terms_revision": plan["terms_revision"], "terms": plan["terms"],
        "groups": {g["id"]: {o["id"]: o["terms"] for o in g["occurrences"]} for g in plan["groups"]},
    }, ensure_ascii=False, separators=(",", ":")) + "\n")
    atomic_write_json(run_dir / "decision_preset.json", plan["decision_preset"])
    llm_usage: dict[str, Any] | None = None
    completed = 0
    def record_usage(value):
        nonlocal llm_usage
        llm_usage = value
        if on_usage:
            on_usage(llm_usage)
    decision = DecisionClient(plan["decision_preset"], retry=config["retry"], project_id=metadata["project_id"],
                              run_id=run_id, stage=STAGE, debug_directory=run_dir / "debug")
    limiter = limiter or SlidingWindowLimiter(config["execution"]["requests_per_minute"], config["execution"]["input_tokens_per_minute"])
    try:
        async with httpx.AsyncClient(proxy=plan["decision_preset"]["proxy_url"] or None) if http_client is None else _borrow_client(http_client) as decision_http:
            decision.http_client = decision_http
            async with LLMClient(config, limiter, run_dir=run_dir, project_id=metadata["project_id"],
                                 run_id=run_id, stage=STAGE, client=http_client, on_usage=record_usage) as llm:
                async def compare(group):
                    nonlocal completed
                    group = dict(group)
                    if group["over_budget"]:
                        group.update(screening="over_budget", proposals=[], keep=[], uncertain=[o["id"] for o in group["occurrences"]])
                    else:
                        answer = (await decision.choose(screening_payload(group), [screen]))["value"]
                        group["screening"] = "refusal" if answer.refused else answer.choice
                        group["confidence"] = answer.confidence
                        if answer.choice == "skip":
                            group.update(proposals=[], keep=[o["id"] for o in group["occurrences"]], uncertain=[])
                        else:
                            messages = render_messages(prompt, comparison_payload(group, plan["evidence"], plan["sources"], config["project"]["target_language"], plan["terms"]))
                            response, _ = await llm.chat(messages=messages, temperature=config["llm"]["temperature_translation"],
                                estimated_input_tokens=estimate_messages(messages, config["execution"]["token_safety_factor"]))
                            group.update(parse_comparison(response.content, group))
                    completed += len(group["occurrences"])
                    if on_progress:
                        on_progress(completed, 0, total)
                    return group
                groups = await run_bounded(plan["groups"], compare,
                    max_parallel=config["execution"]["max_parallel"])
        for group in groups:
            for occurrence in group["occurrences"]:
                occurrence.pop("terms")
        draft = {"run_id": run_id, "groups": groups, "evidence": plan["evidence"], "sources": plan["sources"], "scope": {"only_file": scope.only_file}, "created_at": utc_now()}
        atomic_write_text(draft_path(project, run_id), json.dumps(draft, ensure_ascii=False, separators=(",", ":")) + "\n")
        usage = combine_usage(llm_usage, decision.usage_summary()) if decision.records or llm_usage else unavailable_usage()
        finalize_run(project, run_dir, status="completed", completed=total, failed=0, usage=usage)
        manifest = read_json(project, run_dir / "manifest.json")
        manifest["draft_status"] = "pending"
        manifests = [manifest]
        if old:
            old["draft_status"] = "replaced"
            manifests.append(old)
        warnings = [*plan["warnings"], *write_repetition_state(project, records=[], manifests=manifests)]
        return {"excluded_punctuation_groups": plan["excluded_punctuation_groups"], "stage": STAGE, "run_id": run_id, "selected": total, "completed": total,
                "failed": 0, "pending": 0, "usage": usage, "warnings": warnings,
                "proposal_count": sum(len(g["proposals"]) for g in groups)}
    except BaseException as exc:
        finalize_run(project, run_dir, status="interrupted" if isinstance(exc, asyncio.CancelledError) else "failed",
                     completed=completed, failed=total - completed, usage=combine_usage(llm_usage, decision.usage_summary()))
        raise


@asynccontextmanager
async def _borrow_client(client):
    yield client
