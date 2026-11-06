from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.execution import Scope
from app.mcp import MCPWebBridge, handle_jsonrpc_message
from app.project import init_project, load_segments
from app.repetition_consistency import run_repetition
from app.web import create_app
from app.web_store import WebStore
from tests.test_foundation import make_app_root
from tests.test_repetition_consistency import repetition_project, transport


def _make_bridge(tmp_path: Path, source: str = "アリスは走った。\nボブは笑った。\nキャロルは歌った。") -> tuple[MCPWebBridge, Path, TestClient]:
    app_root = make_app_root(tmp_path)
    input_path = tmp_path / "input.txt"
    input_path.write_text(source, encoding="utf-8")
    projects_root = tmp_path / "projects"
    project, _ = init_project(
        [str(input_path)],
        name="sample",
        app_root=app_root,
        projects_root=projects_root,
    )
    assert project is not None
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "validators = []", 'validators = ["japanese_kana"]'
        ),
        encoding="utf-8",
    )
    WebStore(project).save_term(
        {
            "source": "アリス",
            "preferred_translation": "爱丽丝",
            "category": "character",
            "aliases": [],
            "disabled": False,
        }
    )
    client = TestClient(create_app(projects_root=projects_root, app_root=app_root))
    return MCPWebBridge(client=client), project, client


def test_mcp_jsonrpc_protocol_and_explicit_offline_failure() -> None:
    offline_bridge = MCPWebBridge(base_url="http://127.0.0.1:1")
    init_resp = handle_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        offline_bridge,
    )
    assert init_resp is not None
    assert init_resp["result"]["serverInfo"]["name"] == "another-llm-translator"

    list_resp = handle_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        offline_bridge,
    )
    assert list_resp is not None
    tool_names = {tool["name"] for tool in list_resp["result"]["tools"]}
    assert {
        "list_projects",
        "get_project_overview",
        "get_segment_context",
        "submit_segment_fix",
        "start_agent_stage_run",
        "checkout_stage_batch",
        "submit_stage_batch",
        "finish_agent_stage_run",
        "get_terminology_overview",
        "get_repetition_overview",
    } <= tool_names

    call_resp = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "list_projects", "arguments": {}},
        },
        offline_bridge,
    )
    assert call_resp is not None
    assert call_resp["result"]["isError"] is True
    err_payload = json.loads(call_resp["result"]["content"][0]["text"])
    assert "无法连接本地 Another LLM Translator 服务" in err_payload["error"]


def test_pattern_a_context_and_strict_segment_fix_gatekeeping(tmp_path: Path) -> None:
    bridge, project, _ = _make_bridge(tmp_path)
    segments = load_segments(project)
    first_id = str(segments[0]["segment_id"])

    ctx = bridge.call_tool(
        "get_segment_context",
        {"project": "sample", "segment_id": first_id, "window": 2},
    )
    assert ctx["segment_id"] == first_id
    assert len(ctx["matched_terms"]) == 1
    assert ctx["matched_terms"][0]["source"] == "アリス"
    assert len(ctx["context"]["after"]) == 2

    # Residual kana must be rejected by default (not persisted as warning)
    rejected = bridge.call_tool(
        "submit_segment_fix",
        {
            "project": "sample",
            "stage": "translation",
            "segment_id": first_id,
            "text": "爱丽丝は奔跑了。",
        },
    )
    assert rejected["status"] == "rejected"
    assert rejected["error_layer"] == "translation_validation"
    assert any(f["validator"] == "japanese_kana" for f in rejected["findings"])
    assert rejected["repair_guidance"]

    # Verify rejected translation was NOT written
    queried = bridge.call_tool(
        "query_segments",
        {"project": "sample", "stage": "translation", "status": "completed"},
    )
    assert queried["total_segments"] == 0

    # Valid translation passes gatekeeping and records origin='agent_mcp'
    committed = bridge.call_tool(
        "submit_segment_fix",
        {
            "project": "sample",
            "stage": "translation",
            "segment_id": first_id,
            "text": "爱丽丝奔跑了起来。",
        },
    )
    assert committed["status"] == "committed"
    assert committed["result"]["origin"] == "agent_mcp"
    assert committed["result"]["validation_status"] == "passed"

    # Proofreading suggestion with apply=True
    review_committed = bridge.call_tool(
        "submit_segment_fix",
        {
            "project": "sample",
            "stage": "proofreading",
            "segment_id": first_id,
            "review_status": "suggested",
            "suggested_text": "爱丽丝飞奔而去。",
            "reason": "语感更自然",
            "apply": True,
        },
    )
    assert review_committed["status"] == "committed"
    assert review_committed["suggestion"]["origin"] == "agent_mcp"
    assert review_committed["applied"]["text"] == "爱丽丝飞奔而去。"


def test_pattern_b_agent_stage_run_lifecycle_and_web_visibility(tmp_path: Path) -> None:
    bridge, project, _ = _make_bridge(tmp_path)
    started = bridge.call_tool(
        "start_agent_stage_run",
        {
            "project": "sample",
            "stage": "translation",
            "agent_model_hint": "gemini-4-argon",
        },
    )
    run_id = started["run_id"]
    assert started["selected_count"] == 3
    assert started["pending_count"] == 3

    # Must not fabricate host LLM preset/adapter snapshots
    run_dir = project / "runs" / run_id
    assert not (run_dir / "llm_preset.json").exists()
    assert not (run_dir / "llm_adapter.json").exists()

    # Active task is visible in Web UI / diagnostics
    diag = bridge.call_tool("get_task_and_diagnostics", {"project": "sample"})
    assert len(diag["active_tasks"]) == 1
    assert diag["active_tasks"][0]["executor"] == "agent_mcp"
    assert diag["active_tasks"][0]["run_id"] == run_id

    # Host preflight sees the running agent_mcp run and marks it non-resumable by host LLM
    preflight = bridge.call_tool(
        "preflight_stage_run", {"project": "sample", "stage": "translation"}
    )
    assert preflight["running_run"]["run_id"] == run_id
    assert preflight["running_run"]["executor"] == "agent_mcp"
    assert preflight["running_run"]["resume_compatible"] is False

    # Checkout first 2 segments
    checkout1 = bridge.call_tool(
        "checkout_stage_batch",
        {"project": "sample", "run_id": run_id, "max_segments": 2},
    )
    assert len(checkout1["batch"]) == 2
    seg1_id = checkout1["batch"][0]["segment_id"]
    seg2_id = checkout1["batch"][1]["segment_id"]

    # Submit 1 valid and 1 invalid (residual kana)
    submit1 = bridge.call_tool(
        "submit_stage_batch",
        {
            "project": "sample",
            "run_id": run_id,
            "items": [
                {"segment_id": seg1_id, "text": "爱丽丝奔跑了。"},
                {"segment_id": seg2_id, "text": "ボブ笑了。"},
            ],
        },
    )
    assert submit1["committed_ids"] == [seg1_id]
    assert len(submit1["rejected_items"]) == 1
    assert submit1["rejected_items"][0]["segment_id"] == seg2_id
    assert submit1["completed_count"] == 1
    assert submit1["remaining_pending_count"] == 2

    # Completing early while segments remain pending must fail
    with pytest.raises(Exception, match="未完成"):
        bridge.call_tool(
            "finish_agent_stage_run",
            {"project": "sample", "run_id": run_id, "action": "complete"},
        )

    # Checkout remaining 2 segments (seg2_id + seg3_id) with context_before=1
    checkout2 = bridge.call_tool(
        "checkout_stage_batch",
        {"project": "sample", "run_id": run_id, "max_segments": 5, "context_before": 1},
    )
    assert len(checkout2["batch"]) == 2
    assert checkout2["reference_context"][0]["translation"] == "爱丽丝奔跑了。"
    seg3_id = checkout2["batch"][1]["segment_id"]

    # Submit remaining 2 valid translations
    submit2 = bridge.call_tool(
        "submit_stage_batch",
        {
            "project": "sample",
            "run_id": run_id,
            "items": [
                {"segment_id": seg2_id, "text": "鲍勃笑了。"},
                {"segment_id": seg3_id, "text": "卡萝尔歌唱了。"},
            ],
        },
    )
    assert submit2["remaining_pending_count"] == 0

    finished = bridge.call_tool(
        "finish_agent_stage_run",
        {"project": "sample", "run_id": run_id, "action": "complete"},
    )
    assert finished["status"] == "completed"
    assert finished["completed"] == 3

    diag_after = bridge.call_tool("get_task_and_diagnostics", {"project": "sample"})
    assert diag_after["active_tasks"] == []
    assert diag_after["recent_runs"][0]["run_id"] == run_id
    assert diag_after["recent_runs"][0]["executor"] == "agent_mcp"


def test_terminology_and_repetition_mcp_collaboration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = repetition_project(tmp_path, monkeypatch)
    WebStore(project).save_term(
        {
            "source": "承诺",
            "preferred_translation": "约定",
            "category": "general",
            "aliases": [],
            "disabled": False,
        }
    )

    async def _gen_draft() -> None:
        async with httpx.AsyncClient(transport=transport(uncertain=True)) as http_client:
            await run_repetition(
                project, Scope(), http_client=http_client, screening_enabled=True
            )

    asyncio.run(_gen_draft())

    client = TestClient(create_app(projects_root=project.parent))
    bridge = MCPWebBridge(client=client)

    # Terminology overview & single term edit
    term_ov = bridge.call_tool("get_terminology_overview", {"project": "repetition"})
    assert term_ov["total"] == 1
    assert term_ov["terms_revision"] == 1
    entity = bridge.call_tool(
        "inspect_term_entity", {"project": "repetition", "normalized": "承诺"}
    )
    assert entity["knowledge"]["entity_key"] == "承诺"

    bridge.call_tool(
        "edit_single_term",
        {
            "project": "repetition",
            "action": "save_term",
            "payload": {
                "old_normalized": "承诺",
                "source": "承诺",
                "preferred_translation": "我会来接你",
                "category": "general",
                "aliases": [],
                "disabled": False,
            },
        },
    )

    # Repetition overview & group inspection with custom window
    rep_ov = bridge.call_tool(
        "get_repetition_overview",
        {"project": "repetition", "filter": "uncertain_only"},
    )
    draft = rep_ov["draft"]
    assert draft is not None
    run_id = draft["run_id"]
    group = draft["groups"][0]
    group_id = group["id"]
    assert group["uncertain"] == ["3"]

    inspected = bridge.call_tool(
        "inspect_repetition_group",
        {"project": "repetition", "group_id": group_id, "window": 2},
    )
    assert len(inspected["occurrences"]) == 3

    # Update repetition draft proposals to resolve the uncertain occurrence '3' into subgroup 1
    bridge.call_tool(
        "update_repetition_draft",
        {
            "project": "repetition",
            "run_id": run_id,
            "group_proposals": [
                {
                    "group_id": group_id,
                    "groups": [
                        {
                            "members": ["1", "2", "3"],
                            "target": "1",
                            "apply": ["1", "2", "3"],
                            "reason": "三处语境一致，统一为第一处译文",
                        }
                    ],
                    "keep": [],
                    "uncertain": [],
                }
            ],
        },
    )

    applied = bridge.call_tool(
        "finalize_repetition_draft",
        {
            "project": "repetition",
            "run_id": run_id,
            "action": "apply",
            "confirm": True,
        },
    )
    assert applied["changed"] == 2

    rolled_back = bridge.call_tool(
        "finalize_repetition_draft",
        {
            "project": "repetition",
            "run_id": run_id,
            "action": "rollback",
            "confirm": True,
        },
    )
    assert rolled_back["changed"] == 2


def test_project_and_file_lifecycle_mcp(tmp_path: Path) -> None:
    app_root = make_app_root(tmp_path)
    projects_root = tmp_path / "projects"
    client = TestClient(create_app(projects_root=projects_root, app_root=app_root))
    bridge = MCPWebBridge(client=client)

    ch1 = tmp_path / "ch1.txt"
    ch1.write_text("第一行原文。\n第二行原文。", encoding="utf-8")
    folder = tmp_path / "extra_dir"
    folder.mkdir()
    (folder / "ch2.txt").write_text("第三行原文。", encoding="utf-8")

    # 1. Create project from local file + directory
    created = bridge.call_tool(
        "create_project",
        {
            "name": "mcp-created",
            "paths": [str(ch1), str(folder)],
        },
    )
    assert created["project_selector"] == "mcp-created"
    assert created["file_count"] == 2

    # 2. Add another local file
    ch3 = tmp_path / "ch3.txt"
    ch3.write_text("第四行原文。", encoding="utf-8")
    added = bridge.call_tool(
        "add_project_files",
        {
            "project": "mcp-created",
            "paths": [str(ch3)],
        },
    )
    assert added["added_files"] == 1

    ov = bridge.call_tool("get_project_overview", {"project": "mcp-created"})
    file_ids = [f["file_id"] for f in ov["files"]]
    assert file_ids == ["F0001", "F0002", "F0003"]

    # 3. Reorder files
    bridge.call_tool(
        "manage_project_files",
        {
            "project": "mcp-created",
            "action": "reorder",
            "file_ids": ["F0002", "F0001", "F0003"],
        },
    )

    # 4. Preview & confirm single-file replacement
    ch1_v2 = tmp_path / "ch1_v2.txt"
    ch1_v2.write_text("第一行原文。\n第二行修改后原文。", encoding="utf-8")
    preview = bridge.call_tool(
        "manage_project_files",
        {
            "project": "mcp-created",
            "action": "replace_preview",
            "file_id": "F0001",
            "path": str(ch1_v2),
        },
    )
    assert preview["preview_id"]

    with pytest.raises(Exception, match="confirm=True"):
        bridge.call_tool(
            "manage_project_files",
            {
                "project": "mcp-created",
                "action": "replace_confirm",
                "file_id": "F0001",
                "preview_id": preview["preview_id"],
                "confirm": False,
            },
        )

    confirmed = bridge.call_tool(
        "manage_project_files",
        {
            "project": "mcp-created",
            "action": "replace_confirm",
            "file_id": "F0001",
            "preview_id": preview["preview_id"],
            "confirm": True,
        },
    )
    assert confirmed["file_id"] == "F0001"

    # 5. Remove file (requires confirm=True)
    removed = bridge.call_tool(
        "manage_project_files",
        {
            "project": "mcp-created",
            "action": "remove",
            "file_ids": ["F0003"],
            "confirm": True,
        },
    )
    assert removed["removed_file_ids"] == ["F0003"]

    # 6. Open existing external project directory
    ext_root = tmp_path / "external_projects"
    ext_root.mkdir()
    ext_created = bridge.call_tool(
        "create_project",
        {
            "name": "ext-proj",
            "paths": [str(ch1)],
            "parent_dir": str(ext_root),
        },
    )
    opened = bridge.call_tool(
        "open_project",
        {"path": ext_created["project_path"]},
    )
    assert opened["name"] == "ext-proj"
    assert opened["external"] is True

    # 7. Query segments by file_id alone (without part_id) and verify compact payload
    queried = bridge.call_tool(
        "query_segments",
        {"project": "mcp-created", "file_id": "F0001"},
    )
    assert queried["total_segments"] == 2
    assert len(queried["segments"]) == 2
    assert "reviews" not in queried["segments"][0]
    assert "model_source" not in queried["segments"][0]

    # 8. Batch import terms with unsourced knowledge descriptions & update_knowledge without expected_revision
    imported = bridge.call_tool(
        "edit_single_term",
        {
            "project": "mcp-created",
            "action": "import_terms",
            "payload": {
                "terms": [
                    {
                        "source": "第一行",
                        "preferred_translation": "首行",
                        "category": "general",
                        "description": "项目测试首行术语",
                        "aliases": [],
                        "disabled": False,
                    }
                ]
            },
        },
    )
    assert imported["imported"] == 1
    updated_know = bridge.call_tool(
        "edit_single_term",
        {
            "project": "mcp-created",
            "action": "update_knowledge",
            "payload": {
                "normalized": "第一行",
                "items": [{"text": "更新后的首行说明", "fragment_ids": []}],
            },
        },
    )
    assert updated_know["terms"][0]["description"] == "更新后的首行说明"
    term_ov = bridge.call_tool(
        "get_terminology_overview",
        {"project": "mcp-created"},
    )
    assert term_ov["total"] == 1

    # 9. Configure project (get_config, update_config, delete_project)
    cfg = bridge.call_tool(
        "configure_project",
        {"project": "mcp-created", "action": "get_config"},
    )
    assert "config" in cfg
    assert "available_presets" in cfg
    updated_cfg = bridge.call_tool(
        "configure_project",
        {
            "project": "mcp-created",
            "action": "update_config",
            "config": {"validation": {"translation": {"validators": ["japanese_kana"]}}},
        },
    )
    assert updated_cfg["config"]["validation"]["translation"]["validators"] == ["japanese_kana"]

    # 10. Pure Agent repetition live scan & local draft creation
    rep_proj = tmp_path / "rep.txt"
    rep_proj.write_text("待て！\n進め。\n待て！", encoding="utf-8")
    bridge.call_tool(
        "create_project",
        {"name": "rep-mcp", "paths": [str(rep_proj)]},
    )
    rep_segs = bridge.call_tool(
        "query_segments", {"project": "rep-mcp", "file_id": "F0001"}
    )["segments"]
    part_run = bridge.call_tool(
        "start_agent_stage_run",
        {
            "project": "rep-mcp",
            "stage": "translation",
            "scope": {"only_file": "F0001", "only_part": rep_segs[0]["part_id"]},
        },
    )
    assert part_run["selected_count"] == 3
    bridge.call_tool(
        "finish_agent_stage_run",
        {"project": "rep-mcp", "run_id": part_run["run_id"], "action": "interrupt"},
    )
    bridge.call_tool(
        "submit_segment_fix",
        {
            "project": "rep-mcp",
            "segment_id": rep_segs[0]["segment_id"],
            "stage": "translation",
            "text": "站住！",
        },
    )
    bridge.call_tool(
        "submit_segment_fix",
        {
            "project": "rep-mcp",
            "segment_id": rep_segs[1]["segment_id"],
            "stage": "translation",
            "text": "前进。",
        },
    )
    bridge.call_tool(
        "submit_segment_fix",
        {
            "project": "rep-mcp",
            "segment_id": rep_segs[2]["segment_id"],
            "stage": "translation",
            "text": "等一下！",
        },
    )
    # Inspect repetition group by source before any formal draft exists
    live_inspected = bridge.call_tool(
        "inspect_repetition_group",
        {"project": "rep-mcp", "source": "待て！", "window": 1},
    )
    assert len(live_inspected["occurrences"]) == 2

    rep_overview = bridge.call_tool(
        "get_repetition_overview",
        {"project": "rep-mcp", "create_draft": True},
    )
    assert rep_overview["draft"] is not None
    assert rep_overview["draft"]["total_groups"] == 1
    rep_group = rep_overview["draft"]["groups"][0]
    assert rep_group["source"] == "待て！"

    deleted = bridge.call_tool(
        "configure_project",
        {"project": "rep-mcp", "action": "delete_project", "confirm": True},
    )
    assert deleted["deleted"] is True
