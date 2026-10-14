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
from .diagnostics import current_diagnostics
from .stage_runtime import _prompt, _prompt_language
from .errors import ContextLengthError, ExternalError, StorageError, UsageError
from .execution import (Scope, choose_running_run, combine_usage, continue_run, create_run, estimate_messages, finalize_run, find_running_runs,
                        render_messages, run_bounded, select_scope, unavailable_usage)
from .llm_client import empty_response_split_scope, LLMClient, SlidingWindowLimiter
from .llm_keys import KeyPool
from .project import load_segments, load_source_files
from .sqlite_storage import (atomic_write_json, atomic_write_text, latest_stage_states, read_json,
                             read_repetition_run, utc_now)
from .sqlite_storage import write_json, write_repetition_state
from .term_library import load_terms, term_normalization
from .term_matching import _TermMatchCache
from .user_config import effective_path

STAGE = "repetition_consistency"
DRAFT_FILE = "repetition_draft.json"
PROTOCOL_VERSION = 1


def repetition_resume(project: Path, plan: dict[str, Any]) -> dict[str, Any] | None:
    runs = find_running_runs(project, STAGE)
    if not runs:
        return None
    run = runs[0]
    compatible = (run.get("repetition_checkpoint") == 2 and run.get("repetition_protocol") == PROTOCOL_VERSION
                  and run.get("repetition_input_fingerprint") == plan["input_fingerprint"])
    reason = None if compatible else "Run 检查点或协议不兼容，或原文、译文及术语证据已变化，请结束旧 Run 并新建"
    try:
        checkpoints = load_checkpoints(project / "runs" / run["run_id"], plan) if compatible else {}
    except StorageError as exc:
        checkpoints = {}
        compatible = False
        reason = f"{exc}，请结束旧 Run 并新建"
    completed = sum(len(g["occurrences"]) for g in plan["groups"] if "result" in checkpoints.get(g["id"], {}))
    remaining = [g for g in plan["groups"] if "result" not in checkpoints.get(g["id"], {}) and not group_over_budget(g, plan, checkpoints.get(g["id"], {}))]
    comparison = [g for g in remaining if checkpoints.get(g["id"], {}).get("screening") != "skip"]
    batches = comparison_batches(comparison, plan)
    requests = len(batches)
    tokens = sum(estimate for _, estimate in batches)
    if plan["screening_enabled"]:
        unscreened = [g for g in remaining if "screening" not in checkpoints.get(g["id"], {})]
        requests += len(unscreened)
        tokens += sum(g["screening_input_tokens"] for g in unscreened)
    latest_execution = (run.get("continuations") or [run])[-1]["stage_fingerprint"]
    return {"execution_changed": latest_execution != plan["fingerprint"], "run_id": run["run_id"], "scope": run["scope"], "prompt_language": run.get("prompt_language"),
            "screening_enabled": run.get("screening_enabled"), "exclude_punctuation_only": run.get("exclude_punctuation_only"),
            "resume_compatible": compatible, "resume_incompatibility_reason": reason,
            "completed": completed, "completed_steps": sum((int("screening" in c) if plan["screening_enabled"] else 0) + int("result" in c) for c in checkpoints.values()),
            "estimated_requests": requests, "estimated_input_tokens": tokens,
            "over_budget_groups": sum(group_over_budget(g, plan, checkpoints.get(g["id"], {})) for g in plan["groups"]),
            "total_steps": len(plan["groups"]) * (2 if plan["screening_enabled"] else 1)}


def checkpoint_path(run_dir: Path, group_id: str) -> Path:
    return run_dir / "repetition_checkpoints" / f"{group_id}.json"


def load_checkpoints(run_dir: Path, plan: dict[str, Any]) -> dict[str, Any]:
    result = {}
    manifest = read_json(run_dir.parent.parent, run_dir / "manifest.json")
    last_index = len(manifest.get("continuations", [])) + 1
    for group in plan["groups"]:
        path = checkpoint_path(run_dir, group["id"])
        if not path.exists():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StorageError("无法读取重复译文续作检查点") from exc
        fields = {"screening", "confidence", "screening_execution_index"}
        if (not isinstance(value, dict) or set(value) not in (fields, fields | {"result", "comparison_execution_index"})
            or not isinstance(value["screening"], str) or value["screening"] not in {"compare", "skip", "refusal", "disabled", "over_budget"}
            or ("result" in value and (not isinstance(value["result"], dict) or set(value["result"]) != {"proposals", "keep", "uncertain"}))):
            raise StorageError("重复译文续作检查点无效")
        for field, invoked in (("screening_execution_index", value["screening"] in {"compare", "skip", "refusal"}),
                               ("comparison_execution_index", value.get("comparison_execution_index") is not None)):
            index = value.get(field)
            if invoked:
                if type(index) is not int or not 1 <= index <= last_index:
                    raise StorageError("重复译文检查点执行来源无效")
                directory = run_dir if index == 1 else run_dir / "continuations" / f"{index - 1:04d}"
                resource = directory / ("decision_prompts/repetition_screening.json" if field == "screening_execution_index" else "prompt.txt")
                if not resource.is_file():
                    raise StorageError("重复译文检查点执行快照缺失")
            elif index is not None:
                raise StorageError("重复译文检查点执行来源无效")
        if "result" in value and value["screening"] != "skip" and value["comparison_execution_index"] is None:
            if value["result"] != {"proposals": [], "keep": [], "uncertain": [o["id"] for o in group["occurrences"]]}:
                raise StorageError("重复译文检查点缺少比较执行来源")
            if value["screening_execution_index"] is not None:
                result[group["id"]] = {key: value[key] for key in fields}
        else:
            result[group["id"]] = value
    return result


def group_over_budget(group: dict[str, Any], plan: dict[str, Any], saved: dict[str, Any]) -> bool:
    if "result" in saved or saved.get("screening") == "skip":
        return False
    if "screening" not in saved:
        return group["over_budget"]
    config = plan["config"]
    tokens = group["comparison_input_tokens"]
    itpm = config["execution"]["input_tokens_per_minute"]
    return tokens > config["llm"]["context_window_tokens"] - config["llm"]["context_safety_margin_tokens"] or (itpm > 0 and tokens > itpm)


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


def repetition_execution_fingerprint(config: dict[str, Any], prompt: str, screen_content: dict[str, Any] | None,
                                     preset: dict[str, Any] | None, input_fingerprint: str | None = None) -> str:
    return digest({"input": input_fingerprint, "protocol": PROTOCOL_VERSION,
        "llm": config["_llm_preset_hash"], "adapter": config["_llm_adapter_hash"],
        "temperature": config["llm"]["temperature_translation"], "retry": config["retry"],
        "execution": config["execution"], "decision": preset, "prompt": prompt, "screen": screen_content})


def repetition_resources(project: Path, prompt_language: str | None, screening_enabled: bool) -> tuple[dict[str, Any], str, dict[str, Any] | None, DecisionQuestion | None, dict[str, Any] | None, str]:
    config = load_project_config(project, stage="translation")
    language = _prompt_language(project, STAGE, prompt_language)
    prompt = _prompt(project, STAGE, language)
    screen_content = screen = preset = None
    if screening_enabled:
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
        config["_decision_preset_definitions"][preset_id] = preset
    return config, prompt, screen_content, screen, preset, language


def repetition_plan(project: Path, scope: Scope, *, prompt_language: str | None = None, exclude_punctuation_only: bool = True, screening_enabled: bool = False) -> dict[str, Any]:
    scope.validate()
    if scope.from_file or scope.only_segment or scope.segment_ids is not None or scope.force or scope.dry_run:
        raise UsageError("重复译文审查只支持整个项目或单个 File")
    config, prompt, screen_content, screen, preset, language = repetition_resources(project, prompt_language, screening_enabled)
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
    decision = DecisionClient(preset) if preset else None
    llm_limit = config["llm"]["context_window_tokens"] - config["llm"]["context_safety_margin_tokens"]
    decision_limit = preset["context_window_tokens"] - preset["context_safety_margin_tokens"] if preset else 0
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
        screening_tokens = decision.request_plan(screening_payload(group), [screen])[1] if decision else 0
        messages = render_messages(prompt, comparison_payload([group], evidence, sources, config["project"]["target_language"], terms))
        comparison_tokens = estimate_messages(messages, config["execution"]["token_safety_factor"])
        group["screening_input_tokens"] = screening_tokens
        group["comparison_input_tokens"] = comparison_tokens
        itpm = config["execution"]["input_tokens_per_minute"]
        group["over_budget"] = (screening_enabled and screening_tokens > decision_limit) or comparison_tokens > llm_limit or (itpm > 0 and comparison_tokens > itpm)
        groups.append(group)
    selected_count = sum(len(g["occurrences"]) for g in groups)
    input_fingerprint = digest({"scope": {"only_file": scope.only_file}, "screening_enabled": screening_enabled,
        "exclude_punctuation_only": exclude_punctuation_only, "target_language": config["project"]["target_language"],
        "groups": [{key: value for key, value in group.items() if key not in {"screening_input_tokens", "comparison_input_tokens", "over_budget"}} for group in groups],
        "evidence": evidence, "terms": terms, "terms_revision": terms_revision})
    fingerprint = repetition_execution_fingerprint(config, prompt, screen_content, preset, input_fingerprint)
    plan = {"screening_enabled": screening_enabled, "exclude_punctuation_only": exclude_punctuation_only, "excluded_punctuation_groups": excluded_punctuation_groups, "config": config, "prompt": prompt, "language": language, "screen": screen, "terms": terms, "terms_revision": terms_revision, "warnings": warnings, "decision_preset": preset, "groups": groups, "evidence": evidence, "sources": sources,
            "selected": selected_count, "fingerprint": fingerprint, "input_fingerprint": input_fingerprint}
    eligible = [g for g in groups if not g["over_budget"]]
    batches = comparison_batches(eligible, plan)
    plan["estimated_requests"] = len(batches) + (len(eligible) if screening_enabled else 0)
    plan["estimated_input_tokens"] = sum(estimate for _, estimate in batches) + sum(g["screening_input_tokens"] for g in eligible)
    return plan


def segment_signature(segment: dict[str, Any]) -> str:
    return digest([segment["source"], segment.get("model_source"), segment["file_id"],
                   segment["part_id"], segment["line_index"]])


def screening_payload(group: dict[str, Any]) -> dict[str, Any]:
    counts = [0] * len(group["translations"])
    for occurrence in group["occurrences"]:
        counts[occurrence["variant"]] += 1
    return {"source": group["source"], "translations": group["translations"], "counts": counts}


def comparison_payload(groups: list[dict[str, Any]], evidence: dict[str, Any], sources: dict[str, str], language: str, terms: dict[str, Any]) -> dict[str, Any]:
    occurrences = [o for group in groups for o in group["occurrences"]]
    context_ids = dict.fromkeys(n for o in occurrences for n in (*o["previous"], *o["next"]))
    source_keys = dict.fromkeys(evidence[sid]["source_key"] for sid in context_ids)
    source_ids = {key: str(i) for i, key in enumerate(source_keys, 1)}
    short = {sid: source_ids[evidence[sid]["source_key"]] for sid in context_ids}
    used_terms = dict.fromkeys(ref["id"] for o in occurrences for ref in o["terms"])
    short_terms = {tid: str(i) for i, tid in enumerate(used_terms, 1)}
    return {"target_language": language,
            "files": {key: name for group in groups for key, name in group["files"].items()},
            "terms": {short_terms[tid]: terms[tid] for tid in used_terms},
            "context": {source_ids[key]: sources[key] for key in source_keys},
            "repetitions": [{"id": group["id"], "source": group["source"], "translations": group["translations"],
                "occurrences": [{"id": o["id"], "file": o["file_index"], "part": o["part_id"],
                    "position": o["line_index"], "variant": o["variant"], "terms": [{**ref, "id": short_terms[ref["id"]]} for ref in o["terms"]],
                    "previous": [short[sid] for sid in o["previous"]], "next": [short[sid] for sid in o["next"]]}
                    for o in group["occurrences"]]} for group in groups]}


def comparison_messages(groups: list[dict[str, Any]], plan: dict[str, Any]) -> list[dict[str, str]]:
    return render_messages(plan["prompt"], comparison_payload(groups, plan["evidence"], plan["sources"],
        plan["config"]["project"]["target_language"], plan["terms"]))


def comparison_batches(groups: list[dict[str, Any]], plan: dict[str, Any]) -> list[tuple[list[dict[str, Any]], int]]:
    config = plan["config"]
    limits = [config["execution"]["target_chunk_input_tokens"],
        config["llm"]["context_window_tokens"] - config["llm"]["context_safety_margin_tokens"]]
    if config["execution"]["input_tokens_per_minute"] > 0:
        limits.append(config["execution"]["input_tokens_per_minute"])
    target = min(limits)
    factor = config["execution"]["token_safety_factor"]
    batches = []
    current = []
    estimate = 0
    for group in groups:
        candidate = [*current, group]
        candidate_estimate = estimate_messages(comparison_messages(candidate, plan), factor)
        if current and candidate_estimate > target:
            batches.append((current, estimate))
            current = [group]
            estimate = group["comparison_input_tokens"]
        else:
            current, estimate = candidate, candidate_estimate
    if current:
        batches.append((current, estimate))
    return batches


def parse_comparisons(content: str, groups: list[dict[str, Any]]) -> tuple[dict[str, Any], str | None]:
    expected = {g["id"] for g in groups}
    try:
        value = json.loads(content)
        if not isinstance(value, dict) or set(value) != {"repetitions"} or not isinstance(value["repetitions"], list):
            raise ValueError("repetitions 字段无效")
        entries: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in value["repetitions"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item["id"] not in expected:
                raise ValueError("未知或无效的重复组 ID")
            entries[item["id"]].append(item)
    except (ValueError, TypeError) as exc:
        return {}, f"重复译文审查响应无效：{exc}"
    results, errors = {}, []
    for group in groups:
        items = entries.get(group["id"], [])
        if len(items) != 1:
            errors.append(f"组 {group['id']} 缺失或重复")
            continue
        try:
            results[group["id"]] = parse_comparison({k: v for k, v in items[0].items() if k != "id"}, group)
        except ExternalError as exc:
            errors.append(f"组 {group['id']}：{exc}")
    return results, "; ".join(errors[:3]) if errors else None


def parse_comparison(value: Any, group: dict[str, Any]) -> dict[str, Any]:
    try:
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


async def run_repetition(project: Path, scope: Scope, *, prompt_language: str | None = None, exclude_punctuation_only: bool = True, screening_enabled: bool = False, replace_draft: bool = False, run_action: str | None = None,
                         http_client: httpx.AsyncClient | None = None,
                         limiter: SlidingWindowLimiter | KeyPool | None = None,
                         on_progress: Callable[[int, int, int], None] | None = None,
                         on_usage: Callable[[dict[str, Any] | None], None] | None = None,
                         prepared_plan: dict[str, Any] | None = None) -> dict[str, Any]:
    old = read_repetition_run(project, ("pending",))
    if old and not replace_draft:
        raise UsageError("已有待审核草案，必须明确选择替换")
    plan = prepared_plan if prepared_plan is not None else repetition_plan(project, scope, prompt_language=prompt_language, exclude_punctuation_only=exclude_punctuation_only, screening_enabled=screening_enabled)
    config = plan["config"]
    prompt, screen = plan["prompt"], plan["screen"]
    total = plan["selected"]
    metadata = read_json(project, project / "project.json")
    running = repetition_resume(project, plan)
    if running and run_action == "resume" and not running["resume_compatible"]:
        raise UsageError(running["resume_incompatibility_reason"])
    resume_id, run_warnings = choose_running_run(project, STAGE, action=run_action, dry_run=False, interactive=False)
    if resume_id:
        checkpoints = load_checkpoints(project / "runs" / resume_id, plan)
        reused = sum(len(g["occurrences"]) for g in plan["groups"] if "result" in checkpoints.get(g["id"], {}))
        run_id, run_dir, continuation_index = continue_run(project, resume_id, config=config, stage=STAGE, fingerprint=plan["fingerprint"],
            prompt=prompt, scope=scope, selected_count=total, requested_count=total - reused, reused_count=reused)
    else:
        continuation_index = 0
        checkpoints = {}
        run_id, run_dir = create_run(project, config=config, stage=STAGE, fingerprint=plan["fingerprint"],
            prompt=prompt, selected_count=total, requested_count=total, reused_count=0,
            details={"screening_enabled": plan["screening_enabled"], "exclude_punctuation_only": plan["exclude_punctuation_only"], "excluded_punctuation_groups": plan["excluded_punctuation_groups"], "prompt_language": plan["language"], "terms_revision": plan["terms_revision"], "repetition_checkpoint": 2, "repetition_protocol": PROTOCOL_VERSION, "repetition_input_fingerprint": plan["input_fingerprint"], "usage_invocation_count": 0, "draft_status": "generating", "scope": {"only_file": scope.only_file}, "choices": {}})
        atomic_write_text(run_dir / "repetition_terms.json", json.dumps({
            "terms_revision": plan["terms_revision"], "terms": plan["terms"],
            "groups": {g["id"]: {o["id"]: o["terms"] for o in g["occurrences"]} for g in plan["groups"]},
        }, ensure_ascii=False, separators=(",", ":")) + "\n")
    active_llm: LLMClient | None = None
    usage_recorded = False
    completed = sum(len(g["occurrences"]) for g in plan["groups"] if "result" in checkpoints.get(g["id"], {}))
    if on_progress:
        on_progress(completed, 0, total)
    decision = DecisionClient(plan["decision_preset"], retry=config["retry"], project_id=metadata["project_id"],
                              run_id=run_id, stage=STAGE, debug_directory=run_dir / "debug") if plan["screening_enabled"] else None
    limiter = limiter or SlidingWindowLimiter(config["execution"]["requests_per_minute"], config["execution"]["input_tokens_per_minute"])
    def finalize_execution(status, extra_warnings=()):
        nonlocal usage_recorded
        manifest = read_json(project, run_dir / "manifest.json")
        warnings = list(dict.fromkeys([*manifest.get("warnings", []), *run_warnings, *plan["warnings"],
            *(active_llm.warnings if active_llm else []), *extra_warnings]))
        parts = []
        if active_llm is not None and active_llm.send_count:
            parts.append(active_llm.usage_summary() or unavailable_usage())
        if decision is not None and decision.records:
            parts.append(decision.usage_summary() or unavailable_usage())
        usage = combine_usage(*parts) if len(parts) == 2 else parts[0] if parts else unavailable_usage()
        key_audit = active_llm.key_audit_summary(execution_index=continuation_index + 1) if not usage_recorded and active_llm is not None and active_llm._api_keys is not None and active_llm.send_count else None
        usage = finalize_run(project, run_dir, status=status, completed=completed, failed=0,
            usage=usage, usage_invoked=bool(parts) and not usage_recorded, warnings=warnings, key_audit=key_audit)
        usage_recorded = True
        return usage, warnings

    try:
        async with httpx.AsyncClient(proxy=plan["decision_preset"]["proxy_url"] or None) if decision and http_client is None else _borrow_client(http_client) as decision_http:
            if decision:
                decision.http_client = decision_http
            async with LLMClient(config, limiter, run_dir=run_dir, project_id=metadata["project_id"],
                                 run_id=run_id, stage=STAGE, client=http_client, on_usage=on_usage) as llm:
                active_llm = llm
                groups = [dict(group) for group in plan["groups"]]
                def save_result(group, result, *, comparison=False):
                    nonlocal completed
                    saved = {"screening": group["screening"], "confidence": group.get("confidence"),
                        "screening_execution_index": group.get("screening_execution_index"), "result": result,
                        "comparison_execution_index": continuation_index + 1 if comparison else None}
                    atomic_write_json(checkpoint_path(run_dir, group["id"]), saved)
                    checkpoints[group["id"]] = saved
                    group.update(result)
                    completed += len(group["occurrences"])
                    if on_progress:
                        on_progress(completed, 0, total)

                async def screen_group(group):
                    saved = dict(checkpoints.get(group["id"], {}))
                    if "result" in saved:
                        group.update(screening=saved["screening"], confidence=saved.get("confidence"), over_budget=False, **saved["result"])
                        return None
                    group["over_budget"] = group_over_budget(group, plan, saved)
                    if group["over_budget"]:
                        group.update(screening=saved.get("screening", "over_budget"), confidence=saved.get("confidence"), screening_execution_index=saved.get("screening_execution_index"))
                        save_result(group, {"proposals": [], "keep": [], "uncertain": [o["id"] for o in group["occurrences"]]})
                        return None
                    if "screening" not in saved:
                        answer = (await decision.choose(screening_payload(group), [screen]))["value"] if decision else None
                        saved.update(screening=("refusal" if answer.refused else answer.choice) if answer else "disabled",
                                     confidence=answer.confidence if answer else None, screening_execution_index=continuation_index + 1 if answer else None)
                        if decision and saved["screening"] != "skip":
                            atomic_write_json(checkpoint_path(run_dir, group["id"]), saved)
                            checkpoints[group["id"]] = dict(saved)
                    group.update(screening=saved["screening"], confidence=saved.get("confidence"), screening_execution_index=saved.get("screening_execution_index"))
                    if saved["screening"] == "skip":
                        save_result(group, {"proposals": [], "keep": [o["id"] for o in group["occurrences"]], "uncertain": []})
                        return None
                    return group

                async def compare_batch(batch, parent_request_id=None):
                    pending, estimated = batch
                    for attempt in range(config["retry"]["format_max_attempts"] + 1):
                        messages = comparison_messages(pending, plan)
                        if attempt:
                            estimated = estimate_messages(messages, config["execution"]["token_safety_factor"])
                        try:
                            response, request_id = await llm.chat(messages=messages, temperature=config["llm"]["temperature_translation"],
                                estimated_input_tokens=estimated, parent_request_id=parent_request_id)
                        except ContextLengthError as exc:
                            if len(pending) == 1:
                                exc.args = (f"{exc}；单个完整重复组不能继续拆分",)
                                raise
                            llm.logger.warning("context split parent_request=%s groups=%d", exc.request_id, len(pending))
                            diagnostics = current_diagnostics()
                            if diagnostics is not None:
                                diagnostics.retried(exc.request_id)
                            midpoint = len(pending) // 2
                            for child in (pending[:midpoint], pending[midpoint:]):
                                child_estimate = estimate_messages(comparison_messages(child, plan), config["execution"]["token_safety_factor"])
                                with empty_response_split_scope(exc):
                                    await compare_batch((child, child_estimate), exc.request_id)
                            return
                        results, error = parse_comparisons(response.content, pending)
                        for group in pending:
                            if group["id"] in results:
                                save_result(group, results[group["id"]], comparison=True)
                        pending = [group for group in pending if group["id"] not in results]
                        if not pending:
                            return
                        diagnostics = current_diagnostics()
                        if diagnostics is not None:
                            diagnostics.fail_request(request_id, error)
                        if attempt >= config["retry"]["format_max_attempts"]:
                            failure = ExternalError(error)
                            failure.params = {"reason": "format_error", "request_id": request_id}
                            raise failure
                        llm.logger.warning("format correction request=%s attempt=%d error=%s", request_id, attempt + 1, error)
                        if diagnostics is not None:
                            diagnostics.retried(request_id)
                        parent_request_id = request_id

                screened = await run_bounded(groups, screen_group, max_parallel=config["execution"]["max_parallel"])
                batches = comparison_batches([group for group in screened if group is not None], plan)
                await run_bounded(batches, compare_batch, max_parallel=config["execution"]["max_parallel"])
        for group in groups:
            group.pop("screening_execution_index", None)
            group.pop("screening_input_tokens")
            group.pop("comparison_input_tokens")
            for occurrence in group["occurrences"]:
                occurrence.pop("terms")
        draft = {"run_id": run_id, "groups": groups, "evidence": plan["evidence"], "sources": plan["sources"], "scope": {"only_file": scope.only_file}, "created_at": utc_now()}
        atomic_write_text(draft_path(project, run_id), json.dumps(draft, ensure_ascii=False, separators=(",", ":")) + "\n")
        usage, warnings = finalize_execution("completed")
        manifest = read_json(project, run_dir / "manifest.json")
        manifest.pop("last_interruption", None)
        manifest["draft_status"] = "pending"
        manifests = [manifest]
        if old:
            old["draft_status"] = "replaced"
            manifests.append(old)
        warnings = list(dict.fromkeys([*warnings, *write_repetition_state(project, records=[], manifests=manifests)]))
        return {"excluded_punctuation_groups": plan["excluded_punctuation_groups"], "stage": STAGE, "run_id": run_id, "selected": total, "completed": total,
                "failed": 0, "pending": 0, "usage": usage, "warnings": warnings,
                "proposal_count": sum(len(g["proposals"]) for g in groups)}
    except BaseException as exc:
        finalize_execution("running", ["执行已取消，可续作" if isinstance(exc, asyncio.CancelledError) else str(exc)])
        manifest = read_json(project, run_dir / "manifest.json")
        manifest.update(completed_at=None, last_interruption={"at": utc_now(), "error_code": getattr(exc, "code", "cancelled" if isinstance(exc, asyncio.CancelledError) else "internal_error"),
            "reason": "cancelled" if isinstance(exc, asyncio.CancelledError) else getattr(exc, "params", {}).get("reason", str(exc)),
            "completed_steps": sum((int("screening" in c) if plan["screening_enabled"] else 0) + int("result" in c) for c in checkpoints.values()),
            "total_steps": len(plan["groups"]) * (2 if plan["screening_enabled"] else 1)})
        request_id = getattr(exc, "request_id", None) or getattr(exc, "params", {}).get("request_id")
        if request_id:
            manifest["last_interruption"]["request_id"] = request_id
        write_json(project, run_dir / "manifest.json", manifest)
        raise


@asynccontextmanager
async def _borrow_client(client):
    yield client
