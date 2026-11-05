from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from urllib.parse import quote

import httpx

from .errors import AppError, UsageError

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "another-llm-translator", "version": "0.3.0"}


def default_base_url(port: int | None = None) -> str:
    resolved_port = port or int(os.environ.get("ANOTHER_LLM_WEB_PORT", "8765"))
    return f"http://127.0.0.1:{resolved_port}"


TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_projects",
        "description": "List all registered projects and currently active background tasks.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
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
        "description": "Query project segments by stage, status (e.g. failed, pending, completed, warning), keyword, or file/part boundary.",
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
                "file_id": {"type": "string"},
                "part_id": {"type": "string"},
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
                "from_file": {"type": "string"},
                "only_file": {"type": "string"},
                "only_segment": {"type": "string"},
                "force": {"type": "boolean", "default": False},
                "reuse_mixed_fingerprints": {"type": "boolean", "default": False},
                "run_action": {"type": "string", "enum": ["resume", "decline"]},
                "replace_draft": {"type": "boolean", "default": False},
                "final_review": {"type": "boolean", "default": False},
                "knowledge_only": {"type": "boolean", "default": False},
                "all_terms": {"type": "boolean", "default": False},
                "acknowledge_manual_review": {"type": "boolean", "default": False},
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
                "only_segment": {"type": "string"},
                "segment_ids": {"type": "array", "items": {"type": "string"}},
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
                        "translation",
                        "proofreading_applied",
                        "polishing_applied",
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
                "scope": {"type": "object"},
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
        "description": "Pattern B: Pull the next batch of pending segments from an active agent_mcp Run, including matched terminology, entity knowledge, and preceding bilingual context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "run_id": {"type": "string"},
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
                "items": {"type": "array", "items": {"type": "object"}},
                "allow_warnings": {"type": "boolean", "default": False},
            },
            "required": ["project", "run_id", "items"],
            "additionalProperties": False,
        },
    },
    {
        "name": "finish_agent_stage_run",
        "description": "Pattern B: Complete or interrupt an active agent_mcp Run and release the active Web UI task.",
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
        "description": "Perform a controlled edit on a single term, term relationship, pending change, or entity knowledge.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": [
                        "save_term",
                        "set_primary",
                        "leave_group",
                        "group_related",
                        "convert_to_alias",
                        "update_knowledge",
                        "resolve_change",
                        "remove_terms",
                    ],
                },
                "payload": {"type": "object"},
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
        "description": "Get repetition consistency draft state, candidate groups, and uncertain/over-budget occurrences.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "filter": {
                    "type": "string",
                    "enum": ["all", "uncertain_only", "over_budget_only"],
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
    ) -> Any:
        try:
            if self._client is not None:
                response = self._client.request(
                    method, path, params=params, json=json_body
                )
            else:
                with httpx.Client(base_url=self.base_url, timeout=60.0) as client:
                    response = client.request(
                        method, path, params=params, json=json_body
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

        if name == "get_project_overview":
            proj = quote(str(args["project"]), safe="")
            return self._request("GET", f"/api/v1/projects/{proj}")

        if name == "get_task_and_diagnostics":
            project_name = str(args["project"])
            active = self._request("GET", "/api/v1/tasks/active").get("tasks", [])
            project_tasks = [
                t for t in active if t.get("project") == project_name or t.get("project_id") == project_name
            ]
            task_detail = (
                self._request("GET", f"/api/v1/tasks/{quote(str(args['task_id']), safe='')}")
                if args.get("task_id")
                else None
            )
            runs = self._request(
                "GET",
                "/api/v1/runs",
                params={"project": project_name, "limit": 10, **({"stage": args["stage"]} if args.get("stage") else {})},
            )
            result: dict[str, Any] = {
                "active_tasks": project_tasks,
                "task_detail": task_detail,
                "recent_runs": runs.get("runs", []),
            }
            if args.get("include_events"):
                diag = self._request(
                    "POST",
                    "/api/v1/diagnostics",
                    json_body={"project": project_name, "stage": args.get("stage")},
                )
                result["diagnostics"] = diag
            return result

        if name == "query_segments":
            proj = quote(str(args["project"]), safe="")
            body = {
                "stage": args.get("stage", "translation"),
                "status": args.get("status", ""),
                "q": args.get("q", ""),
                "file_id": args.get("file_id", ""),
                "part_id": args.get("part_id", ""),
                "offset": int(args.get("offset", 0)),
                "limit": int(args.get("limit", 50)),
            }
            return self._request("POST", f"/api/v1/projects/{proj}/segments/query", json_body=body)

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
                    params={"final_review": bool(args.get("final_review", False))},
                )
            return self._request(
                "GET",
                f"/api/v1/projects/{proj}/task-options/{quote(stage, safe='')}",
                params={
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
            return self._request(
                "POST",
                f"/api/v1/projects/{proj}/apply",
                json_body={
                    "stage": args["stage"],
                    "only_file": args.get("only_file"),
                    "only_segment": args.get("only_segment"),
                    "segment_ids": args.get("segment_ids"),
                    "allow_outdated_base": bool(args.get("allow_outdated_base", False)),
                    "all": bool(args.get("confirm", False)),
                },
            )

        if name == "export_project":
            proj = quote(str(args["project"]), safe="")
            return self._request(
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

        if name == "start_agent_stage_run":
            proj = quote(str(args["project"]), safe="")
            body = {k: v for k, v in args.items() if k != "project" and v is not None}
            return self._request(
                "POST", f"/api/v1/projects/{proj}/agent/runs/start", json_body=body
            )

        if name == "checkout_stage_batch":
            proj = quote(str(args["project"]), safe="")
            rid = quote(str(args["run_id"]), safe="")
            return self._request(
                "POST",
                f"/api/v1/projects/{proj}/agent/runs/{rid}/checkout",
                json_body={
                    "max_segments": int(args.get("max_segments", 15)),
                    "context_before": int(args.get("context_before", 3)),
                },
            )

        if name == "submit_stage_batch":
            proj = quote(str(args["project"]), safe="")
            rid = quote(str(args["run_id"]), safe="")
            return self._request(
                "POST",
                f"/api/v1/projects/{proj}/agent/runs/{rid}/submit",
                json_body={
                    "items": args["items"],
                    "allow_warnings": bool(args.get("allow_warnings", False)),
                },
            )

        if name == "finish_agent_stage_run":
            proj = quote(str(args["project"]), safe="")
            rid = quote(str(args["run_id"]), safe="")
            return self._request(
                "POST",
                f"/api/v1/projects/{proj}/agent/runs/{rid}/finish",
                json_body={
                    "action": args.get("action", "complete"),
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
                return decision
            terms_data = self._request("GET", f"/api/v1/projects/{proj}/terms")
            items = list(terms_data.get("terms", []))
            if flt == "conflicts":
                items = [t for t in items if t.get("has_conflict")]
            elif flt == "changes":
                items = [t for t in items if t.get("pending_change")]
            elif flt == "stale_knowledge":
                items = [
                    t
                    for t in items
                    if t.get("knowledge_status") in {"stale", "pending"}
                ]
            return {
                "total": len(items),
                "terms": items[offset : offset + limit],
                "summary": terms_data.get("summary"),
                "decision_state": {
                    "has_pending_draft": decision.get("draft") is not None,
                    "manual_review": decision.get("manual_review"),
                    "can_rollback": decision.get("can_rollback", False),
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
            routes = {
                "save_term": ("POST", f"/api/v1/projects/{proj}/terms"),
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
            }
            if action not in routes:
                raise UsageError(f"不支持的术语编辑动作：{action}")
            method, path = routes[action]
            return self._request(method, path, json_body=payload)

        if name == "submit_term_decision_draft":
            proj = quote(str(args["project"]), safe="")
            result = {}
            if "rejected_proposal_ids" in args and args["rejected_proposal_ids"] is not None:
                result["rejections"] = self._request(
                    "PUT",
                    f"/api/v1/projects/{proj}/terms/decision/rejections",
                    json_body={"rejected_proposal_ids": args["rejected_proposal_ids"]},
                )
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
            return self._request(
                "POST",
                f"/api/v1/projects/{proj}/terms/decision/{action}",
                json_body={"confirm": bool(args.get("confirm", False))},
            )

        if name == "get_repetition_overview":
            proj = quote(str(args["project"]), safe="")
            state = self._request("GET", f"/api/v1/projects/{proj}/repetition")
            draft = state.get("draft")
            if draft and isinstance(draft.get("groups"), list):
                flt = args.get("filter", "all")
                groups = draft["groups"]
                if flt == "uncertain_only":
                    groups = [g for g in groups if g.get("uncertain")]
                elif flt == "over_budget_only":
                    groups = [g for g in groups if g.get("over_budget")]
                offset = int(args.get("offset", 0))
                limit = int(args.get("limit", 50))
                draft = {
                    **draft,
                    "total_groups": len(groups),
                    "groups": groups[offset : offset + limit],
                }
            return {**state, "draft": draft}

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
        "--url",
        type=str,
        default=None,
        help="本地 Web 服务完整地址（优先于 --port）",
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
