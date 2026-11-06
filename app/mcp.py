from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .errors import AppError, UsageError

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "another-llm-translator", "version": "0.3.0"}


def default_base_url(port: int | None = None) -> str:
    if port is None:
        env_url = os.environ.get("ANOTHER_LLM_WEB_URL", "").strip()
        if env_url:
            return env_url.rstrip("/")
    resolved_port = port or int(os.environ.get("ANOTHER_LLM_WEB_PORT", "8765"))
    return f"http://127.0.0.1:{resolved_port}"


def _resolve_server_inputs(paths: Any) -> tuple[list[str], list[str]]:
    if (
        not isinstance(paths, list)
        or not paths
        or not all(isinstance(p, str) and p.strip() for p in paths)
    ):
        raise UsageError("paths 必须是非空字符串数组")
    resolved_paths: list[str] = []
    kinds: list[str] = []
    for raw in paths:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise UsageError(f"输入路径必须是本机绝对路径：{raw}")
        resolved_paths.append(str(candidate))
        kinds.append("folder" if candidate.is_dir() else "file")
    return resolved_paths, kinds


def _deep_merge_dict(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_projects",
        "description": "List all registered projects and currently active background tasks.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "create_project",
        "description": "Create a new project from local file/folder paths (or an empty project when empty=True) and register it in the Web workbench.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project name"},
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Absolute local file or directory paths to import",
                },
                "empty": {"type": "boolean", "default": False},
                "parent_dir": {
                    "type": "string",
                    "description": "Optional custom parent directory (absolute path)",
                },
                "adapter_options": {
                    "type": "object",
                    "description": "Optional Document Adapter import options {adapter_id: {option_id: value}}",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "open_project",
        "description": "Open, verify, and register an existing project directory on disk in the Web workbench.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the existing project directory",
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "add_project_files",
        "description": "Import and append local files or directories into an existing project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project name or ID"},
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Absolute local file or directory paths to import",
                },
                "adapter_options": {
                    "type": "object",
                    "description": "Optional Document Adapter import options {adapter_id: {option_id: value}}",
                },
            },
            "required": ["project", "paths"],
            "additionalProperties": False,
        },
    },
    {
        "name": "manage_project_files",
        "description": "Manage project files: remove files (requires confirm=True), reorder files, or preview/confirm/cancel single-file replacement.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project name or ID"},
                "action": {
                    "type": "string",
                    "enum": [
                        "remove",
                        "reorder",
                        "replace_preview",
                        "replace_confirm",
                        "replace_cancel",
                    ],
                },
                "file_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Target file IDs for remove or full ordered file IDs for reorder",
                },
                "file_id": {
                    "type": "string",
                    "description": "Target file ID for replace_preview, replace_confirm, or replace_cancel",
                },
                "path": {
                    "type": "string",
                    "description": "Absolute local file path for replace_preview",
                },
                "preview_id": {
                    "type": "string",
                    "description": "Replacement preview_id returned by replace_preview",
                },
                "adapter_options": {
                    "type": "object",
                    "description": "Optional Document Adapter options for replace_preview",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "Explicit confirmation required for remove and replace_confirm",
                },
            },
            "required": ["project", "action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "configure_project",
        "description": "Inspect or update project settings (config.toml, LLM/Decision presets), Document Adapter file run options, or delete a project (requires confirm=True).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project name or ID"},
                "action": {
                    "type": "string",
                    "enum": [
                        "get_config",
                        "update_config",
                        "get_file_options",
                        "update_file_options",
                        "delete_project",
                    ],
                },
                "config": {
                    "type": "object",
                    "description": "Partial or full config object to merge and save when action='update_config'",
                },
                "file_id": {
                    "type": "string",
                    "description": "File ID for get_file_options or update_file_options",
                },
                "options": {
                    "type": "object",
                    "description": "String key-value options for update_file_options",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "Explicit confirmation required for delete_project",
                },
            },
            "required": ["project", "action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_project_overview",
        "description": "Get project overview including files, per-stage progress counts, unfinished runs, and pending drafts.",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string", "description": "Project name or ID"}},
            "required": ["project"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_task_and_diagnostics",
        "description": "Get active task progress, recent stage runs, and optional diagnostic events for a project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "task_id": {"type": "string"},
                "stage": {"type": "string"},
                "include_events": {"type": "boolean", "default": False},
            },
            "required": ["project"],
            "additionalProperties": False,
        },
    },
    {
        "name": "query_segments",
        "description": "Query project segments by stage, status (e.g. failed, pending, completed, warning), keyword, file_id, or (file_id + part_id) chapter boundary.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "stage": {
                    "type": "string",
                    "enum": ["translation", "proofreading", "polishing"],
                    "default": "translation",
                },
                "status": {"type": "string"},
                "q": {"type": "string"},
                "file_id": {
                    "type": "string",
                    "description": "Optional file_id filter (can be used alone or paired with part_id)",
                },
                "part_id": {
                    "type": "string",
                    "description": "Optional part_id filter (requires file_id)",
                },
                "offset": {"type": "integer", "default": 0},
                "limit": {"type": "integer", "default": 50},
            },
            "required": ["project"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_segment_context",
        "description": "Fetch full details of a segment, matched terminology with entity knowledge, Document Adapter requirements, and surrounding bilingual context segments.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "segment_id": {"type": "string"},
                "window": {"type": "integer", "default": 5},
            },
            "required": ["project", "segment_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "preflight_stage_run",
        "description": "Inspect stage preflight options, unfinished run compatibility, and mixed fingerprint status before starting a host stage run.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "stage": {
                    "type": "string",
                    "enum": [
                        "terminology",
                        "terminology_decision",
                        "translation",
                        "proofreading",
                        "polishing",
                        "repetition_consistency",
                        "content_summary",
                    ],
                },
                "final_review": {"type": "boolean", "default": False},
                "knowledge_only": {"type": "boolean", "default": False},
                "all_terms": {"type": "boolean", "default": False},
            },
            "required": ["project", "stage"],
            "additionalProperties": False,
        },
    },
    {
        "name": "start_host_stage_run",
        "description": "Start or resume a host LLMClient background stage task (terminology, terminology_decision, translation, proofreading, polishing, repetition_consistency).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "stage": {"type": "string"},
                "language": {"type": "string"},
                "from_file": {"type": "string"},
                "only_file": {"type": "string"},
                "only_segment": {"type": "string"},
                "force": {"type": "boolean", "default": False},
                "confirm_knowledge_replacement": {"type": "boolean", "default": False},
                "reuse_mixed_fingerprints": {"type": "boolean", "default": False},
                "run_action": {"type": "string", "enum": ["resume", "decline"]},
                "replace_draft": {"type": "boolean", "default": False},
                "include_summaries": {"type": "boolean", "default": False},
                "include_draft_translation": {"type": "boolean", "default": False},
                "aggregate_full_summaries": {"type": "boolean", "default": False},
                "final_review": {"type": "boolean", "default": False},
                "knowledge_only": {"type": "boolean", "default": False},
                "all_terms": {"type": "boolean", "default": False},
                "review_draft_run_id": {"type": "string"},
                "acknowledge_manual_review": {"type": "boolean", "default": False},
                "exclude_punctuation_only": {"type": "boolean", "default": True},
                "screening_enabled": {"type": "boolean", "default": False},
                "repetition_final_review": {"type": "boolean", "default": False},
            },
            "required": ["project", "stage"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cancel_stage_run",
        "description": "Cancel an active background task by task_id.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "submit_segment_fix",
        "description": "Submit a strictly gated single-segment translation or review/polish fix (origin='agent_mcp'). Rejects unresolved validation findings unless allow_warnings=True.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "stage": {
                    "type": "string",
                    "enum": ["translation", "proofreading", "polishing"],
                    "default": "translation",
                },
                "segment_id": {"type": "string"},
                "text": {"type": "string"},
                "review_status": {"type": "string", "enum": ["accepted", "suggested"]},
                "suggested_text": {"type": "string"},
                "reason": {"type": "string"},
                "apply": {"type": "boolean", "default": False},
                "allow_warnings": {"type": "boolean", "default": False},
            },
            "required": ["project", "segment_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "apply_stage_suggestions",
        "description": "Apply proofreading or polishing suggestions across the selected scope (requires confirm=True).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "stage": {"type": "string", "enum": ["proofreading", "polishing"]},
                "only_file": {"type": "string"},
                "only_part": {"type": "string"},
                "only_segment": {"type": "string"},
                "segment_ids": {"type": "array", "items": {"type": "string"}},
                "scope": {"type": "object"},
                "allow_outdated_base": {"type": "boolean", "default": False},
                "confirm": {"type": "boolean"},
            },
            "required": ["project", "stage", "confirm"],
            "additionalProperties": False,
        },
    },
    {
        "name": "export_project",
        "description": "Export project files for a given stage in original or txt format.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "stage": {
                    "type": "string",
                    "enum": [
                        "translated",
                        "proofread",
                        "polished",
                    ],
                },
                "format": {
                    "type": "string",
                    "enum": ["original", "txt"],
                    "default": "original",
                },
                "bilingual": {"type": "boolean", "default": False},
                "allow_missing": {"type": "boolean", "default": False},
                "file_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["project", "stage"],
            "additionalProperties": False,
        },
    },
    {
        "name": "start_agent_stage_run",
        "description": "Pattern B: Create or resume an executor='agent_mcp' stage Run for Agent-driven batch generation and register live progress in Web UI.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "stage": {
                    "type": "string",
                    "enum": ["translation", "proofreading", "polishing"],
                },
                "scope": {
                    "type": "object",
                    "description": "Optional scope selector: {only_file, only_part (requires only_file), from_file, only_segment, segment_ids, force}",
                },
                "run_action": {"type": "string", "enum": ["resume", "decline"]},
                "reuse_mixed_fingerprints": {"type": "boolean", "default": False},
                "prompt_language": {"type": "string"},
                "agent_model_hint": {"type": "string"},
            },
            "required": ["project", "stage"],
            "additionalProperties": False,
        },
    },
    {
        "name": "checkout_stage_batch",
        "description": "Pattern B: Pull the next batch of pending segments from an active agent_mcp Run, optionally filtered by part_id / line range (or via run_id@part_id:start-end), including matched terminology, entity knowledge, and preceding bilingual context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "run_id": {"type": "string"},
                "part_id": {"type": "string"},
                "line_start": {"type": "integer"},
                "line_end": {"type": "integer"},
                "max_segments": {"type": "integer", "default": 15},
                "context_before": {"type": "integer", "default": 3},
            },
            "required": ["project", "run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "submit_stage_batch",
        "description": "Pattern B: Submit Agent-generated results for a batch of segments in an active agent_mcp Run. Validates via Document Adapter and 3-layer translation validators before committing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "run_id": {"type": "string"},
                "part_id": {"type": "string"},
                "line_start": {"type": "integer"},
                "line_end": {"type": "integer"},
                "items": {"type": "array", "items": {"type": "object"}},
                "allow_warnings": {"type": "boolean", "default": False},
            },
            "required": ["project", "run_id", "items"],
            "additionalProperties": False,
        },
    },
    {
        "name": "finish_agent_stage_run",
        "description": (
            "Pattern B: Complete or interrupt an active agent_mcp Run and release the active Web UI task. "
            "For proofreading/polishing with action='complete', set accept_remaining=True to batch-confirm "
            "all remaining pending segments in the Run as accepted (unchanged)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "run_id": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": ["complete", "interrupt"],
                    "default": "complete",
                },
                "accept_remaining": {"type": "boolean", "default": False},
                "reason": {"type": "string"},
            },
            "required": ["project", "run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_terminology_overview",
        "description": "Get terminology status, conflicts, pending changes, stale entity knowledge, decision draft proposals, and manual review queue.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "filter": {
                    "type": "string",
                    "enum": [
                        "all",
                        "conflicts",
                        "changes",
                        "stale_knowledge",
                        "draft",
                    ],
                    "default": "all",
                },
                "offset": {"type": "integer", "default": 0},
                "limit": {"type": "integer", "default": 50},
            },
            "required": ["project"],
            "additionalProperties": False,
        },
    },
    {
        "name": "inspect_term_entity",
        "description": "Inspect a term/entity including group members, applied description items, source evidence fragments, and segment hits.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "normalized": {"type": "string"},
                "include_hits": {"type": "boolean", "default": True},
                "hits_limit": {"type": "integer", "default": 20},
                "run_id": {"type": "string"},
                "proposal_id": {"type": "string"},
            },
            "required": ["project"],
            "additionalProperties": False,
        },
    },
    {
        "name": "edit_single_term",
        "description": (
            "Perform a controlled edit on terminology: save_term (single term or batch {'terms': [...]}), "
            "import_terms ({'terms': [...]} or {'path': '...'}), complete_scan (mark terminology scan completed for full project or scope), "
            "set_primary, leave_group, group_related, convert_to_alias, "
            "update_knowledge ({'normalized': str, 'action'?: 'save_items'|'confirm_keep'|'set_fragment_enabled', "
            "'items'?: [{'text': str, 'fragment_ids': [str]}], 'expected_revision'?: int}), "
            "resolve_change, remove_terms, clear_terms, or publish_partial."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": [
                        "save_term",
                        "import_terms",
                        "complete_scan",
                        "set_primary",
                        "leave_group",
                        "group_related",
                        "convert_to_alias",
                        "update_knowledge",
                        "resolve_change",
                        "remove_terms",
                        "clear_terms",
                        "publish_partial",
                    ],
                },
                "payload": {
                    "type": "object",
                    "description": "Action payload. For import_terms, pass {'terms': [{'source', 'preferred_translation', 'category', 'description', 'aliases', 'disabled', 'group_primary'}]}. For complete_scan, pass {} for full project or {'only_file': '...', 'only_part': '...'}.",
                },
            },
            "required": ["project", "action", "payload"],
            "additionalProperties": False,
        },
    },
    {
        "name": "submit_term_decision_draft",
        "description": "Update rejected proposal IDs or mark manual review (needs_review) items resolved in the terminology decision state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "rejected_proposal_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "manual_reviews": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "run_id": {"type": "string"},
                            "normalized": {"type": "string"},
                            "resolved": {"type": "boolean"},
                        },
                        "required": ["run_id", "normalized", "resolved"],
                    },
                },
            },
            "required": ["project"],
            "additionalProperties": False,
        },
    },
    {
        "name": "finalize_term_decision_draft",
        "description": "Explicitly apply, discard, or rollback a terminology decision draft (requires confirm=True).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": ["apply", "discard", "rollback"],
                },
                "confirm": {"type": "boolean"},
            },
            "required": ["project", "action", "confirm"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_repetition_overview",
        "description": "Get repetition consistency draft state, live scan of repeated source segments with inconsistent translations, or create an agent_mcp repetition draft without calling host LLM.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "filter": {
                    "type": "string",
                    "enum": ["all", "uncertain_only", "over_budget_only"],
                    "default": "all",
                },
                "only_file": {"type": "string"},
                "only_part": {
                    "type": "string",
                    "description": "Optional chapter part_id for live scan (requires only_file)",
                },
                "exclude_punctuation_only": {"type": "boolean", "default": True},
                "create_draft": {
                    "type": "boolean",
                    "default": False,
                    "description": "Create a formal pending repetition_consistency draft locally without calling external LLM",
                },
                "replace_draft": {"type": "boolean", "default": False},
                "offset": {"type": "integer", "default": 0},
                "limit": {"type": "integer", "default": 50},
            },
            "required": ["project"],
            "additionalProperties": False,
        },
    },
    {
        "name": "inspect_repetition_group",
        "description": "Inspect all occurrences of a repeated source group with configurable surrounding context window.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "group_id": {"type": "string"},
                "source": {"type": "string"},
                "window": {"type": "integer", "default": 5},
            },
            "required": ["project"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_repetition_draft",
        "description": "Update review choices or subgroup proposals for a pending repetition consistency draft.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "run_id": {"type": "string"},
                "choices": {"type": "object"},
                "group_proposals": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["project", "run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "finalize_repetition_draft",
        "description": "Explicitly apply, discard, or rollback a repetition consistency draft (requires confirm=True).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "run_id": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": ["apply", "discard", "rollback"],
                },
                "choices": {"type": "object"},
                "confirm": {"type": "boolean"},
            },
            "required": ["project", "run_id", "action", "confirm"],
            "additionalProperties": False,
        },
    },
]


class MCPWebBridge:
    def __init__(
        self,
        base_url: str | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = (base_url or default_base_url()).rstrip("/")
        self._client = client

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        form_data: dict[str, Any] | None = None,
        files: Any = None,
    ) -> Any:
        try:
            if self._client is not None:
                response = self._client.request(
                    method,
                    path,
                    params=params,
                    json=json_body,
                    data=form_data,
                    files=files,
                )
            else:
                with httpx.Client(base_url=self.base_url, timeout=60.0) as client:
                    response = client.request(
                        method,
                        path,
                        params=params,
                        json=json_body,
                        data=form_data,
                        files=files,
                    )
        except httpx.RequestError as exc:
            raise UsageError(
                f"无法连接本地 Another LLM Translator 服务 ({self.base_url})；请先启动 Web 或桌面端服务后再调用 MCP 工具：{exc}"
            ) from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise UsageError(
                f"本地 Web 服务返回了非 JSON 响应 (HTTP {response.status_code})"
            ) from exc
        if response.status_code >= 400:
            if isinstance(data, dict) and "error" in data:
                raise UsageError(str(data["error"]))
            raise UsageError(f"HTTP {response.status_code}: {data}")
        return data

    def call_tool(self, name: str, arguments: dict[str, Any] | None) -> Any:
        args = dict(arguments or {})
        if name == "list_projects":
            projects = self._request("GET", "/api/v1/projects")
            active = self._request("GET", "/api/v1/tasks/active")
            return {**projects, "active_tasks": active.get("tasks", [])}

        if name == "create_project":
            empty = bool(args.get("empty", False))
            raw_paths = args.get("paths")
            form: dict[str, Any] = {
                "name": str(args["name"]),
                "empty": "true" if empty else "false",
                "parent_dir": str(args.get("parent_dir") or ""),
                "adapter_options": json.dumps(
                    args.get("adapter_options") or {}, ensure_ascii=False
                ),
            }
            if raw_paths:
                server_paths, server_kinds = _resolve_server_inputs(raw_paths)
                form["server_paths"] = server_paths
                form["server_input_kinds"] = server_kinds
            elif not empty:
                raise UsageError("必须提供 paths 导入文件，或显式设置 empty=True 创建空项目")
            return self._request("POST", "/api/v1/projects", form_data=form)

        if name == "open_project":
            raw_path = args.get("path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise UsageError("path 必须是非空字符串")
            return self._request(
                "POST",
                "/api/v1/projects/open",
                json_body={"path": str(Path(raw_path).expanduser())},
            )

        if name == "add_project_files":
            proj = quote(str(args["project"]), safe="")
            server_paths, server_kinds = _resolve_server_inputs(args.get("paths"))
            form = {
                "server_paths": server_paths,
                "server_input_kinds": server_kinds,
                "adapter_options": json.dumps(
                    args.get("adapter_options") or {}, ensure_ascii=False
                ),
            }
            return self._request("POST", f"/api/v1/projects/{proj}/files", form_data=form)

        if name == "manage_project_files":
            proj = quote(str(args["project"]), safe="")
            action = str(args["action"])
            if action == "remove":
                if args.get("confirm") is not True:
                    raise UsageError("移除项目文件必须显式指定 confirm=True")
                return self._request(
                    "POST",
                    f"/api/v1/projects/{proj}/files/remove",
                    json_body={"file_ids": args.get("file_ids")},
                )
            if action == "reorder":
                return self._request(
                    "POST",
                    f"/api/v1/projects/{proj}/files/reorder",
                    json_body={"file_ids": args.get("file_ids")},
                )
            file_id = args.get("file_id")
            if not isinstance(file_id, str) or not file_id:
                raise UsageError(f"{action} 必须提供 file_id")
            fid = quote(file_id, safe="")
            if action == "replace_preview":
                server_paths, server_kinds = _resolve_server_inputs([args.get("path")])
                if server_kinds[0] != "file":
                    raise UsageError("单文件替换的 path 必须是文件路径")
                form = {
                    "server_path": server_paths[0],
                    "adapter_options": json.dumps(
                        args.get("adapter_options") or {}, ensure_ascii=False
                    ),
                }
                return self._request(
                    "POST",
                    f"/api/v1/projects/{proj}/files/{fid}/replacement-preview",
                    form_data=form,
                )
            preview_id = args.get("preview_id")
            if not isinstance(preview_id, str) or not preview_id:
                raise UsageError(f"{action} 必须提供 preview_id")
            if action == "replace_confirm":
                if args.get("confirm") is not True:
                    raise UsageError("确认替换文件必须显式指定 confirm=True")
                return self._request(
                    "POST",
                    f"/api/v1/projects/{proj}/files/{fid}/replacement-confirm",
                    json_body={"preview_id": preview_id},
                )
            if action == "replace_cancel":
                pid = quote(preview_id, safe="")
                return self._request(
                    "DELETE",
                    f"/api/v1/projects/{proj}/files/{fid}/replacement-preview/{pid}",
                )
            raise UsageError(f"不支持的文件管理动作：{action}")

        if name == "configure_project":
            proj = quote(str(args["project"]), safe="")
            action = str(args["action"])
            if action == "get_config":
                cfg = self._request("GET", f"/api/v1/projects/{proj}/config")
                presets = self._request("GET", "/api/v1/global/presets")
                decision_presets = self._request(
                    "GET", "/api/v1/global/decision-presets"
                )
                def _compact_preset(item: dict[str, Any]) -> dict[str, Any]:
                    out = {
                        k: item[k]
                        for k in ("preset_id", "adapter_id", "protocol", "model", "valid")
                        if k in item
                    }
                    if item.get("missing_fields"):
                        out["missing_fields"] = item["missing_fields"]
                    if item.get("error"):
                        out["error"] = item["error"]
                    return out
                return {
                    **cfg,
                    "available_presets": [
                        _compact_preset(p)
                        for p in presets.get("presets", [])
                        if isinstance(p, dict)
                    ],
                    "available_decision_presets": [
                        _compact_preset(p)
                        for p in decision_presets.get("presets", [])
                        if isinstance(p, dict)
                    ],
                }
            if action == "update_config":
                patch = args.get("config")
                if not isinstance(patch, dict) or not patch:
                    raise UsageError("update_config 必须提供非空 config 对象")
                current = self._request(
                    "GET", f"/api/v1/projects/{proj}/config"
                ).get("config", {})
                merged = _deep_merge_dict(
                    dict(current) if isinstance(current, dict) else {}, patch
                )
                saved = self._request(
                    "PUT",
                    f"/api/v1/projects/{proj}/config",
                    json_body={"config": merged},
                )
                latest = self._request("GET", f"/api/v1/projects/{proj}/config")
                return {**latest, **saved}
            if action == "get_file_options":
                file_id = args.get("file_id")
                if not isinstance(file_id, str) or not file_id:
                    raise UsageError("get_file_options 必须提供 file_id")
                fid = quote(file_id, safe="")
                return self._request(
                    "GET", f"/api/v1/projects/{proj}/files/{fid}/run-options"
                )
            if action == "update_file_options":
                file_id = args.get("file_id")
                options = args.get("options")
                if not isinstance(file_id, str) or not file_id:
                    raise UsageError("update_file_options 必须提供 file_id")
                if not isinstance(options, dict):
                    raise UsageError("update_file_options 必须提供 options 对象")
                fid = quote(file_id, safe="")
                return self._request(
                    "PUT",
                    f"/api/v1/projects/{proj}/files/{fid}/run-options",
                    json_body={"options": options},
                )
            if action == "delete_project":
                if args.get("confirm") is not True:
                    raise UsageError("删除项目必须显式指定 confirm=True")
                return self._request(
                    "DELETE",
                    f"/api/v1/projects/{proj}",
                    json_body={"confirm": True},
                )
            raise UsageError(f"不支持的项目配置动作：{action}")

        if name == "get_project_overview":
            proj = quote(str(args["project"]), safe="")
            data = self._request("GET", f"/api/v1/projects/{proj}", params={"limit": 1})
            if isinstance(data, dict):
                data.pop("segments", None)
                data.pop("offset", None)
                data.pop("limit", None)
            return data

        if name == "get_task_and_diagnostics":
            project_name = str(args["project"])
            _falsy_task_keys = {
                "current_stage",
                "steps",
                "final_review",
                "knowledge_only",
                "all_terms",
                "include_summaries",
                "include_draft_translation",
                "aggregate_full_summaries",
                "draft_progress",
                "summary_selection",
                "summary_selection_progress",
                "failure_counts",
            }
            def _compact_task(t: dict[str, Any]) -> dict[str, Any]:
                return {
                    k: v
                    for k, v in t.items()
                    if not (k in _falsy_task_keys and not v)
                }
            active = self._request("GET", "/api/v1/tasks/active").get("tasks", [])
            project_tasks = [
                _compact_task(t)
                for t in active
                if isinstance(t, dict)
                and (t.get("project") == project_name or t.get("project_id") == project_name)
            ]
            raw_detail = (
                self._request("GET", f"/api/v1/tasks/{quote(str(args['task_id']), safe='')}")
                if args.get("task_id")
                else None
            )
            task_detail = _compact_task(raw_detail) if isinstance(raw_detail, dict) else raw_detail
            runs = self._request(
                "GET",
                "/api/v1/runs",
                params={"project": project_name, "limit": 5, **({"stage": args["stage"]} if args.get("stage") else {})},
            )
            _strip_run_keys = {
                "document_adapter_prompt_requirements",
                "document_adapters",
                "document_adapter_options",
                "translation_validators",
                "stage_fingerprint",
            }
            recent_runs = [
                {
                    k: v
                    for k, v in r.items()
                    if k not in _strip_run_keys
                    and not (k in {"failure_counts", "warnings"} and not v)
                }
                for r in runs.get("items", [])
                if isinstance(r, dict)
            ]
            result: dict[str, Any] = {
                "active_tasks": project_tasks,
                "task_detail": task_detail,
                "recent_runs": recent_runs,
            }
            if args.get("include_events"):
                diag = self._request(
                    "POST",
                    "/api/v1/diagnostics",
                    json_body={"project": project_name, "stage": args.get("stage")},
                )
                if isinstance(diag, dict):
                    logs = diag.get("logs")
                    if isinstance(logs, list) and len(logs) > 20:
                        diag = {**diag, "total_logs": len(logs), "logs": logs[-20:]}
                    reqs = diag.get("requests")
                    if isinstance(reqs, dict) and isinstance(reqs.get("items"), list):
                        req_items = reqs["items"]
                        _req_keys = {
                            "request_id",
                            "stage",
                            "request_kind",
                            "segment_id",
                            "status",
                            "attempt_count",
                            "last_http_status",
                            "latency_ms",
                            "usage",
                            "stream_event_count",
                            "stream_received_bytes",
                            "error",
                            "timestamp",
                            "finished_at",
                        }
                        compact_reqs = [
                            {k: v for k, v in item.items() if k in _req_keys}
                            for item in req_items[-15:]
                            if isinstance(item, dict)
                        ]
                        diag = {
                            **diag,
                            "requests": {
                                **{k: v for k, v in reqs.items() if k != "items"},
                                "total_items": len(req_items),
                                "items": compact_reqs,
                            },
                        }
                result["diagnostics"] = diag
            return result

        if name == "query_segments":
            proj = quote(str(args["project"]), safe="")
            stage = str(args.get("stage", "translation"))
            body = {
                "stage": stage,
                "status": args.get("status", ""),
                "q": args.get("q", ""),
                "file_id": args.get("file_id", ""),
                "part_id": args.get("part_id", ""),
                "offset": int(args.get("offset", 0)),
                "limit": int(args.get("limit", 50)),
            }
            data = self._request(
                "POST", f"/api/v1/projects/{proj}/segments/query", json_body=body
            )
            if isinstance(data, dict) and isinstance(data.get("segments"), list):
                compact_segs = []
                for seg in data["segments"]:
                    if not isinstance(seg, dict):
                        compact_segs.append(seg)
                        continue
                    row = dict(seg)
                    if row.get("model_source") is None:
                        row.pop("model_source", None)
                    if not row.get("stage_errors"):
                        row.pop("stage_errors", None)
                    reviews = row.get("reviews")
                    if stage == "translation" and isinstance(reviews, dict):
                        has_review = any(
                            isinstance(v, dict)
                            and (v.get("base") or v.get("suggestion") or v.get("applied"))
                            for v in reviews.values()
                        )
                        if not has_review:
                            row.pop("reviews", None)
                    compact_segs.append(row)
                data = {**data, "segments": compact_segs}
            return data

        if name == "get_segment_context":
            proj = quote(str(args["project"]), safe="")
            return self._request(
                "POST",
                f"/api/v1/projects/{proj}/agent/segments/context",
                json_body={
                    "segment_id": args["segment_id"],
                    "window": int(args.get("window", 5)),
                },
            )

        if name == "preflight_stage_run":
            proj = quote(str(args["project"]), safe="")
            stage = str(args["stage"])
            if stage == "repetition_consistency":
                return self._request(
                    "GET",
                    f"/api/v1/projects/{proj}/repetition/options",
                    params={
                        "estimate_usage": True,
                        "final_review": bool(args.get("final_review", False)),
                    },
                )
            return self._request(
                "GET",
                f"/api/v1/projects/{proj}/task-options/{quote(stage, safe='')}",
                params={
                    "estimate_usage": True,
                    "final_review": bool(args.get("final_review", False)),
                    "knowledge_only": bool(args.get("knowledge_only", False)),
                    "all_terms": bool(args.get("all_terms", False)),
                },
            )

        if name == "start_host_stage_run":
            proj = quote(str(args["project"]), safe="")
            body = {
                k: v
                for k, v in args.items()
                if k != "project" and v is not None
            }
            return self._request("POST", f"/api/v1/projects/{proj}/tasks", json_body=body)

        if name == "cancel_stage_run":
            tid = quote(str(args["task_id"]), safe="")
            return self._request("POST", f"/api/v1/tasks/{tid}/cancel")

        if name == "submit_segment_fix":
            proj = quote(str(args["project"]), safe="")
            body = {k: v for k, v in args.items() if k != "project" and v is not None}
            return self._request(
                "POST", f"/api/v1/projects/{proj}/agent/segments/fix", json_body=body
            )

        if name == "apply_stage_suggestions":
            proj = quote(str(args["project"]), safe="")
            scope_arg = args.get("scope") if isinstance(args.get("scope"), dict) else {}
            return self._request(
                "POST",
                f"/api/v1/projects/{proj}/apply",
                json_body={
                    "stage": args["stage"],
                    "only_file": args.get("only_file") or scope_arg.get("only_file"),
                    "only_part": args.get("only_part") or scope_arg.get("only_part"),
                    "only_segment": args.get("only_segment") or scope_arg.get("only_segment"),
                    "segment_ids": args.get("segment_ids") or scope_arg.get("segment_ids"),
                    "allow_outdated_base": bool(args.get("allow_outdated_base", False)),
                    "all": bool(args.get("confirm", False)),
                },
            )

        if name == "export_project":
            proj = quote(str(args["project"]), safe="")
            data = self._request(
                "POST",
                f"/api/v1/projects/{proj}/export",
                json_body={
                    "stage": args["stage"],
                    "format": args.get("format", "original"),
                    "bilingual": bool(args.get("bilingual", False)),
                    "allow_missing": bool(args.get("allow_missing", False)),
                    "file_ids": args.get("file_ids"),
                },
            )
            if isinstance(data, dict) and isinstance(data.get("fallback_segments"), list):
                fallback = data["fallback_segments"]
                data = {
                    **data,
                    "fallback_segment_count": len(fallback),
                }
                if len(fallback) > 50:
                    data["fallback_segments"] = fallback[:50]
                    data["fallback_segments_truncated"] = True
            return data

        if name == "start_agent_stage_run":
            proj = quote(str(args["project"]), safe="")
            body = {k: v for k, v in args.items() if k != "project" and v is not None}
            return self._request(
                "POST", f"/api/v1/projects/{proj}/agent/runs/start", json_body=body
            )

        if name == "checkout_stage_batch":
            proj = quote(str(args["project"]), safe="")
            rid = quote(str(args["run_id"]), safe="")
            body: dict[str, Any] = {
                "max_segments": int(args.get("max_segments", 15)),
                "context_before": int(args.get("context_before", 3)),
            }
            for opt_key in ("part_id", "line_start", "line_end"):
                if args.get(opt_key) is not None:
                    body[opt_key] = args[opt_key]
            return self._request(
                "POST",
                f"/api/v1/projects/{proj}/agent/runs/{rid}/checkout",
                json_body=body,
            )

        if name == "submit_stage_batch":
            proj = quote(str(args["project"]), safe="")
            rid = quote(str(args["run_id"]), safe="")
            body = {
                "items": args["items"],
                "allow_warnings": bool(args.get("allow_warnings", False)),
            }
            for opt_key in ("part_id", "line_start", "line_end"):
                if args.get(opt_key) is not None:
                    body[opt_key] = args[opt_key]
            return self._request(
                "POST",
                f"/api/v1/projects/{proj}/agent/runs/{rid}/submit",
                json_body=body,
            )

        if name == "finish_agent_stage_run":
            proj = quote(str(args["project"]), safe="")
            rid = quote(str(args["run_id"]), safe="")
            return self._request(
                "POST",
                f"/api/v1/projects/{proj}/agent/runs/{rid}/finish",
                json_body={
                    "action": args.get("action", "complete"),
                    "accept_remaining": bool(args.get("accept_remaining", False)),
                    "reason": args.get("reason"),
                },
            )

        if name == "get_terminology_overview":
            proj = quote(str(args["project"]), safe="")
            flt = args.get("filter", "all")
            offset = int(args.get("offset", 0))
            limit = int(args.get("limit", 50))
            decision = self._request("GET", f"/api/v1/projects/{proj}/terms/decision")
            if flt == "draft":
                draft = decision.get("draft")
                if isinstance(draft, dict) and isinstance(draft.get("proposals"), list):
                    proposals = draft["proposals"]
                    compact_proposals = []
                    for p in proposals[offset : offset + limit]:
                        if not isinstance(p, dict):
                            compact_proposals.append(p)
                            continue
                        cp = {
                            k: v
                            for k, v in p.items()
                            if k
                            not in {
                                "before",
                                "after",
                                "conflicts",
                                "knowledge_before",
                                "knowledge_after",
                                "evidence",
                                "knowledge_evidence",
                            }
                        }
                        for side in ("before", "after"):
                            if isinstance(p.get(side), list):
                                cp[side] = [
                                    {
                                        k: v
                                        for k, v in st.items()
                                        if k
                                        in {
                                            "normalized",
                                            "source",
                                            "preferred_translation",
                                            "category",
                                            "aliases",
                                            "group_primary",
                                            "disabled",
                                        }
                                        and (
                                            v not in (None, [], False)
                                            or k
                                            in {
                                                "normalized",
                                                "source",
                                                "preferred_translation",
                                                "category",
                                            }
                                        )
                                    }
                                    for st in p[side]
                                    if isinstance(st, dict)
                                ]
                        if p.get("conflicts"):
                            cp["conflict_count"] = len(p["conflicts"])
                        compact_proposals.append(cp)
                    decision = {
                        **decision,
                        "draft": {
                            **{
                                k: v
                                for k, v in draft.items()
                                if k
                                not in {
                                    "proposals",
                                    "decision_targets",
                                    "reviewed_terms",
                                    "model_fingerprint",
                                    "prompt_fingerprint",
                                }
                            },
                            "total_proposals": len(proposals),
                            "offset": offset,
                            "limit": limit,
                            "proposals": compact_proposals,
                        },
                    }
                return decision
            terms_data = self._request("GET", f"/api/v1/projects/{proj}/terms")
            items = list(terms_data.get("terms", []))
            if flt == "conflicts":
                items = [t for t in items if t.get("has_conflicts")]
            elif flt == "changes":
                items = [t for t in items if t.get("pending_change")]
            elif flt == "stale_knowledge":
                items = [
                    t
                    for t in items
                    if t.get("knowledge_status") in {"stale", "pending"}
                ]
            compact_terms = []
            for t in items[offset : offset + limit]:
                if not isinstance(t, dict):
                    compact_terms.append(t)
                    continue
                row = {
                    k: v
                    for k, v in t.items()
                    if k not in {"description_items", "pending_change"}
                    and not (k == "conflicts" and not t.get("has_conflicts"))
                }
                pc = t.get("pending_change")
                if isinstance(pc, dict):
                    if flt == "changes":
                        row["pending_change"] = {
                            **pc,
                            "candidates": [
                                {k: v for k, v in c.items() if k != "source_range"}
                                for c in pc.get("candidates", [])
                                if isinstance(c, dict)
                            ],
                        }
                    else:
                        row["pending_change"] = {
                            "kind": pc.get("kind"),
                            "candidate_count": len(pc.get("candidates", [])),
                        }
                compact_terms.append(row)
            return {
                "total": len(items),
                "terms": compact_terms,
                "terms_revision": terms_data.get("terms_revision"),
                "conflict_count": terms_data.get("conflict_count", 0),
                "scan": terms_data.get("scan"),
                "decision_state": {
                    "has_pending_draft": decision.get("draft") is not None,
                    "manual_review": decision.get("manual_review"),
                    "can_rollback": decision.get("rollback") is not None,
                },
            }

        if name == "inspect_term_entity":
            proj = quote(str(args["project"]), safe="")
            if args.get("run_id") and args.get("proposal_id"):
                rid = quote(str(args["run_id"]), safe="")
                pid = quote(str(args["proposal_id"]), safe="")
                return self._request(
                    "GET",
                    f"/api/v1/projects/{proj}/terms/decision/{rid}/proposals/{pid}",
                )
            normalized = args.get("normalized")
            if not normalized:
                raise UsageError("必须提供 normalized 或 (run_id + proposal_id)")
            knowledge = self._request(
                "GET",
                f"/api/v1/projects/{proj}/terms/knowledge",
                params={"normalized": normalized},
            )
            hits = (
                self._request(
                    "POST",
                    f"/api/v1/projects/{proj}/terms/hits",
                    json_body={
                        "normalized": normalized,
                        "offset": 0,
                        "limit": int(args.get("hits_limit", 20)),
                    },
                )
                if args.get("include_hits", True)
                else None
            )
            return {"knowledge": knowledge, "hits": hits}

        if name == "edit_single_term":
            proj = quote(str(args["project"]), safe="")
            action = str(args["action"])
            payload = dict(args["payload"])
            if action == "import_terms":
                if isinstance(payload.get("terms"), list):
                    doc = {
                        "schema_version": 2,
                        "record_type": "terminology_exchange",
                        "terms": payload["terms"],
                    }
                    raw_bytes = json.dumps(doc, ensure_ascii=False).encode("utf-8")
                    return self._request(
                        "POST",
                        f"/api/v1/projects/{proj}/terms/import",
                        files={"file": ("terms.json", raw_bytes, "application/json")},
                    )
                if isinstance(payload.get("path"), str) and payload["path"].strip():
                    file_path = Path(payload["path"]).expanduser().resolve()
                    if not file_path.is_file():
                        raise UsageError(f"术语导入文件不存在：{file_path}")
                    raw_bytes = file_path.read_bytes()
                    filename = file_path.name
                    content_type = (
                        "text/csv"
                        if filename.lower().endswith(".csv")
                        else "application/json"
                    )
                    return self._request(
                        "POST",
                        f"/api/v1/projects/{proj}/terms/import",
                        files={"file": (filename, raw_bytes, content_type)},
                    )
                raise UsageError(
                    "import_terms 必须在 payload 中提供 terms 列表或本地文件 path"
                )
            routes = {
                "save_term": ("POST", f"/api/v1/projects/{proj}/terms"),
                "complete_scan": (
                    "POST",
                    f"/api/v1/projects/{proj}/agent/terms/complete-scan",
                ),
                "set_primary": ("POST", f"/api/v1/projects/{proj}/terms/set-primary"),
                "leave_group": ("POST", f"/api/v1/projects/{proj}/terms/leave-group"),
                "group_related": ("POST", f"/api/v1/projects/{proj}/terms/group-related"),
                "convert_to_alias": (
                    "POST",
                    f"/api/v1/projects/{proj}/terms/convert-to-alias",
                ),
                "update_knowledge": ("PUT", f"/api/v1/projects/{proj}/terms/knowledge"),
                "resolve_change": (
                    "POST",
                    f"/api/v1/projects/{proj}/terms/changes/resolve",
                ),
                "remove_terms": ("POST", f"/api/v1/projects/{proj}/terms/remove"),
                "clear_terms": ("POST", f"/api/v1/projects/{proj}/terms/clear"),
                "publish_partial": (
                    "POST",
                    f"/api/v1/projects/{proj}/terms/publish-partial",
                ),
            }
            if action not in routes:
                raise UsageError(f"不支持的术语编辑动作：{action}")
            method, path = routes[action]
            res = self._request(method, path, json_body=payload)
            if isinstance(res, dict) and isinstance(res.get("terms"), list) and len(res["terms"]) > 5:
                terms_list = res["terms"]
                target_keys = {
                    str(v)
                    for k in ("normalized", "source", "old_normalized", "primary")
                    if (v := payload.get(k))
                }
                if isinstance(payload.get("related_normalized"), list):
                    target_keys.update(str(x) for x in payload["related_normalized"])
                matched = [
                    t
                    for t in terms_list
                    if isinstance(t, dict)
                    and (
                        t.get("normalized") in target_keys
                        or t.get("source") in target_keys
                        or t.get("group_primary") in target_keys
                    )
                ]
                res = {
                    **res,
                    "total_terms": len(terms_list),
                    "terms_truncated": True,
                    "terms": matched if matched else terms_list[:5],
                }
            return res

        if name == "submit_term_decision_draft":
            proj = quote(str(args["project"]), safe="")
            result = {}
            if "rejected_proposal_ids" in args and args["rejected_proposal_ids"] is not None:
                rej = self._request(
                    "PUT",
                    f"/api/v1/projects/{proj}/terms/decision/rejections",
                    json_body={"rejected_proposal_ids": args["rejected_proposal_ids"]},
                )
                if isinstance(rej, dict) and isinstance(rej.get("draft"), dict):
                    draft_obj = rej["draft"]
                    rej = {
                        "run_id": draft_obj.get("run_id"),
                        "rejected_proposal_ids": draft_obj.get("rejected_proposal_ids", []),
                        "total_proposals": len(draft_obj.get("proposals", [])),
                    }
                result["rejections"] = rej
            if "manual_reviews" in args and args["manual_reviews"]:
                resolved_results = []
                for entry in args["manual_reviews"]:
                    resolved_results.append(
                        self._request(
                            "PUT",
                            f"/api/v1/projects/{proj}/terms/decision/manual-review",
                            json_body=entry,
                        )
                    )
                result["manual_reviews"] = resolved_results
            if not result:
                raise UsageError("必须提供 rejected_proposal_ids 或 manual_reviews")
            return result

        if name == "finalize_term_decision_draft":
            proj = quote(str(args["project"]), safe="")
            action = str(args["action"])
            if action not in {"apply", "discard", "rollback"}:
                raise UsageError(f"不支持的术语决策收尾动作：{action}")
            res = self._request(
                "POST",
                f"/api/v1/projects/{proj}/terms/decision/{action}",
                json_body={"confirm": bool(args.get("confirm", False))},
            )
            if isinstance(res, dict) and isinstance(res.get("terms"), dict):
                terms_summary = res["terms"]
                res = {
                    **{k: v for k, v in res.items() if k != "terms"},
                    "conflict_count": terms_summary.get("conflict_count", 0),
                    "total_terms": len(terms_summary.get("terms", [])),
                }
            return res

        if name == "get_repetition_overview":
            proj = quote(str(args["project"]), safe="")
            offset = int(args.get("offset", 0))
            limit = int(args.get("limit", 50))
            exclude_punct = bool(args.get("exclude_punctuation_only", True))
            if args.get("create_draft"):
                self._request(
                    "POST",
                    f"/api/v1/projects/{proj}/agent/repetition/scan",
                    json_body={
                        "only_file": args.get("only_file"),
                        "exclude_punctuation_only": exclude_punct,
                        "create_draft": True,
                        "replace_draft": bool(args.get("replace_draft", False)),
                        "offset": offset,
                        "limit": limit,
                    },
                )
            state = self._request("GET", f"/api/v1/projects/{proj}/repetition")
            draft = state.get("draft")
            if draft and isinstance(draft.get("groups"), list):
                flt = args.get("filter", "all")
                groups = draft["groups"]
                if flt == "uncertain_only":
                    groups = [g for g in groups if g.get("uncertain")]
                elif flt == "over_budget_only":
                    groups = [g for g in groups if g.get("over_budget")]
                compact_groups = []
                for g in groups[offset : offset + limit]:
                    cg = {k: v for k, v in g.items() if k != "files"}
                    if isinstance(cg.get("occurrences"), list):
                        cg["occurrences"] = [
                            {
                                k: v
                                for k, v in occ.items()
                                if k in {"id", "segment_id", "part_id", "line_index", "text"}
                            }
                            for occ in cg["occurrences"]
                            if isinstance(occ, dict)
                        ]
                    compact_groups.append(cg)
                draft = {
                    **{k: v for k, v in draft.items() if k not in {"evidence", "sources"}},
                    "total_groups": len(groups),
                    "groups": compact_groups,
                }
            result_state: dict[str, Any] = {**state, "draft": draft}
            if draft is None or args.get("only_file") or args.get("only_part"):
                result_state["live_scan"] = self._request(
                    "POST",
                    f"/api/v1/projects/{proj}/agent/repetition/scan",
                    json_body={
                        "only_file": args.get("only_file"),
                        "only_part": args.get("only_part"),
                        "exclude_punctuation_only": exclude_punct,
                        "create_draft": False,
                        "offset": offset,
                        "limit": limit,
                    },
                )
            return result_state

        if name == "inspect_repetition_group":
            proj = quote(str(args["project"]), safe="")
            return self._request(
                "POST",
                f"/api/v1/projects/{proj}/agent/repetition/inspect",
                json_body={
                    "group_id": args.get("group_id"),
                    "source": args.get("source"),
                    "window": int(args.get("window", 5)),
                },
            )

        if name == "update_repetition_draft":
            proj = quote(str(args["project"]), safe="")
            run_id = str(args["run_id"])
            result = {}
            if args.get("group_proposals"):
                result["proposals"] = self._request(
                    "POST",
                    f"/api/v1/projects/{proj}/agent/repetition/proposals",
                    json_body={
                        "run_id": run_id,
                        "group_proposals": args["group_proposals"],
                    },
                )
            if args.get("choices") is not None:
                result["choices"] = self._request(
                    "PUT",
                    f"/api/v1/projects/{proj}/repetition/choices",
                    json_body={"run_id": run_id, "choices": args["choices"]},
                )
            if not result:
                raise UsageError("必须提供 choices 或 group_proposals")
            return result

        if name == "finalize_repetition_draft":
            proj = quote(str(args["project"]), safe="")
            action = str(args["action"])
            if action not in {"apply", "discard", "rollback"}:
                raise UsageError(f"不支持的重复译文草案动作：{action}")
            body: dict[str, Any] = {
                "run_id": args["run_id"],
                "confirm": bool(args.get("confirm", False)),
            }
            if action == "apply":
                choices = args.get("choices")
                if choices is None:
                    current = self._request("GET", f"/api/v1/projects/{proj}/repetition")
                    choices = (current.get("draft") or {}).get("choices", {})
                body["choices"] = choices
            return self._request(
                "POST",
                f"/api/v1/projects/{proj}/repetition/{action}",
                json_body=body,
            )

        raise UsageError(f"未知 MCP 工具：{name}")


def handle_jsonrpc_message(
    message: dict[str, Any], bridge: MCPWebBridge
) -> dict[str, Any] | None:
    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") or {}

    if method == "notifications/initialized" or msg_id is None:
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": TOOLS},
        }

    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(tool_name, str) or not tool_name:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32602, "message": "Missing tool name"},
            }
        try:
            payload = bridge.call_tool(tool_name, arguments)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(payload, ensure_ascii=False),
                        }
                    ],
                    "isError": False,
                },
            }
        except (AppError, KeyError, TypeError, ValueError) as exc:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"error": str(exc)}, ensure_ascii=False
                            ),
                        }
                    ],
                    "isError": True,
                },
            }

    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.mcp",
        description="启动桥接本地 Web 服务的 MCP (stdio) 服务端",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="本地 Web 服务端口（默认读取 ANOTHER_LLM_WEB_PORT 或 8765）",
    )
    parser.add_argument(
        "--web-url",
        "--url",
        dest="url",
        type=str,
        default=None,
        help="本地 Web 服务完整地址（优先于 --port，也可通过 ANOTHER_LLM_WEB_URL 指定）",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    base_url = args.url or default_base_url(args.port)
    bridge = MCPWebBridge(base_url=base_url)
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            continue
        if not isinstance(message, dict):
            continue
        response = handle_jsonrpc_message(message, bridge)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
