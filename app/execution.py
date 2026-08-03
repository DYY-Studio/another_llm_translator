from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import random
import re
import shutil
import sys
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx

from .config import load_project_config, load_run_config
from .diagnostics import current_diagnostics
from .errors import (
    ConfigError,
    ContextLengthError,
    ExternalError,
    FatalExternalError,
    RequestSizeError,
    StorageError,
    UsageError,
)
from .logging_utils import get_logger
from .llm_adapter import JSONLLMAdapter, LLMResponse, Usage
from .storage import (
    append_jsonl,
    atomic_write_json,
    read_json,
    read_jsonl,
    record_header,
    utc_now,
)


STAGE_FILES = {
    "translation": "translation.jsonl",
    "proofreading": "proofreading.jsonl",
    "proofreading_applied": "proofreading_applied.jsonl",
    "polishing": "polishing.jsonl",
    "polishing_applied": "polishing_applied.jsonl",
}

STAGE_CODES = {
    "terminology": "TERM",
    "translation": "TR",
    "proofreading": "PR",
    "polishing": "PO",
    "proofreading_applied": "PRA",
    "polishing_applied": "POA",
}

CJK_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]"
)


@dataclass(frozen=True)
class Scope:
    from_file: str | None = None
    only_file: str | None = None
    only_segment: str | None = None
    segment_ids: tuple[str, ...] | None = None
    force: bool = False
    dry_run: bool = False

    def validate(self) -> None:
        selectors = [
            self.from_file,
            self.only_file,
            self.only_segment,
            self.segment_ids,
        ]
        if sum(value is not None for value in selectors) > 1:
            raise UsageError(
                "--from-file、--only-file、--only-segment 和 segment_ids 不能同时使用"
            )


@dataclass(frozen=True)
class ChunkPlan:
    file_id: str
    segments: tuple[dict[str, Any], ...]
    payload: dict[str, Any]
    estimated_input_tokens: int
    chunk_id: str | None = None


@dataclass(frozen=True)
class StageSelection:
    selected: tuple[dict[str, Any], ...]
    work: tuple[dict[str, Any], ...]
    reusable: tuple[dict[str, Any], ...]
    latest_completed: dict[str, dict[str, Any]]
    last_attempt_failed: tuple[str, ...]
    fingerprints: frozenset[str]


@dataclass(frozen=True)
class JSONLDocument:
    records: tuple[dict[str, Any], ...]
    errors: tuple[str, ...]
    complete: bool


def select_scope(
    segments: Iterable[dict[str, Any]],
    files: Iterable[dict[str, Any]],
    scope: Scope,
) -> list[dict[str, Any]]:
    scope.validate()
    file_order = {str(item["file_id"]): int(item["file_order"]) for item in files}
    selected = [item for item in segments if not item["is_empty"]]
    if scope.segment_ids is not None:
        requested = set(scope.segment_ids)
        if not requested:
            raise UsageError("segment_ids 不能为空")
        selected = [
            item for item in selected if str(item["segment_id"]) in requested
        ]
        found = {str(item["segment_id"]) for item in selected}
        missing = sorted(requested - found)
        if missing:
            raise UsageError(f"未知或空 Segment：{', '.join(missing[:10])}")
    elif scope.only_segment:
        selected = [
            item for item in selected if item["segment_id"] == scope.only_segment
        ]
    elif scope.only_file:
        selected = [item for item in selected if item["file_id"] == scope.only_file]
    elif scope.from_file:
        if scope.from_file not in file_order:
            raise UsageError(f"未知 file_id：{scope.from_file}")
        start = file_order[scope.from_file]
        selected = [
            item for item in selected if file_order[str(item["file_id"])] >= start
        ]
    if (scope.only_file or scope.only_segment or scope.segment_ids) and not selected:
        raise UsageError("选择范围为空或 ID 不存在")
    return selected


def stage_result_path(project: Path, stage: str) -> Path:
    try:
        filename = STAGE_FILES[stage]
    except KeyError as exc:
        raise UsageError(f"未知阶段：{stage}") from exc
    return project / "stages" / filename


def load_stage_history(
    project: Path, stage: str, *, repair_tail: bool = True
) -> list[dict[str, Any]]:
    return read_jsonl(stage_result_path(project, stage), repair_tail=repair_tail)


def latest_completed_by_segment(
    history: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for record in history:
        segment_id = record.get("segment_id")
        if not segment_id:
            continue
        if record.get("status") == "reset":
            latest.pop(str(segment_id), None)
        elif record.get("status") == "completed":
            latest[str(segment_id)] = record
    return latest


def classify_stage(
    selected: Iterable[dict[str, Any]],
    history: Iterable[dict[str, Any]],
    *,
    force: bool,
) -> StageSelection:
    selected_list = list(selected)
    history_list = list(history)
    completed = latest_completed_by_segment(history_list)
    records_by_segment: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in history_list:
        if record.get("segment_id"):
            records_by_segment[str(record["segment_id"])].append(record)
    reusable = [
        segment
        for segment in selected_list
        if str(segment["segment_id"]) in completed
    ]
    work = (
        selected_list
        if force
        else [
            segment
            for segment in selected_list
            if str(segment["segment_id"]) not in completed
        ]
    )
    last_failed = tuple(
        str(segment["segment_id"])
        for segment in selected_list
        if str(segment["segment_id"]) in completed
        and records_by_segment[str(segment["segment_id"])]
        and records_by_segment[str(segment["segment_id"])][-1].get("status") == "failed"
    )
    reusable_ids = {
        str(segment["segment_id"]) for segment in reusable
    }
    fingerprints = frozenset(
        str(completed[segment_id]["stage_fingerprint"])
        for segment_id in reusable_ids
        if completed[segment_id].get("stage_fingerprint")
    )
    return StageSelection(
        selected=tuple(selected_list),
        work=tuple(work),
        reusable=tuple(reusable),
        latest_completed=completed,
        last_attempt_failed=last_failed,
        fingerprints=fingerprints,
    )


def full_prompt(stage: str, middle: str) -> str:
    common = (
        "只处理 user 消息中的待处理内容。reference_context 仅供理解，"
        "不得输出或计入进度。严格使用 JSONL："
        "每个非空物理行只能包含一个紧凑 JSON 对象，不得跨行格式化，"
        "不要使用 Markdown 代码块或解释文字。最后一行只能是"
        '{"type":"end"}。'
    )
    stage_rules = {
        "terminology": (
            "只从 source_segments[].source 提取术语；reference_context 中的"
            "内容不得单独触发提取。term 记录的 source 必须填写原文中实际"
            "出现的术语文本。"
            '每个术语输出一条 type="term" 记录，包含 source、category、'
            "description，preferred_translation 和 aliases 可选。没有术语时"
            "直接输出 end。记录格式："
            '{"type":"term","source":"Alice","category":"女性人名",'
            '"description":"人物","preferred_translation":"爱丽丝","aliases":[]}。'
        ),
        "translation": (
            "只使用请求中从 1 开始的短 ID，不要臆造或改写 ID。"
            "源文若来自 EPUB Aozora Ruby，只有确有必要时保留；保留时严格使用"
            "｜base《reading》形式，可将 reading 翻译或转写为目标语言适用的字母/注音；"
            "不保留 Ruby 也合法。若 source 含形如 <em1> 的受控格式标记，"
            "只保留已有且成对、正确嵌套的标记，不添加 attrs、未知标记或 HTML；"
            "没有这类标记时不要输出其他标记。"
            '每个 Segment 输出一条 type="segment" 记录，只包含 type、id '
            "和完整 translation。记录格式："
            '{"type":"segment","id":"1","translation":"完整译文"}。'
        ),
        "proofreading": (
            "只使用请求中从 1 开始的短 ID，不要臆造或改写 ID。"
            "源文若来自 EPUB Aozora Ruby，只有确有必要时保留；保留时严格使用"
            "｜base《reading》形式，可将 reading 翻译或转写为目标语言适用的字母/注音；"
            "不保留 Ruby 也合法。若 source 含形如 <em1> 的受控格式标记，"
            "只保留已有且成对、正确嵌套的标记，不添加 attrs、未知标记或 HTML；"
            "没有这类标记时不要输出其他标记。"
            '每个 Segment 输出一条 type="segment" 记录；status 只能是 '
            "accepted 或 suggested。accepted 表示无条件保留当前基准，只输出 "
            "type、id、status；即使附带 suggested_text 或 reason 也不会采用。"
            "accepted 记录格式："
            '{"type":"segment","id":"1","status":"accepted"}。'
            "suggested 必须包含非空完整 suggested_text，reason 为字符串或 "
            "null。suggested 记录格式："
            '{"type":"segment","id":"1","status":"suggested",'
            '"suggested_text":"完整建议","reason":"原因"}。'
        ),
        "polishing": (
            "只使用请求中从 1 开始的短 ID，不要臆造或改写 ID。"
            "源文若来自 EPUB Aozora Ruby，只有确有必要时保留；保留时严格使用"
            "｜base《reading》形式，可将 reading 翻译或转写为目标语言适用的字母/注音；"
            "不保留 Ruby 也合法。若 source 含形如 <em1> 的受控格式标记，"
            "只保留已有且成对、正确嵌套的标记，不添加 attrs、未知标记或 HTML；"
            "没有这类标记时不要输出其他标记。"
            '每个 Segment 输出一条 type="segment" 记录；status 只能是 '
            "accepted 或 suggested。accepted 表示无条件保留当前基准，只输出 "
            "type、id、status；即使附带 suggested_text 或 reason 也不会采用。"
            "accepted 记录格式："
            '{"type":"segment","id":"1","status":"accepted"}。'
            "suggested 必须包含非空完整 suggested_text，reason 为字符串或 "
            "null。suggested 记录格式："
            '{"type":"segment","id":"1","status":"suggested",'
            '"suggested_text":"完整建议","reason":"原因"}。'
        ),
    }
    if stage not in stage_rules:
        raise UsageError(f"阶段没有 LLM Prompt：{stage}")
    return f"{common}\n\n{stage_rules[stage]}\n\n{middle.strip()}"


def stage_fingerprint(
    config: dict[str, Any],
    stage: str,
    prompt: str | None,
    *,
    terms_revision: int | None = None,
    apply_semantics: dict[str, Any] | None = None,
) -> str:
    if stage.endswith("_applied"):
        data = {
            "stage": stage,
            "apply_rule_version": 1,
            **(apply_semantics or {}),
        }
    else:
        temperature_key = f"temperature_{stage}"
        data = {
            "stage": stage,
            "target_language": config["project"]["target_language"],
            "model": config["llm"]["model"],
            "llm_adapter": config["llm"]["adapter"],
            "llm_adapter_hash": config.get("_llm_adapter_hash"),
            "llm_preset": config.get("_llm_preset_id"),
            "llm_preset_hash": config.get("_llm_preset_hash"),
            "prompt": prompt,
            "temperature": config["llm"][temperature_key],
            "context": config["context"][stage],
            "scheduling_mode": config["execution"]["scheduling_mode"],
            "terms_revision": terms_revision,
            "document_adapter_options": config.get(
                "_document_adapter_options", {}
            ),
            "document_adapters": config.get("_document_adapters", {}),
        }
        if stage == "terminology":
            data["terminology"] = config["terminology"]
        if stage == "translation":
            data["validation"] = {
                key: value
                for key, value in config["validation"]["translation"].items()
                if key != "max_retry_attempts"
            }
    encoded = json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk_count = len(CJK_RE.findall(text))
    non_cjk = CJK_RE.sub("", text)
    non_space = sum(not char.isspace() for char in non_cjk)
    whitespace = sum(char.isspace() for char in non_cjk)
    return max(1, math.ceil(cjk_count * 1.1 + non_space / 4 + whitespace / 8))


def estimate_messages(messages: list[dict[str, str]], factor: float) -> int:
    rendered = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    return math.ceil(estimate_tokens(rendered) * factor)


def render_messages(prompt: str, payload: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        },
    ]


def localize_request_ids(
    payload: dict[str, Any],
    items: Iterable[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Replace output-bearing Segment IDs with request-local short IDs."""
    localized = deepcopy(payload)
    entries = localized.get("segments")
    source_items = list(items)
    if not isinstance(entries, list):
        return localized, {}
    if len(entries) != len(source_items):
        raise UsageError("请求载荷与 Segment 映射数量不一致")
    mapping: dict[str, str] = {}
    for index, (entry, source_item) in enumerate(
        zip(entries, source_items, strict=True), start=1
    ):
        if not isinstance(entry, dict):
            raise UsageError("请求载荷中的 Segment 必须是对象")
        short_id = str(index)
        entry["id"] = short_id
        mapping[short_id] = str(source_item["segment_id"])
    return localized, mapping


def _segment_part_key(segment: dict[str, Any]) -> tuple[str, str]:
    return str(segment["file_id"]), str(segment["part_id"])


def segment_model_source(segment: dict[str, Any]) -> str:
    value = segment.get("model_source")
    return str(value) if isinstance(value, str) else str(segment["source"])


def previous_context(
    all_segments: list[dict[str, Any]],
    first: dict[str, Any],
    count: int,
    *,
    target_resolver: Callable[[str], str | None] | None = None,
    source_key: str = "source",
) -> list[dict[str, str]]:
    if count <= 0:
        return []
    first_part = _segment_part_key(first)
    candidates = [
        item
        for item in all_segments
        if _segment_part_key(item) == first_part
        and int(item["line_index"]) < int(first["line_index"])
        and not item["is_empty"]
    ][-count:]
    result: list[dict[str, str]] = []
    for item in candidates:
        context = {
            "source": (
                segment_model_source(item)
                if source_key == "model_source"
                else str(item["source"])
            )
        }
        if target_resolver is not None:
            target = target_resolver(str(item["segment_id"]))
            if target is not None:
                context["translation"] = target
        result.append(context)
    return result


def contiguous_groups(
    segments: Iterable[dict[str, Any]],
    *,
    all_segments: Iterable[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    empty_positions = {
        (*_segment_part_key(item), int(item["line_index"]))
        for item in all_segments
        if item["is_empty"]
    }
    ordered = sorted(
        segments, key=lambda item: (str(item["file_id"]), int(item["line_index"]))
    )
    groups: list[list[dict[str, Any]]] = []
    for segment in ordered:
        if not groups:
            groups.append([segment])
            continue
        previous = groups[-1][-1]
        same_part = _segment_part_key(previous) == _segment_part_key(segment)
        previous_index = int(previous["line_index"])
        current_index = int(segment["line_index"])
        part_key = _segment_part_key(segment)
        gap_is_empty = same_part and current_index > previous_index and all(
            (*part_key, line_index) in empty_positions
            for line_index in range(previous_index + 1, current_index)
        )
        if gap_is_empty:
            groups[-1].append(segment)
        else:
            groups.append([segment])
    return groups


def _iter_contiguous_groups(
    segments: Iterable[dict[str, Any]],
    *,
    all_segments: Iterable[dict[str, Any]],
) -> Iterable[list[dict[str, Any]]]:
    empty_positions = {
        (*_segment_part_key(item), int(item["line_index"]))
        for item in all_segments
        if item["is_empty"]
    }
    ordered = sorted(
        segments,
        key=lambda item: (
            str(item["file_id"]),
            int(item["line_index"]),
            str(item["segment_id"]),
        ),
    )
    current: list[dict[str, Any]] = []
    for segment in ordered:
        if not current:
            current = [segment]
            continue
        previous = current[-1]
        same_part = _segment_part_key(previous) == _segment_part_key(segment)
        previous_index = int(previous["line_index"])
        current_index = int(segment["line_index"])
        part_key = _segment_part_key(segment)
        gap_is_empty = same_part and current_index > previous_index and all(
            (*part_key, line_index) in empty_positions
            for line_index in range(previous_index + 1, current_index)
        )
        if same_part and (current_index == previous_index or gap_is_empty):
            current.append(segment)
            continue
        yield current
        current = [segment]
    if current:
        yield current


def iter_chunk_plans(
    work: Iterable[dict[str, Any]],
    *,
    all_segments: Iterable[dict[str, Any]],
    config: dict[str, Any],
    prompt: str,
    payload_builder: Callable[[list[dict[str, Any]]], dict[str, Any]],
) -> Iterable[ChunkPlan]:
    input_limit = (
        config["llm"]["context_window_tokens"]
        - config["llm"]["context_safety_margin_tokens"]
    )
    target_limits = [
        config["chunking"]["target_chunk_input_tokens"],
        input_limit,
    ]
    if config["execution"]["input_tokens_per_minute"] > 0:
        target_limits.append(config["execution"]["input_tokens_per_minute"])
    target = min(target_limits)
    factor = config["execution"]["token_safety_factor"]

    def plan_groups(
        groups: Iterable[list[dict[str, Any]]],
    ) -> Iterable[ChunkPlan]:
        for group in groups:
            current: list[dict[str, Any]] = []
            for segment in group:
                candidate = [*current, segment]
                payload = payload_builder(candidate)
                estimated = estimate_messages(
                    render_messages(prompt, payload), factor
                )
                started_new_chunk = False
                if current and estimated > target:
                    current_payload = payload_builder(current)
                    current_estimate = estimate_messages(
                        render_messages(prompt, current_payload), factor
                    )
                    yield ChunkPlan(
                        file_id=str(current[0]["file_id"]),
                        segments=tuple(current),
                        payload=current_payload,
                        estimated_input_tokens=current_estimate,
                    )
                    current = [segment]
                    payload = payload_builder(current)
                    estimated = estimate_messages(
                        render_messages(prompt, payload), factor
                    )
                    started_new_chunk = True
                if estimated > input_limit:
                    raise RequestSizeError(
                        f"单 Segment Prompt 超过模型硬限制：{segment['segment_id']}",
                        reason="context",
                    )
                if (
                    config["execution"]["input_tokens_per_minute"] > 0
                    and estimated
                    > config["execution"]["input_tokens_per_minute"]
                ):
                    raise RequestSizeError(
                        f"单请求预测 Token 超过 ITPM：{segment['segment_id']}",
                        reason="itpm",
                    )
                if not started_new_chunk:
                    current = candidate
            if current:
                payload = payload_builder(current)
                estimated = estimate_messages(
                    render_messages(prompt, payload), factor
                )
                yield ChunkPlan(
                    file_id=str(current[0]["file_id"]),
                    segments=tuple(current),
                    payload=payload,
                    estimated_input_tokens=estimated,
                )

    if config["execution"]["scheduling_mode"] != "ordered_by_file":
        yield from plan_groups(_iter_contiguous_groups(work, all_segments=all_segments))
        return

    by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in work:
        by_file.setdefault(str(item["file_id"]), []).append(item)
    streams = [
        iter(
            plan_groups(
                _iter_contiguous_groups(items, all_segments=all_segments)
            )
        )
        for items in by_file.values()
    ]
    while streams:
        remaining: list[Iterable[ChunkPlan]] = []
        for stream in streams:
            try:
                yield next(stream)  # type: ignore[arg-type]
            except StopIteration:
                continue
            remaining.append(stream)
        streams = remaining


def build_chunk_plans(
    work: Iterable[dict[str, Any]],
    *,
    all_segments: Iterable[dict[str, Any]],
    config: dict[str, Any],
    prompt: str,
    payload_builder: Callable[[list[dict[str, Any]]], dict[str, Any]],
) -> list[ChunkPlan]:
    return list(
        iter_chunk_plans(
            work,
            all_segments=all_segments,
            config=config,
            prompt=prompt,
            payload_builder=payload_builder,
        )
    )


def materialize_chunk_stream(
    run_id: str,
    stage: str,
    plans: Iterable[ChunkPlan],
) -> Iterable[ChunkPlan]:
    code = STAGE_CODES[stage]
    for index, plan in enumerate(plans, start=1):
        yield replace(
            plan,
            chunk_id=f"CHK-{run_id}-{code}-{plan.file_id}-C{index:05d}",
        )


def create_run(
    project: Path,
    *,
    config: dict[str, Any],
    stage: str,
    fingerprint: str,
    prompt: str | None,
    selected_count: int,
    requested_count: int,
    reused_count: int,
    details: dict[str, Any] | None = None,
) -> tuple[str, Path]:
    project_metadata = read_json(project / "project.json")
    suffix = uuid.uuid4().hex[:6].upper()
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    run_id = f"RUN-{timestamp}-{STAGE_CODES[stage]}-{suffix}"
    run_dir = project / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    shutil.copy2(project / "config.toml", run_dir / "config.toml")
    _write_llm_snapshots(run_dir, config)
    if prompt is not None:
        (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    manifest = record_header(
        "run",
        str(project_metadata["project_id"]),
        record_id=run_id,
        run_id=run_id,
        stage=stage,
        status="running",
        stage_fingerprint=fingerprint,
        selected_segment_count=selected_count,
        requested_segment_count=requested_count,
        reused_segment_count=reused_count,
        document_adapters=config.get("_document_adapters", {}),
        document_adapter_options=config.get("_document_adapter_options", {}),
        **(details or {}),
        started_at=utc_now(),
        completed_at=None,
    )
    if stage in {"terminology", "translation", "proofreading", "polishing"}:
        manifest["usage_invocation_count"] = 0
    atomic_write_json(run_dir / "manifest.json", manifest)
    return run_id, run_dir


def find_running_runs(project: Path, stage: str) -> list[dict[str, Any]]:
    from .sqlite_storage import list_runs

    runs = list_runs(project, stage=stage, status="running")
    return sorted(
        runs,
        key=lambda item: (
            str(item.get("started_at", "")),
            str(item.get("run_id", "")),
        ),
        reverse=True,
    )


def _interrupt_run(
    project: Path,
    manifest: dict[str, Any],
    *,
    reason: str,
    superseded_by_run_id: str | None = None,
) -> None:
    manifest.update(
        status="interrupted",
        interrupted_reason=reason,
        interrupted_at=utc_now(),
        completed_at=utc_now(),
    )
    if superseded_by_run_id is not None:
        manifest["superseded_by_run_id"] = superseded_by_run_id
    if reason == "resume_declined":
        manifest["resume_declined"] = True
    run_id = str(manifest["run_id"])
    atomic_write_json(project / "runs" / run_id / "manifest.json", manifest)


def choose_running_run(
    project: Path,
    stage: str,
    *,
    action: str | None,
    dry_run: bool,
    interactive: bool | None = None,
) -> tuple[str | None, list[str]]:
    candidates = find_running_runs(project, stage)
    if not candidates:
        if action == "resume":
            raise UsageError(f"{stage} 没有可续用的 running Run")
        return None, []
    latest = candidates[0]
    run_id = str(latest["run_id"])
    warning = f"发现未完成 Run：{run_id}"
    if dry_run:
        if action == "resume":
            return run_id, [f"{warning}；dry-run 将按其原范围规划续作"]
        return None, [
            f"{warning}；dry-run 未修改状态，使用 --resume-run 可检查续作范围"
        ]
    for older in candidates[1:]:
        _interrupt_run(
            project,
            older,
            reason="superseded",
            superseded_by_run_id=run_id,
        )
    interactive = sys.stdin.isatty() if interactive is None else interactive
    if action is None and not interactive:
        raise UsageError(
            f"{warning}；非交互环境必须指定 --resume-run 或 --decline-run"
        )
    if action is None:
        old_config = load_run_config(project / "runs" / run_id)
        current_config = load_project_config(project, stage=stage)
        scope = latest.get("scope", {})
        print(
            f"{warning}\n"
            f"原范围：{json.dumps(scope, ensure_ascii=False)}\n"
            f"旧模型/端点：{old_config['llm']['model']} "
            f"{old_config['llm']['base_url']}{old_config['llm']['endpoint']}\n"
            f"当前模型/端点：{current_config['llm']['model']} "
            f"{current_config['llm']['base_url']}{current_config['llm']['endpoint']}\n"
            "使用当前 config 和 Prompt 继续该 Run？"
            "[r]esume/[n]ew: ",
            end="",
            file=sys.stderr,
            flush=True,
        )
        while True:
            answer = input().strip().casefold()
            if answer in {"r", "resume"}:
                action = "resume"
                break
            if answer in {"n", "new"}:
                action = "decline"
                break
            print("请输入 resume 或 new: ", end="", file=sys.stderr, flush=True)
    if action == "decline":
        _interrupt_run(project, latest, reason="resume_declined")
        return None, [f"已拒绝续用 Run：{run_id}"]
    if action != "resume":
        raise UsageError("Run 选择必须是 resume 或 decline")
    return run_id, [f"将使用当前 config 和 Prompt 续用 Run：{run_id}"]


def scope_from_run(
    project: Path,
    run_id: str,
    *,
    dry_run: bool,
) -> Scope:
    manifest = read_json(project / "runs" / run_id / "manifest.json")
    raw = manifest.get("scope")
    if not isinstance(raw, dict):
        raise StorageError(f"Run 缺少 scope：{run_id}")
    return Scope(
        from_file=raw.get("from_file"),
        only_file=raw.get("only_file"),
        only_segment=raw.get("only_segment"),
        segment_ids=(
            tuple(str(value) for value in raw["segment_ids"])
            if isinstance(raw.get("segment_ids"), list)
            else None
        ),
        force=False,
        dry_run=dry_run,
    )


def continue_run(
    project: Path,
    run_id: str,
    *,
    config: dict[str, Any],
    stage: str,
    fingerprint: str,
    prompt: str,
    scope: Scope,
    selected_count: int,
    requested_count: int,
    reused_count: int,
) -> tuple[str, Path]:
    run_dir = project / "runs" / run_id
    manifest = read_json(run_dir / "manifest.json")
    if manifest.get("status") != "running" or manifest.get("stage") != stage:
        raise StorageError(f"Run 不可续用：{run_id}")
    continuations = list(manifest.get("continuations", []))
    index = len(continuations) + 1
    relative = Path("continuations") / f"{index:04d}"
    snapshot_dir = run_dir / relative
    snapshot_dir.mkdir(parents=True, exist_ok=False)
    shutil.copy2(project / "config.toml", snapshot_dir / "config.toml")
    _write_llm_snapshots(snapshot_dir, config)
    (snapshot_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    continuations.append(
        {
            "started_at": utc_now(),
            "stage_fingerprint": fingerprint,
            "scope": {
                "all_nonempty": not (
                    scope.from_file or scope.only_file or scope.only_segment
                ),
                "from_file": scope.from_file,
                "only_file": scope.only_file,
                "only_segment": scope.only_segment,
                "force": False,
            },
            "selected_segment_count": selected_count,
            "requested_segment_count": requested_count,
            "reused_segment_count": reused_count,
        }
    )
    manifest["continuations"] = continuations
    atomic_write_json(run_dir / "manifest.json", manifest)
    return run_id, run_dir


def _write_llm_snapshots(path: Path, config: dict[str, Any]) -> None:
    adapter = config.get("_llm_adapter")
    if not isinstance(adapter, JSONLLMAdapter):
        raise ConfigError("项目配置缺少已加载的 LLM Adapter")
    atomic_write_json(path / "llm_adapter.json", adapter.definition)
    preset = config.get("_llm_preset_definition")
    if preset is not None:
        if not isinstance(preset, dict):
            raise ConfigError("项目配置中的 LLM Preset 快照无效")
        atomic_write_json(path / "llm_preset.json", preset)


def finalize_run(
    run_dir: Path,
    *,
    status: str,
    completed: int,
    failed: int,
    warnings: list[str] | None = None,
    usage: dict[str, Any] | None = None,
    failure_counts: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    manifest = read_json(run_dir / "manifest.json")
    manifest.update(
        status=status,
        completed_segment_count=completed,
        failed_segment_count=failed,
        failure_counts={
            str(key): int(value)
            for key, value in (failure_counts or {}).items()
            if int(value) > 0
        },
        warnings=warnings or [],
        completed_at=utc_now(),
    )
    invocation_count = manifest.get("usage_invocation_count")
    tracked = type(invocation_count) is int or bool(
        manifest.get("continuations")
    )
    if usage is not None or tracked:
        previous = manifest.get("usage")
        if type(invocation_count) is int and invocation_count > 0:
            usage = combine_usage(previous, usage)
        elif type(invocation_count) is not int and manifest.get(
            "continuations"
        ):
            usage = unavailable_usage()
        elif usage is None:
            usage = unavailable_usage()
        manifest["usage"] = usage
        manifest["usage_invocation_count"] = (
            invocation_count + 1 if type(invocation_count) is int else 1
        )
    atomic_write_json(run_dir / "manifest.json", manifest)
    return usage


def unavailable_usage() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "available": False,
    }


def combine_usage(
    previous: Any, current: Any
) -> dict[str, Any]:
    values = (previous, current)
    if any(
        not isinstance(value, dict)
        or value.get("available") is not True
        or any(
            not isinstance(value.get(key), int) or value[key] < 0
            for key in ("input_tokens", "output_tokens", "total_tokens")
        )
        for value in values
    ):
        return unavailable_usage()
    return {
        "input_tokens": sum(value["input_tokens"] for value in values),
        "output_tokens": sum(value["output_tokens"] for value in values),
        "total_tokens": sum(value["total_tokens"] for value in values),
        "available": True,
    }


def save_debug_chunks(
    run_dir: Path,
    project_id: str,
    run_id: str,
    stage: str,
    chunks: Iterable[ChunkPlan],
) -> None:
    for chunk in chunks:
        append_jsonl(
            run_dir / "chunks.jsonl",
            record_header(
                "chunk_manifest",
                project_id,
                record_id=str(chunk.chunk_id),
                run_id=run_id,
                stage=stage,
                chunk_id=chunk.chunk_id,
                file_id=chunk.file_id,
                segment_ids=[
                    str(segment["segment_id"]) for segment in chunk.segments
                ],
                estimated_input_tokens=chunk.estimated_input_tokens,
            ),
        )


class SlidingWindowLimiter:
    def __init__(
        self,
        requests_per_minute: int,
        input_tokens_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.requests_per_minute = requests_per_minute
        self.input_tokens_per_minute = input_tokens_per_minute
        self.clock = clock
        self.sleeper = sleeper
        self.records: deque[tuple[float, int]] = deque()
        self.lock = asyncio.Lock()
        self.pacing_lock = asyncio.Lock() if requests_per_minute > 0 else None
        self.last_admitted_at: float | None = None

    async def acquire(
        self,
        estimated_tokens: int,
        *,
        on_wait_start: Callable[[], None] | None = None,
        on_wait_end: Callable[[], None] | None = None,
    ) -> float:
        waited = 0.0
        if self.requests_per_minute == 0 and self.input_tokens_per_minute == 0:
            return waited
        if (
            self.input_tokens_per_minute > 0
            and estimated_tokens > self.input_tokens_per_minute
        ):
            raise ConfigError("单请求预测 Token 超过 ITPM")
        waiting = False

        def begin_wait() -> None:
            nonlocal waiting
            if waiting:
                return
            waiting = True
            if on_wait_start is not None:
                on_wait_start()

        async def sleep_for(delay: float) -> None:
            nonlocal waited
            begin_wait()
            await self.sleeper(delay)
            waited += delay

        pacing_lock = self.pacing_lock
        pacing_acquired = False
        try:
            if pacing_lock is not None:
                if pacing_lock.locked():
                    begin_wait()
                await pacing_lock.acquire()
                pacing_acquired = True
            while True:
                async with self.lock:
                    now = self.clock()
                    while self.records and now - self.records[0][0] >= 60:
                        self.records.popleft()
                    pace_wait = 0.0
                    if self.requests_per_minute > 0 and self.last_admitted_at is not None:
                        pace_wait = max(
                            0.0,
                            self.last_admitted_at
                            + 60 / self.requests_per_minute
                            - now,
                        )
                    request_full = (
                        self.requests_per_minute > 0
                        and len(self.records) >= self.requests_per_minute
                    )
                    token_full = (
                        self.input_tokens_per_minute > 0
                        and (
                            sum(tokens for _, tokens in self.records)
                            + estimated_tokens
                            > self.input_tokens_per_minute
                        )
                    )
                    window_wait = 0.0
                    if request_full or token_full:
                        window_wait = max(
                            0.01,
                            60 - (now - self.records[0][0]),
                        )
                    wait = max(pace_wait, window_wait)
                    if wait <= 0:
                        self.records.append((now, estimated_tokens))
                        self.last_admitted_at = now
                        return waited
                await sleep_for(wait)
        finally:
            if waiting and on_wait_end is not None:
                on_wait_end()
            if pacing_lock is not None and pacing_acquired:
                pacing_lock.release()


class LLMClient:
    def __init__(
        self,
        config: dict[str, Any],
        limiter: SlidingWindowLimiter,
        *,
        run_dir: Path,
        project_id: str,
        run_id: str,
        stage: str,
        client: httpx.AsyncClient | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_usage: Callable[[dict[str, Any] | None], None] | None = None,
    ) -> None:
        self.config = config
        self.limiter = limiter
        self.run_dir = run_dir
        self.project_id = project_id
        self.run_id = run_id
        self.stage = stage
        self.client = client
        self.owns_client = client is None
        self.sleeper = sleeper
        self.on_usage = on_usage
        self.log_lock = asyncio.Lock()
        self.send_count = 0
        self.warnings: list[str] = []
        self._reported_output_clamp = False
        self.usage = Usage(input_tokens=0, output_tokens=0, total_tokens=0)
        self.usage_observed = False
        self.usage_complete = True
        self.logger = get_logger(stage)
        adapter = config.get("_llm_adapter")
        if not isinstance(adapter, JSONLLMAdapter):
            raise ConfigError("项目配置缺少已加载的 LLM Adapter")
        self.adapter = adapter

    async def __aenter__(self) -> "LLMClient":
        if self.client is None:
            timeout = float(self.config["execution"]["request_timeout_seconds"])
            limits = httpx.Limits(
                max_connections=self.config["execution"]["max_parallel"],
                max_keepalive_connections=self.config["execution"]["max_parallel"],
            )
            self.client = httpx.AsyncClient(
                timeout=timeout,
                limits=limits,
                proxy=self.config["llm"]["proxy_url"] or None,
            )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self.owns_client and self.client is not None:
            await self.client.aclose()

    async def _debug_attempt(
        self,
        request_id: str,
        attempt: int,
        payload: dict[str, Any],
        *,
        response: dict[str, Any] | None = None,
        error: str | None = None,
        status: int | None = None,
        parent_request_id: str | None = None,
    ) -> None:
        if not self.config["debug"]["enabled"]:
            return
        payload_dir = self.run_dir / "payloads"
        payload_dir.mkdir(parents=True, exist_ok=True)
        base = f"{request_id}-A{attempt:03d}"
        atomic_write_json(payload_dir / f"{base}.request.json", payload)
        if response is not None:
            atomic_write_json(payload_dir / f"{base}.response.json", response)
        if error is not None:
            atomic_write_json(
                payload_dir / f"{base}.error.json",
                {"schema_version": 1, "error": error, "http_status": status},
            )
        async with self.log_lock:
            append_jsonl(
                self.run_dir / "attempts.jsonl",
                record_header(
                    "request_attempt",
                    self.project_id,
                    record_id=f"{base}",
                    run_id=self.run_id,
                    request_id=request_id,
                    parent_request_id=parent_request_id,
                    stage=self.stage,
                    attempt=attempt,
                    http_status=status,
                    status="completed" if response is not None else "failed",
                    error=error,
                ),
            )

    async def chat(
        self,
        *,
        messages: list[dict[str, str]],
        temperature: float,
        estimated_input_tokens: int,
        request_id: str | None = None,
        parent_request_id: str | None = None,
        segment_id_map: dict[str, str] | None = None,
    ) -> tuple[LLMResponse, str]:
        if self.client is None:
            raise RuntimeError("LLMClient must be used as an async context manager")
        api_key = os.getenv(str(self.config["llm"]["api_key_env"]))
        if not api_key:
            raise ExternalError(
                f"缺少环境变量：{self.config['llm']['api_key_env']}"
            )
        request_id = request_id or f"REQ-{uuid.uuid4().hex[:12].upper()}"
        configured_output = int(self.config["llm"]["max_output_tokens"])
        available_output = max(
            1,
            int(self.config["llm"]["context_window_tokens"])
            - int(self.config["llm"]["context_safety_margin_tokens"])
            - estimated_input_tokens,
        )
        effective_output = min(configured_output, available_output)
        if effective_output < configured_output and not self._reported_output_clamp:
            warning = (
                "max_output_tokens "
                f"已从配置上限 {configured_output} 按本次剩余上下文"
                f"自动收窄为 {effective_output}"
            )
            self.warnings.append(warning)
            self.logger.warning("%s request=%s", warning, request_id)
            self._reported_output_clamp = True
        headers, payload = self.adapter.build_request(
            api_key=api_key,
            model=str(self.config["llm"]["model"]),
            messages=messages,
            temperature=temperature,
            max_output_tokens=effective_output,
            stream=False,
            extra_body=self.config.get("_llm_extra_body"),
        )
        url = (
            self.config["llm"]["base_url"].rstrip("/")
            + "/"
            + str(self.config["llm"]["endpoint"])
            .replace("${model}", str(self.config["llm"]["model"]))
            .lstrip("/")
        )
        attempts = int(self.config["retry"]["http_max_attempts"])
        diagnostics = current_diagnostics()
        if diagnostics is not None:
            diagnostics.begin_request(
                request_id=request_id,
                model=str(self.config["llm"]["model"]),
                messages=messages,
                max_attempts=attempts,
                segment_id_map=segment_id_map,
            )
        for attempt in range(1, attempts + 1):
            waited = await self.limiter.acquire(
                estimated_input_tokens,
                on_wait_start=(
                    diagnostics.rate_limit_wait_started
                    if diagnostics is not None
                    else None
                ),
                on_wait_end=(
                    diagnostics.rate_limit_wait_finished
                    if diagnostics is not None
                    else None
                ),
            )
            if waited:
                self.logger.info(
                    "rate-limit wait=%.2fs request=%s attempt=%d",
                    waited,
                    request_id,
                    attempt,
                )
            self.send_count += 1
            self.logger.info(
                "request start request=%s attempt=%d/%d input_tokens=%d max_tokens=%d",
                request_id,
                attempt,
                attempts,
                estimated_input_tokens,
                effective_output,
            )
            started = time.monotonic()
            response_status: int | None = None
            if diagnostics is not None:
                diagnostics.request_started(request_id)
            try:
                debug = self.config["debug"]
                if (
                    debug["enabled"]
                    and debug["inject_timeout_every"]
                    and self.send_count % debug["inject_timeout_every"] == 0
                ):
                    raise httpx.ReadTimeout("injected timeout")
                if (
                    debug["enabled"]
                    and debug["inject_429_every"]
                    and self.send_count % debug["inject_429_every"] == 0
                ):
                    response = httpx.Response(429, text="injected 429")
                elif (
                    debug["enabled"]
                    and debug["inject_500_every"]
                    and self.send_count % debug["inject_500_every"] == 0
                ):
                    response = httpx.Response(500, text="injected 500")
                else:
                    response = await self.client.post(
                        url,
                        headers=headers,
                        json=payload,
                    )
                response_status = response.status_code
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                elapsed = time.monotonic() - started
                self.logger.warning(
                    "request network-error request=%s attempt=%d elapsed=%.2fs kind=%s",
                    request_id,
                    attempt,
                    elapsed,
                    type(exc).__name__,
                )
                await self._debug_attempt(
                    request_id,
                    attempt,
                    payload,
                    error=str(exc),
                    parent_request_id=parent_request_id,
                )
                if attempt == attempts:
                    if diagnostics is not None:
                        diagnostics.fail_request(request_id, "network_error")
                    raise ExternalError(f"HTTP 请求重试耗尽：{exc}") from exc
                if diagnostics is not None:
                    diagnostics.retried()
                await self._backoff(attempt)
                continue
            finally:
                if diagnostics is not None:
                    diagnostics.request_finished(
                        request_id=request_id,
                        attempt=attempt,
                        latency_seconds=time.monotonic() - started,
                        status=response_status,
                        error=response_status is None or response_status >= 400,
                    )
            elapsed = time.monotonic() - started
            try:
                response_data = response.json()
            except ValueError:
                response_data = {"raw_text": response.text}
            if 200 <= response.status_code < 300:
                debug = self.config["debug"]
                if (
                    debug["enabled"]
                    and debug["inject_invalid_json_every"]
                    and self.send_count % debug["inject_invalid_json_every"] == 0
                ):
                    self.adapter.replace_content(response_data, "{invalid json")
                elif (
                    debug["enabled"]
                    and debug["inject_missing_segment_every"]
                    and self.send_count % debug["inject_missing_segment_every"] == 0
                ):
                    try:
                        content = self.adapter.parse_response(response_data).content
                        lines = extract_jsonl_content(content).splitlines()
                        segment_indexes = []
                        for index, line in enumerate(lines):
                            value = json.loads(line)
                            if isinstance(value, dict) and value.get("type") == "segment":
                                segment_indexes.append(index)
                        if segment_indexes:
                            lines.pop(segment_indexes[-1])
                            self.adapter.replace_content(
                                response_data, "\n".join(lines)
                            )
                    except (
                        KeyError,
                        IndexError,
                        TypeError,
                        ValueError,
                        json.JSONDecodeError,
                    ):
                        pass
                await self._debug_attempt(
                    request_id,
                    attempt,
                    payload,
                    response=response_data,
                    status=response.status_code,
                    parent_request_id=parent_request_id,
                )
                try:
                    parsed = self.adapter.parse_response(response_data)
                    normalized = normalize_llm_response(parsed)
                except Exception:
                    if diagnostics is not None:
                        diagnostics.fail_request(
                            request_id, "response_parse_error"
                        )
                    raise
                if diagnostics is not None:
                    diagnostics.complete_request(
                        request_id,
                        content=normalized.content,
                        reasoning_content=normalized.reasoning_content,
                    )
                extracted = self.adapter.extract_usage(response_data)
                if extracted is not None:
                    self.usage = Usage(
                        input_tokens=(
                            self.usage.input_tokens + extracted.input_tokens
                        ),
                        output_tokens=(
                            self.usage.output_tokens + extracted.output_tokens
                        ),
                        total_tokens=(
                            self.usage.total_tokens + extracted.total_tokens
                        ),
                    )
                    self.usage_observed = True
                elif self.adapter.usage_pointers is not None:
                    self.usage_complete = False
                if self.on_usage is not None:
                    self.on_usage(self.usage_summary())
                self.logger.info(
                    "request complete request=%s attempt=%d status=%d elapsed=%.2fs",
                    request_id,
                    attempt,
                    response.status_code,
                    elapsed,
                )
                return normalized, request_id
            retryable = response.status_code in {408, 429} or response.status_code >= 500
            await self._debug_attempt(
                request_id,
                attempt,
                payload,
                error=response.text,
                status=response.status_code,
                parent_request_id=parent_request_id,
            )
            if response.status_code in {401, 403}:
                self.logger.error(
                    "request fatal request=%s attempt=%d status=%d elapsed=%.2fs",
                    request_id,
                    attempt,
                    response.status_code,
                    elapsed,
                )
                if diagnostics is not None:
                    diagnostics.fail_request(request_id, "authentication_error")
                raise FatalExternalError(f"鉴权失败：HTTP {response.status_code}")
            response_hint = response.text.casefold()
            if response.status_code == 400 and (
                "context_length" in response_hint
                or (
                    "context" in response_hint
                    and ("token" in response_hint or "maximum" in response_hint)
                )
            ):
                self.logger.warning(
                    "request context-too-long request=%s attempt=%d elapsed=%.2fs",
                    request_id,
                    attempt,
                    elapsed,
                )
                if diagnostics is not None:
                    diagnostics.fail_request(request_id, "context_length_error")
                raise ContextLengthError(
                    "模型报告上下文过长",
                    request_id=request_id,
                )
            if response.status_code in {400, 404}:
                self.logger.error(
                    "request fatal request=%s attempt=%d status=%d elapsed=%.2fs",
                    request_id,
                    attempt,
                    response.status_code,
                    elapsed,
                )
                if diagnostics is not None:
                    diagnostics.fail_request(
                        request_id, "request_configuration_error"
                    )
                raise FatalExternalError(
                    f"请求或端点配置错误：HTTP {response.status_code}"
                )
            if not retryable or attempt == attempts:
                self.logger.error(
                    "request failed request=%s attempt=%d status=%d elapsed=%.2fs",
                    request_id,
                    attempt,
                    response.status_code,
                    elapsed,
                )
                if diagnostics is not None:
                    diagnostics.fail_request(request_id, "http_error")
                raise ExternalError(f"LLM 请求失败：HTTP {response.status_code}")
            self.logger.warning(
                "request retry request=%s attempt=%d status=%d elapsed=%.2fs",
                request_id,
                attempt,
                response.status_code,
                elapsed,
            )
            if diagnostics is not None:
                diagnostics.retried()
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = float(retry_after)
                    self.logger.info(
                        "retry-after request=%s wait=%.2fs", request_id, delay
                    )
                    await self._retry_sleep(
                        delay,
                        diagnostics=diagnostics
                        if response.status_code == 429
                        else None,
                    )
                    continue
                except ValueError:
                    pass
            await self._backoff(
                attempt,
                diagnostics=diagnostics if response.status_code == 429 else None,
            )
        raise ExternalError("HTTP 请求重试耗尽")

    async def _retry_sleep(
        self,
        delay: float,
        *,
        diagnostics: Any | None,
    ) -> None:
        if diagnostics is not None:
            diagnostics.rate_limit_wait_started()
        try:
            await self.sleeper(delay)
        finally:
            if diagnostics is not None:
                diagnostics.rate_limit_wait_finished()

    async def _backoff(
        self,
        attempt: int,
        *,
        diagnostics: Any | None = None,
    ) -> None:
        delay = min(
            float(self.config["retry"]["max_delay_seconds"]),
            float(self.config["retry"]["base_delay_seconds"]) * (2 ** (attempt - 1)),
        )
        delay += random.uniform(0, float(self.config["retry"]["jitter_seconds"]))
        self.logger.info("retry backoff attempt=%d wait=%.2fs", attempt, delay)
        await self._retry_sleep(delay, diagnostics=diagnostics)

    def usage_summary(self) -> dict[str, Any] | None:
        if self.adapter.usage_pointers is None:
            return None
        available = self.usage_observed and self.usage_complete
        return {
            "input_tokens": self.usage.input_tokens if available else 0,
            "output_tokens": self.usage.output_tokens if available else 0,
            "total_tokens": self.usage.total_tokens if available else 0,
            "available": available,
        }


async def dispatch_chunks(
    chunks: Iterable[ChunkPlan],
    worker: Callable[[ChunkPlan], Awaitable[Any]],
    *,
    mode: str,
    max_parallel: int,
) -> list[Any]:
    if max_parallel < 1:
        raise ConfigError("max_parallel 必须是正整数")
    if mode == "ordered_by_file":
        iterator = iter(chunks)
        pending: dict[asyncio.Task[Any], tuple[int, str]] = {}
        buffered: dict[str, ChunkPlan] = {}
        results: dict[int, Any] = {}
        active_files: set[str] = set()
        next_index = 0
        deferred: ChunkPlan | None = None

        def start(chunk: ChunkPlan) -> None:
            nonlocal next_index
            file_id = str(chunk.file_id)
            task = asyncio.create_task(worker(chunk))
            pending[task] = (next_index, file_id)
            active_files.add(file_id)
            next_index += 1

        def fill() -> None:
            nonlocal deferred
            while len(pending) < max_parallel:
                inactive = [
                    file_id
                    for file_id in buffered
                    if file_id not in active_files
                ]
                if inactive:
                    file_id = inactive[0]
                    start(buffered.pop(file_id))
                    continue
                chunk = deferred
                deferred = None
                if chunk is None:
                    try:
                        chunk = next(iterator)
                    except StopIteration:
                        return
                file_id = str(chunk.file_id)
                if file_id in active_files:
                    if file_id in buffered:
                        deferred = chunk
                        return
                    buffered[file_id] = chunk
                    continue
                start(chunk)

        fill()
        try:
            while pending:
                done, _ = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    index, file_id = pending.pop(task)
                    active_files.discard(file_id)
                    results[index] = task.result()
                fill()
        except BaseException:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            raise
        return [results[index] for index in range(next_index)]
    if mode != "parallel":
        raise ConfigError(f"未知调度模式：{mode}")
    iterator = iter(chunks)
    pending: dict[asyncio.Task[Any], int] = {}
    results: dict[int, Any] = {}
    next_index = 0

    def fill() -> None:
        nonlocal next_index
        while len(pending) < max_parallel:
            try:
                chunk = next(iterator)
            except StopIteration:
                return
            task = asyncio.create_task(worker(chunk))
            pending[task] = next_index
            next_index += 1

    fill()
    try:
        while pending:
            done, _ = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                index = pending.pop(task)
                results[index] = task.result()
            fill()
    except BaseException:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        raise
    return [results[index] for index in range(next_index)]


_SUPPORTED_FENCE_LABELS = {"", "jsonl", "ndjson", "json"}
_FENCE_RE = re.compile(
    r"```[ \t]*(?P<label>[^\r\n`]*)\r?\n(?P<body>.*?)```",
    re.DOTALL,
)
_THOUGHT_BLOCK_TAGS = (
    ("<think>", "</think>"),
    ("<thinking>", "</thinking>"),
    ("<thought>", "</thought>"),
    ("<analysis>", "</analysis>"),
)


def normalize_llm_response(response: LLMResponse) -> LLMResponse:
    embedded = _extract_embedded_reasoning(response.content)
    if response.reasoning_content and embedded.reasoning_content:
        raise ExternalError(
            "LLM 响应同时包含结构化和 content 内嵌思考正文"
        )
    return LLMResponse(
        content=embedded.content,
        reasoning_content=(
            response.reasoning_content or embedded.reasoning_content
        ),
    )


def _extract_embedded_reasoning(content: str) -> LLMResponse:
    normalized = content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    stripped = normalized.lstrip()
    for opening, closing in _THOUGHT_BLOCK_TAGS:
        if not stripped.startswith(opening):
            continue
        closing_at = stripped.find(closing, len(opening))
        if closing_at < 0:
            return LLMResponse(stripped.strip(), None)
        thought = stripped[len(opening) : closing_at]
        remainder = stripped[closing_at + len(closing) :].lstrip()
        if any(
            tag in thought
            for pair in _THOUGHT_BLOCK_TAGS
            for tag in pair
        ) or any(
            remainder.startswith(tag)
            for pair in _THOUGHT_BLOCK_TAGS
            for tag in pair
        ):
            return LLMResponse(stripped.strip(), None)
        return LLMResponse(remainder, thought)
    return LLMResponse(stripped.strip(), None)


def extract_jsonl_content(content: str) -> str:
    normalized = _extract_embedded_reasoning(content).content
    for match in _FENCE_RE.finditer(normalized):
        label = match.group("label").strip().casefold()
        body = match.group("body").strip()
        if label in _SUPPORTED_FENCE_LABELS and body:
            outside = normalized[: match.start()] + normalized[match.end() :]
            if any(
                tag in outside
                for pair in _THOUGHT_BLOCK_TAGS
                for tag in pair
            ):
                return normalized.strip()
            return body
    return normalized.strip()


def parse_jsonl_document(content: str, *, record_type: str) -> JSONLDocument:
    body = extract_jsonl_content(content)
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_end = False
    for line_number, raw_line in enumerate(body.split("\n"), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if seen_end:
            errors.append(f"第 {line_number} 行位于 end 之后")
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"第 {line_number} 行不是合法 JSON 对象")
            continue
        if not isinstance(value, dict):
            errors.append(f"第 {line_number} 行必须是 JSON 对象")
            continue
        item_type = value.get("type")
        if item_type == "end":
            seen_end = True
            continue
        if item_type != record_type:
            errors.append(f"第 {line_number} 行包含未知 type")
            continue
        records.append(value)
    if not seen_end:
        errors.append("响应缺少最终 end 记录")
    return JSONLDocument(
        records=tuple(records),
        errors=tuple(errors),
        complete=seen_end and not errors,
    )
