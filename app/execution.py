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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx

from .config import load_config
from .errors import (
    ConfigError,
    ContextLengthError,
    ExternalError,
    FatalExternalError,
    StorageError,
    UsageError,
)
from .logging_utils import get_logger
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
    force: bool = False
    dry_run: bool = False

    def validate(self) -> None:
        selectors = [self.from_file, self.only_file, self.only_segment]
        if sum(value is not None for value in selectors) > 1:
            raise UsageError(
                "--from-file、--only-file 和 --only-segment 不能同时使用"
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
    if scope.only_segment:
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
    if (scope.only_file or scope.only_segment) and not selected:
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
        if record.get("status") == "completed" and record.get("segment_id"):
            latest[str(record["segment_id"])] = record
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
    fingerprints = frozenset(
        str(record["stage_fingerprint"])
        for record in completed.values()
        if record.get("stage_fingerprint")
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
        "不要使用 Markdown 代码块或解释文字。最后一行必须是"
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
            "保持 Segment ID 不变。"
            '每个 Segment 输出一条 type="segment" 记录，只包含 type、id '
            "和完整 translation。记录格式："
            '{"type":"segment","id":"F0001-S000001","translation":"完整译文"}。'
        ),
        "proofreading": (
            "保持 Segment ID 不变。"
            '每个 Segment 输出一条 type="segment" 记录，包含 id、status、'
            "suggested_text、reason；status 只能是 accepted 或 suggested。"
            "记录格式："
            '{"type":"segment","id":"F0001-S000001","status":"suggested",'
            '"suggested_text":"完整建议","reason":"原因"}。'
        ),
        "polishing": (
            "保持 Segment ID 不变。"
            '每个 Segment 输出一条 type="segment" 记录，包含 id、status、'
            "suggested_text、reason；status 只能是 accepted 或 suggested。"
            "记录格式："
            '{"type":"segment","id":"F0001-S000001","status":"suggested",'
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
            "prompt": prompt,
            "temperature": config["llm"][temperature_key],
            "context": config["context"][stage],
            "scheduling_mode": config["execution"]["scheduling_mode"],
            "terms_revision": terms_revision,
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


def previous_context(
    all_segments: list[dict[str, Any]],
    first: dict[str, Any],
    count: int,
    *,
    target_resolver: Callable[[str], str | None] | None = None,
) -> list[dict[str, str]]:
    if count <= 0:
        return []
    candidates = [
        item
        for item in all_segments
        if item["file_id"] == first["file_id"]
        and int(item["line_index"]) < int(first["line_index"])
        and not item["is_empty"]
    ][-count:]
    result: list[dict[str, str]] = []
    for item in candidates:
        context = {"id": str(item["segment_id"]), "source": str(item["source"])}
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
        (str(item["file_id"]), int(item["line_index"]))
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
        same_file = previous["file_id"] == segment["file_id"]
        previous_index = int(previous["line_index"])
        current_index = int(segment["line_index"])
        gap_is_empty = same_file and current_index > previous_index and all(
            (str(segment["file_id"]), line_index) in empty_positions
            for line_index in range(previous_index + 1, current_index)
        )
        if gap_is_empty:
            groups[-1].append(segment)
        else:
            groups.append([segment])
    return groups


def build_chunk_plans(
    work: Iterable[dict[str, Any]],
    *,
    all_segments: Iterable[dict[str, Any]],
    config: dict[str, Any],
    prompt: str,
    payload_builder: Callable[[list[dict[str, Any]]], dict[str, Any]],
) -> list[ChunkPlan]:
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
    plans: list[ChunkPlan] = []
    for group in contiguous_groups(work, all_segments=all_segments):
        current: list[dict[str, Any]] = []
        for segment in group:
            candidate = [*current, segment]
            payload = payload_builder(candidate)
            estimated = estimate_messages(render_messages(prompt, payload), factor)
            if current and estimated > target:
                current_payload = payload_builder(current)
                current_estimate = estimate_messages(
                    render_messages(prompt, current_payload), factor
                )
                plans.append(
                    ChunkPlan(
                        file_id=str(current[0]["file_id"]),
                        segments=tuple(current),
                        payload=current_payload,
                        estimated_input_tokens=current_estimate,
                    )
                )
                current = [segment]
                payload = payload_builder(current)
                estimated = estimate_messages(render_messages(prompt, payload), factor)
            if estimated > input_limit:
                raise ConfigError(
                    f"单 Segment Prompt 超过模型硬限制：{segment['segment_id']}"
                )
            if (
                config["execution"]["input_tokens_per_minute"] > 0
                and estimated > config["execution"]["input_tokens_per_minute"]
            ):
                raise ConfigError(
                    f"单请求预测 Token 超过 ITPM：{segment['segment_id']}"
                )
            if len(current) == 1 and current[0] is segment:
                continue
            current = candidate
        if current:
            payload = payload_builder(current)
            estimated = estimate_messages(render_messages(prompt, payload), factor)
            plans.append(
                ChunkPlan(
                    file_id=str(current[0]["file_id"]),
                    segments=tuple(current),
                    payload=payload,
                    estimated_input_tokens=estimated,
                )
            )
    return plans


def materialize_chunks(run_id: str, stage: str, plans: list[ChunkPlan]) -> list[ChunkPlan]:
    code = STAGE_CODES[stage]
    return [
        replace(
            plan,
            chunk_id=(
                f"CHK-{run_id}-{code}-{plan.file_id}-C{index:05d}"
            ),
        )
        for index, plan in enumerate(plans, start=1)
    ]


def create_run(
    project: Path,
    *,
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
        **(details or {}),
        started_at=utc_now(),
        completed_at=None,
    )
    atomic_write_json(run_dir / "manifest.json", manifest)
    return run_id, run_dir


def find_running_runs(project: Path, stage: str) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    runs_dir = project / "runs"
    if not runs_dir.exists():
        return runs
    for run_dir in runs_dir.iterdir():
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = read_json(manifest_path)
        if manifest.get("stage") == stage and manifest.get("status") == "running":
            runs.append(manifest)
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
        old_config = load_config(project / "runs" / run_id / "config.toml")
        current_config = load_config(project / "config.toml")
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
        force=False,
        dry_run=dry_run,
    )


def continue_run(
    project: Path,
    run_id: str,
    *,
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


def finalize_run(
    run_dir: Path,
    *,
    status: str,
    completed: int,
    failed: int,
    warnings: list[str] | None = None,
) -> None:
    manifest = read_json(run_dir / "manifest.json")
    manifest.update(
        status=status,
        completed_segment_count=completed,
        failed_segment_count=failed,
        warnings=warnings or [],
        completed_at=utc_now(),
    )
    atomic_write_json(run_dir / "manifest.json", manifest)


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

    async def acquire(self, estimated_tokens: int) -> float:
        waited = 0.0
        if self.requests_per_minute == 0 and self.input_tokens_per_minute == 0:
            return waited
        if (
            self.input_tokens_per_minute > 0
            and estimated_tokens > self.input_tokens_per_minute
        ):
            raise ConfigError("单请求预测 Token 超过 ITPM")
        while True:
            async with self.lock:
                now = self.clock()
                while self.records and now - self.records[0][0] >= 60:
                    self.records.popleft()
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
                if not request_full and not token_full:
                    self.records.append((now, estimated_tokens))
                    return waited
                wait = max(0.01, 60 - (now - self.records[0][0]))
            await self.sleeper(wait)
            waited += wait


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
        self.log_lock = asyncio.Lock()
        self.send_count = 0
        self.warnings: list[str] = []
        self._reported_output_clamp = False
        self.logger = get_logger(stage)

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
    ) -> tuple[str, str]:
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
        payload = {
            "model": self.config["llm"]["model"],
            "messages": messages,
            "temperature": temperature,
            "max_tokens": effective_output,
            "stream": False,
        }
        url = (
            self.config["llm"]["base_url"].rstrip("/")
            + "/"
            + self.config["llm"]["endpoint"].lstrip("/")
        )
        attempts = int(self.config["retry"]["http_max_attempts"])
        for attempt in range(1, attempts + 1):
            waited = await self.limiter.acquire(estimated_input_tokens)
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
                        headers={"Authorization": f"Bearer {api_key}"},
                        json=payload,
                    )
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
                    raise ExternalError(f"HTTP 请求重试耗尽：{exc}") from exc
                await self._backoff(attempt)
                continue
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
                    response_data = {
                        "choices": [{"message": {"content": "{invalid json"}}]
                    }
                elif (
                    debug["enabled"]
                    and debug["inject_missing_segment_every"]
                    and self.send_count % debug["inject_missing_segment_every"] == 0
                ):
                    try:
                        content = response_data["choices"][0]["message"]["content"]
                        lines = extract_jsonl_content(str(content)).splitlines()
                        segment_indexes = []
                        for index, line in enumerate(lines):
                            value = json.loads(line)
                            if isinstance(value, dict) and value.get("type") == "segment":
                                segment_indexes.append(index)
                        if segment_indexes:
                            lines.pop(segment_indexes[-1])
                            response_data["choices"][0]["message"]["content"] = "\n".join(
                                lines
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
                    content = response_data["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as exc:
                    raise ExternalError("响应缺少 choices[0].message.content") from exc
                if not isinstance(content, str):
                    raise ExternalError("LLM 响应正文不是字符串")
                self.logger.info(
                    "request complete request=%s attempt=%d status=%d elapsed=%.2fs",
                    request_id,
                    attempt,
                    response.status_code,
                    elapsed,
                )
                return content, request_id
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
                raise ExternalError(f"LLM 请求失败：HTTP {response.status_code}")
            self.logger.warning(
                "request retry request=%s attempt=%d status=%d elapsed=%.2fs",
                request_id,
                attempt,
                response.status_code,
                elapsed,
            )
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = float(retry_after)
                    self.logger.info(
                        "retry-after request=%s wait=%.2fs", request_id, delay
                    )
                    await self.sleeper(delay)
                    continue
                except ValueError:
                    pass
            await self._backoff(attempt)
        raise ExternalError("HTTP 请求重试耗尽")

    async def _backoff(self, attempt: int) -> None:
        delay = min(
            float(self.config["retry"]["max_delay_seconds"]),
            float(self.config["retry"]["base_delay_seconds"]) * (2 ** (attempt - 1)),
        )
        delay += random.uniform(0, float(self.config["retry"]["jitter_seconds"]))
        self.logger.info("retry backoff attempt=%d wait=%.2fs", attempt, delay)
        await self.sleeper(delay)


async def dispatch_chunks(
    chunks: list[ChunkPlan],
    worker: Callable[[ChunkPlan], Awaitable[Any]],
    *,
    mode: str,
    max_parallel: int,
) -> list[Any]:
    semaphore = asyncio.Semaphore(max_parallel)

    async def guarded(chunk: ChunkPlan) -> Any:
        async with semaphore:
            return await worker(chunk)

    if mode == "parallel":
        return list(await asyncio.gather(*(guarded(chunk) for chunk in chunks)))
    if mode != "ordered_by_file":
        raise ConfigError(f"未知调度模式：{mode}")
    by_file: dict[str, list[ChunkPlan]] = defaultdict(list)
    for chunk in chunks:
        by_file[chunk.file_id].append(chunk)

    async def file_chain(file_chunks: list[ChunkPlan]) -> list[Any]:
        values = []
        for chunk in file_chunks:
            values.append(await guarded(chunk))
        return values

    nested = await asyncio.gather(*(file_chain(value) for value in by_file.values()))
    return [item for group in nested for item in group]


_SUPPORTED_FENCE_LABELS = {"", "jsonl", "ndjson", "json"}
_FENCE_RE = re.compile(
    r"```[ \t]*(?P<label>[^\r\n`]*)\r?\n(?P<body>.*?)```",
    re.DOTALL,
)


def extract_jsonl_content(content: str) -> str:
    normalized = content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    for match in _FENCE_RE.finditer(normalized):
        label = match.group("label").strip().casefold()
        body = match.group("body").strip()
        if label in _SUPPORTED_FENCE_LABELS and body:
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
