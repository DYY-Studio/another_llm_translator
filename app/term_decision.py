from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx

from .config import load_project_config
from .documents import aozora_match_views
from .errors import RequestSizeError, StorageError, UsageError
from .execution import (
    LLMClient,
    Scope,
    SlidingWindowLimiter,
    combine_usage,
    continue_run,
    create_run,
    estimate_messages,
    finalize_run,
    full_prompt,
    parse_jsonl_document,
    render_messages,
    unavailable_usage,
)
from .i18n import SUPPORTED_LANGUAGES, resolve_language
from .project import prompt_file
from .sqlite_storage import (
    atomic_write_json,
    list_runs,
    read_json,
    read_segment_sources,
    record_header,
    utc_now,
    write_json,
    write_terminology_decision_state,
)
from .stages import (
    build_term_library_rows,
    load_terms,
    normalize_term,
    term_normalization,
)

STAGE = "terminology_decision"
DRAFT_FILE = "terminology_decision_draft.json"
CHECKPOINT_FILE = "terminology_decision_checkpoint.json"
EVIDENCE_SAMPLE_LIMIT = 5
EVIDENCE_SNIPPET_LIMIT = 600
RELATED_ANCHOR_LIMIT = 24
_TOKEN_SPLIT = re.compile(r"[\s・·･._—–\-]+")
_DECISION_ACTIONS = frozenset({"keep", "update", "disable", "needs_review"})
_STATE_FIELDS = (
    "category",
    "description",
    "preferred_translation",
    "aliases",
    "group_primary",
    "disabled",
)

_PHASES = ("adjudication", "consistency")


def _prompt_language(project: Path, requested: str | None) -> str:
    language = requested or resolve_language()
    if language not in SUPPORTED_LANGUAGES:
        language = "zh-CN"
    if not (project / "prompts" / prompt_file(STAGE, language)).is_file():
        language = "zh-CN"
    return language


def _prompt(project: Path, language: str) -> dict[str, str]:
    path = project / "prompts" / prompt_file(STAGE, language)
    try:
        middle = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StorageError(f"无法读取 Prompt：{path.name}: {exc}") from exc
    return {
        phase: full_prompt(STAGE, middle, language, phase=phase)
        for phase in _PHASES
    }


def _prompt_snapshot(prompts: dict[str, str]) -> str:
    return "\n\n".join(
        f"===== terminology_decision/{phase} =====\n{prompts[phase]}"
        for phase in _PHASES
    )


def _term_state(
    term: dict[str, Any], *, disabled: bool | None = None
) -> dict[str, Any]:
    return {
        "normalized": str(term["normalized"]),
        "source": str(term["source"]),
        "category": term.get("category"),
        "description": term.get("description") or None,
        "preferred_translation": term.get("preferred_translation"),
        "aliases": [str(value) for value in term.get("aliases", [])],
        "group_primary": term.get("group_primary"),
        "disabled": bool(term.get("disabled", False)) if disabled is None else disabled,
    }


def _normalized_forms(term: dict[str, Any], spec: Any) -> list[tuple[str, str, str]]:
    values = [("source", str(term["source"]))]
    values.extend(("alias", str(value)) for value in term.get("aliases", []))
    result: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for kind, value in values:
        normalized = normalize_term(value, spec)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append((kind, value, normalized))
    return result


def collect_term_evidence(
    project: Path,
    terms: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Count all source/alias hits in one Segment pass with existing match views."""
    config = config or load_project_config(project)
    spec = term_normalization(config)
    forms_by_term = {
        str(term["normalized"]): _normalized_forms(term, spec) for term in terms
    }
    prefix_index: dict[str, set[str]] = {}
    single_index: dict[str, set[str]] = {}
    for normalized, forms in forms_by_term.items():
        for _, _, form in forms:
            target = single_index if len(form) == 1 else prefix_index
            key = form if len(form) == 1 else form[:2]
            target.setdefault(key, set()).add(normalized)
    evidence = {
        normalized: {
            "hit_count": 0,
            "source_hit_count": 0,
            "alias_hit_counts": {
                value: 0 for kind, value, _ in forms if kind == "alias"
            },
            "samples": [],
        }
        for normalized, forms in forms_by_term.items()
    }
    sample_files: dict[str, set[str]] = {key: set() for key in evidence}
    for segment in read_segment_sources(project):
        source = str(segment["source"])
        views = [normalize_term(value, spec) for value in aozora_match_views(source)]
        candidates: set[str] = set()
        for view in views:
            candidates.update(
                normalized
                for index in range(max(0, len(view) - 1))
                for normalized in prefix_index.get(view[index : index + 2], ())
            )
            candidates.update(
                normalized
                for character in view
                for normalized in single_index.get(character, ())
            )
        for normalized in candidates:
            matched: list[tuple[str, str]] = []
            for kind, original, form in forms_by_term[normalized]:
                if any(form in view for view in views):
                    matched.append((kind, original))
            if not matched:
                continue
            item = evidence[normalized]
            item["hit_count"] += 1
            if any(kind == "source" for kind, _ in matched):
                item["source_hit_count"] += 1
            for kind, original in matched:
                if kind == "alias":
                    item["alias_hit_counts"][original] += 1
            samples = item["samples"]
            if len(samples) < EVIDENCE_SAMPLE_LIMIT:
                file_id = str(segment["file_id"])
                sample = {
                    "file_id": file_id,
                    "segment_id": str(segment["segment_id"]),
                    "source": source[:EVIDENCE_SNIPPET_LIMIT],
                }
                if file_id not in sample_files[normalized]:
                    samples.insert(len(sample_files[normalized]), sample)
                    sample_files[normalized].add(file_id)
                else:
                    samples.append(sample)
                del samples[EVIDENCE_SAMPLE_LIMIT:]
    return evidence


def _relation_keys(state: dict[str, Any], spec: Any) -> tuple[str, ...]:
    values = [state["source"], *state.get("aliases", [])]
    keys: set[str] = set()
    for raw in values:
        value = normalize_term(str(raw), spec)
        if not value:
            continue
        keys.add(f"whole:{value}")
        if len(value) >= 2:
            keys.add(f"prefix:{value[:2]}")
            keys.add(f"suffix:{value[-2:]}")
        for token in _TOKEN_SPLIT.split(value):
            if len(token) >= 2:
                keys.add(f"token:{token}")
    preferred = state.get("preferred_translation")
    if preferred:
        value = normalize_term(str(preferred), spec)
        if value:
            keys.add(f"translation:{value}")
            if len(value) >= 2:
                keys.add(f"translation-prefix:{value[:2]}")
                keys.add(f"translation-suffix:{value[-2:]}")
    primary = state.get("group_primary")
    if primary:
        keys.add(f"group:{primary}")
    return tuple(sorted(keys))


def _ordered_states(states: Iterable[dict[str, Any]], spec: Any) -> list[dict[str, Any]]:
    return sorted(
        states,
        key=lambda state: (
            _relation_keys(state, spec)[:1],
            normalize_term(str(state["source"]), spec)[::-1],
            str(state["normalized"]),
        ),
    )


def _payload_term(
    state: dict[str, Any], evidence: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    return {
        key: deepcopy(state[key])
        for key in (
            "normalized",
            "source",
            "category",
            "description",
            "preferred_translation",
            "aliases",
            "group_primary",
        )
    } | {"evidence": deepcopy(evidence[state["normalized"]])}


def _compact_anchor_evidence(
    evidence: dict[str, dict[str, Any]],
    anchors: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return payload evidence with anchor samples removed when requested."""
    if not any(item.get("_compact_evidence") for item in anchors):
        return evidence
    compacted = deepcopy(evidence)
    for anchor in anchors:
        normalized = str(anchor["normalized"])
        value = compacted.get(normalized)
        if isinstance(value, dict):
            value["samples"] = []
    return compacted


def _compact_anchors(anchors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {**deepcopy(anchor), "_compact_evidence": True}
        for anchor in anchors
    ]


def _related_anchors(
    focus: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    spec: Any,
) -> list[dict[str, Any]]:
    focus_ids = {str(item["normalized"]) for item in focus}
    focus_keys = {key for item in focus for key in _relation_keys(item, spec)}
    focus_forms = [
        normalize_term(str(value), spec)
        for item in focus
        for value in [item["source"], *item.get("aliases", [])]
    ]

    def related_to_focus(item: dict[str, Any]) -> bool:
        if focus_keys.intersection(_relation_keys(item, spec)):
            return True
        item_forms = [
            normalize_term(str(value), spec)
            for value in [item["source"], *item.get("aliases", [])]
        ]
        return any(
            min(len(left), len(right)) >= 2 and (left in right or right in left)
            for left in focus_forms
            for right in item_forms
        )

    related = [
        item
        for item in candidates
        if str(item["normalized"]) not in focus_ids and related_to_focus(item)
    ]
    return related[:RELATED_ANCHOR_LIMIT]


def _request_limits(config: dict[str, Any]) -> tuple[int, int, int, int]:
    soft_target = int(config["chunking"]["target_chunk_input_tokens"])
    context_limit = (
        int(config["llm"]["context_window_tokens"])
        - int(config["llm"]["context_safety_margin_tokens"])
    )
    itpm_limit = int(config["execution"]["input_tokens_per_minute"])
    hard_limit = min(
        context_limit,
        itpm_limit if itpm_limit > 0 else 2**63 - 1,
    )
    return soft_target, hard_limit, context_limit, itpm_limit


def _make_payload(
    *,
    phase: str,
    target_language: str,
    focus: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
    evidence: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "phase": phase,
        "target_language": target_language,
        "terms": [_payload_term(item, evidence) for item in focus],
        "anchors": [_payload_term(item, evidence) for item in anchors],
    }


def _pack_batches(
    states: list[dict[str, Any]],
    *,
    phase: str,
    target_language: str,
    anchors: list[dict[str, Any]],
    evidence: dict[str, dict[str, Any]],
    prompt: str,
    config: dict[str, Any],
    spec: Any,
) -> tuple[list[tuple[list[dict[str, Any]], list[dict[str, Any]]]], int]:
    soft_target, hard_limit, context_limit, itpm_limit = _request_limits(config)
    decision_config = config["terminology_decision"]
    allow_soft_overflow = bool(
        decision_config["allow_soft_target_overflow"]
    )
    anchor_overflow_mode = str(decision_config["anchor_overflow_mode"])
    token_factor = float(config["execution"]["token_safety_factor"])

    def estimate_for(
        focus: list[dict[str, Any]],
        anchor_list: list[dict[str, Any]],
    ) -> int:
        payload = _make_payload(
            phase=phase,
            target_language=target_language,
            focus=focus,
            anchors=anchor_list,
            evidence=_compact_anchor_evidence(evidence, anchor_list),
        )
        return estimate_messages(
            render_messages(prompt, payload),
            token_factor,
        )

    def overflow_reason() -> str:
        if hard_limit == context_limit:
            return "context"
        if hard_limit == itpm_limit:
            return "itpm"
        return "context"

    def fail_size(
        state: dict[str, Any],
        estimate: int,
        *,
        limit: int,
        reason: str,
        detail: str,
    ) -> None:
        raise RequestSizeError(
            f"术语 {state['source']} 的自动决策请求估算 {estimate} tokens，"
            f"限制 {limit} tokens；{detail}",
            reason=reason,
        )

    def prepare_single(
        focus: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int]:
        state = focus[0]
        selected_anchors = _related_anchors(focus, anchors, spec)
        had_anchors = bool(selected_anchors)
        estimate = estimate_for(focus, selected_anchors)
        if estimate > hard_limit:
            if anchor_overflow_mode == "trim":
                while selected_anchors and estimate > hard_limit:
                    selected_anchors = selected_anchors[:-1]
                    estimate = estimate_for(focus, selected_anchors)
            elif anchor_overflow_mode == "compact":
                selected_anchors = _compact_anchors(selected_anchors)
                estimate = estimate_for(focus, selected_anchors)
            if estimate > hard_limit:
                detail = (
                    "无 Anchors 时完整术语及证据仍超过硬限制"
                    if not had_anchors
                    else "完整术语证据和 Anchors 仍超过硬限制"
                )
                fail_size(
                    state,
                    estimate,
                    limit=hard_limit,
                    reason=overflow_reason(),
                    detail=f"{detail}，Anchor 超限策略为 {anchor_overflow_mode}",
                )
        if estimate > soft_target and not allow_soft_overflow:
            fail_size(
                state,
                estimate,
                limit=soft_target,
                reason="context",
                detail="超过软目标且配置禁止继续执行",
            )
        return selected_anchors, estimate

    batches: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []
    total = 0
    current: list[dict[str, Any]] = []
    batch_limit = min(soft_target, hard_limit)
    for state in _ordered_states(states, spec):
        proposed = [*current, state]
        related = _related_anchors(proposed, anchors, spec)
        estimate = estimate_for(proposed, related)
        if current and estimate > batch_limit:
            previous_anchors, previous_estimate = prepare_single(current)
            batches.append((current, previous_anchors))
            total += previous_estimate
            current = [state]
            continue
        if not current and estimate > batch_limit:
            selected_anchors, single_estimate = prepare_single([state])
            batches.append(([state], selected_anchors))
            total += single_estimate
            current = []
            continue
        current = proposed
    if current:
        selected_anchors, final_estimate = prepare_single(current)
        total += final_estimate
        batches.append((current, selected_anchors))
    return batches, total


def _nullable_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise UsageError(f"术语决策 {field} 必须是字符串或 null")
    value = value.strip()
    return value or None


def _parse_decisions(
    content: str,
    focus: list[dict[str, Any]],
    *,
    all_forms: set[str],
    known_terms: set[str],
    read_only_terms: set[str],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    document = parse_jsonl_document(content, record_type="decision")
    errors = list(document.errors)
    expected = {str(item["normalized"]): item for item in focus}
    decisions: dict[str, dict[str, Any]] = {}
    ignored_read_only: list[str] = []
    for value in document.records:
        normalized = value.get("normalized")
        action = value.get("action")
        if not isinstance(normalized, str) or normalized not in expected:
            if isinstance(normalized, str) and normalized in read_only_terms:
                ignored_read_only.append(normalized)
                continue
            errors.append(f"未知术语决策 normalized：{normalized}")
            continue
        if normalized in decisions:
            errors.append(f"术语决策重复：{normalized}")
            continue
        if action not in _DECISION_ACTIONS:
            errors.append(f"术语决策 action 无效：{normalized}")
            continue
        reason = value.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"术语决策 reason 必须是非空字符串：{normalized}")
            continue
        simple_keys = {"type", "normalized", "action", "reason"}
        if action != "update":
            if set(value) != simple_keys:
                errors.append(f"{action} 决策包含额外字段：{normalized}")
                continue
            decisions[normalized] = {
                "action": action,
                "reason": reason.strip(),
            }
            continue
        update_keys = simple_keys | {
            "category",
            "description",
            "preferred_translation",
            "aliases",
            "group_primary",
        }
        if set(value) != update_keys:
            errors.append(f"update 决策字段不完整：{normalized}")
            continue
        aliases = value.get("aliases")
        if not isinstance(aliases, list) or not all(
            isinstance(alias, str) and alias.strip() for alias in aliases
        ):
            errors.append(f"术语决策 aliases 无效：{normalized}")
            continue
        aliases = list(dict.fromkeys(alias.strip() for alias in aliases))
        if any(alias not in all_forms for alias in aliases):
            errors.append(f"术语决策发明了源文 alias：{normalized}")
            continue
        group_primary = value.get("group_primary")
        if group_primary is not None and (
            not isinstance(group_primary, str) or group_primary not in known_terms
        ):
            errors.append(f"术语决策 group_primary 无效：{normalized}")
            continue
        original_description = expected[normalized].get("description") or None
        description = _nullable_string(value.get("description"), "description")
        if description not in {None, original_description}:
            errors.append(f"术语决策不得新写 description：{normalized}")
            continue
        decisions[normalized] = {
            "action": action,
            "reason": reason.strip(),
            "after": {
                **_term_state(expected[normalized]),
                "category": _nullable_string(value.get("category"), "category"),
                "description": description,
                "preferred_translation": _nullable_string(
                    value.get("preferred_translation"), "preferred_translation"
                ),
                "aliases": aliases,
                "group_primary": group_primary,
                "disabled": False,
            },
        }
    missing = sorted(set(expected) - set(decisions))
    if missing:
        errors.append(f"术语决策缺少记录：{', '.join(missing[:10])}")
    if not document.complete:
        errors.append("术语决策响应缺少 end")
    if errors:
        raise UsageError("；".join(errors[:10]))
    return decisions, list(dict.fromkeys(ignored_read_only))


async def _request_batch(
    llm: LLMClient,
    *,
    focus: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
    phase: str,
    prompt: str,
    config: dict[str, Any],
    evidence: dict[str, dict[str, Any]],
    all_forms: set[str],
    known_terms: set[str],
    read_only_terms: set[str],
    prompt_language: str,
) -> dict[str, dict[str, Any]]:
    errors: list[str] = []
    parent_request_id: str | None = None
    for attempt in range(int(config["retry"]["format_max_attempts"]) + 1):
        payload = _make_payload(
            phase=phase,
            target_language=str(config["project"]["target_language"]),
            focus=focus,
            anchors=anchors,
            evidence=_compact_anchor_evidence(evidence, anchors),
        )
        if attempt:
            allowed = json.dumps(
                [str(item["normalized"]) for item in focus],
                ensure_ascii=False,
            )
            if prompt_language == "en":
                payload["format_correction"] = (
                    "The previous response violated the terminology decision "
                    "protocol and could not be accepted. "
                    f"The only allowed decision normalized values are {allowed}. "
                    "Output exactly one decision for each terms[] item, no decision "
                    "for any anchors[] item, and then the exact end record."
                )
            else:
                payload["format_correction"] = (
                    "上次响应不符合术语决策协议："
                    f"{errors[0] if errors else '响应无效'}。"
                    f"本批唯一允许输出的 normalized 为 {allowed}。"
                    "必须为每个 terms[] 项各输出一条 decision；不得为 anchors[] 任一项输出"
                    " decision；最后输出精确的 end 记录。"
                )
        messages = render_messages(prompt, payload)
        request_id = f"REQ-{uuid.uuid4().hex[:12].upper()}"
        estimate = estimate_messages(
            messages, float(config["execution"]["token_safety_factor"])
        )
        response, _ = await llm.chat(
            messages=messages,
            temperature=float(config["llm"]["temperature_terminology_decision"]),
            estimated_input_tokens=estimate,
            request_id=request_id,
            parent_request_id=parent_request_id,
        )
        try:
            decisions, ignored_read_only = _parse_decisions(
                response.content,
                focus,
                all_forms=all_forms,
                known_terms=known_terms,
                read_only_terms=read_only_terms,
            )
            if ignored_read_only:
                llm.logger.warning(
                    "ignored read-only terminology decisions request=%s count=%d normalized=%s",
                    request_id,
                    len(ignored_read_only),
                    ",".join(ignored_read_only[:10]),
                )
            return decisions
        except UsageError as exc:
            errors = [str(exc)]
            parent_request_id = request_id
    raise UsageError("术语决策格式修正重试耗尽：" + "；".join(errors[:5]))


async def _dispatch_batches(
    batches: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]],
    worker: Callable[
        [list[dict[str, Any]], list[dict[str, Any]]],
        Awaitable[None],
    ],
    *,
    max_parallel: int,
) -> None:
    iterator = iter(batches)
    pending: set[asyncio.Task[None]] = set()

    def fill() -> None:
        while len(pending) < max_parallel:
            try:
                focus, anchors = next(iterator)
            except StopIteration:
                return
            pending.add(asyncio.create_task(worker(focus, anchors)))

    fill()
    try:
        while pending:
            done, still_pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            pending = still_pending
            try:
                for task in done:
                    task.result()
            except BaseException:
                pending.update(done)
                raise
            fill()
    except BaseException:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        raise


def _checkpoint_path(project: Path, run_id: str) -> Path:
    return project / "runs" / run_id / CHECKPOINT_FILE


def decision_checkpoint_progress(project: Path, run_id: str) -> int:
    path = _checkpoint_path(project, run_id)
    if not path.is_file():
        return 0
    checkpoint = _read_checkpoint_file(path)
    phases = checkpoint.get("phases")
    if not isinstance(phases, dict):
        raise StorageError("术语决策检查点 phases 无效")
    completed = 0
    for phase in _PHASES:
        records = phases.get(phase)
        if not isinstance(records, dict):
            raise StorageError(f"术语决策检查点 {phase} 无效")
        completed += len(records)
    return completed


def _read_checkpoint_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageError(f"无法读取术语决策检查点：{exc}") from exc
    if not isinstance(value, dict):
        raise StorageError("术语决策检查点必须是 JSON 对象")
    return value


def _new_checkpoint(
    project_id: str, run_id: str, revision: int
) -> dict[str, Any]:
    return record_header(
        "terminology_decision_checkpoint",
        project_id,
        record_id=f"TERMINOLOGY-DECISION-CHECKPOINT-{run_id}",
        run_id=run_id,
        source_terms_revision=revision,
        phases={phase: {} for phase in _PHASES},
    )


def _load_checkpoint(
    project: Path,
    run_id: str,
    *,
    project_id: str,
    revision: int,
    known_terms: set[str],
) -> dict[str, Any]:
    path = _checkpoint_path(project, run_id)
    if not path.is_file():
        checkpoint = _new_checkpoint(project_id, run_id, revision)
        atomic_write_json(path, checkpoint)
        return checkpoint
    checkpoint = _read_checkpoint_file(path)
    if (
        checkpoint.get("record_type") != "terminology_decision_checkpoint"
        or checkpoint.get("project_id") != project_id
        or checkpoint.get("run_id") != run_id
        or checkpoint.get("source_terms_revision") != revision
    ):
        raise UsageError("术语决策检查点与当前 Run 或术语 revision 不一致")
    phases = checkpoint.get("phases")
    if not isinstance(phases, dict):
        raise StorageError("术语决策检查点 phases 无效")
    for phase in _PHASES:
        records = phases.get(phase)
        if not isinstance(records, dict):
            raise StorageError(f"术语决策检查点 {phase} 无效")
        unknown = set(map(str, records)) - known_terms
        if unknown:
            raise StorageError(
                "术语决策检查点包含未知术语：" + ", ".join(sorted(unknown)[:10])
            )
        if any(
            not isinstance(value, dict)
            or not isinstance(value.get("decision"), dict)
            or not isinstance(value.get("decision_fingerprint"), str)
            or not isinstance(value.get("model_fingerprint"), str)
            or not isinstance(value.get("prompt_fingerprint"), str)
            for value in records.values()
        ):
            raise StorageError(f"术语决策检查点 {phase} 记录无效")
    return checkpoint


def _checkpoint_decisions(
    checkpoint: dict[str, Any], phase: str
) -> dict[str, dict[str, Any]]:
    return {
        str(normalized): deepcopy(record["decision"])
        for normalized, record in checkpoint["phases"][phase].items()
    }


def _composite_fingerprint(checkpoint: dict[str, Any], field: str) -> str:
    values = {
        str(record[field])
        for phase in _PHASES
        for record in checkpoint["phases"][phase].values()
    }
    if not values:
        raise StorageError("术语决策检查点缺少批次指纹")
    if len(values) == 1:
        return values.pop()
    encoded = json.dumps(sorted(values), separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _apply_tentative(
    states: dict[str, dict[str, Any]], decisions: dict[str, dict[str, Any]]
) -> None:
    for normalized, decision in decisions.items():
        if decision["action"] == "update":
            states[normalized] = deepcopy(decision["after"])
        elif decision["action"] == "disable":
            states[normalized] = {**states[normalized], "disabled": True}


def _proposal_id(values: list[dict[str, Any]]) -> str:
    encoded = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "TDP-" + hashlib.sha256(encoded.encode()).hexdigest()[:16].upper()


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
    fingerprint: str,
    source_library: dict[str, Any],
    source_overrides: dict[str, Any],
    model_fingerprint: str,
    prompt_fingerprint: str,
) -> dict[str, Any]:
    changed = {
        key
        for key in final
        if key not in protected and any(original[key][field] != final[key][field] for field in _STATE_FIELDS)
    }
    parent = {key: key for key in changed}

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for key in changed:
        for primary in (original[key].get("group_primary"), final[key].get("group_primary")):
            if primary in changed:
                union(key, str(primary))
    original_owners: dict[str, set[str]] = {}
    for key, state in original.items():
        for value in [state["source"], *state.get("aliases", [])]:
            original_owners.setdefault(str(value), set()).add(key)
    for key in changed:
        added = set(final[key].get("aliases", [])) - set(
            original[key].get("aliases", [])
        )
        for alias in added:
            for owner in original_owners.get(alias, set()):
                if owner in changed:
                    union(key, owner)
    components: dict[str, list[str]] = {}
    for key in changed:
        components.setdefault(find(key), []).append(key)
    proposals: list[dict[str, Any]] = []
    for keys in sorted(components.values(), key=lambda value: sorted(value)):
        keys.sort()
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
            }
        )
    needs_review = [
        {
            "normalized": key,
            "source": original[key]["source"],
            "reason": decision["reason"],
            "evidence": deepcopy(evidence[key]),
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
        decision_fingerprint=fingerprint,
        model_fingerprint=model_fingerprint,
        prompt_fingerprint=prompt_fingerprint,
        proposals=proposals,
        needs_review=needs_review,
        rejected_proposal_ids=[],
        source_library=source_library,
        source_overrides=source_overrides,
    )


def _validate_final_states(
    project: Path, states: dict[str, dict[str, Any]]
) -> None:
    active = [
        deepcopy(state) for state in states.values() if not state.get("disabled")
    ]
    built = build_term_library_rows(project, active, {})
    built_by_key = {str(item["normalized"]): item for item in built}
    expected_keys = {
        key for key, state in states.items() if not state.get("disabled")
    }
    if set(built_by_key) != expected_keys:
        raise UsageError("术语决策生成了不完整的最终术语集合")
    for normalized in expected_keys:
        expected_primary = states[normalized].get("group_primary")
        if built_by_key[normalized].get("group_primary") != expected_primary:
            raise UsageError(f"术语决策产生未声明的组关系：{normalized}")


def _decision_fingerprint(
    config: dict[str, Any], prompts: dict[str, str], library: dict[str, Any]
) -> str:
    data = {
        "stage": STAGE,
        "rules_version": 2,
        "target_language": config["project"]["target_language"],
        "model": config["llm"]["model"],
        "adapter_hash": config.get("_llm_adapter_hash"),
        "preset_hash": config.get("_llm_preset_hash"),
        "temperature": config["llm"]["temperature_terminology_decision"],
        "prompts": {
            phase: hashlib.sha256(prompts[phase].encode()).hexdigest()
            for phase in _PHASES
        },
        "terms_revision": library["terms_revision"],
        "terminology": config["terminology"],
        "terminology_decision": config["terminology_decision"],
    }
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


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
        draft["rejected_proposal_ids"] = list(
            manifest.get("rejected_proposal_ids", [])
        )
        return draft
    return None


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
    return {"draft": draft, "rollback": rollback}


def decision_plan(project: Path, prompt_language: str | None = None) -> dict[str, Any]:
    library = load_terms(project)
    if library is None or not library.get("terms"):
        raise UsageError("没有已发布术语库可供自动决策")
    config = load_project_config(project, stage=STAGE)
    overrides_document = read_json(
        project, project / "terminology" / "overrides.json"
    )
    protected = {
        str(item["normalized"])
        for item in overrides_document.get("overrides", [])
    }
    states = {
        str(item["normalized"]): _term_state(item)
        for item in library.get("terms", [])
    }
    eligible = [
        state
        for key, state in states.items()
        if key not in protected and not state["disabled"]
    ]
    if not eligible:
        raise UsageError("已发布术语全部受到人工 override 保护")
    evidence = collect_term_evidence(
        project, list(library.get("terms", [])), config
    )
    language = _prompt_language(project, prompt_language)
    prompts = _prompt(project, language)
    spec = term_normalization(config)
    protected_states = [states[key] for key in sorted(protected & set(states))]
    batches, tokens = _pack_batches(
        eligible,
        phase="adjudication",
        target_language=str(config["project"]["target_language"]),
        anchors=protected_states,
        evidence=evidence,
        prompt=prompts["adjudication"],
        config=config,
        spec=spec,
    )
    return {
        "library": library,
        "config": config,
        "overrides_document": overrides_document,
        "protected": protected,
        "states": states,
        "eligible": eligible,
        "evidence": evidence,
        "language": language,
        "prompts": prompts,
        "spec": spec,
        "protected_states": protected_states,
        "phase_one": batches,
        "estimated_requests": len(batches) * 2,
        "estimated_input_tokens": tokens * 2,
    }


async def run_terminology_decision(
    project: Path,
    *,
    dry_run: bool = False,
    replace_draft: bool = False,
    resume_run_id: str | None = None,
    prompt_language: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    on_progress: Callable[[int, int, int], None] | None = None,
    on_usage: Callable[[dict[str, Any] | None], None] | None = None,
) -> dict[str, Any]:
    existing = current_decision_draft(project)
    if existing is not None and not replace_draft:
        raise UsageError("已有待处理术语决策草案；必须明确替换")
    plan = decision_plan(project, prompt_language)
    library = plan["library"]
    config = plan["config"]
    metadata = read_json(project, project / "project.json")
    overrides_document = plan["overrides_document"]
    protected = plan["protected"]
    states = plan["states"]
    eligible = plan["eligible"]
    evidence = plan["evidence"]
    language = plan["language"]
    prompts = plan["prompts"]
    fingerprint = _decision_fingerprint(config, prompts, library)
    model_fingerprint = "sha256:" + hashlib.sha256(
        json.dumps(
            {
                "model": config["llm"]["model"],
                "adapter": config.get("_llm_adapter_hash"),
                "preset": config.get("_llm_preset_hash"),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    prompt_fingerprints = {
        phase: "sha256:" + hashlib.sha256(prompts[phase].encode()).hexdigest()
        for phase in _PHASES
    }
    prompt_snapshot = _prompt_snapshot(prompts)
    spec = plan["spec"]
    protected_states = plan["protected_states"]
    revision = int(library["terms_revision"])
    resumed_steps = 0
    if resume_run_id is not None:
        resume_manifest = read_json(
            project, project / "runs" / resume_run_id / "manifest.json"
        )
        if int(resume_manifest.get("source_terms_revision", -1)) != revision:
            raise UsageError("术语库 revision 已变化，不能续用自动决策 Run")
        resumed_steps = decision_checkpoint_progress(project, resume_run_id)
    if dry_run:
        return {
            "stage": STAGE,
            "dry_run": True,
            "terms_revision": int(library["terms_revision"]),
            "eligible": len(eligible),
            "protected": len(protected_states),
            "estimated_requests": plan["estimated_requests"],
            "estimated_input_tokens": plan["estimated_input_tokens"],
            "resume_run_id": resume_run_id,
            "completed_steps": resumed_steps,
        }
    if resume_run_id is None:
        run_id, run_dir = create_run(
            project,
            config=config,
            stage=STAGE,
            fingerprint=fingerprint,
            prompt=prompt_snapshot,
            selected_count=len(eligible),
            requested_count=len(eligible),
            reused_count=0,
            details={
                "source_terms_revision": revision,
                "decision_status": "generating",
                "rejected_proposal_ids": [],
                "prompt_language": language,
            },
        )
    else:
        run_id, run_dir = continue_run(
            project,
            resume_run_id,
            config=config,
            stage=STAGE,
            fingerprint=fingerprint,
            prompt=prompt_snapshot,
            scope=Scope(),
            selected_count=len(eligible),
            requested_count=len(eligible),
            reused_count=0,
        )
    limiter = SlidingWindowLimiter(
        int(config["execution"]["requests_per_minute"]),
        int(config["execution"]["input_tokens_per_minute"]),
    )
    all_forms = {
        value
        for state in states.values()
        for value in [str(state["source"]), *map(str, state.get("aliases", []))]
    }
    known_terms = set(states)
    eligible_terms = {str(item["normalized"]) for item in eligible}
    checkpoint = _load_checkpoint(
        project,
        run_id,
        project_id=str(metadata["project_id"]),
        revision=revision,
        known_terms=eligible_terms,
    )
    tentative = deepcopy(states)
    decisions = _checkpoint_decisions(checkpoint, "adjudication")
    _apply_tentative(tentative, decisions)
    final_decisions = _checkpoint_decisions(checkpoint, "consistency")
    if final_decisions and len(decisions) != len(eligible):
        raise StorageError("术语决策检查点在第一阶段完成前包含第二阶段结果")
    completed = len(decisions) + len(final_decisions)
    usage: dict[str, Any] | None = None

    def record_resumable_interruption(
        current_usage: dict[str, Any] | None,
    ) -> None:
        manifest = read_json(project, run_dir / "manifest.json")
        previous_usage = manifest.get("usage")
        invocation_count = manifest.get("usage_invocation_count")
        if type(invocation_count) is int and invocation_count > 0:
            current_usage = combine_usage(previous_usage, current_usage)
        manifest.update(
            status="running",
            decision_status="generating",
            completed_segment_count=completed,
            failed_segment_count=0,
            failure_counts={},
            completed_at=None,
            usage=current_usage or unavailable_usage(),
            usage_invocation_count=(
                invocation_count + 1 if type(invocation_count) is int else 1
            ),
        )
        manifest.pop("proposal_count", None)
        manifest.pop("needs_review_count", None)
        write_json(project, run_dir / "manifest.json", manifest)

    try:
        async with LLMClient(
            config,
            limiter,
            run_dir=run_dir,
            project_id=str(metadata["project_id"]),
            run_id=run_id,
            stage=STAGE,
            client=http_client,
            on_usage=on_usage,
        ) as llm:
            total = len(eligible) * 2
            if on_progress:
                on_progress(completed, 0, total)
            remaining_phase_one = [
                item
                for item in eligible
                if str(item["normalized"]) not in decisions
            ]
            phase_one, _ = _pack_batches(
                remaining_phase_one,
                phase="adjudication",
                target_language=str(config["project"]["target_language"]),
                anchors=protected_states,
                evidence=evidence,
                prompt=prompts["adjudication"],
                config=config,
                spec=spec,
            )

            async def adjudicate(
                focus: list[dict[str, Any]], anchors: list[dict[str, Any]]
            ) -> None:
                nonlocal completed
                result = await _request_batch(
                    llm,
                    focus=focus,
                    anchors=anchors,
                    phase="adjudication",
                    prompt=prompts["adjudication"],
                    config=config,
                    evidence=evidence,
                    all_forms=all_forms,
                    known_terms=known_terms,
                    read_only_terms={str(item["normalized"]) for item in anchors},
                    prompt_language=language,
                )
                decisions.update(result)
                records = checkpoint["phases"]["adjudication"]
                for normalized, decision in result.items():
                    records[normalized] = {
                        "decision": deepcopy(decision),
                        "decision_fingerprint": fingerprint,
                        "model_fingerprint": model_fingerprint,
                        "prompt_fingerprint": prompt_fingerprints["adjudication"],
                    }
                atomic_write_json(_checkpoint_path(project, run_id), checkpoint)
                completed += len(focus)
                if on_progress:
                    on_progress(completed, 0, total)

            await _dispatch_batches(
                phase_one,
                adjudicate,
                max_parallel=int(config["execution"]["max_parallel"]),
            )
            tentative = deepcopy(states)
            _apply_tentative(tentative, decisions)
            phase_two_focus = [tentative[item["normalized"]] for item in eligible]
            phase_two_anchors = [
                *protected_states,
                *[tentative[item["normalized"]] for item in eligible],
            ]
            remaining_phase_two = [
                item
                for item in phase_two_focus
                if str(item["normalized"]) not in final_decisions
            ]
            phase_two, _ = _pack_batches(
                remaining_phase_two,
                phase="consistency",
                target_language=str(config["project"]["target_language"]),
                anchors=phase_two_anchors,
                evidence=evidence,
                prompt=prompts["consistency"],
                config=config,
                spec=spec,
            )

            async def review_consistency(
                focus: list[dict[str, Any]], anchors: list[dict[str, Any]]
            ) -> None:
                nonlocal completed
                result = await _request_batch(
                    llm,
                    focus=focus,
                    anchors=anchors,
                    phase="consistency",
                    prompt=prompts["consistency"],
                    config=config,
                    evidence=evidence,
                    all_forms=all_forms,
                    known_terms=known_terms,
                    read_only_terms={str(item["normalized"]) for item in anchors},
                    prompt_language=language,
                )
                final_decisions.update(result)
                records = checkpoint["phases"]["consistency"]
                for normalized, decision in result.items():
                    records[normalized] = {
                        "decision": deepcopy(decision),
                        "decision_fingerprint": fingerprint,
                        "model_fingerprint": model_fingerprint,
                        "prompt_fingerprint": prompt_fingerprints["consistency"],
                    }
                atomic_write_json(_checkpoint_path(project, run_id), checkpoint)
                completed += len(focus)
                if on_progress:
                    on_progress(completed, 0, total)

            await _dispatch_batches(
                phase_two,
                review_consistency,
                max_parallel=int(config["execution"]["max_parallel"]),
            )
            final = deepcopy(tentative)
            _apply_tentative(final, final_decisions)
            for normalized, decision in final_decisions.items():
                if decision["action"] == "needs_review":
                    final[normalized] = deepcopy(states[normalized])
            decisions = final_decisions
            usage = llm.usage_summary()
        _validate_final_states(project, final)
        draft = _build_draft(
            project_id=str(metadata["project_id"]),
            run_id=run_id,
            revision=int(library["terms_revision"]),
            original=states,
            final=final,
            decisions=decisions,
            protected=protected,
            evidence=evidence,
            fingerprint=_composite_fingerprint(checkpoint, "decision_fingerprint"),
            source_library=deepcopy(library),
            source_overrides=deepcopy(overrides_document),
            model_fingerprint=_composite_fingerprint(
                checkpoint, "model_fingerprint"
            ),
            prompt_fingerprint=_composite_fingerprint(
                checkpoint, "prompt_fingerprint"
            ),
        )
        atomic_write_json(_draft_path(project, run_id), draft)
        manifest = read_json(project, run_dir / "manifest.json")
        manifest.update(
            decision_status="pending",
            proposal_count=len(draft["proposals"]),
            needs_review_count=len(draft["needs_review"]),
            protected_term_count=len(protected_states),
        )
        write_json(project, run_dir / "manifest.json", manifest)
        usage = finalize_run(
            project,
            run_dir,
            status="completed",
            completed=len(eligible),
            failed=0,
            usage=usage,
        )
        if existing is not None:
            old_run_id = str(existing["run_id"])
            old_path = project / "runs" / old_run_id / "manifest.json"
            old_manifest = read_json(project, old_path)
            old_manifest.update(
                decision_status="superseded",
                superseded_by_run_id=run_id,
            )
            write_json(project, old_path, old_manifest)
        return {
            "stage": STAGE,
            "run_id": run_id,
            "terms_revision": int(library["terms_revision"]),
            "eligible": len(eligible),
            "protected": len(protected_states),
            "proposals": len(draft["proposals"]),
            "needs_review": len(draft["needs_review"]),
            "usage": usage or unavailable_usage(),
        }
    except asyncio.CancelledError:
        if "llm" in locals():
            usage = llm.usage_summary()
        record_resumable_interruption(usage)
        raise
    except Exception:
        if "llm" in locals():
            usage = llm.usage_summary()
        record_resumable_interruption(usage)
        raise


def _pending_manifest(project: Path, run_id: str) -> dict[str, Any]:
    manifest = read_json(project, project / "runs" / run_id / "manifest.json")
    if manifest.get("stage") != STAGE or manifest.get("decision_status") != "pending":
        raise UsageError("术语决策草案不再处于待审核状态")
    return manifest


def save_decision_rejections(
    project: Path, proposal_ids: list[str]
) -> dict[str, Any]:
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


def _proposal_after_states(
    draft: dict[str, Any], rejected: set[str]
) -> tuple[dict[str, dict[str, Any]], int]:
    values: dict[str, dict[str, Any]] = {}
    accepted = 0
    for proposal in draft["proposals"]:
        if str(proposal["proposal_id"]) in rejected:
            continue
        accepted += 1
        for state in proposal["after"]:
            normalized = str(state["normalized"])
            if normalized in values:
                raise StorageError(f"术语决策建议重复修改：{normalized}")
            values[normalized] = deepcopy(state)
    return values, accepted


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
    manifest_rejected = set(map(str, draft.get("rejected_proposal_ids", [])))
    requested_rejected = set(map(str, rejected_proposal_ids or []))
    known = {str(item["proposal_id"]) for item in draft["proposals"]}
    unknown = sorted(requested_rejected - known)
    if unknown:
        raise UsageError(f"未知术语决策建议：{', '.join(unknown[:10])}")
    rejected = manifest_rejected | requested_rejected
    after_states, accepted = _proposal_after_states(draft, rejected)
    run_id = str(draft["run_id"])
    manifest_path = project / "runs" / run_id / "manifest.json"
    manifest = _pending_manifest(project, run_id)
    if accepted == 0:
        manifest.update(
            decision_status="rejected",
            rejected_proposal_ids=sorted(rejected),
            applied_proposal_count=0,
            applied_at=utc_now(),
        )
        write_json(project, manifest_path, manifest)
        return {
            "run_id": run_id,
            "applied": 0,
            "rejected": len(rejected),
            "terms_revision": int(library["terms_revision"]),
        }
    overrides_document = read_json(
        project, project / "terminology" / "overrides.json"
    )
    overrides = {
        str(item["normalized"]): deepcopy(item)
        for item in overrides_document.get("overrides", [])
    }
    protected = set(overrides)
    if protected.intersection(after_states):
        raise UsageError("术语决策试图修改受保护的人工 override")
    current = {
        str(item["normalized"]): deepcopy(item)
        for item in library.get("terms", [])
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
        if state.get("group_primary") is not None:
            override["group_primary"] = state["group_primary"]
        else:
            override["group_primary"] = None
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
