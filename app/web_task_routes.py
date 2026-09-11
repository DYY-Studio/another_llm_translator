from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any
from fastapi import FastAPI, Query

from .errors import UsageError
from .execution import Scope
from .project import PROMPT_LANGUAGES
from .sqlite_storage import (
    RECORD_STATUSES,
    STAGES,
    list_run_index,
    read_project_meta_read_only,
    read_run_record,
)
from .web_payloads import TaskStartPayload
from .web_tasks import task_options


_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}\Z")
_SCOPE_FIELDS = (
    "all_nonempty",
    "from_file",
    "only_file",
    "only_segment",
    "segment_ids",
    "force",
    "dry_run",
)
_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "available",
    "partial",
)
_SNAPSHOT_FILES = {
    "config": ("config.toml", "toml"),
    "prompt": ("prompt.txt", "text"),
    "adapter": ("llm_adapter.json", "json"),
    "preset": ("llm_preset.json", "json"),
    "requirements": ("document_adapter_prompt_requirements.json", "json"),
}


def _safe_run_id(value: str) -> str:
    if not _RUN_ID_PATTERN.fullmatch(value) or value in {".", ".."}:
        raise UsageError("Run ID 无效")
    return value


def _safe_scope(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    for key in _SCOPE_FIELDS:
        if key not in value:
            continue
        item = value[key]
        if key == "segment_ids":
            if isinstance(item, list) and all(isinstance(entry, str) for entry in item):
                result[key] = list(item)
            continue
        if key in {"all_nonempty", "force", "dry_run"}:
            if isinstance(item, bool):
                result[key] = item
            continue
        if item is None or isinstance(item, str):
            result[key] = item
    return result


def _safe_usage(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    for key in _USAGE_FIELDS:
        item = value.get(key)
        if key in {"available", "partial"}:
            if isinstance(item, bool):
                result[key] = item
        elif type(item) is int and item >= 0:
            result[key] = item
    return result or None


def _safe_failure_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): int(item)
        for key, item in value.items()
        if isinstance(key, str) and type(item) is int and item > 0
    }


def _safe_warnings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _safe_document_adapters(value: Any) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for file_id, item in value.items():
        if not isinstance(file_id, str) or not isinstance(item, dict):
            continue
        adapter_id = item.get("adapter_id")
        version = item.get("version")
        if isinstance(adapter_id, str) and isinstance(version, str):
            result[file_id] = {"adapter_id": adapter_id, "version": version}
    return result


def _safe_document_options(value: Any) -> dict[str, dict[str, Any]]:
    allowed = {"ruby_mode", "inline_format_mode", "inline_format_policy"}
    if not isinstance(value, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for file_id, item in value.items():
        if not isinstance(file_id, str) or not isinstance(item, dict):
            continue
        result[file_id] = {
            key: item[key]
            for key in allowed
            if key in item and isinstance(item[key], (bool, int, float, str))
        }
    return result


def _safe_requirements(value: Any) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for file_id, item in value.items():
        if not isinstance(file_id, str) or not isinstance(item, dict):
            continue
        result[file_id] = {
            str(language): requirement
            for language, requirement in item.items()
            if isinstance(language, str) and isinstance(requirement, str)
        }
    return result


def _safe_validators(value: Any) -> list[dict[str, str]]:
    allowed = ("validator_id", "version", "plugin_id", "plugin_version")
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        entry = {
            key: item[key]
            for key in allowed
            if isinstance(item.get(key), str)
        }
        if entry:
            result.append(entry)
    return result


def _safe_run_summary(
    run: dict[str, Any], *, project_name: str, project_id: str, debug_available: bool
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "run_id": str(run.get("run_id", "")),
        "project_id": project_id,
        "project_name": project_name,
        "stage": str(run.get("stage", "")),
        "status": str(run.get("status", "")),
        "started_at": run.get("started_at"),
        "created_at": run.get("created_at"),
        "completed_at": run.get("completed_at"),
        "selected_segment_count": run.get("selected_segment_count"),
        "requested_segment_count": run.get("requested_segment_count"),
        "reused_segment_count": run.get("reused_segment_count"),
        "completed_segment_count": run.get("completed_segment_count"),
        "failed_segment_count": run.get("failed_segment_count"),
        "failure_counts": _safe_failure_counts(run.get("failure_counts")),
        "warnings": _safe_warnings(run.get("warnings")),
        "usage": _safe_usage(run.get("usage")),
        "scope": _safe_scope(run.get("scope")),
        "debug_available": debug_available,
    }
    for key in (
        "stage_fingerprint",
        "prompt_language",
        "review_stage",
        "primary_mode",
    ):
        if isinstance(run.get(key), str):
            result[key] = run[key]
    if type(run.get("terms_revision")) is int:
        result["terms_revision"] = run["terms_revision"]
    result["document_adapters"] = _safe_document_adapters(
        run.get("document_adapters")
    )
    result["document_adapter_options"] = _safe_document_options(
        run.get("document_adapter_options")
    )
    result["document_adapter_prompt_requirements"] = _safe_requirements(
        run.get("document_adapter_prompt_requirements")
    )
    result["translation_validators"] = _safe_validators(
        run.get("translation_validators")
    )
    return result


def _safe_snapshot_path(directory: Path, filename: str) -> Path | None:
    path = directory / filename
    try:
        resolved_directory = directory.resolve()
        resolved_path = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not resolved_path.is_file() or not resolved_path.is_relative_to(resolved_directory):
        return None
    return resolved_path


def _read_snapshot(directory: Path, filename: str, kind: str) -> dict[str, Any]:
    path = _safe_snapshot_path(directory, filename)
    if path is None:
        return {"status": "missing"}
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {"status": "unavailable"}
    if kind == "json":
        try:
            value = json.loads(content)
        except json.JSONDecodeError:
            return {"status": "invalid"}
        if not isinstance(value, (dict, list)):
            return {"status": "invalid"}
    elif kind == "toml":
        try:
            tomllib.loads(content)
        except tomllib.TOMLDecodeError:
            return {"status": "invalid"}
    return {"status": "available", "format": kind, "content": content}


def _read_prompt_variants(directory: Path) -> dict[str, Any]:
    metadata = _read_snapshot(directory, "prompt_variants.json", "json")
    if metadata["status"] == "missing":
        return metadata
    if metadata["status"] != "available":
        return metadata
    try:
        value = json.loads(str(metadata["content"]))
    except json.JSONDecodeError:
        return {"status": "invalid"}
    if not isinstance(value, dict):
        return {"status": "invalid"}
    items: list[dict[str, Any]] = []
    for name, entry in value.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        relative = entry.get("path")
        if not isinstance(relative, str):
            continue
        relative_path = Path(relative)
        if (
            relative_path.is_absolute()
            or any(part in {"", ".", ".."} for part in relative_path.parts)
            or relative_path.parts[:1] != ("prompt_variants",)
        ):
            snapshot = {"status": "invalid"}
        else:
            snapshot = _read_snapshot(directory, relative, "text")
        requirements = entry.get("requirements")
        items.append(
            {
                "name": name,
                "requirements": [
                    item for item in requirements if isinstance(item, str)
                ]
                if isinstance(requirements, list)
                else [],
                "primary_mode": entry.get("primary_mode")
                if isinstance(entry.get("primary_mode"), str)
                else None,
                "snapshot": snapshot,
            }
        )
    return {"status": "available", "items": items}


def _execution_snapshot(directory: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    snapshots = {
        key: _read_snapshot(directory, filename, kind)
        for key, (filename, kind) in _SNAPSHOT_FILES.items()
    }
    snapshots["prompt_variants"] = _read_prompt_variants(directory)
    result: dict[str, Any] = {"snapshots": snapshots}
    for key in (
        "started_at",
        "completed_at",
        "stage_fingerprint",
        "prompt_language",
        "primary_mode",
    ):
        value = metadata.get(key)
        if isinstance(value, str):
            result["fingerprint" if key == "stage_fingerprint" else key] = value
    result["scope"] = _safe_scope(metadata.get("scope"))
    for key in (
        "selected_segment_count",
        "requested_segment_count",
        "reused_segment_count",
    ):
        if type(metadata.get(key)) is int:
            result[key] = metadata[key]
    result["document_adapters"] = _safe_document_adapters(
        metadata.get("document_adapters")
    )
    result["document_adapter_options"] = _safe_document_options(
        metadata.get("document_adapter_options")
    )
    result["document_adapter_prompt_requirements"] = _safe_requirements(
        metadata.get("document_adapter_prompt_requirements")
    )
    result["prompt_languages"] = (
        {
            str(language): value
            for language, value in metadata.get("prompt_languages", {}).items()
            if isinstance(language, str) and isinstance(value, str)
        }
        if isinstance(metadata.get("prompt_languages"), dict)
        else {}
    )
    return result


def _run_detail(
    run: dict[str, Any], *, project_name: str, project_id: str, run_directory: Path
) -> dict[str, Any]:
    debug_available = (
        (run_directory / "attempts.jsonl").is_file()
        or (run_directory / "payloads").is_dir()
    )
    result = _safe_run_summary(
        run,
        project_name=project_name,
        project_id=project_id,
        debug_available=debug_available,
    )
    result["executions"] = [
        {
            "id": "root",
            "kind": "root",
            **_execution_snapshot(run_directory, run),
        }
    ]
    continuations = run.get("continuations")
    if isinstance(continuations, list):
        for index, metadata in enumerate(continuations, start=1):
            if not isinstance(metadata, dict):
                continue
            result["executions"].append(
                {
                    "id": f"continuation-{index:04d}",
                    "kind": "continuation",
                    **_execution_snapshot(
                        run_directory / "continuations" / f"{index:04d}",
                        metadata,
                    ),
                }
            )
    result["requests"] = {
        "status": "available" if debug_available else "unavailable",
        "items": [],
    }
    if not debug_available:
        result["requests"]["reason"] = "debug_disabled"
    return result


def register_task_routes(
    *,
    app: FastAPI,
    projects_root: Path,
    app_root: Path,
    project: Callable[[str], Path],
    project_paths: Callable[[], list[Path]],
) -> None:
    def validate_language(value: object) -> str:
        if value not in PROMPT_LANGUAGES:
            raise UsageError("language 必须是 zh-CN 或 en")
        return str(value)

    def run_directory(root: Path, run_id: str) -> Path:
        return root / "runs" / _safe_run_id(run_id)

    def debug_available(root: Path, run_id: str) -> bool:
        directory = run_directory(root, run_id)
        return (directory / "attempts.jsonl").is_file() or (
            directory / "payloads"
        ).is_dir()

    @app.get("/api/v1/runs")
    async def historical_runs(
        project_selector: str | None = Query(default=None, alias="project"),
        stage: str | None = None,
        status: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> dict[str, Any]:
        if stage is not None and stage not in STAGES:
            raise UsageError(f"未知 Run 阶段：{stage}")
        if status is not None and status not in RECORD_STATUSES:
            raise UsageError(f"未知 Run 状态：{status}")
        if offset < 0:
            raise UsageError("offset 不能小于 0")
        if limit < 1 or limit > 100:
            raise UsageError("limit 必须是 1 到 100 之间的整数")

        roots = (
            [(project(project_selector), None)]
            if project_selector is not None
            else [(root, None) for root in project_paths()]
        )
        all_items: list[dict[str, Any]] = []
        total = 0
        global_query = project_selector is None and len(roots) > 1
        fetch_limit = offset + limit if global_query else limit
        for root, _ in roots:
            metadata = read_project_meta_read_only(root)
            project_name = metadata.get("name")
            project_id = metadata.get("project_id")
            if not isinstance(project_name, str) or not isinstance(project_id, str):
                raise UsageError(f"项目元数据不完整：{root}")
            rows, count = list_run_index(
                root,
                stage=stage,
                status=status,
                offset=0 if global_query else offset,
                limit=fetch_limit,
            )
            total += count
            all_items.extend(
                _safe_run_summary(
                    row,
                    project_name=project_name,
                    project_id=project_id,
                    debug_available=debug_available(root, str(row["run_id"])),
                )
                for row in rows
            )
        if global_query:
            all_items.sort(
                key=lambda item: (
                    str(item.get("started_at") or ""),
                    str(item.get("run_id") or ""),
                ),
                reverse=True,
            )
            all_items = all_items[offset : offset + limit]
        return {
            "items": all_items,
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    @app.get("/api/v1/projects/{name}/runs/{run_id}")
    async def historical_run_detail(name: str, run_id: str) -> dict[str, Any]:
        root = project(name)
        safe_run_id = _safe_run_id(run_id)
        record = read_run_record(root, safe_run_id)
        if record is None:
            raise UsageError(f"Run 不存在：{safe_run_id}")
        metadata = read_project_meta_read_only(root)
        project_name = metadata.get("name")
        project_id = metadata.get("project_id")
        if not isinstance(project_name, str) or not isinstance(project_id, str):
            raise UsageError(f"项目元数据不完整：{root}")
        return _run_detail(
            record,
            project_name=project_name,
            project_id=project_id,
            run_directory=run_directory(root, safe_run_id),
        )

    @app.post("/api/v1/projects/{name}/tasks")
    async def start_task(
        name: str, payload: TaskStartPayload
    ) -> dict[str, Any]:
        stage = payload.stage
        scope = Scope(
            from_file=payload.from_file,
            only_file=payload.only_file,
            only_segment=payload.only_segment,
            force=payload.force,
            dry_run=False,
        )
        scope.validate()
        summary_selection_provided = "summary_selection" in payload.model_fields_set
        summary_selection = [
            value.model_dump() for value in payload.summary_selection
        ]
        if summary_selection_provided and stage != "content_summary":
            raise UsageError("summary_selection 只允许内容概括阶段")
        return await app.state.tasks.start(
            project(name),
            stage,
            scope=scope,
            reuse_mixed_fingerprints=payload.reuse_mixed_fingerprints,
            run_action=payload.run_action,
            prompt_language=(
                validate_language(payload.language)
                if "language" in payload.model_fields_set
                else None
            ),
            replace_draft=payload.replace_draft,
            acknowledge_manual_review=payload.acknowledge_manual_review,
            include_summaries=payload.include_summaries,
            summary_selection=summary_selection,
        )

    @app.get("/api/v1/projects/{name}/task-options/{stage}")
    async def get_task_options(
        name: str,
        stage: str,
        include_summaries: bool = False,
        language: str | None = None,
    ) -> dict[str, Any]:
        return task_options(
            project(name),
            stage,
            include_summaries=include_summaries,
            prompt_language=(validate_language(language) if language is not None else None),
        )

    @app.get("/api/v1/tasks/active")
    async def active_tasks() -> dict[str, Any]:
        return {"tasks": app.state.tasks.active_tasks()}

    @app.get("/api/v1/tasks/{task_id}")
    async def task(task_id: str) -> dict[str, Any]:
        return app.state.tasks.get(task_id)

    @app.post("/api/v1/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str) -> dict[str, Any]:
        return await app.state.tasks.cancel(task_id)

    @app.post("/api/v1/diagnostics")
    async def diagnostics(payload: dict[str, Any]) -> dict[str, Any]:
        values = {
            key: payload.get(key)
            for key in (
                "level",
                "project",
                "stage",
                "q",
                "request_session",
                "request_after",
            )
        }
        for key in ("level", "project", "stage", "q", "request_session"):
            if values[key] is not None and not isinstance(values[key], str):
                raise UsageError(f"{key} 必须是字符串")
        request_after = values["request_after"]
        if request_after is not None:
            try:
                request_after = int(request_after)
            except (TypeError, ValueError) as exc:
                raise UsageError("request_after 必须是整数") from exc
        try:
            return app.state.diagnostics.snapshot(
                level=values["level"] or None,
                project=values["project"] or None,
                stage=values["stage"] or None,
                query=values["q"] or None,
                request_session=values["request_session"] or None,
                request_after=request_after,
            )
        except ValueError as exc:
            raise UsageError(str(exc)) from exc

    @app.get("/api/v1/diagnostics/requests/{request_id}")
    async def diagnostic_request(request_id: str) -> dict[str, Any]:
        try:
            return app.state.diagnostics.request_detail(request_id)
        except ValueError as exc:
            raise UsageError(str(exc)) from exc
