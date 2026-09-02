from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import LLM_STAGES, load_project_config, load_run_config
from .diagnostics import Diagnostics
from .errors import (
    AppError,
    ConfigError,
    UsageError,
    app_error_payload,
    internal_error_payload,
)
from .execution import (
    Scope,
    choose_running_run,
    combine_usage,
    find_running_runs,
    select_scope,
    stage_fingerprint,
    unavailable_usage,
)
from .llm_client import SlidingWindowLimiter
from .llm_keys import KeyPool
from .llm_preset import endpoint_url
from .locking import project_write_lock
from .logging_utils import get_logger
from .project import load_segments, load_source_files
from .sqlite_storage import (
    latest_stage_summary,
    read_json,
    read_jsonl,
    record_exists,
    utc_now,
)
from .stages import run_all
from .stage_review import run_review
from .stage_terminology import run_terminology
from .stage_translation import run_translation
from .stage_runtime import prompt_middle_digests
from .term_library import load_terms
from .term_decision import STAGE as TERMINOLOGY_DECISION_STAGE
from .term_decision import (
    _decision_fingerprint,
    decision_checkpoint_progress,
    decision_plan,
    decision_resume_compatibility,
    run_terminology_decision,
)
from .term_decision_drafts import current_decision_draft, manual_review_state


def _endpoint_summary(config: dict[str, Any]) -> dict[str, str]:
    return {
        "model": str(config["llm"]["model"]),
        "endpoint": endpoint_url(
            config["llm"]["base_url"],
            config["llm"]["endpoint"],
            model=config["llm"]["model"],
        ),
    }


def _require_decision_library(project: Path) -> dict[str, Any]:
    library = load_terms(project)
    if library is None or not library.get("terms"):
        raise UsageError("没有已发布术语库可供自动决策")
    return library


def _running_run(
    project: Path, stage: str, current_config: dict[str, Any]
) -> dict[str, Any] | None:
    candidates = find_running_runs(project, stage)
    if not candidates:
        return None
    manifest = candidates[0]
    run_id = str(manifest["run_id"])
    old_config = load_run_config(project / "runs" / run_id)
    result = {
        "run_id": run_id,
        "started_at": manifest.get("started_at"),
        "scope": manifest.get("scope"),
        "previous": _endpoint_summary(old_config),
        "current": _endpoint_summary(current_config),
    }
    if isinstance(manifest.get("last_interruption"), dict):
        result["last_interruption"] = manifest["last_interruption"]
    return result


def _stage_summary(
    project: Path,
    stage: str,
    config: dict[str, Any],
    *,
    active_segment_ids: set[str],
    nonempty_count: int,
    terms_revision: int | None,
) -> dict[str, Any]:
    summary = latest_stage_summary(project, stage, active_segment_ids)
    completed = {
        segment_id: item for segment_id, item in summary.items() if item["completed"]
    }
    failed = {
        segment_id for segment_id, item in summary.items() if item["failed"]
    }
    current_fingerprint = stage_fingerprint(
        config,
        stage,
        prompt_middle_digests(project, stage),
        terms_revision=terms_revision,
    )
    return {
        "completed": len(completed),
        "failed": len(failed),
        "pending": nonempty_count - len(completed) - len(failed),
        "current_fingerprint_completed": sum(
            item["stage_fingerprint"] == current_fingerprint
            for item in completed.values()
        ),
    }


def _terminology_summary(
    project: Path,
    config: dict[str, Any],
    *,
    active_segment_ids: set[str],
    nonempty_count: int,
) -> dict[str, Any]:
    base = {
        "completed": 0,
        "failed": 0,
        "pending": nonempty_count,
        "current_fingerprint_completed": 0,
    }
    active_path = project / "terminology" / "active_task.json"
    if not record_exists(project, active_path):
        return base
    active = read_json(project, active_path)
    if active.get("status") not in {"active", "completed"}:
        return base
    scans = [
        item
        for item in read_jsonl(
            project,
            project / "terminology" / "scans.jsonl",
            task_id=active.get("active_task_id"),
        )
        if str(item.get("segment_id")) in active_segment_ids
    ]
    completed = {
        item["segment_id"] for item in scans if item["status"] == "completed"
    }
    failed = {
        item["segment_id"]
        for item in scans
        if item["status"] == "failed" and item["segment_id"] not in completed
    }
    current_fingerprint = stage_fingerprint(
        config,
        "terminology",
        prompt_middle_digests(project, "terminology"),
    )
    return {
        "completed": len(completed),
        "failed": len(failed),
        "pending": nonempty_count - len(completed) - len(failed),
        "current_fingerprint_completed": sum(
            item.get("status") == "completed"
            and item.get("stage_fingerprint") == current_fingerprint
            for item in scans
        ),
    }


def task_options(project: Path, stage: str) -> dict[str, Any]:
    if stage == TERMINOLOGY_DECISION_STAGE:
        library = _require_decision_library(project)
        overrides = read_json(
            project, project / "terminology" / "overrides.json"
        )
        protected = {
            str(item["normalized"])
            for item in overrides.get("overrides", [])
        }
        has_eligible = any(
            str(item["normalized"]) not in protected
            and not bool(item.get("disabled", False))
            for item in library.get("terms", [])
        )
        config = load_project_config(project, stage=stage)
        plan = decision_plan(project) if has_eligible else None
        selected = len(plan["eligible"]) if plan else 0
        running_run = _running_run(project, stage, config)
        if running_run is not None:
            compatible, reason = decision_resume_compatibility(
                project,
                str(running_run["run_id"]),
                source_terms_revision=int(library["terms_revision"]),
            )
            running_run["completed_steps"] = decision_checkpoint_progress(
                project, str(running_run["run_id"])
            )
            running_run["total_steps"] = selected * 2
            running_run["resume_compatible"] = compatible
            running_run["resume_incompatibility_reason"] = reason
        return {
            "stage": stage,
            "preset": {
                "id": str(config["_llm_preset_id"]),
                "model": str(config["llm"]["model"]),
            },
            "selected": selected,
            "protected": len(plan["protected"]) if plan else len(protected),
            "overflow_policy": {
                "allow_soft_target_overflow": bool(
                    config["terminology_decision"]["allow_soft_target_overflow"]
                ),
                "anchor_overflow_mode": str(
                    config["terminology_decision"]["anchor_overflow_mode"]
                ),
            },
            "completed": 0,
            "pending": selected,
            "failed": 0,
            "current_fingerprint_completed": 0,
            "mismatched_fingerprint_completed": 0,
            "running_run": running_run,
            "has_pending_draft": current_decision_draft(project) is not None,
            "estimated_requests": int(plan["estimated_requests"]) if plan else 0,
            "estimated_input_tokens": (
                int(plan["estimated_input_tokens"]) if plan else 0
            ),
        }
    if stage not in LLM_STAGES:
        raise UsageError(f"未知 Web 阶段：{stage}")
    segments = load_segments(project)
    nonempty = [item for item in segments if not item["is_empty"]]
    if not nonempty:
        raise UsageError("项目没有可处理的非空 Segment；请先添加源文件")
    active_segment_ids = {str(item["segment_id"]) for item in nonempty}
    config = load_project_config(project, stage=stage)
    if stage == "terminology":
        summary = _terminology_summary(
            project,
            config,
            active_segment_ids=active_segment_ids,
            nonempty_count=len(nonempty),
        )
    else:
        library = load_terms(project)
        summary = _stage_summary(
            project,
            stage,
            config,
            active_segment_ids=active_segment_ids,
            nonempty_count=len(nonempty),
            terms_revision=(
                int(library["terms_revision"]) if library else None
            ),
        )
    completed = summary["completed"]
    current_completed = summary["current_fingerprint_completed"]
    return {
        "stage": stage,
        "preset": {
            "id": str(config["_llm_preset_id"]),
            "model": str(config["llm"]["model"]),
        },
        "selected": len(nonempty),
        "completed": completed,
        "pending": summary["pending"],
        "failed": summary["failed"],
        "current_fingerprint_completed": current_completed,
        "mismatched_fingerprint_completed": max(
            0, completed - current_completed
        ),
        "running_run": _running_run(project, stage, config),
    }


def _stage_fingerprint_snapshot(project: Path, stage: str) -> str:
    config = load_project_config(project, stage=stage)
    library = load_terms(project)
    terms_revision = (
        int(library["terms_revision"])
        if stage != "terminology" and library is not None
        else None
    )
    return stage_fingerprint(
        config,
        stage,
        prompt_middle_digests(project, stage),
        terms_revision=terms_revision,
    )


def _decision_fingerprint_snapshot(
    project: Path, prompt_language: str | None
) -> str:
    plan = decision_plan(project, prompt_language)
    return _decision_fingerprint(plan["config"], plan["prompts"], plan["library"])


def _stable_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _selection_snapshot(
    project: Path,
    scope: Scope,
    *,
    force_all: bool = False,
) -> tuple[tuple[str, str, int, int, str, str, str, str], ...]:
    files = load_source_files(project)
    segments = load_segments(project)
    selected = (
        [segment for segment in segments if not segment["is_empty"]]
        if force_all
        else select_scope(segments, files, scope)
    )
    file_order = {
        str(item["file_id"]): int(item["file_order"]) for item in files
    }
    adapter_snapshots: dict[str, str] = {}
    for file_record in files:
        state_path = file_record.get("document_adapter_state")
        state_record = (
            read_json(project, project / state_path)
            if isinstance(state_path, str)
            and record_exists(project, project / state_path)
            else None
        )
        adapter_snapshots[str(file_record["file_id"])] = _stable_digest(
            {
                "adapter_id": file_record.get("document_adapter_id"),
                "adapter_version": file_record.get("document_adapter_version"),
                "state_path": state_path,
                "state": state_record,
            }
        )
    return tuple(
        (
            str(segment["segment_id"]),
            str(segment["file_id"]),
            file_order[str(segment["file_id"])],
            int(segment["line_index"]),
            str(segment["part_id"]),
            str(segment["source"]),
            str(segment.get("model_source") or ""),
            adapter_snapshots[str(segment["file_id"])],
        )
        for segment in selected
    )


def _running_runs_snapshot(
    project: Path,
    stages: tuple[str, ...],
) -> tuple[tuple[str, str, str, str], ...]:
    snapshots: list[tuple[str, str, str, str]] = []
    for stage in stages:
        for manifest in find_running_runs(project, stage):
            run_id = str(manifest["run_id"])
            stable = {
                "stage": stage,
                "run_id": run_id,
                "status": manifest.get("status"),
                "started_at": manifest.get("started_at"),
                "stage_fingerprint": manifest.get("stage_fingerprint"),
                "scope": manifest.get("scope"),
                "selected_segment_count": manifest.get("selected_segment_count"),
                "requested_segment_count": manifest.get("requested_segment_count"),
                "reused_segment_count": manifest.get("reused_segment_count"),
            }
            snapshots.append(
                (
                    stage,
                    run_id,
                    str(manifest.get("status", "")),
                    _stable_digest(stable),
                )
            )
    return tuple(snapshots)


def _decision_input_snapshot(plan: dict[str, Any]) -> str:
    return _stable_digest(
        {
            "terms_revision": plan["library"]["terms_revision"],
            "overrides": plan["overrides_document"],
            "protected": sorted(str(value) for value in plan["protected"]),
            "eligible": plan["eligible"],
            "states": plan["states"],
            "source_conflicts": plan["source_conflicts"],
            "evidence": plan["evidence"],
            "language": plan["language"],
        }
    )


def _run_all_includes_terminology(
    project: Path,
    selection_snapshots: tuple[
        tuple[str, tuple[tuple[str, str, int, int, str, str, str, str], ...]], ...
    ],
    *,
    force: bool,
) -> bool:
    terms = load_terms(project)
    active_path = project / "terminology" / "active_task.json"
    active = (
        read_json(project, active_path)
        if record_exists(project, active_path)
        else None
    )
    include = force or terms is None
    if active and active.get("status") == "active":
        return True
    if not active or active.get("status") != "completed":
        return include
    selected = dict(selection_snapshots).get("terminology", ())
    selected_ids = {item[0] for item in selected}
    scans = read_jsonl(
        project,
        project / "terminology" / "scans.jsonl",
        task_id=str(active.get("active_task_id", "")),
    )
    completed_ids = {
        str(item["segment_id"])
        for item in scans
        if item.get("status") == "completed"
    }
    return bool(selected_ids - completed_ids)


@dataclass(frozen=True)
class _StartDecision:
    selected_count: int
    running_run_id: str | None
    fingerprints: tuple[tuple[str, str], ...]
    selection_snapshots: tuple[
        tuple[str, tuple[tuple[str, str, int, int, str, str, str, str], ...]], ...
    ]
    running_runs: tuple[tuple[str, str, str, str], ...]
    decision_inputs: str | None = None
    options_selected_count: int | None = None


@dataclass
class WebTask:
    task_id: str
    project: Path
    project_id: str
    stage: str
    status: str = "queued"
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    completed_at: str | None = None
    summary: dict[str, Any] | None = None
    error: dict[str, object] | None = None
    completed_segments: int = 0
    failed_segments: int = 0
    total_segments: int = 0
    failure_counts: dict[str, int] = field(default_factory=dict)
    usage: dict[str, Any] = field(
        default_factory=lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "available": False,
            "partial": False,
        }
    )
    asyncio_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _scope: Scope = field(default_factory=Scope, repr=False)
    _reuse_mixed_fingerprints: bool = field(default=False, repr=False)
    _run_action: str | None = field(default=None, repr=False)
    _prompt_language: str | None = field(default=None, repr=False)
    _replace_draft: bool = field(default=False, repr=False)
    _acknowledge_manual_review: bool = field(default=False, repr=False)
    _start_decision: _StartDecision | None = field(default=None, repr=False)

    def view(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "project": self.project.name,
            "project_id": self.project_id,
            "stage": self.stage,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "summary": self.summary,
            "error": self.error,
            "completed_segments": self.completed_segments,
            "failed_segments": self.failed_segments,
            "pending_segments": max(
                0,
                self.total_segments
                - self.completed_segments
                - self.failed_segments,
            ),
            "total_segments": self.total_segments,
            "failure_counts": dict(self.failure_counts),
            "usage": self.usage,
        }


@dataclass
class _LimiterEntry:
    limiter: SlidingWindowLimiter
    requests_per_minute: int
    input_tokens_per_minute: int
    leases: int = 0
    released_at: float | None = None


class SharedLimiterPool:
    """Share provider rate-limit windows among Web tasks in one process."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retention_seconds: float = 60.0,
    ) -> None:
        self.clock = clock
        self.sleeper = sleeper
        self.retention_seconds = retention_seconds
        self.entries: dict[tuple[str, str], _LimiterEntry] = {}

    @staticmethod
    def _key(config: dict[str, Any]) -> tuple[str, str]:
        try:
            return (
                str(config["_llm_preset_id"]),
                str(config["_llm_preset_hash"]),
            )
        except KeyError as exc:
            raise UsageError("LLM Preset 缺少共享限流身份") from exc

    def _prune(self, now: float) -> None:
        expired = [
            key
            for key, entry in self.entries.items()
            if entry.leases == 0
            and entry.released_at is not None
            and now - entry.released_at >= self.retention_seconds
        ]
        for key in expired:
            self.entries.pop(key, None)

    def acquire(
        self, config: dict[str, Any]
    ) -> tuple[SlidingWindowLimiter, Callable[[], None]]:
        now = self.clock()
        self._prune(now)
        key = self._key(config)
        execution = config["execution"]
        requests_per_minute = int(execution["requests_per_minute"])
        input_tokens_per_minute = int(execution["input_tokens_per_minute"])
        entry = self.entries.get(key)
        if entry is None:
            entry = _LimiterEntry(
                limiter=SlidingWindowLimiter(
                    requests_per_minute,
                    input_tokens_per_minute,
                    clock=self.clock,
                    sleeper=self.sleeper,
                ),
                requests_per_minute=requests_per_minute,
                input_tokens_per_minute=input_tokens_per_minute,
            )
            self.entries[key] = entry
        elif (
            entry.requests_per_minute != requests_per_minute
            or entry.input_tokens_per_minute != input_tokens_per_minute
        ):
            raise ConfigError("相同 Preset 身份的共享限流配置不一致")
        entry.leases += 1
        entry.released_at = None
        released = False

        def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            current = self.entries.get(key)
            if current is None:
                return
            current.leases = max(0, current.leases - 1)
            if current.leases == 0:
                current.released_at = self.clock()

        return entry.limiter, release

@dataclass
class _KeyPoolEntry:
    pool: KeyPool
    requests_per_minute: int
    input_tokens_per_minute: int
    max_parallel: int
    max_parallel_per_key: int
    leases: int = 0
    released_at: float | None = None


class SharedKeyPool:
    """Share per-key windows and both concurrency caps within one process."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retention_seconds: float = 60.0,
    ) -> None:
        self.clock = clock
        self.sleeper = sleeper
        self.retention_seconds = retention_seconds
        self.entries: dict[tuple[str, str], _KeyPoolEntry] = {}

    @staticmethod
    def _key(config: dict[str, Any]) -> tuple[str, str]:
        try:
            return (
                str(config["_llm_preset_id"]),
                str(config["_llm_preset_hash"]),
            )
        except KeyError as exc:
            raise UsageError("LLM Preset 缺少共享限流身份") from exc

    def _prune(self, now: float) -> None:
        expired = [
            key
            for key, entry in self.entries.items()
            if entry.leases == 0
            and entry.released_at is not None
            and now - entry.released_at >= self.retention_seconds
            and not entry.pool.has_unexpired_cooldown(now)
        ]
        for key in expired:
            self.entries.pop(key, None)

    def acquire(self, config: dict[str, Any]) -> tuple[KeyPool, Callable[[], None]]:
        now = self.clock()
        self._prune(now)
        key = self._key(config)
        execution = config["execution"]
        values = {
            "requests_per_minute": int(execution["requests_per_minute"]),
            "input_tokens_per_minute": int(execution["input_tokens_per_minute"]),
            "max_parallel": int(execution["max_parallel"]),
            "max_parallel_per_key": int(
                execution.get("max_parallel_per_key", execution["max_parallel"])
            ),
        }
        entry = self.entries.get(key)
        if entry is None:
            entry = _KeyPoolEntry(
                pool=KeyPool(**values, clock=self.clock, sleeper=self.sleeper),
                **values,
            )
            self.entries[key] = entry
        elif any(getattr(entry, name) != value for name, value in values.items()):
            raise ConfigError("相同 Preset 身份的共享限流配置不一致")
        entry.leases += 1
        entry.released_at = None
        released = False

        def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            current = self.entries.get(key)
            if current is None:
                return
            current.leases = max(0, current.leases - 1)
            if current.leases == 0:
                current.released_at = self.clock()

        return entry.pool, release


class WebTaskManager:
    def __init__(
        self,
        diagnostics: Diagnostics | None = None,
        *,
        max_active_projects: int = 2,
        limiter_pool: SharedKeyPool | SharedLimiterPool | None = None,
    ) -> None:
        if (
            not isinstance(max_active_projects, int)
            or isinstance(max_active_projects, bool)
            or max_active_projects < 1
        ):
            raise UsageError("max_active_projects 必须是正整数")
        self.tasks: dict[str, WebTask] = {}
        self.active_by_project: dict[Path, str] = {}
        self.queued_task_ids: deque[str] = deque()
        self.running_task_ids: set[str] = set()
        self.max_active_projects = max_active_projects
        self.guard = asyncio.Lock()
        self.diagnostics = diagnostics
        self.limiter_pool = limiter_pool or SharedKeyPool()
        self._shutting_down = False

    def _acquire_limiter(
        self, config: dict[str, Any]
    ) -> tuple[KeyPool | SlidingWindowLimiter, Callable[[], None]]:
        return self.limiter_pool.acquire(config)

    def _validate_start(
        self,
        project: Path,
        stage: str,
        *,
        scope: Scope,
        reuse_mixed_fingerprints: bool,
        run_action: str | None,
        acknowledge_manual_review: bool,
        ensure_unique: bool,
        prompt_language: str | None,
    ) -> _StartDecision:
        if stage not in {
            "terminology",
            "translation",
            "proofreading",
            "polishing",
            TERMINOLOGY_DECISION_STAGE,
            "run-all",
        }:
            raise UsageError(f"未知后台阶段：{stage}")
        force = scope.force
        if force and reuse_mixed_fingerprints:
            raise UsageError("force 与 reuse_mixed_fingerprints 不能同时使用")
        if run_action not in {None, "resume", "decline"}:
            raise UsageError("run_action 必须是 resume、decline 或 null")
        if stage == "run-all" and run_action is not None:
            raise UsageError("run-all 不支持 run_action")
        if stage == TERMINOLOGY_DECISION_STAGE and reuse_mixed_fingerprints:
            raise UsageError("自动术语决策不支持复用已发布结果")
        if ensure_unique:
            active_id = self.active_by_project.get(project)
            if active_id is not None:
                active = self.tasks[active_id]
                if active.status in {"queued", "running", "cancelling"}:
                    raise UsageError(f"项目已有后台任务：{active.task_id}")
        selected_count = 0
        running_run_id: str | None = None
        decision_plan_snapshot: dict[str, Any] | None = None
        selection_snapshots: tuple[
            tuple[str, tuple[tuple[str, str, int, int, str, str, str, str], ...]], ...
        ] = ()
        options_selected_count: int | None = None
        if stage == TERMINOLOGY_DECISION_STAGE:
            decision_plan_snapshot = decision_plan(project, prompt_language)
            library = decision_plan_snapshot["library"]
            selected_count = len(decision_plan_snapshot["eligible"]) * 2
            current_terms_revision = int(library["terms_revision"])
            if (
                run_action != "resume"
                and not acknowledge_manual_review
                and manual_review_state(project)["remaining"] > 0
            ):
                raise UsageError(
                    "存在未处理人工待办；请先确认新一轮决策会在成功应用后取代旧队列"
                )
            running = find_running_runs(project, stage)
            if running:
                running_run_id = str(running[0]["run_id"])
            if run_action == "resume":
                if not running:
                    raise UsageError(f"{stage} 没有可续用的 running Run")
                if force:
                    raise UsageError("续用 Run 时不能同时指定 force")
                compatible, reason = decision_resume_compatibility(
                    project,
                    str(running[0]["run_id"]),
                    source_terms_revision=current_terms_revision,
                )
                if not compatible:
                    raise UsageError(f"{reason}；请结束旧 Run 并强制新建")
            elif running:
                if run_action != "decline" or not force:
                    raise UsageError("发现未完成 Run，必须选择续用或强制重做全部")
            elif run_action == "decline" and not force:
                raise UsageError("结束自动决策 Run 时必须同时指定 force")
        elif stage != "run-all":
            options = task_options(project, stage)
            options_selected_count = int(options["selected"])
            selection = _selection_snapshot(
                project,
                scope,
                force_all=stage == "terminology" and scope.force,
            )
            selected_count = len(selection)
            selection_snapshots = ((stage, selection),)
            running_run = options["running_run"]
            if running_run is not None:
                running_run_id = str(running_run["run_id"])
            if run_action == "resume":
                if running_run is None:
                    raise UsageError(f"{stage} 没有可续用的 running Run")
                if force or reuse_mixed_fingerprints:
                    raise UsageError("续用 Run 时不能同时指定 force 或复用结果")
            else:
                if running_run is not None and run_action != "decline":
                    raise UsageError("发现未完成 Run，必须选择续用或结束并新建")
                if (
                    options["mismatched_fingerprint_completed"]
                    and not force
                    and not reuse_mixed_fingerprints
                ):
                    raise UsageError(
                        "存在不同设置指纹的已完成结果，必须明确选择复用或 force"
                    )
        if stage == TERMINOLOGY_DECISION_STAGE:
            fingerprints = (
                (stage, _decision_fingerprint_snapshot(project, prompt_language)),
            )
            decision_inputs = _decision_input_snapshot(decision_plan_snapshot)
        elif stage == "run-all":
            selection_snapshots = tuple(
                (
                    item,
                    _selection_snapshot(
                        project,
                        scope,
                        force_all=item == "terminology" and scope.force,
                    ),
                )
                for item in LLM_STAGES
            )
            include_terminology = _run_all_includes_terminology(
                project,
                selection_snapshots,
                force=scope.force,
            )
            selected_count = sum(
                len(selection)
                for item, selection in selection_snapshots
                if item != "terminology" or include_terminology
            )
            fingerprints = tuple(
                (item, _stage_fingerprint_snapshot(project, item))
                for item in LLM_STAGES
            )
            decision_inputs = None
        else:
            fingerprints = ((stage, _stage_fingerprint_snapshot(project, stage)),)
            decision_inputs = None
        relevant_stages = (
            LLM_STAGES
            if stage == "run-all"
            else (stage,)
        )
        return _StartDecision(
            selected_count=selected_count,
            running_run_id=running_run_id,
            fingerprints=fingerprints,
            selection_snapshots=selection_snapshots,
            running_runs=_running_runs_snapshot(project, relevant_stages),
            decision_inputs=decision_inputs,
            options_selected_count=options_selected_count,
        )

    def _dispatch_locked(self) -> None:
        if self._shutting_down:
            return
        while (
            len(self.running_task_ids) < self.max_active_projects
            and self.queued_task_ids
        ):
            task_id = self.queued_task_ids.popleft()
            state = self.tasks[task_id]
            if state.status != "queued":
                continue
            state.status = "running"
            self.running_task_ids.add(task_id)
            state.asyncio_task = asyncio.create_task(
                self._run(
                    state,
                    scope=state._scope,
                    reuse_mixed_fingerprints=state._reuse_mixed_fingerprints,
                    run_action=state._run_action,
                    prompt_language=state._prompt_language,
                    replace_draft=state._replace_draft,
                    acknowledge_manual_review=state._acknowledge_manual_review,
                )
            )
            state.asyncio_task.add_done_callback(
                lambda task, state=state: self._task_done(state, task)
            )

    def _task_done(self, state: WebTask, task: asyncio.Task[None]) -> None:
        if task.cancelled() and state.status in {"running", "cancelling"}:
            state.status = "cancelled"
            state.completed_at = state.completed_at or utc_now()
        self.running_task_ids.discard(state.task_id)
        if self.active_by_project.get(state.project) == state.task_id:
            self.active_by_project.pop(state.project, None)
        self._dispatch_locked()

    async def set_max_active_projects(self, value: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise UsageError("max_active_projects 必须是正整数")
        async with self.guard:
            self.max_active_projects = value
            self._dispatch_locked()

    async def start(
        self,
        project: Path,
        stage: str,
        *,
        scope: Scope,
        reuse_mixed_fingerprints: bool,
        run_action: str | None,
        prompt_language: str | None = None,
        replace_draft: bool = False,
        acknowledge_manual_review: bool = False,
    ) -> dict[str, Any]:
        async with self.guard:
            if self._shutting_down:
                raise UsageError("任务管理器正在关闭")
            decision = self._validate_start(
                project,
                stage,
                scope=scope,
                reuse_mixed_fingerprints=reuse_mixed_fingerprints,
                run_action=run_action,
                acknowledge_manual_review=acknowledge_manual_review,
                ensure_unique=True,
                prompt_language=prompt_language,
            )
            task_id = f"TASK-{uuid.uuid4().hex[:12].upper()}"
            state = WebTask(
                task_id=task_id,
                project=project,
                project_id=str(
                    read_json(project, project / "project.json")["project_id"]
                ),
                stage=stage,
                total_segments=decision.selected_count,
            )
            state._scope = scope
            state._reuse_mixed_fingerprints = reuse_mixed_fingerprints
            state._run_action = run_action
            state._prompt_language = prompt_language
            state._replace_draft = replace_draft
            state._acknowledge_manual_review = acknowledge_manual_review
            state._start_decision = decision
            self.tasks[task_id] = state
            self.active_by_project[project] = task_id
            self.queued_task_ids.append(task_id)
            self._dispatch_locked()
            return state.view()

    async def _run(
        self,
        state: WebTask,
        *,
        scope: Scope,
        reuse_mixed_fingerprints: bool,
        run_action: str | None,
        prompt_language: str | None = None,
        replace_draft: bool = False,
        acknowledge_manual_review: bool = False,
    ) -> None:
        state.started_at = utc_now()
        usage_base: dict[str, Any] | None = None
        resuming = False
        limiter_releases: list[Callable[[], None]] = []

        def progress(completed: int, failed: int, total: int) -> None:
            state.completed_segments = completed
            state.failed_segments = failed
            state.total_segments = total

        def usage_changed(current: dict[str, Any] | None) -> None:
            state.usage = _task_usage(usage_base, current, resuming=resuming)
            if self.diagnostics is not None:
                self.diagnostics.set_usage(state.usage)

        try:
            diagnostics_context = (
                self.diagnostics.activate(
                    state.project.name, state.stage, task_id=state.task_id
                )
                if self.diagnostics is not None
                else nullcontext()
            )
            with diagnostics_context, project_write_lock(state.project):
                decision = self._validate_start(
                    state.project,
                    state.stage,
                    scope=scope,
                    reuse_mixed_fingerprints=reuse_mixed_fingerprints,
                    run_action=run_action,
                    acknowledge_manual_review=acknowledge_manual_review,
                    ensure_unique=False,
                    prompt_language=prompt_language,
                )
                if (
                    state._start_decision is not None
                    and decision != state._start_decision
                ):
                    raise UsageError("排队期间项目选择或设置已变化")
                shared_limiters: dict[tuple[str, str], SlidingWindowLimiter] = {}
                if state.stage == "run-all":
                    for stage in LLM_STAGES:
                        config = load_project_config(state.project, stage=stage)
                        key = (
                            str(config["_llm_preset_id"]),
                            str(config["_llm_preset_hash"]),
                        )
                        limiter, release = self._acquire_limiter(config)
                        if key in shared_limiters:
                            release()
                            continue
                        shared_limiters[key] = limiter
                        limiter_releases.append(release)
                else:
                    config = load_project_config(state.project, stage=state.stage)
                    limiter, release = self._acquire_limiter(config)
                    shared_limiters[
                        (
                            str(config["_llm_preset_id"]),
                            str(config["_llm_preset_hash"]),
                        )
                    ] = limiter
                    limiter_releases.append(release)
                resume_run_id = None
                if state.stage != "run-all":
                    resume_run_id, _ = choose_running_run(
                        state.project,
                        state.stage,
                        action=run_action,
                        dry_run=False,
                        interactive=False,
                    )
                    if resume_run_id is not None:
                        resuming = True
                        manifest = read_json(
                            state.project,
                            state.project
                            / "runs"
                            / resume_run_id
                            / "manifest.json",
                        )
                        if type(manifest.get("usage_invocation_count")) is int:
                            raw_usage = manifest.get("usage")
                            if isinstance(raw_usage, dict):
                                usage_base = raw_usage
                if state.stage == TERMINOLOGY_DECISION_STAGE:
                    summary = await run_terminology_decision(
                        state.project,
                        replace_draft=replace_draft,
                        resume_run_id=resume_run_id,
                        prompt_language=prompt_language,
                        on_progress=progress,
                        on_usage=usage_changed,
                        limiter=next(iter(shared_limiters.values())),
                    )
                elif state.stage == "terminology":
                    summary = await run_terminology(
                        state.project,
                        scope,
                        resume_run_id=resume_run_id,
                        reuse_mixed_fingerprints=reuse_mixed_fingerprints,
                        prompt_language=prompt_language,
                        on_progress=progress,
                        on_usage=usage_changed,
                        limiter=next(iter(shared_limiters.values())),
                    )
                elif state.stage == "translation":
                    summary = await run_translation(
                        state.project,
                        scope,
                        resume_run_id=resume_run_id,
                        reuse_mixed_fingerprints=reuse_mixed_fingerprints,
                        prompt_language=prompt_language,
                        on_progress=progress,
                        on_usage=usage_changed,
                        limiter=next(iter(shared_limiters.values())),
                    )
                elif state.stage in {"proofreading", "polishing"}:
                    summary = await run_review(
                        state.project,
                        state.stage,
                        scope,
                        resume_run_id=resume_run_id,
                        reuse_mixed_fingerprints=reuse_mixed_fingerprints,
                        prompt_language=prompt_language,
                        on_progress=progress,
                        on_usage=usage_changed,
                        limiter=next(iter(shared_limiters.values())),
                    )
                else:
                    summary = await run_all(
                        state.project,
                        scope,
                        reuse_mixed_fingerprints=reuse_mixed_fingerprints,
                        prompt_language=prompt_language,
                        limiters=shared_limiters,
                        on_progress=progress,
                        on_usage=usage_changed,
                    )
            state.summary = summary
            state.completed_segments = int(summary.get("completed", 0)) + int(
                summary.get("reused", 0)
            )
            state.failed_segments = int(summary.get("failed", 0))
            state.failure_counts = {
                str(key): int(value)
                for key, value in (summary.get("failure_counts") or {}).items()
            }
            if summary.get("selected") is not None:
                state.total_segments = int(summary["selected"])
            summary_usage = summary.get("usage")
            if isinstance(summary_usage, dict):
                state.usage = summary_usage
            state.status = (
                "failed"
                if summary.get("failed") or summary.get("pending")
                else "completed"
            )
        except asyncio.CancelledError:
            state.status = "cancelled"
        except AppError as exc:
            state.status = "failed"
            state.error = app_error_payload(exc)
        except Exception as exc:
            state.status = "failed"
            state.error = internal_error_payload()
            get_logger(state.stage).exception(
                "unexpected background task error task_id=%s project=%s",
                state.task_id,
                state.project,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        finally:
            state.completed_at = utc_now()
            for release in limiter_releases:
                release()

    def get(self, task_id: str) -> dict[str, Any]:
        try:
            return self.tasks[task_id].view()
        except KeyError as exc:
            raise UsageError(f"未知后台任务：{task_id}") from exc

    def active_tasks(self) -> list[dict[str, Any]]:
        active = {
            "queued",
            "running",
            "cancelling",
        }
        states = [
            state.view()
            for state in self.tasks.values()
            if state.status in active
        ]
        return sorted(
            states,
            key=lambda value: (str(value["created_at"]), str(value["task_id"])),
        )

    def is_running(self, project: Path, stage: str) -> bool:
        return any(
            state.project == project
            and state.stage == stage
            and state.status in {"queued", "running", "cancelling"}
            for state in self.tasks.values()
        )

    def is_project_running(self, project: Path) -> bool:
        return any(
            state.project == project
            and state.status in {"queued", "running", "cancelling"}
            for state in self.tasks.values()
        )

    async def cancel(self, task_id: str) -> dict[str, Any]:
        async with self.guard:
            try:
                state = self.tasks[task_id]
            except KeyError as exc:
                raise UsageError(f"未知后台任务：{task_id}") from exc
            if state.status == "queued":
                self.queued_task_ids = deque(
                    value for value in self.queued_task_ids if value != task_id
                )
                state.status = "cancelled"
                state.completed_at = utc_now()
                if self.active_by_project.get(state.project) == task_id:
                    self.active_by_project.pop(state.project, None)
                return state.view()
            if state.status != "running" or state.asyncio_task is None:
                raise UsageError("后台任务当前不可取消")
            state.status = "cancelling"
            state.asyncio_task.cancel()
            return state.view()

    async def shutdown(self) -> None:
        async with self.guard:
            self._shutting_down = True
            queued = [
                self.tasks[task_id]
                for task_id in self.queued_task_ids
                if self.tasks[task_id].status == "queued"
            ]
            self.queued_task_ids.clear()
            for state in queued:
                state.status = "cancelled"
                state.completed_at = utc_now()
                if self.active_by_project.get(state.project) == state.task_id:
                    self.active_by_project.pop(state.project, None)
            running = [
                self.tasks[task_id]
                for task_id in self.running_task_ids
                if self.tasks[task_id].asyncio_task is not None
            ]
            for state in running:
                if state.status == "running":
                    state.status = "cancelling"
                state.asyncio_task.cancel()
        if running:
            await asyncio.gather(
                *(
                    state.asyncio_task
                    for state in running
                    if state.asyncio_task is not None
                ),
                return_exceptions=True,
            )


def _task_usage(
    previous: dict[str, Any] | None,
    current: dict[str, Any] | None,
    *,
    resuming: bool,
) -> dict[str, Any]:
    if resuming:
        return combine_usage(previous, current)
    return current or unavailable_usage()
