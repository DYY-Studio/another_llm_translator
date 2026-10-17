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
    payload, refs = knowledge_payload([view], "简体中文")
    assert refs == {"K1": ("alpha", "persistent")}
    assert payload["entities"][0]["fragments"][0]["text"] == "完整说明"
    assert payload["entities"][0]["fragments"][0]["source_range"] == [
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
    from app.sqlite_storage import read_json, write_json, database_path
    from app.term_library import load_terms
    project = make_project(tmp_path)
    data = {"terms_revision": 3, "terms": [{"normalized": "alpha", "source": "Alpha", "description": "已有说明", "group_primary": None}]}
    write_json(project, project / "terminology" / "terms.json", record_header("terminology_library", "test", **data))
    write_json(project, project / "terminology" / "overrides.json", record_header("terminology_overrides", "test",
               overrides=[{"normalized": "alpha", "description": "人工决定"},
                          {"normalized": "beta", "source": "Beta", "description": "已移除资料", "disabled": True}]))
    import sqlite3, zlib
    from app import sqlite_storage
    with sqlite3.connect(database_path(project)) as connection:
        raw = zlib.decompress(connection.execute("SELECT payload_json FROM terms_state WHERE key='terms'").fetchone()[0]).decode()
        connection.execute("UPDATE terms_state SET payload_json=? WHERE key='terms'", (raw,))
        connection.execute("UPDATE schema_meta SET value='7' WHERE key='schema_version'")
    sqlite_storage._SUPPORTED_CACHE.discard(database_path(project))
    migrated = load_terms(project)
    backup, = (project / "snapshots" / "storage_migrations").glob("*.sqlite")
    assert backup.is_file()
    import json
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
    knowledge_prompt = project / "prompts" / prompt_file("terminology_knowledge", "zh-CN")
    knowledge_prompt.write_text("KNOWLEDGE_RULE_SENTINEL", encoding="utf-8")
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
        assert "KNOWLEDGE_RULE_SENTINEL" in messages[0]["content"]
        scan = next(f for f in payload["entities"][0]["fragments"] if f["origin"] == "scan")
        assert scan["source_range"] == [{"file_id": "F0001", "part_id": "document",
                                        "first": {"position": 0, "start": 0}, "last": {"position": 0, "end": 14}}]
        attempts.append(payload)
        refs = [f["id"] for f in payload["entities"][0]["fragments"]]
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
        import zlib
        payload = json.loads(zlib.decompress(connection.execute(
            "SELECT payload_json FROM terminology_candidates WHERE record_id=?", (record["record_id"],)
        ).fetchone()[0]))
        payload["terms"] = record["terms"]
        connection.execute("UPDATE terminology_candidates SET payload_json=? WHERE record_id=?",
                           (zlib.compress(json.dumps(payload).encode()), record["record_id"]))
    publish_evidence(project, data, [record], term_normalization(load_project_config(project)))
    meta = read_json(project, project / "project.json")
    write_json(project, project / "terminology" / "terms.json", record_header("terminology_library", meta["project_id"], **data))
    calls = []
    allow_alpha = False
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        calls.append((payload["phase"], tuple(e["entity"] for e in payload.get("entities", []))))
        if payload["phase"] == "knowledge":
            records = []
            for entity in payload["entities"]:
                root = entity["entity"]
                refs = [entity["fragments"][0]["id"]] if root == "beta" or allow_alpha else ["missing"]
                records.append({"type": "entity_knowledge", "entity": root, "reason": "身份",
                                "items": [{"text": root, "refs": refs}]})
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
    assert calls == [("knowledge", ("alpha",))]
    assert result["proposals"] == 2
    for proposal in current_decision_draft(project)["proposals"]:
        refs = {key for aggregate in proposal["knowledge_after"].values()
                for item in aggregate["items"] for key in item["fragment_ids"]}
        from app.term_decision_drafts import decision_fragment_detail
        assert "knowledge_evidence" not in proposal
        for fragment_id in refs:
            fragment = decision_fragment_detail(project, run_id, fragment_id)
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
    from app.term_library import term_normalization
    from app.term_knowledge import publish_evidence, entity_view, pack_knowledge_batches
    project = make_project(tmp_path)
    data = library()
    config = load_project_config(project, stage="terminology_decision")
    publish_evidence(project, data, [scan_record(project, "身份信息" * 1000)], term_normalization(config))
    config["llm"].update(context_window_tokens=100, context_safety_margin_tokens=0)
    batches, _, errors = pack_knowledge_batches([entity_view(project, data, "alpha")], "聚合", config)
    assert not batches
    assert "完整证据" in errors["alpha"]


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
    assert entities[0]["items"][1]["source_relation"] == ["within"]
    assert set(entities[0]) == {"entity", "items"}
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


@pytest.mark.parametrize("fail", [False, True])
def test_automatic_migration_ends_old_tasks_atomically_and_opens_silently(tmp_path, monkeypatch, fail):
    import json
    import sqlite3
    from app import sqlite_storage, term_knowledge
    from app.errors import StorageError
    from app.sqlite_storage import database_path, read_json, write_json

    project = make_project(tmp_path)
    terms = record_header("terminology_library", "test", terms_revision=3, terms=[
        {"source": "Alpha", "normalized": "alpha", "description": "组主说明", "group_primary": None},
        {"source": "Ally", "normalized": "ally", "description": "成员说明", "group_primary": "alpha"},
    ])
    write_json(project, project / "terminology/terms.json", terms)
    write_json(project, project / "terminology/active_task.json",
               record_header("terminology_task", "test", status="partial_published"))
    runs = [("old-scan", "terminology", "running", None),
            ("old-draft", "terminology_decision", "completed", "pending"),
            ("old-generating", "terminology_decision", "running", "generating"),
            ("translation", "translation", "running", None)]
    for run_id, stage, status, decision in runs:
        value = record_header("run", "test", run_id=run_id, stage=stage, status=status)
        if decision:
            value["decision_status"] = decision
        write_json(project, project / "runs" / run_id / "manifest.json", value)
    with sqlite3.connect(database_path(project)) as db:
        db.execute("UPDATE schema_meta SET value='7' WHERE key='schema_version'")
        before_terms = db.execute("SELECT key,payload_json FROM terms_state ORDER BY key").fetchall()
        before_runs = db.execute("SELECT run_id,status,payload_json FROM runs ORDER BY run_id").fetchall()
    sqlite_storage._SUPPORTED_CACHE.discard(database_path(project))

    def unexpected(*args, **kwargs):
        raise AssertionError("Migration must not read scan evidence or rebuild Segment metadata")

    monkeypatch.setattr(term_knowledge, "evidence", unexpected)
    monkeypatch.setattr(sqlite_storage, "_refresh_segment_format_counts", unexpected)
    if fail:
        def fail_projection(*args, **kwargs):
            raise StorageError("migration probe failed")
        monkeypatch.setattr(term_knowledge, "project_descriptions", fail_projection)
        with pytest.raises(StorageError, match="migration probe failed"):
            sqlite_storage.ensure_supported(project)
        with sqlite3.connect(database_path(project)) as db:
            assert db.execute("SELECT key,payload_json FROM terms_state ORDER BY key").fetchall() == before_terms
            assert db.execute("SELECT run_id,status,payload_json FROM runs ORDER BY run_id").fetchall() == before_runs
            assert db.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "7"
        assert database_path(project) not in sqlite_storage._SUPPORTED_CACHE
    else:
        from fastapi.testclient import TestClient
        from app.web import create_app
        client = TestClient(create_app(projects_root=project.parent))
        response = client.post("/api/v1/projects/open", json={"path": str(project)})
        assert response.status_code == 200, response.text
        assert response.json()["warnings"] == []
        assert read_json(project, project / "terminology/active_task.json")["status"] == "reset"
        for run_id, stage, status, decision in runs:
            run = read_json(project, project / "runs" / run_id / "manifest.json")
            assert run["status"] == ("interrupted" if status == "running" and stage != "translation" else status)
            if decision:
                assert run["decision_status"] == "discarded"
            if stage != "translation":
                assert json.loads((project / "runs" / run_id / "manifest.json").read_text()) == run
        migrated = read_json(project, project / "terminology/terms.json")
        assert migrated["terms_revision"] == 4
        assert len(migrated["knowledge"]["entities"]) == 1
        assert {item["text"] for item in migrated["knowledge"]["entities"]["alpha"]["items"]} == {"组主说明", "成员说明"}
        assert all("description_items" not in term for term in migrated["terms"])
        assert client.post("/api/v1/projects/open", json={"path": str(project)}).status_code == 200
        assert read_json(project, project / "terminology/terms.json") == migrated
    backup, = (project / "snapshots/storage_migrations").glob("*.sqlite")
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT key,payload_json FROM terms_state ORDER BY key").fetchall() == before_terms
        assert db.execute("SELECT run_id,status,payload_json FROM runs ORDER BY run_id").fetchall() == before_runs
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_storage_upgrade_preserves_current_knowledge_and_pending_tasks(tmp_path):
    import sqlite3
    from app import sqlite_storage
    from app.sqlite_storage import database_path, read_json, write_json

    project = make_project(tmp_path)
    value = record_header("terminology_library", "test", **library())
    active = record_header("terminology_task", "test", status="active")
    run = record_header("run", "test", run_id="new-draft", stage="terminology_decision",
                        status="running", decision_status="generating")
    write_json(project, project / "terminology/terms.json", value)
    write_json(project, project / "terminology/active_task.json", active)
    write_json(project, project / "runs/new-draft/manifest.json", run)
    before = [read_json(project, project / path) for path in
              ("terminology/terms.json", "terminology/active_task.json", "runs/new-draft/manifest.json")]
    with sqlite3.connect(database_path(project)) as db:
        db.execute("UPDATE schema_meta SET value='7' WHERE key='schema_version'")
    sqlite_storage._SUPPORTED_CACHE.discard(database_path(project))
    sqlite_storage.ensure_supported(project)
    assert [read_json(project, project / path) for path in
            ("terminology/terms.json", "terminology/active_task.json", "runs/new-draft/manifest.json")] == before



def test_automatic_upgrade_ends_unpublished_legacy_scan(tmp_path):
    import sqlite3
    from app import sqlite_storage
    from app.sqlite_storage import database_path, read_json, write_json

    project = make_project(tmp_path)
    write_json(project, project / "terminology/active_task.json",
               record_header("terminology_task", "test", status="active"))
    with sqlite3.connect(database_path(project)) as db:
        db.execute("DELETE FROM terms_state WHERE key='terms'")
        db.execute("UPDATE schema_meta SET value='7' WHERE key='schema_version'")
    sqlite_storage._SUPPORTED_CACHE.discard(database_path(project))
    sqlite_storage.ensure_supported(project)
    assert read_json(project, project / "terminology/active_task.json")["status"] == "reset"


def test_scan_publication_transfers_new_alias_evidence(tmp_path: Path) -> None:
    from app.sqlite_storage import read_json, write_json
    from app.term_library import _merge_and_publish_terms
    from app.term_knowledge import entity_view
    from tests.helpers import scan_source_range

    project = make_project(tmp_path)
    data = library()
    data['terms'][0].update(aliases=['Beta'], category='name', preferred_translation=None, conflicts={})
    write_json(project, project / 'terminology/terms.json', record_header('terminology_library', 'test', **data))
    write_json(project, project / 'terminology/overrides.json', record_header(
        'terminology_overrides', 'test', overrides=[{'normalized': 'beta', 'source': 'Beta', 'disabled': True}]))
    write_json(project, project / 'terminology/active_task.json', record_header(
        'terminology_task', 'test', active_task_id='rescan', status='active'))
    record = record_header('terminology_candidates', 'test', active_task_id='rescan',
                           source_order={'segment_id': 'F0001-S000001', 'offset': 0},
                           source_range=scan_source_range(project),
                           terms=[{'source': 'Beta', 'description': '新身份', 'category': 'name'}])
    append_jsonl(project, project / 'terminology/candidates.jsonl', record)
    published = _merge_and_publish_terms(project, task_id='rescan', project_id='test', published_run_id='run')
    fragment, = entity_view(project, published, 'alpha')['fragments']
    assert fragment['owner'] == 'alpha'
    assert fragment['original_owner'] == 'beta'
    assert fragment['text'] == '新身份'
    assert read_json(project, project / 'terminology/terms.json')['knowledge']['fragments'][fragment['id']]['owner'] == 'alpha'


def test_scan_publication_failure_does_not_write_alias_cleanup(tmp_path: Path, monkeypatch) -> None:
    from app.sqlite_storage import read_json, write_json
    from app.term_library import _merge_and_publish_terms

    project = make_project(tmp_path)
    data = library()
    data['terms'][0].update(aliases=['Beta'], category='name', preferred_translation=None, conflicts={})
    data['terms'].append({'source': 'Beta', 'normalized': 'beta',
                          'aliases': [], 'category': 'name', 'preferred_translation': None, 'conflicts': {}})
    terms_path = project / 'terminology/terms.json'
    overrides_path = project / 'terminology/overrides.json'
    write_json(project, terms_path, record_header('terminology_library', 'test', **data))
    write_json(project, overrides_path, record_header('terminology_overrides', 'test',
               overrides=[{'normalized': 'alpha', 'source': 'Alpha', 'aliases': ['Beta']}]))
    before = read_json(project, overrides_path)
    def fail(*args, **kwargs):
        raise UsageError('发布失败')
    monkeypatch.setattr('app.term_knowledge.publish_evidence', fail)
    with pytest.raises(UsageError, match='发布失败'):
        _merge_and_publish_terms(project, task_id='scan', project_id='test', published_run_id='run')
    assert read_json(project, overrides_path) == before
    assert read_json(project, terms_path)['terms_revision'] == 1


@pytest.mark.parametrize(('start', 'end', 'changed_source', 'expected'), [
    (5, 10, False, 'active'),
    (4, 6, False, 'superseded'),
    (5, 10, True, 'superseded'),
])
def test_replacement_indexes_each_request_and_preserves_slice_boundaries(
    tmp_path: Path, monkeypatch, start, end, changed_source, expected,
) -> None:
    from copy import deepcopy
    from app import term_knowledge
    from app.term_library import term_normalization
    from app.config import load_project_config

    project = make_project(tmp_path)
    data = library()
    segments = read_segments(project)
    first = record_header('terminology_candidates', 'test', active_task_id='scan',
                          source_range=term_knowledge.source_range(segments, {s['segment_id']: s for s in segments}, {}),
                          terms=[{'source': 'Alpha', 'description': '旧证据'}])
    first['source_range'][0].update(start=0, end=5)
    append_jsonl(project, project / 'terminology/candidates.jsonl', first)
    spec = term_normalization(load_project_config(project))
    term_knowledge.publish_evidence(project, data, [first], spec)
    replacement = deepcopy(first)
    replacement.update(record_id='replacement', active_task_id='rescan', terms=[])
    replacement['source_range'][0].update(start=start, end=end)
    if changed_source:
        replacement['source_range'][0]['source_digest'] = 'changed'
    calls = []
    index = term_knowledge._range_index
    def counted(ranges):
        calls.append(ranges)
        return index(ranges)
    monkeypatch.setattr(term_knowledge, '_range_index', counted)
    term_knowledge.publish_evidence(project, data, [replacement], spec)
    assert len(calls) == 1
    assert data['knowledge']['fragments'][f"{first['record_id']}:0"]['state'] == expected


@pytest.mark.parametrize(("sourced", "unsourced", "position", "expected"), [
    ("all", "all", 1, ["前文", "当前", "后文一", "后文并列", "后文二", "总结"]),
    ("prefer_previous", "when_no_sourced", 1, ["前文", "当前"]),
    ("prefer_previous", "all", 0, ["后文一", "后文并列", "总结"]),
    ("none", "when_no_sourced", 1, ["总结"]),
    ("all", "none", 1, ["前文", "当前", "后文一", "后文并列", "后文二"]),
    ("none", "none", 1, []),
])
def test_entity_injection_policies(sourced, unsourced, position, expected):
    from app.term_matching import _TermMatchCache
    from app.term_library import TermNormalization
    segments = [{"segment_id": str(i), "source": "Alpha"} for i in range(5)]
    def entry(text, index):
        return {"text": text, "sources": [None] if index is None else [[
            {"segment_id": str(index), "file_id": "F", "part_id": "P", "start": 0, "end": 5}]]}
    data = {"terms": [{"source": "Alpha", "normalized": "alpha", "aliases": [],
        "preferred_translation": "阿尔法", "knowledge_status": "current", "description_items": [
            entry("前文", 0 if position else 2), entry("当前", 1 if position else 3),
            entry("后文一", 2 if position else 1), entry("后文并列", 2 if position else 1),
            entry("后文二", 4), entry("总结", None)]}]}
    cache = _TermMatchCache(data, TermNormalization("NFKC", True), 10, segments=segments,
                           sourced_policy=sourced, unsourced_policy=unsourced)
    terms = cache.for_items([segments[position]])
    assert terms[0]["preferred_translation"] == "阿尔法"
    assert [i["text"] for i in terms[0].get("entity_knowledge", {}).get("items", [])] == expected
    if not expected:
        assert "entity_knowledge" not in terms[0]


def test_lazy_matching_rejects_stale_entity_before_compacting_missing_sources(tmp_path, monkeypatch):
    from app.term_matching import _TermMatchCache
    from app.term_library import TermNormalization

    def stale_view(*args, **kwargs):
        return {"entity_key": "alpha", "members": ["alpha"], "status": "stale",
                "items": [{"text": "旧说明", "sources": [[{"segment_id": "removed", "start": 0, "end": 5}]]}]}

    monkeypatch.setattr("app.term_knowledge.entity_view", stale_view)
    matcher = _TermMatchCache(library(), TermNormalization("NFKC", True), 10,
                             segments=[], project=tmp_path)
    with pytest.raises(UsageError, match="实体说明已过期"):
        matcher.for_items([{"segment_id": "current", "source": "Alpha"}])
