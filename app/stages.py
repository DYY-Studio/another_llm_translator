from __future__ import annotations






import shlex





from collections import Counter

from collections.abc import Callable, Mapping

from contextlib import AsyncExitStack


from pathlib import Path

from typing import Any

import httpx

from .config import load_project_config


from .errors import (
    ConfigError,
    IncompleteError,
)

from .term_library import (load_terms)


from .stage_runtime import (_base_results, _project_context, _require_nonempty_segments, prompt_middle_digests)
from .stage_translation import run_translation
from .stage_review import run_review
from .stage_terminology import _terminology_scan_selection, run_terminology

from .execution import (
    Scope,
    classify_stage,
    combine_usage,
    find_running_runs,
    load_stage_history,
    stage_fingerprint,
    unavailable_usage,
)


from .llm_keys import KeyPool
from .llm_client import SlidingWindowLimiter



from .project import (
    load_segments,
    load_source_files,
)

from .sqlite_storage import (
    read_json,
    read_jsonl,
    record_exists,
)


def require_success(summary: dict[str, Any]) -> None:
    if summary.get("failed") or summary.get("pending"):
        raise IncompleteError("选定范围仍有 pending 或 failed")


async def run_all(
    project: Path,
    scope: Scope,
    *,
    http_client: httpx.AsyncClient | None = None,
    reuse_mixed_fingerprints: bool = False,
    prompt_language: str | None = None,
    limiters: Mapping[tuple[str, str], SlidingWindowLimiter | KeyPool] | None = None,
    on_progress: Callable[[int, int, int], None] | None = None,
    on_usage: Callable[[dict[str, Any] | None], None] | None = None,
) -> dict[str, Any]:
    segments = load_segments(project)
    _require_nonempty_segments(segments)
    files = load_source_files(project)
    stages = ("terminology", "translation", "proofreading", "polishing")
    configs = {
        stage: load_project_config(project, stage=stage) for stage in stages
    }
    resource_keys = {
        stage: (
            str(configs[stage]["_llm_preset_id"]),
            str(configs[stage]["_llm_preset_hash"]),
        )
        for stage in stages
    }
    if limiters is None:
        local_limiters: dict[tuple[str, str], SlidingWindowLimiter | KeyPool] = {}
        local_limits: dict[tuple[str, str], tuple[int, int, int, int]] = {}
        for stage, key in resource_keys.items():
            execution = configs[stage]["execution"]
            limits = (
                int(execution["requests_per_minute"]),
                int(execution["input_tokens_per_minute"]),
                int(execution["max_parallel"]),
                int(
                    execution.get(
                        "max_parallel_per_key", execution["max_parallel"]
                    )
                ),
            )
            previous_limits = local_limits.get(key)
            if previous_limits is not None and previous_limits != limits:
                raise ConfigError("相同 Preset 身份的共享限流配置不一致")
            local_limits[key] = limits
            if key not in local_limiters:
                local_limiters[key] = KeyPool(
                    limits[0],
                    limits[1],
                    limits[2],
                    limits[3],
                )
        limiters = local_limiters
    missing_limiters = set(resource_keys.values()) - set(limiters)
    if missing_limiters:
        raise ConfigError("run-all 缺少阶段共享限流器")

    async def execute(
        clients: dict[tuple[str, str], httpx.AsyncClient] | None,
    ) -> dict[str, Any]:
        def client_for(stage: str) -> httpx.AsyncClient | None:
            if http_client is not None:
                return http_client
            return clients[resource_keys[stage]] if clients is not None else None

        summaries: list[dict[str, Any]] = []
        progress_by_stage: dict[str, tuple[int, int, int]] = {}
        usage_by_stage: dict[str, dict[str, Any] | None] = {}

        def report_progress(
            stage: str,
            completed: int,
            failed: int,
            total: int,
        ) -> None:
            progress_by_stage[stage] = (completed, failed, total)
            if on_progress is not None:
                on_progress(
                    sum(value[0] for value in progress_by_stage.values()),
                    sum(value[1] for value in progress_by_stage.values()),
                    sum(value[2] for value in progress_by_stage.values()),
                )

        def report_usage(
            stage: str,
            current: dict[str, Any] | None,
        ) -> None:
            usage_by_stage[stage] = current
            if on_usage is None:
                return
            aggregate: dict[str, Any] | None = None
            for value in usage_by_stage.values():
                aggregate = combine_usage(aggregate, value)
            on_usage(aggregate or unavailable_usage())

        def record_summary(stage: str, summary: dict[str, Any]) -> dict[str, Any]:
            selected = int(summary.get("selected", 0))
            reused = int(summary.get("reused", 0))
            completed = int(summary.get("completed", 0))
            failed = int(summary.get("failed", 0))
            pending_value = summary.get("pending")
            pending = (
                int(pending_value)
                if isinstance(pending_value, int) and not isinstance(pending_value, bool)
                else max(0, selected - reused - completed - failed)
            )
            if summary.get("dry_run") is True:
                pending = 0
            report_progress(
                stage,
                completed + reused,
                failed,
                selected,
            )
            if "usage" in summary:
                report_usage(stage, summary.get("usage"))
            elif stage not in usage_by_stage:
                report_usage(stage, None)
            normalized = {**summary, "pending": pending}
            summaries.append(normalized)
            return normalized

        def progress_for(stage: str) -> Callable[[int, int, int], None]:
            return lambda completed, failed, total: report_progress(
                stage, completed, failed, total
            )

        def usage_for(
            stage: str,
        ) -> Callable[[dict[str, Any] | None], None]:
            return lambda current: report_usage(stage, current)

        terms = load_terms(project)
        active_path = project / "terminology" / "active_task.json"
        active = read_json(project, active_path) if record_exists(project, active_path) else None
        run_terminology_stage = scope.force or terms is None
        if active and active.get("status") == "active":
            run_terminology_stage = True
        elif active and active.get("status") == "completed":
            (
                _selected,
                selected_ids,
                completed_ids,
                _fingerprints,
            ) = _terminology_scan_selection(
                project,
                segments,
                files,
                scope,
                str(active.get("active_task_id", "")),
            )
            run_terminology_stage = bool(selected_ids - completed_ids)
        if run_terminology_stage:
            term_summary = await run_terminology(
                project,
                scope,
                http_client=client_for("terminology"),
                limiter=limiters[resource_keys["terminology"]],
                reuse_mixed_fingerprints=reuse_mixed_fingerprints,
                prompt_language=prompt_language,
                on_progress=progress_for("terminology"),
                on_usage=usage_for("terminology"),
            )
            term_summary = record_summary("terminology", term_summary)
            require_success(term_summary)
        translation = await run_translation(
            project,
            scope,
            http_client=client_for("translation"),
            limiter=limiters[resource_keys["translation"]],
            reuse_mixed_fingerprints=reuse_mixed_fingerprints,
            prompt_language=prompt_language,
            on_progress=progress_for("translation"),
            on_usage=usage_for("translation"),
        )
        translation = record_summary("translation", translation)
        require_success(translation)
        proofreading = await run_review(
            project,
            "proofreading",
            scope,
            http_client=client_for("proofreading"),
            limiter=limiters[resource_keys["proofreading"]],
            reuse_mixed_fingerprints=reuse_mixed_fingerprints,
            prompt_language=prompt_language,
            on_progress=progress_for("proofreading"),
            on_usage=usage_for("proofreading"),
        )
        proofreading = record_summary("proofreading", proofreading)
        require_success(proofreading)
        polishing = await run_review(
            project,
            "polishing",
            scope,
            http_client=client_for("polishing"),
            limiter=limiters[resource_keys["polishing"]],
            reuse_mixed_fingerprints=reuse_mixed_fingerprints,
            prompt_language=prompt_language,
            on_progress=progress_for("polishing"),
            on_usage=usage_for("polishing"),
        )
        polishing = record_summary("polishing", polishing)
        require_success(polishing)
        failure_counts: Counter[str] = Counter()
        for summary in summaries:
            failure_counts.update(
                {
                    str(key): int(value)
                    for key, value in (summary.get("failure_counts") or {}).items()
                }
            )
        usage: dict[str, Any] | None = None
        for value in usage_by_stage.values():
            usage = combine_usage(usage, value)
        return {
            "stage": "run-all",
            "steps": summaries,
            "selected": sum(int(summary.get("selected", 0)) for summary in summaries),
            "requested": sum(int(summary.get("requested", 0)) for summary in summaries),
            "reused": sum(int(summary.get("reused", 0)) for summary in summaries),
            "completed": sum(int(summary.get("completed", 0)) for summary in summaries),
            "failed": sum(int(summary.get("failed", 0)) for summary in summaries),
            "pending": sum(int(summary.get("pending", 0)) for summary in summaries),
            "failure_counts": dict(failure_counts),
            "usage": usage or unavailable_usage(),
        }

    if http_client is not None or scope.dry_run:
        return await execute(None)
    async with AsyncExitStack() as stack:
        clients: dict[tuple[str, str], httpx.AsyncClient] = {}
        for stage, key in resource_keys.items():
            if key in clients:
                continue
            config = configs[stage]
            maximum = int(config["execution"]["max_parallel"])
            clients[key] = await stack.enter_async_context(
                httpx.AsyncClient(
                    timeout=float(config["execution"]["request_timeout_seconds"]),
                    limits=httpx.Limits(
                        max_connections=maximum,
                        max_keepalive_connections=maximum,
                    ),
                    proxy=config["llm"]["proxy_url"] or None,
                )
            )
        return await execute(clients)

def inspect_full(project: Path, *, dry_run: bool = False) -> dict[str, Any]:
    config, metadata, files, segments = _project_context(project)
    nonempty = [item for item in segments if not item["is_empty"]]
    active_segment_ids = {
        str(item["segment_id"]) for item in nonempty
    }
    summary: dict[str, Any] = {
        "name": metadata["name"],
        "files": len(files),
        "segments": len(segments),
        "empty_segments": len(segments) - len(nonempty),
        "terms_revision": None,
        "terminology": {
            "completed": 0,
            "failed": 0,
            "pending": len(nonempty),
            "fingerprint_count": 0,
            "current_fingerprint_completed": 0,
        },
        "stages": {},
        "outdated_suggestions": {},
        "validation_warnings": 0,
        "running_runs": [
            {
                "run_id": item["run_id"],
                "stage": item["stage"],
                "started_at": item.get("started_at"),
                "scope": item.get("scope"),
            }
            for stage in ("terminology", "translation", "proofreading", "polishing")
            for item in find_running_runs(project, stage)
        ],
    }
    library = load_terms(project)
    terms_revision = int(library["terms_revision"]) if library else None
    current_term_fingerprint = stage_fingerprint(
        load_project_config(project, stage="terminology"),
        "terminology",
        prompt_middle_digests(project, "terminology"),
    )
    summary["terminology"]["current_fingerprint"] = current_term_fingerprint
    if library:
        summary["terms_revision"] = library["terms_revision"]
    active_path = project / "terminology" / "active_task.json"
    if record_exists(project, active_path):
        active = read_json(project, active_path)
        if active.get("status") in {"active", "completed"}:
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
            summary["terminology"] = {
                "active_task_id": active["active_task_id"],
                "completed": len(completed),
                "failed": len(failed),
                "pending": len(nonempty) - len(completed) - len(failed),
                "fingerprint_count": len(
                    {
                        str(item["stage_fingerprint"])
                        for item in scans
                        if item.get("stage_fingerprint")
                    }
                ),
                "current_fingerprint": current_term_fingerprint,
                "current_fingerprint_completed": sum(
                    item.get("status") == "completed"
                    and item.get("stage_fingerprint") == current_term_fingerprint
                    for item in scans
                ),
            }
    histories: dict[str, list[dict[str, Any]]] = {}
    for stage in (
        "translation",
        "proofreading",
        "proofreading_applied",
        "polishing",
        "polishing_applied",
    ):
        history = load_stage_history(project, stage)
        active_history = [
            item
            for item in history
            if str(item.get("segment_id")) in active_segment_ids
        ]
        histories[stage] = active_history
        completed = classify_stage(
            nonempty, active_history, force=False
        ).latest_completed
        failed = {
            str(item["segment_id"])
            for item in active_history
            if item.get("status") == "failed"
            and str(item.get("segment_id")) not in completed
        }
        latest_failed_after_success = 0
        by_segment: dict[str, list[dict[str, Any]]] = {}
        for item in active_history:
            by_segment.setdefault(str(item.get("segment_id")), []).append(item)
        for segment_id in completed:
            if by_segment[segment_id][-1].get("status") == "failed":
                latest_failed_after_success += 1
        fingerprints = {
            str(item["stage_fingerprint"])
            for item in completed.values()
            if item.get("stage_fingerprint")
        }
        if stage in {"translation", "proofreading", "polishing"}:
            current_fingerprint = stage_fingerprint(
                load_project_config(project, stage=stage),
                stage,
                prompt_middle_digests(project, stage),
                terms_revision=terms_revision,
            )
        else:
            current_fingerprint = stage_fingerprint(
                config,
                stage,
                None,
                apply_semantics={
                    "review_stage": stage.removesuffix("_applied"),
                    "allow_outdated_base": False,
                },
            )
        summary["stages"][stage] = {
            "completed": len(completed),
            "failed": len(failed),
            "pending": len(nonempty) - len(completed) - len(failed),
            "fingerprint_count": len(fingerprints),
            "current_fingerprint": current_fingerprint,
            "current_fingerprint_completed": sum(
                record.get("stage_fingerprint") == current_fingerprint
                for record in completed.values()
            ),
            "last_attempt_failed": latest_failed_after_success,
        }
    latest_translations = classify_stage(
        [], histories["translation"], force=False
    ).latest_completed
    summary["validation_warnings"] = sum(
        item.get("validation_status") == "warning"
        for item in latest_translations.values()
    )
    for review_stage in ("proofreading", "polishing"):
        suggestions = classify_stage(
            [], histories[review_stage], force=False
        ).latest_completed
        bases = _base_results(project, review_stage)
        summary["outdated_suggestions"][review_stage] = sum(
            suggestion.get("base_result_id")
            != bases.get(segment_id, {}).get("record_id")
            for segment_id, suggestion in suggestions.items()
        )
    project_arg = shlex.quote(str(project))
    if not nonempty:
        summary["next_command"] = (
            f"python -m app.main files-add {project_arg} INPUT"
        )
    elif summary["terminology"]["pending"] or summary["terminology"]["failed"]:
        summary["next_command"] = (
            f"python -m app.main terminology {project_arg}"
        )
    elif summary["terms_revision"] is None:
        summary["next_command"] = (
            f"python -m app.main terminology {project_arg}"
        )
    elif summary["stages"]["translation"]["pending"] or summary["stages"][
        "translation"
    ]["failed"]:
        summary["next_command"] = f"python -m app.main translate {project_arg}"
    elif summary["stages"]["proofreading"]["pending"] or summary["stages"][
        "proofreading"
    ]["failed"]:
        summary["next_command"] = f"python -m app.main proofread {project_arg}"
    elif summary["stages"]["polishing"]["pending"] or summary["stages"][
        "polishing"
    ]["failed"]:
        summary["next_command"] = f"python -m app.main polish {project_arg}"
    else:
        summary["next_command"] = (
            f"python -m app.main export {project_arg} --stage translated"
        )
    return summary
