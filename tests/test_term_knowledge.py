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


def test_knowledge_payload_compacts_ranges_without_bridging_gaps_or_slices():
    from copy import deepcopy
    from app.term_knowledge import knowledge_payload, source_range

    originals = {f"S{i}": {"segment_id": f"S{i}", "file_id": "F1", "part_id": "P1", "source": "abcdef"}
                 for i in range(8)}
    originals["S7"]["part_id"] = "P2"
    inputs = [originals["S0"], originals["S1"], originals["S3"],
              {**originals["S4"], "source": "abc"}, originals["S5"],
              {**originals["S6"], "source": "ef", "_source_offset": 4}, originals["S7"]]
    ranges = source_range(inputs, originals, {})
    fragment = {"id": "persistent", "state": "active", "text": "完整说明",
                "owner": "alpha", "original_owner": "alpha", "origin": "scan",
                "source_range": ranges, "_positions": {f"S{i}": i for i in range(8)}}
    before = deepcopy(fragment)
    view = {"entity_key": "alpha", "members": ["alpha"], "items": [], "fragments": [fragment]}
    payload, refs = knowledge_payload(view, "简体中文")
    assert refs == {"K1": "persistent"}
    assert payload["fragments"][0]["text"] == "完整说明"
    assert payload["fragments"][0]["source_range"] == [
        {"file_id": "F1", "part_id": "P1", "first": {"position": 0, "start": 0}, "last": {"position": 1, "end": 6}},
        {"file_id": "F1", "part_id": "P1", "first": {"position": 3, "start": 0}, "last": {"position": 4, "end": 3}},
        {"file_id": "F1", "part_id": "P1", "first": {"position": 5, "start": 0}, "last": {"position": 5, "end": 6}},
        {"file_id": "F1", "part_id": "P1", "first": {"position": 6, "start": 4}, "last": {"position": 6, "end": 6}},
        {"file_id": "F1", "part_id": "P2", "first": {"position": 7, "start": 0}, "last": {"position": 7, "end": 6}},
    ]
    assert fragment == before


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
    write_json(project, project / "terminology" / "overrides.json", record_header("terminology_overrides", "test",
               overrides=[{"normalized": "alpha", "description": "人工决定"},
                          {"normalized": "beta", "source": "Beta", "description": "已移除资料", "disabled": True}]))
    with pytest.raises(UsageError, match="迁移"):
        load_terms(project)
    backup = migrate(project)
    assert backup.is_file()
    import sqlite3, json
    with sqlite3.connect(backup) as connection:
        old = json.loads(connection.execute("SELECT payload_json FROM terms_state WHERE key='terms'").fetchone()[0])
    assert "knowledge" not in old
    migrated = load_terms(project)
    assert migrated["terms"][0]["description"] == "人工决定"
    assert migrated["terms"][0]["knowledge_status"] == "current"
    from app.term_knowledge import entity_view
    fragment = entity_view(project, migrated, "alpha")["fragments"][0]
    assert fragment["origin"] == "legacy"
    assert fragment["source_range"] is None
    from app.web_store import WebStore
    assert next(row for row in WebStore(project).terms()["terms"] if row["normalized"] == "beta")["description"] == "已移除资料"
    from app.term_exchange import export_terms
    output = tmp_path / "exchange.json"
    summary = export_terms(project, output, include_disabled=True)
    assert summary["warnings"]
    assert next(row for row in json.loads(output.read_text())["terms"] if row["source"] == "Beta")["description"] == "已移除资料"


@pytest.mark.asyncio
async def test_protected_explanation_updates_through_decision_and_rollback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import httpx
    from app.config import load_project_config
    from app.sqlite_storage import read_json, write_json
    from app.term_library import term_normalization, load_terms
    from app.term_knowledge import publish_evidence, save_items, project_descriptions
    from app.term_decision import run_terminology_decision
    from app.term_decision_drafts import apply_decision_draft, current_decision_draft, rollback_decision
    from tests.helpers import llm_jsonl
    monkeypatch.setenv("LLM_API_KEY", "test")
    project = make_project(tmp_path)
    data = library()
    data["terms"][0].update(category="name", preferred_translation="阿尔法", aliases=[], conflicts={})
    from app.project import prompt_file
    prompt_path = project / "prompts" / prompt_file("terminology_decision", "zh-CN")
    prompt_path.write_text(prompt_path.read_text() + "\nNAME_RULE_SENTINEL", encoding="utf-8")
    save_items(project, data, "alpha", [{"text": "人工已有说明", "fragment_ids": []}])
    project_descriptions(project, data)
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
        messages = json.loads(request.content)["messages"]
        payload = json.loads(messages[1]["content"])
        assert payload["phase"] == "knowledge"
        assert "NAME_RULE_SENTINEL" not in messages[0]["content"]
        scan = next(f for f in payload["fragments"] if f["origin"] == "scan")
        assert scan["source_range"] == [{"file_id": "F0001", "part_id": "document",
                                        "first": {"position": 0, "start": 0}, "last": {"position": 0, "end": 14}}]
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
    stored = read_json(project, project / "terminology" / "terms.json")
    assert all(not {"entity_key", "knowledge_status", "description_items"} & term.keys() for term in stored["terms"])
    from app.web_store import WebStore
    assert all(set(item) == {"text", "fragment_ids"} for term in WebStore(project).terms()["terms"]
               for item in term["description_items"])
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
    for proposal in current_decision_draft(project)["proposals"]:
        refs = {key for aggregate in proposal["knowledge_after"].values()
                for item in aggregate["items"] for key in item["fragment_ids"]}
        assert set(proposal["knowledge_evidence"]) == refs
        for fragment in proposal["knowledge_evidence"].values():
            assert all(set(r) == {"segment_id", "file_id", "part_id", "start", "end"}
                       for r in fragment["source_range"])
    from collections import Counter
    import app.term_knowledge as knowledge_module
    from app.term_decision_drafts import apply_decision_draft
    counts = Counter()
    for name in ("evidence", "project_descriptions"):
        original = getattr(knowledge_module, name)
        def counted(*args, _name=name, _original=original, **kwargs):
            counts[_name] += 1
            return _original(*args, **kwargs)
        monkeypatch.setattr(knowledge_module, name, counted)
    assert apply_decision_draft(project, confirm_all=True)["applied"] == 2
    assert counts == {"evidence": 2, "project_descriptions": 2}


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
    from app.term_knowledge import entity_view, save_items, project_descriptions
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
    project_descriptions(project, data)
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


def test_generation_context_shares_entity_once_and_blocks_stale_hits(tmp_path: Path) -> None:
    from app.term_knowledge import save_items, project_descriptions, publish_evidence, entity_view
    from app.term_matching import _TermMatchCache
    from app.term_library import TermNormalization, term_normalization
    from app.config import load_project_config
    from app.sqlite_storage import read_segments
    project = make_project(tmp_path)
    data = library()
    data["terms"][0].update(aliases=[],category="name",preferred_translation="阿尔法")
    data["terms"].append(dict(source="Beta",normalized="beta",group_primary="alpha",aliases=[],category="name",preferred_translation="贝塔"))
    save_items(project,data,"alpha",[{"text":"共享说明","fragment_ids":[]}])
    publish_evidence(project, data, [scan_record(project, "身份一"), scan_record(project, "身份二")],
                     term_normalization(load_project_config(project)), replace=False)
    view = entity_view(project, data, "alpha")
    refs = [f["id"] for f in view["fragments"] if f["origin"] == "scan"]
    save_items(project, data, "alpha", [{"text": "共享说明", "fragment_ids": view["items"][0]["fragment_ids"]},
                                        {"text": "有来源说明", "fragment_ids": refs}])
    project_descriptions(project, data)
    segments = read_segments(project)
    cache = _TermMatchCache(data,TermNormalization("NFKC",True),10,segments=segments)
    matches = cache.for_items(segments)
    entities = [m["entity_knowledge"] for m in matches if "entity_knowledge" in m]
    assert len(entities)==1
    assert entities[0]["items"][0]["text"]=="共享说明"
    assert entities[0]["items"][0]["unsourced"] is True
    assert entities[0]["items"][1]["source_ranges"] == [{"file_id": "F0001", "part_id": "document",
                                                       "first": {"position": 0, "start": 0}, "last": {"position": 0, "end": 14}}]
    assert entities[0]["current_positions"][0]["segment_id"]==segments[0]["segment_id"]
    assert all("description" not in match for match in matches)
    data["terms"][1]["group_primary"]=None
    project_descriptions(project,data)
    stale = _TermMatchCache(data,TermNormalization("NFKC",True),10,segments=segments)
    with pytest.raises(UsageError,match="过期"):
        stale.for_items(segments)


def test_knowledge_api_checks_revision_and_explicit_fragment_operations(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient
    from app.web import create_app
    from app.sqlite_storage import read_json, write_json
    from app.term_knowledge import save_items
    project = make_project(tmp_path)
    data = library()
    data["terms"][0].update(aliases=[],category="name",preferred_translation="阿尔法",conflicts={})
    save_items(project,data,"alpha",[{"text":"人工说明","fragment_ids":[]}])
    meta = read_json(project,project / "project.json")
    write_json(project,project / "terminology" / "terms.json",record_header("terminology_library",meta["project_id"],**data))
    app = create_app(projects_root=project.parent,app_root=tmp_path / "terms")
    client = TestClient(app)
    url="/api/v1/projects/terms/terms/knowledge"
    response=client.get(url,params={"normalized":"alpha"})
    assert response.status_code==200
    details=response.json()
    assert details["items"][0]["text"]=="人工说明"
    bad=client.put(url,json={"normalized":"alpha","expected_revision":0,"action":"save_items","items":[]})
    assert bad.status_code==400
    term_url = "/api/v1/projects/terms/terms"
    assert client.post(term_url, json={"source": "Alpha", "description_items": []}).status_code == 400
    assert client.post(term_url, json={"source": "Alpha", "expected_revision": 0, "description_items": []}).status_code == 400
    fragment=details["fragments"][0]["id"]
    disabled=client.put(url,json={"normalized":"alpha","expected_revision":1,"action":"set_fragment_enabled","fragment_id":fragment,"enabled":False})
    assert disabled.status_code==200
    assert client.get(url,params={"normalized":"alpha"}).json()["status"]=="stale"
    keep=client.put(url,json={"normalized":"alpha","expected_revision":2,"action":"confirm_keep"})
    assert keep.status_code==400
    fixed=client.put(url,json={"normalized":"alpha","expected_revision":2,"action":"save_items","items":[{"text":"人工修复","fragment_ids":[]}]})
    assert fixed.status_code==200
    assert client.get(url,params={"normalized":"alpha"}).json()["status"]=="current"


@pytest.mark.asyncio
async def test_decision_application_keeps_other_requests_responsive(tmp_path, monkeypatch):
    import asyncio
    import threading
    import httpx
    from app.web import create_app

    project = make_project(tmp_path)
    app = create_app(projects_root=project.parent)
    entered = threading.Event()
    release = threading.Event()

    def apply(*args, **kwargs):
        entered.set()
        assert release.wait(5), "应用期间其他请求被阻塞"
        return {"run_id": "test", "applied": 0, "rejected": 0, "terms_revision": 0}

    monkeypatch.setattr("app.web_term_routes.apply_decision_draft", apply)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        pending = asyncio.create_task(client.post("/api/v1/projects/terms/terms/decision/apply", json={"confirm": True}))
        try:
            assert await asyncio.to_thread(entered.wait, 2), (await pending).text
            response = await asyncio.wait_for(client.get("/api/v1/tasks/active"), 2)
            assert response.status_code == 200
            assert not pending.done()
        finally:
            release.set()
        assert (await pending).status_code == 200


@pytest.mark.asyncio
async def test_force_scan_requires_replacement_confirmation_and_empty_results_retire_old_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import httpx
    from app.execution import Scope
    from app.stage_terminology import run_terminology
    from app.term_library import load_terms
    from app.term_knowledge import entity_view
    from tests.helpers import llm_jsonl
    monkeypatch.setenv("LLM_API_KEY","test")
    project=make_project(tmp_path)
    calls=0
    def handler(request):
        nonlocal calls
        calls+=1
        records=[dict(type="term",source="Alpha",category="name",description="船长")] if calls==1 else []
        return httpx.Response(200,json={"choices":[{"message":{"content":llm_jsonl(records)}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await run_terminology(project,Scope(),http_client=client)
        with pytest.raises(UsageError,match="确认"):
            await run_terminology(project,Scope(force=True),http_client=client)
        assert calls==1
        await run_terminology(project,Scope(force=True,confirm_knowledge_replacement=True),http_client=client)
    assert calls==2
    assert all(f["state"]=="superseded" for f in entity_view(project,load_terms(project),"alpha")["fragments"])


def test_import_creates_unsourced_explanations_and_preserves_applied_existing_entity(tmp_path: Path) -> None:
    import json
    from app.term_exchange import import_terms
    from app.term_library import load_terms
    from app.term_knowledge import entity_view
    project=make_project(tmp_path)
    path=tmp_path / "terms.json"
    def write(text):
        path.write_text(json.dumps({"schema_version":1,"record_type":"terminology_exchange","terms":[{"source":"Alpha","category":"name","description":text,"aliases":[],"disabled":False}]}),encoding="utf-8")
    write("最初说明")
    import_terms(project,path,dry_run=False)
    assert load_terms(project)["terms"][0]["description"]=="最初说明"
    assert entity_view(project,load_terms(project),"alpha")["fragments"][0]["source_range"] is None
    write("新说明")
    import_terms(project,path,dry_run=False)
    view=entity_view(project,load_terms(project),"alpha")
    assert view["status"]=="pending"
    assert view["items"][0]["text"]=="最初说明"
    assert {f["text"] for f in view["fragments"]}=={"最初说明","新说明"}
    repeated=import_terms(project,path,dry_run=False)
    assert repeated["changed"] is False


def test_clear_preserves_cited_candidates_in_rollback_and_increments_revision(tmp_path: Path) -> None:
    import json
    from app.config import load_project_config
    from app.sqlite_storage import clear_terminology_state, read_json, write_json, read_terminology_candidates
    from app.execution import create_run
    from app.term_knowledge import publish_evidence, evidence, save_items
    from app.term_library import term_normalization, load_terms
    project = make_project(tmp_path)
    data = library()
    first = scan_record(project, "captain")
    unused = scan_record(project, "unpublished")
    publish_evidence(project, data, [first], term_normalization(load_project_config(project)))
    save_items(project, data, "alpha", [{"text": "captain", "fragment_ids": [f'{first["record_id"]}:0']}])
    write_json(project, project / "terminology" / "terms.json", record_header("terminology_library", "test", **data))
    _, directory = create_run(project, config=load_project_config(project), stage="terminology_decision",
                              fingerprint="test", prompt="test", selected_count=1, requested_count=1, reused_count=0)
    (directory / "terminology_decision_draft.json").write_text(json.dumps({"source_library": data}), encoding="utf-8")
    clear_terminology_state(project, read_json(project, project / "terminology" / "overrides.json"))
    assert load_terms(project)["terms_revision"] == 2
    assert load_terms(project)["terms"] == []
    assert [r["record_id"] for r in read_terminology_candidates(project, [first["record_id"], unused["record_id"]])] == [first["record_id"]]
    assert evidence(project, data)[f'{first["record_id"]}:0']["text"] == "captain"


def test_reordered_reviewed_evidence_is_stale_even_when_not_cited(tmp_path: Path) -> None:
    from app.project import add_project_files
    from app.config import load_project_config
    from app.term_library import term_normalization
    from app.term_knowledge import publish_evidence, save_items, entity_view, source_range
    project = make_project(tmp_path)
    additional = tmp_path / "later.txt"
    additional.write_text("Alpha returns", encoding="utf-8")
    add_project_files(project, [str(additional)])
    segments = read_segments(project)
    data = library()
    records = []
    for index, segment in enumerate(segments):
        record = record_header("terminology_candidates", "test", active_task_id="scan",
                               terms=[{"source": "Alpha", "description": f"fact-{index}"}],
                               source_range=source_range([segment], {s["segment_id"]: s for s in segments}, {}))
        append_jsonl(project, project / "terminology" / "candidates.jsonl", record)
        records.append(record)
    publish_evidence(project, data, records, term_normalization(load_project_config(project)))
    save_items(project, data, "alpha", [{"text": "first fact", "fragment_ids": [f'{records[0]["record_id"]}:0']}])
    assert entity_view(project, data, "alpha")["status"] == "current"
    from app.project import reorder_project_files
    reorder_project_files(project, ["F0002", "F0001"])
    assert entity_view(project, data, "alpha")["status"] == "stale"
