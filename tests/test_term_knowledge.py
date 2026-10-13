from pathlib import Path

import pytest

from app.errors import UsageError
from app.sqlite_storage import append_jsonl, read_segments, record_header
from tests.test_terms_exchange import make_project


def scan_record(project: Path, text: str, *, terms: bool = True) -> dict:
    from app.term_knowledge import source_range
    segments = read_segments(project)
    record = record_header(
        "terminology_candidates", "test", active_task_id="scan", status="completed",
        terms=[{"source": "Alpha", "category": "name", "description": text}] if terms else [],
        source_range=source_range(segments, {s["segment_id"]: s for s in segments}, {}),
    )
    record["active_task_id"] = record["record_id"]
    append_jsonl(project, project / "terminology" / "candidates.jsonl", record)
    return record


def library() -> dict:
    from app.term_knowledge import empty_knowledge
    return {"terms_revision": 1, "terms": [
        {"source": "Alpha", "normalized": "alpha", "group_primary": None},
    ], "knowledge": empty_knowledge()}


def test_published_evidence_needs_application_and_source_changes_block_use(tmp_path: Path) -> None:
    from app.term_knowledge import publish_evidence, entity_view, save_items, project_descriptions
    from app.term_library import term_normalization
    from app.config import load_project_config
    project = make_project(tmp_path)
    data = library()
    spec = term_normalization(load_project_config(project))
    first = scan_record(project, "Alpha is a captain")
    publish_evidence(project, data, [first], spec)
    project_descriptions(project, data)
    assert data["terms"][0]["description"] == ""
    view = entity_view(project, data, "alpha")
    assert view["status"] == "pending"
    fragment = view["fragments"][0]["id"]
    save_items(project, data, "alpha", [{"text": "船长", "fragment_ids": [fragment]}])
    assert entity_view(project, data, "alpha")["status"] == "current"
    second = scan_record(project, "Alpha knows Beta")
    publish_evidence(project, data, [second], spec, replace=False)
    assert entity_view(project, data, "alpha")["status"] == "pending"
    project_descriptions(project, data)
    assert data["terms"][0]["description"] == "船长"
    # Actual persisted source mutation, not a test of implementation text.
    from app.sqlite_storage import database_path
    import sqlite3
    with sqlite3.connect(database_path(project)) as connection:
        connection.execute("UPDATE segments SET source = 'changed'")
    assert entity_view(project, data, "alpha")["status"] == "stale"
    with pytest.raises(UsageError, match="过期"):
        entity_view(project, data, "alpha", require_current=True)


def test_empty_success_replaces_evidence_and_publication_is_idempotent(tmp_path: Path) -> None:
    from app.term_knowledge import publish_evidence, entity_view, save_items
    from app.term_library import term_normalization
    from app.config import load_project_config
    project = make_project(tmp_path)
    data = library()
    spec = term_normalization(load_project_config(project))
    first = scan_record(project, "captain")
    publish_evidence(project, data, [first], spec)
    view = entity_view(project, data, "alpha")
    save_items(project, data, "alpha", [{"text": "captain", "fragment_ids": [view["fragments"][0]["id"]]}])
    publish_evidence(project, data, [first], spec)
    assert entity_view(project, data, "alpha")["status"] == "current"
    empty = scan_record(project, "", terms=False)
    publish_evidence(project, data, [first, empty], spec)
    assert entity_view(project, data, "alpha")["status"] == "stale"
    assert all(f["state"] == "superseded" for f in entity_view(project, data, "alpha")["fragments"])


def test_items_validate_references_and_explicit_manual_items(tmp_path: Path) -> None:
    from app.term_knowledge import save_items, entity_view
    project = make_project(tmp_path)
    data = library()
    with pytest.raises(UsageError, match="引用"):
        save_items(project, data, "alpha", [{"text": "invented", "fragment_ids": ["missing"]}])
    save_items(project, data, "alpha", [{"text": "人工说明", "fragment_ids": []}])
    view = entity_view(project, data, "alpha")
    assert view["status"] == "current"
    assert view["fragments"][0]["origin"] == "manual"
    assert view["fragments"][0]["source_range"] is None


def test_migration_preserves_old_explanations_without_fabricating_sources(tmp_path: Path) -> None:
    from scripts.migrate_term_knowledge import migrate
    from app.sqlite_storage import read_json, write_json, database_path
    from app.term_library import load_terms
    project = make_project(tmp_path)
    data = {"terms_revision": 3, "terms": [{"normalized": "alpha", "source": "Alpha", "description": "已有说明", "group_primary": None}]}
    write_json(project, project / "terminology" / "terms.json", record_header("terminology_library", "test", **data))
    with pytest.raises(UsageError, match="迁移"):
        load_terms(project)
    backup = migrate(project)
    assert backup.is_file()
    import sqlite3, json
    with sqlite3.connect(backup) as connection:
        old = json.loads(connection.execute("SELECT payload_json FROM terms_state WHERE key='terms'").fetchone()[0])
    assert "knowledge" not in old
    migrated = load_terms(project)
    assert migrated["terms"][0]["description"] == "已有说明"
    assert migrated["terms"][0]["knowledge_status"] == "current"
    from app.term_knowledge import entity_view
    fragment = entity_view(project, migrated, "alpha")["fragments"][0]
    assert fragment["origin"] == "legacy"
    assert fragment["source_range"] is None


@pytest.mark.asyncio
async def test_protected_explanation_updates_through_decision_and_rollback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import httpx
    from app.config import load_project_config
    from app.sqlite_storage import read_json, write_json
    from app.term_library import term_normalization, load_terms
    from app.term_knowledge import publish_evidence, save_items
    from app.term_decision import run_terminology_decision
    from app.term_decision_drafts import apply_decision_draft, current_decision_draft, rollback_decision
    from tests.helpers import llm_jsonl
    monkeypatch.setenv("LLM_API_KEY", "test")
    project = make_project(tmp_path)
    data = library()
    data["terms"][0].update(category="name", preferred_translation="阿尔法", aliases=[], conflicts={})
    save_items(project, data, "alpha", [{"text": "人工已有说明", "fragment_ids": []}])
    before = data["terms"][0]["description"]
    record = scan_record(project, "Alpha is a captain")
    publish_evidence(project, data, [record], term_normalization(load_project_config(project)))
    meta = read_json(project, project / "project.json")
    write_json(project, project / "terminology" / "terms.json", record_header("terminology_library", meta["project_id"], **data))
    overrides = read_json(project, project / "terminology" / "overrides.json")
    overrides["overrides"] = [{"normalized": "alpha", "preferred_translation": "阿尔法"}]
    write_json(project, project / "terminology" / "overrides.json", overrides)
    attempts = []
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert payload["phase"] == "knowledge"
        attempts.append(payload)
        refs = [f["id"] for f in payload["fragments"]]
        # Invalid references must trigger a format retry, not a partially accepted explanation.
        content = llm_jsonl([{"type": "entity_knowledge", "entity": "alpha", "reason": "补充身份", "items": [
            {"text": "船长，保留人工说明", "refs": ["unknown"] if len(attempts) == 1 else refs},
        ]}])
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await run_terminology_decision(project, http_client=client)
    assert len(attempts) == 2
    assert result["failed"] == 0
    draft = current_decision_draft(project)
    assert len(draft["proposals"]) == 1
    assert load_terms(project)["terms"][0]["description"] == before
    apply_decision_draft(project, confirm_all=True)
    applied = load_terms(project)["terms"][0]
    assert applied["description"] == "船长，保留人工说明"
    assert applied["preferred_translation"] == "阿尔法"
    assert applied["knowledge_status"] == "current"
    rollback_decision(project, confirm=True)
    assert load_terms(project)["terms"][0]["description"] == before
    assert load_terms(project)["terms"][0]["knowledge_status"] == "pending"


@pytest.mark.asyncio
async def test_failed_entity_holds_its_name_changes_and_resume_reuses_successes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import httpx
    from app.config import load_project_config
    from app.sqlite_storage import read_json, write_json
    from app.term_library import term_normalization
    from app.term_knowledge import publish_evidence
    from app.term_decision import run_terminology_decision
    from app.term_decision_drafts import current_decision_draft
    from tests.helpers import llm_jsonl
    monkeypatch.setenv("LLM_API_KEY", "test")
    project = make_project(tmp_path)
    data = library()
    data["terms"] = [dict(source=name, normalized=name.lower(), group_primary=None,
                          category="name", preferred_translation=None, aliases=[], conflicts={}) for name in ["Alpha", "Beta"]]
    record = scan_record(project, "Alpha is a captain")
    record["terms"].append({"source": "Beta", "category": "name", "description": "Beta is a doctor"})
    # Persist the second term as part of the same successful request.
    from app.sqlite_storage import database_path
    import sqlite3
    with sqlite3.connect(database_path(project)) as connection:
        connection.execute("UPDATE terminology_candidates SET payload_json=json_set(payload_json,'$.terms',json(?)) WHERE record_id=?", (json.dumps(record["terms"]),record["record_id"]))
    publish_evidence(project, data, [record], term_normalization(load_project_config(project)))
    meta = read_json(project, project / "project.json")
    write_json(project, project / "terminology" / "terms.json", record_header("terminology_library", meta["project_id"], **data))
    calls = []
    allow_alpha = False
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        calls.append((payload["phase"], payload.get("entity")))
        if payload["phase"] == "knowledge":
            root = payload["entity"]
            refs = [payload["fragments"][0]["id"]] if root == "beta" or allow_alpha else ["missing"]
            records = [{"type":"entity_knowledge", "entity":root,"reason":"身份", "items":[{"text":root,"refs":refs}]}]
        else:
            assert payload["phase"] in {"adjudication", "consistency"}
            records = [{"type":"decision","normalized":s["normalized"],"reason":"译名",
                        **({"action":"update","changes":{"preferred_translation":"阿尔法" if s["normalized"]=="alpha" else "贝塔"}}
                           if payload["phase"]=="adjudication" else {"action":"keep"})} for s in payload["terms"]]
        return httpx.Response(200,json={"choices":[{"message":{"content":llm_jsonl(records)}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UsageError, match="聚合失败"):
            await run_terminology_decision(project,http_client=client)
        draft = current_decision_draft(project)
        assert [p["normalized"] for p in draft["proposals"]] == [["beta"]]
        assert {item["normalized"] for item in draft["needs_review"]} == {"alpha"}
        run_id = draft["run_id"]
        calls.clear()
        allow_alpha = True
        result = await run_terminology_decision(project,http_client=client,resume_run_id=run_id)
    assert calls == [("knowledge", "alpha")]
    assert result["proposals"] == 2


@pytest.mark.asyncio
async def test_entity_budget_fails_before_any_model_request(tmp_path: Path) -> None:
    from app.config import load_project_config
    from app.errors import RequestSizeError
    from app.term_library import term_normalization
    from app.term_knowledge import publish_evidence, entity_view, aggregate_entity
    project = make_project(tmp_path)
    data = library()
    config = load_project_config(project, stage="terminology_decision")
    publish_evidence(project, data, [scan_record(project, "身份信息" * 1000)], term_normalization(config))
    config["llm"].update(context_window_tokens=100, context_safety_margin_tokens=0)
    class NoRequests:
        async def chat(self, **kwargs):
            raise AssertionError("超限请求不得发送")
    with pytest.raises(RequestSizeError, match="完整证据"):
        await aggregate_entity(NoRequests(), entity_view(project,data,"alpha"), "聚合", config)


def test_group_changes_invalidate_shared_explanation_and_cross_entity_refs(tmp_path: Path) -> None:
    from app.term_knowledge import entity_view, save_items
    project = make_project(tmp_path)
    data = library()
    save_items(project,data,"alpha",[{"text":"Alpha说明","fragment_ids":[]}])
    alpha_ref = entity_view(project,data,"alpha")["items"][0]["fragment_ids"][0]
    data["terms"].append({"source":"Beta","normalized":"beta","group_primary":None})
    with pytest.raises(UsageError,match="引用"):
        save_items(project,data,"beta",[{"text":"错用","fragment_ids":[alpha_ref]}])
    data["terms"][1]["group_primary"] = "alpha"
    assert entity_view(project,data,"beta")["status"] == "stale"
    save_items(project,data,"alpha",[{"text":"共享实体说明","fragment_ids":[alpha_ref]}])
    assert data["terms"][0]["description"] == data["terms"][1]["description"] == "共享实体说明"
    data["terms"][1]["group_primary"] = None
    assert entity_view(project,data,"alpha")["status"] == "stale"
    assert entity_view(project,data,"beta")["status"] == "pending"


@pytest.mark.asyncio
async def test_sibling_name_updates_share_one_review_unit_through_protected_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import httpx
    from app.sqlite_storage import read_json, write_json
    from app.term_decision import run_terminology_decision
    from app.term_decision_drafts import current_decision_draft, apply_decision_draft
    from app.term_library import load_terms
    from app.term_knowledge import save_items
    from tests.helpers import llm_jsonl
    monkeypatch.setenv("LLM_API_KEY","test")
    project = make_project(tmp_path)
    data = library()
    data["terms"] = [dict(source=name,normalized=name.lower(),group_primary=None if name=="Alpha" else "alpha",category="name",preferred_translation=None,aliases=[],conflicts={}) for name in ["Alpha","Beta","Gamma"]]
    save_items(project,data,"alpha",[])
    meta = read_json(project,project / "project.json")
    write_json(project,project / "terminology" / "terms.json",record_header("terminology_library",meta["project_id"],**data))
    overrides = read_json(project,project / "terminology" / "overrides.json")
    overrides["overrides"] = [{"normalized":"alpha","category":"name"}]
    write_json(project,project / "terminology" / "overrides.json",overrides)
    def handler(request):
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        rows = [dict(type="decision",normalized=s["normalized"],reason="译名",**(
            {"action":"update","changes":{"preferred_translation":s["source"]+"译"}} if payload["phase"]=="adjudication" else {"action":"keep"}
        )) for s in payload["terms"]]
        return httpx.Response(200,json={"choices":[{"message":{"content":llm_jsonl(rows)}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await run_terminology_decision(project,http_client=client)
    proposals = current_decision_draft(project)["proposals"]
    assert len(proposals)==1
    assert proposals[0]["normalized"]==["alpha","beta","gamma"]
    apply_decision_draft(project,confirm_all=True,rejected_proposal_ids=[proposals[0]["proposal_id"]])
    assert all(s["preferred_translation"] is None for s in load_terms(project)["terms"])
