import json
from types import SimpleNamespace

import pytest

from app.config import load_project_config
from tests.test_terms_exchange import make_project
from tests.helpers import llm_jsonl


def view(key, text="说明"):
    return {"entity_key": key, "members": [key], "items": [], "input_version": key,
            "fragments": [{"id": key + "-fragment", "text": text, "owner": key,
                           "original_owner": key, "origin": "manual", "source_range": None,
                           "_positions": {}, "state": "active", "valid": True}]}


@pytest.mark.asyncio
async def test_batch_keeps_success_before_retry_and_isolates_cross_entity_refs(tmp_path):
    from app.term_knowledge import aggregate_batch
    config = load_project_config(make_project(tmp_path), stage="terminology_decision")
    saved, requests = {}, []

    class Model:
        async def chat(self, **kwargs):
            payload = json.loads(kwargs["messages"][1]["content"])
            requests.append(payload)
            entities = payload["entities"]
            if len(requests) == 1:
                assert [e["entity"] for e in entities] == ["alpha", "beta"]
                assert len({f["id"] for e in entities for f in e["fragments"]}) == 2
                records = [{"type": "entity_knowledge", "entity": e["entity"], "reason": "整理",
                            "items": [{"text": "说明", "refs": [entities[0]["fragments"][0]["id"]]}]}
                           for e in reversed(entities)]
            else:
                assert set(saved) == {"alpha"}
                assert [e["entity"] for e in entities] == ["beta"]
                records = [{"type": "entity_knowledge", "entity": "beta", "reason": "整理",
                            "items": [{"text": "说明", "refs": [entities[0]["fragments"][0]["id"]]}]}]
            return SimpleNamespace(content=llm_jsonl(records)), str(len(requests))

    errors = await aggregate_batch(Model(), [view("alpha"), view("beta")], "整理", config,
                                   on_result=lambda v, r: saved.update({v["entity_key"]: r}))
    assert errors == {}
    assert set(saved) == {"alpha", "beta"}
    assert saved["beta"]["items"][0]["fragment_ids"] == ["beta-fragment"]


def test_budget_packing_isolates_oversized_entity(tmp_path):
    from app.term_knowledge import pack_knowledge_batches
    config = load_project_config(make_project(tmp_path), stage="terminology_decision")
    config["execution"].update(target_chunk_input_tokens=1000, input_tokens_per_minute=0)
    config["llm"].update(context_window_tokens=2000, context_safety_margin_tokens=0)
    batches, tokens, errors = pack_knowledge_batches([view("a"), view("large", "大" * 10000), view("b")], "整理", config)
    assert [[v["entity_key"] for v in b] for b in batches] == [["a", "b"]]
    assert tokens > 0
    assert set(errors) == {"large"}


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing", "duplicate", "unknown", "broken", "no_end"])
async def test_batch_response_failure_scope(tmp_path, damage):
    from app.term_knowledge import aggregate_batch
    config = load_project_config(make_project(tmp_path), stage="terminology_decision")
    config["retry"]["format_max_attempts"] = 0
    saved = {}
    records = [{"type": "entity_knowledge", "entity": k, "reason": "整理", "items": []}
               for k in ("alpha", "beta")]
    if damage == "missing":
        records.pop()
    elif damage == "duplicate":
        records.append(records[-1])
    elif damage == "unknown":
        records.append({**records[-1], "entity": "other"})
    content = llm_jsonl(records)
    if damage == "broken":
        content = "{bad}\n" + content
    elif damage == "no_end":
        content = content.rsplit("\n", 1)[0]

    class Model:
        async def chat(self, **kwargs):
            return SimpleNamespace(content=content), "request"

    errors = await aggregate_batch(Model(), [view("alpha"), view("beta")], "整理", config,
                                   on_result=lambda v, r: saved.update({v["entity_key"]: r}))
    assert set(saved) == ({"alpha"} if damage in {"missing", "duplicate"} else set())
    assert set(errors) == ({"beta"} if saved else {"alpha", "beta"})


@pytest.mark.asyncio
async def test_knowledge_run_uses_bounded_parallel_batches(tmp_path, monkeypatch):
    import asyncio
    import httpx
    from app.sqlite_storage import read_json, write_json, record_header
    from app.term_knowledge import add_unsourced, empty_knowledge, entity_view, knowledge_payload
    from app.term_decision import run_terminology_decision, _prompt
    from app.execution import estimate_messages, render_messages
    from tests.helpers import use_llm_preset

    project = make_project(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test")
    config = load_project_config(project, stage="terminology_decision")
    terms = [{"source": k, "normalized": k, "category": "name", "preferred_translation": k,
              "aliases": [], "group_primary": None, "conflicts": {}} for k in ("a", "b", "c", "d")]
    data = {"terms_revision": 1, "terms": terms, "knowledge": empty_knowledge()}
    for term in terms:
        add_unsourced(data["knowledge"], term["normalized"], "身份说明", "manual")
    meta = read_json(project, project / "project.json")
    write_json(project, project / "terminology/terms.json", record_header("terminology_library", meta["project_id"], **data))
    overrides = read_json(project, project / "terminology/overrides.json")
    overrides["overrides"] = [{"normalized": t["normalized"], "preferred_translation": t["normalized"]} for t in terms]
    write_json(project, project / "terminology/overrides.json", overrides)
    prompt = _prompt(project, "zh-CN")["knowledge"]
    single = estimate_messages(render_messages(prompt, knowledge_payload([entity_view(project, data, "a")], config["project"]["target_language"])[0]), config["execution"]["token_safety_factor"])
    use_llm_preset(tmp_path, target_chunk_input_tokens=single, max_parallel=2, max_parallel_per_key=2)
    active = maximum = 0
    first_pair = asyncio.Event()
    calls = []

    async def handler(request):
        nonlocal active, maximum
        messages = json.loads(request.content)["messages"]
        payload = json.loads(messages[1]["content"])
        assert messages[0]["content"] == _prompt(project, "zh-CN")["knowledge"]
        assert payload["phase"] == "knowledge"
        assert len(payload["entities"]) == 1
        calls.extend(e["entity"] for e in payload["entities"])
        active += 1
        maximum = max(maximum, active)
        if active == 2:
            first_pair.set()
        try:
            await asyncio.wait_for(first_pair.wait(), 3)
            await asyncio.sleep(0)
            return httpx.Response(200, json={"choices": [{"message": {"content": llm_jsonl([
                {"type": "entity_knowledge", "entity": e["entity"], "reason": "整理", "items": []}
                for e in payload["entities"]])}}]})
        finally:
            active -= 1

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await run_terminology_decision(project, http_client=client)
    assert maximum == 2
    assert sorted(calls) == ["a", "b", "c", "d"]
    assert result["failed"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("knowledge_only", [False, True])
async def test_cancelled_retry_preserves_checkpoint_and_resume_requests_only_unfinished(tmp_path, monkeypatch, knowledge_only):
    import asyncio
    import httpx
    from app.sqlite_storage import read_json, write_json, record_header
    from app.term_knowledge import add_unsourced, empty_knowledge
    from app.term_decision import run_terminology_decision, decision_resume_compatibility, decision_plan, _decision_fingerprint
    project = make_project(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test")
    terms = [{"source": k, "normalized": k, "category": "name", "preferred_translation": k,
              "aliases": [], "group_primary": None, "conflicts": {}} for k in ("alpha", "beta")]
    data = {"terms_revision": 1, "terms": terms, "knowledge": empty_knowledge()}
    for term in terms:
        add_unsourced(data["knowledge"], term["normalized"], "身份说明", "manual")
    meta = read_json(project, project / "project.json")
    write_json(project, project / "terminology/terms.json", record_header("terminology_library", meta["project_id"], **data))
    overrides = read_json(project, project / "terminology/overrides.json")
    overrides["overrides"] = [{"normalized": t["normalized"], "preferred_translation": t["normalized"]} for t in terms]
    write_json(project, project / "terminology/overrides.json", overrides)
    calls = []
    resumed = False

    async def handler(request):
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        entities = payload["entities"]
        calls.append([e["entity"] for e in entities])
        if len(calls) == 2:
            checkpoint = json.loads(next((project / "runs").glob("*/terminology_decision_checkpoint.json")).read_text())
            assert set(checkpoint["phases"]["knowledge"]) == {"beta"}
            raise asyncio.CancelledError()
        records = [{"type": "entity_knowledge", "entity": e["entity"], "reason": "整理",
                    "items": [{"text": "身份", "refs": [e["fragments"][0]["id"] if resumed or e["entity"] == "beta" else "unknown"]}]}
                   for e in entities]
        return httpx.Response(200, json={"choices": [{"message": {"content": llm_jsonl(records)}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(asyncio.CancelledError):
            await run_terminology_decision(project, knowledge_only=knowledge_only, http_client=client)
        run_id = next((project / "runs").glob("*/terminology_decision_checkpoint.json")).parent.name
        assert decision_resume_compatibility(project, run_id, source_terms_revision=1, knowledge_only=knowledge_only)[0]
        assert not decision_resume_compatibility(project, run_id, source_terms_revision=1, knowledge_only=not knowledge_only)[0]
        if knowledge_only:
            from app.web_tasks import task_options
            from app.config import dump_config, load_config
            current = task_options(project, "terminology_decision", knowledge_only=True)
            assert current["running_run"]["resume_compatible"] is True
            assert current["running_run"]["total_steps"] == 2
            assert task_options(project, "terminology_decision")["running_run"]["resume_compatible"] is False
            config = load_config(project / "config.toml")
            config["terminology_decision"]["description_generation"] = "summary_only"
            (project / "config.toml").write_text(dump_config(config))
            changed = task_options(project, "terminology_decision", knowledge_only=True)
            assert changed["running_run"]["resume_compatible"] is False
            from app.errors import UsageError
            with pytest.raises(UsageError, match="生成方式"):
                await run_terminology_decision(project, knowledge_only=True, http_client=client, resume_run_id=run_id)
            config["terminology_decision"]["description_generation"] = "sourced"
            (project / "config.toml").write_text(dump_config(config))
        resumed = True
        result = await run_terminology_decision(project, knowledge_only=knowledge_only, http_client=client, resume_run_id=run_id)
    assert calls == [["alpha", "beta"], ["alpha"], ["alpha"]]
    assert result["proposals"] == 2
    # Independent policy changes participate in the existing resume fingerprint.
    plan = decision_plan(project, knowledge_only=knowledge_only)
    before = _decision_fingerprint(plan["config"], plan["prompts"], plan["library"], final_review=False)
    path = project / "prompts/terminology_knowledge.zh-CN.middle.txt"
    path.write_text(path.read_text() + "\nchanged policy")
    plan = decision_plan(project, knowledge_only=knowledge_only)
    after = _decision_fingerprint(plan["config"], plan["prompts"], plan["library"], final_review=False)
    assert before != after


def test_soft_overflow_and_feedback_budget_are_explicit(tmp_path):
    from app.term_knowledge import pack_knowledge_batches, knowledge_payload
    from app.execution import estimate_messages, render_messages
    config = load_project_config(make_project(tmp_path), stage="terminology_decision")
    config["execution"]["input_tokens_per_minute"] = 0
    size = estimate_messages(render_messages("整理", knowledge_payload([view("alpha")], config["project"]["target_language"])[0]), config["execution"]["token_safety_factor"])
    config["execution"]["target_chunk_input_tokens"] = size - 1
    config["terminology_decision"]["allow_soft_target_overflow"] = False
    assert set(pack_knowledge_batches([view("alpha")], "整理", config)[2]) == {"alpha"}
    config["terminology_decision"]["allow_soft_target_overflow"] = True
    assert len(pack_knowledge_batches([view("alpha")], "整理", config)[0]) == 1
    config["llm"].update(context_window_tokens=size + 5, context_safety_margin_tokens=0)
    batches, _, errors = pack_knowledge_batches([view("alpha")], "整理", config, feedback="格式修正" * 100)
    assert not batches and set(errors) == {"alpha"}


@pytest.mark.asyncio
async def test_request_failure_preserves_prior_success_and_fatal_errors_propagate(tmp_path):
    from app.term_knowledge import aggregate_batch
    from app.errors import ExternalError, FatalExternalError
    config = load_project_config(make_project(tmp_path), stage="terminology_decision")
    saved = {}
    calls = 0

    class Model:
        async def chat(self, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ExternalError("请求失败")
            return SimpleNamespace(content=llm_jsonl([
                {"type": "entity_knowledge", "entity": "alpha", "reason": "整理", "items": []},
                {"type": "entity_knowledge", "entity": "beta", "reason": "整理", "items": [{"text": "错误引用", "refs": ["unknown"]}]}])), "request"

    errors = await aggregate_batch(Model(), [view("alpha"), view("beta")], "整理", config,
                                   on_result=lambda v, r: saved.update({v["entity_key"]: r}))
    assert set(saved) == {"alpha"}
    assert errors == {"beta": "请求失败"}

    class FatalModel:
        async def chat(self, **kwargs):
            raise FatalExternalError("致命错误")

    with pytest.raises(FatalExternalError):
        await aggregate_batch(FatalModel(), [view("alpha")], "整理", config, on_result=lambda *_: None)


@pytest.mark.asyncio
async def test_description_generation_modes_apply_without_growing_evidence(tmp_path, monkeypatch):
    import httpx
    from app.config import dump_config, load_config
    from app.sqlite_storage import read_json, write_json, record_header
    from app.term_decision import run_terminology_decision
    from app.term_decision_drafts import apply_decision_draft, current_decision_draft
    from app.term_library import load_terms, term_normalization
    from app.term_knowledge import empty_knowledge, publish_evidence, entity_view
    from tests.test_term_knowledge import scan_record

    project = make_project(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test")
    config = load_config(project / "config.toml")
    term = {"source": "Alpha", "normalized": "alpha", "category": "name", "preferred_translation": "阿尔法",
            "aliases": [], "group_primary": None, "conflicts": {}}
    data = {"terms_revision": 1, "terms": [term], "knowledge": empty_knowledge()}
    publish_evidence(project, data, [scan_record(project, "Alpha is a captain")], term_normalization(config))
    meta = read_json(project, project / "project.json")
    write_json(project, project / "terminology/terms.json", record_header("terminology_library", meta["project_id"], **data))
    overrides = read_json(project, project / "terminology/overrides.json")
    overrides["overrides"] = [{"normalized": "alpha", "preferred_translation": "阿尔法"}]
    write_json(project, project / "terminology/overrides.json", overrides)
    requests = []

    async def handler(request):
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        mode = payload["description_generation"]
        requests.append(mode)
        entity = payload["entities"][0]
        record = {"type": "entity_knowledge", "entity": "alpha", "reason": "整理", "items": []}
        if mode != "summary_only":
            record["items"] = [{"text": "详细身份说明", "refs": [entity["fragments"][0]["id"]]}]
        if mode != "sourced":
            record["summary"] = "船长"
        return httpx.Response(200, json={"choices": [{"message": {"content": llm_jsonl([record])}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for mode, expected in [("sourced", ["详细身份说明"]),
                               ("sourced_and_summary", ["详细身份说明", "船长"]),
                               ("summary_only", ["船长"])]:
            config["terminology_decision"]["description_generation"] = mode
            (project / "config.toml").write_text(dump_config(config))
            result = await run_terminology_decision(project, http_client=client)
            assert result["proposals"] == 1
            proposed = current_decision_draft(project)["proposals"][0]["knowledge_after"]["alpha"]
            assert [i["text"] for i in proposed["items"]] == expected
            apply_decision_draft(project, confirm_all=True)
            published = load_terms(project)
            view = entity_view(project, published, "alpha")
            assert view["status"] == "current"
            assert [i["text"] for i in view["items"]] == expected
            assert len(published["knowledge"]["fragments"]) == 1
            if mode != "sourced":
                assert view["items"][-1]["fragment_ids"] == []
                assert view["items"][-1]["sources"] == [None]
    assert requests == ["sourced", "sourced_and_summary", "summary_only"]
    from app.project import apply_file_replacement, prepare_file_replacement
    revised = tmp_path / "revised.txt"
    revised.write_text("Changed source", encoding="utf-8")
    apply_file_replacement(project, prepare_file_replacement(project, "F0001", revised))
    assert entity_view(project, published, "alpha")["status"] == "stale"


@pytest.mark.parametrize(("mode", "items", "summary"), [
    ("sourced_and_summary", [], ""),
    ("summary_only", [{"text": "冗余详情", "refs": ["K1"]}], "总结"),
    ("sourced_and_summary", [{"text": "无引用", "refs": []}], "总结"),
])
def test_description_generation_rejects_invalid_model_output(mode, items, summary):
    from app.term_knowledge import _knowledge_result
    from app.errors import UsageError
    with pytest.raises(UsageError):
        _knowledge_result(view("alpha"), {"type": "entity_knowledge", "entity": "alpha", "reason": "整理",
                          "items": items, "summary": summary}, {"K1": ("alpha", "alpha-fragment")}, mode)


@pytest.mark.asyncio
async def test_knowledge_only_regenerates_current_entities_and_reuses_draft_lifecycle(tmp_path, monkeypatch):
    import httpx
    from copy import deepcopy
    from app.sqlite_storage import read_json, write_json, record_header
    from app.term_knowledge import add_unsourced, empty_knowledge, save_items, entity_view
    from app.term_library import load_terms
    from app.term_decision import run_terminology_decision, decision_plan
    from app.term_decision_drafts import current_decision_draft, apply_decision_draft, rollback_decision

    project = make_project(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test")
    terms = [{"source": "Alpha", "normalized": "alpha", "category": "name", "preferred_translation": "阿尔法",
              "aliases": ["Al"], "group_primary": None, "conflicts": {}}]
    data = {"terms_revision": 1, "terms": terms, "knowledge": empty_knowledge()}
    orphan = add_unsourced(data["knowledge"], "al", "别名原有证据", "manual")
    save_items(project, data, "alpha", [{"text": "原有说明", "fragment_ids": []}])
    meta = read_json(project, project / "project.json")
    write_json(project, project / "terminology/terms.json", record_header("terminology_library", meta["project_id"], **data))
    before = deepcopy(load_terms(project))
    assert entity_view(project, before, "alpha")["status"] == "current"
    plan = decision_plan(project, knowledge_only=True)
    assert plan["eligible"] == []
    assert plan["knowledge_pending"] == 1
    assert set(plan["prompts"]) == {"knowledge"}
    calls = []
    invalid_output = False

    async def handler(request):
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        calls.append(payload["phase"])
        assert payload["phase"] == "knowledge"
        entity = payload["entities"][0]
        return httpx.Response(200, json={"choices": [{"message": {"content": llm_jsonl([
            {"type": "entity_knowledge", "entity": "alpha", "reason": "重新整理说明",
             "items": [{"text": "更新说明", "refs": ["unknown" if invalid_output else entity["fragments"][0]["id"]]}]}])}}]})

    from fastapi.testclient import TestClient
    from app.web import create_app
    from app.web_tasks import WebTaskManager
    from app.execution import Scope
    api = TestClient(create_app(projects_root=project.parent))
    options = api.get("/api/v1/projects/terms/task-options/terminology_decision?knowledge_only=true")
    assert options.status_code == 200
    assert options.json()["selected"] == 1
    assert options.json()["knowledge_only"] is True
    assert api.get("/api/v1/projects/terms/task-options/translation?knowledge_only=true").status_code == 400
    assert api.get("/api/v1/projects/terms/task-options/terminology_decision?knowledge_only=true&final_review=true").status_code == 400

    async def execute(*args, **kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await run_terminology_decision(*args, http_client=client, **kwargs)

    monkeypatch.setattr("app.web_tasks.run_terminology_decision", execute)
    manager = WebTaskManager()
    started = await manager.start(project, "terminology_decision", scope=Scope(),
                                  reuse_mixed_fingerprints=False, run_action=None, knowledge_only=True)
    await manager.tasks[started["task_id"]].asyncio_task
    finished = manager.get(started["task_id"])
    assert finished["status"] == "completed", finished.get("error")
    assert finished["knowledge_only"] is True
    result = finished["summary"]
    draft = current_decision_draft(project)
    assert draft["knowledge_only"] is True
    assert len(draft["proposals"]) == 1
    assert draft["proposals"][0]["kind"] == "entity_knowledge"
    assert draft["proposals"][0]["evidence"] == {}
    proposal_id = draft["proposals"][0]["proposal_id"]
    rejected = api.put("/api/v1/projects/terms/terms/decision/rejections", json={"rejected_proposal_ids": [proposal_id]})
    assert rejected.status_code == 200
    assert rejected.json()["draft"]["proposals"][0]["knowledge_evidence"] == {}
    assert "knowledge_evidence" not in draft["proposals"][0]
    restored = api.put("/api/v1/projects/terms/terms/decision/rejections", json={"rejected_proposal_ids": []})
    assert restored.status_code == 200
    assert restored.json()["draft"]["rejected_proposal_ids"] == []
    apply_decision_draft(project, confirm_all=True)
    after = load_terms(project)
    assert after["knowledge"]["fragments"][orphan]["owner"] == "al"
    assert after["terms"][0]["description"] == "更新说明"
    for field in ("source", "normalized", "category", "preferred_translation", "aliases", "group_primary"):
        assert after["terms"][0][field] == before["terms"][0][field]
    manifest = read_json(project, project / "runs" / result["run_id"] / "manifest.json")
    assert manifest["knowledge_only"] is True
    assert manifest["manual_review_replaces_previous"] is False
    rollback_decision(project, confirm=True)
    assert load_terms(project)["terms"][0]["description"] == "原有说明"
    assert calls == ["knowledge"]
    from app.errors import UsageError
    invalid_output = True
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UsageError, match="实体说明聚合失败"):
            await run_terminology_decision(project, knowledge_only=True, http_client=client)
    assert current_decision_draft(project) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("schema_version", [7, 8])
async def test_sidecar_migration_preserves_real_draft_apply_and_rollback(tmp_path, monkeypatch, schema_version):
    import httpx
    import sqlite3
    from copy import deepcopy
    from app.config import load_project_config
    from app.project import add_project_files
    from app.sqlite_storage import (
        _SUPPORTED_CACHE, append_jsonl, atomic_write_json, database_path, ensure_supported,
        read_json, read_segments, record_header, write_json,
    )
    from app.term_knowledge import empty_knowledge, publish_evidence, save_items, source_range, entity_view
    from app.term_library import load_terms, term_normalization
    from app.summary_provenance import digest
    from app.term_decision import run_terminology_decision
    from app.errors import UsageError
    from app.term_decision_drafts import apply_decision_draft, current_decision_draft, rollback_decision

    project = make_project(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test")
    data = {"terms_revision": 1, "terms": [
        {"source": "Alpha", "normalized": "alpha", "category": "name",
         "preferred_translation": "阿尔法", "aliases": [], "group_primary": None, "conflicts": {}},
        {"source": "Beta", "normalized": "beta", "category": "name",
         "preferred_translation": "贝塔", "aliases": [], "group_primary": None, "conflicts": {}},
    ], "knowledge": empty_knowledge()}
    additional = tmp_path / "later.txt"
    additional.write_text("Alpha returns", encoding="utf-8")
    add_project_files(project, [str(additional)])
    segments = read_segments(project)
    originals = {segment["segment_id"]: segment for segment in segments}
    records = []
    for record_id, segment, source, description in zip(
        ("REC-Z", "REC-A"), segments, ("Alpha", "Beta"), ("first fact", "second fact"), strict=True,
    ):
        record = record_header("terminology_candidates", "test", record_id=record_id,
                               active_task_id="scan", terms=[{"source": source, "description": description}],
                               source_range=source_range([segment], originals, {}))
        append_jsonl(project, project / "terminology/candidates.jsonl", record)
        records.append(record)
    publish_evidence(project, data, records, term_normalization(load_project_config(project)))
    save_items(project, data, "alpha", [{"text": "Alpha原有说明", "fragment_ids": ["REC-Z:0"]}])
    save_items(project, data, "beta", [{"text": "Beta原有说明", "fragment_ids": ["REC-A:0"]}])
    metadata = read_json(project, project / "project.json")
    write_json(project, project / "terminology/terms.json",
               record_header("terminology_library", metadata["project_id"], **data))

    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        if payload["phase"] in {"adjudication", "consistency"}:
            records = []
            for term in payload["terms"]:
                if payload["phase"] == "consistency" and term["normalized"] == "beta":
                    records.append({"type": "decision", "normalized": "beta", "action": "update",
                                    "reason": "合并实体", "changes": {"group_primary": "alpha"}})
                else:
                    records.append({"type": "decision", "normalized": term["normalized"],
                                    "action": "keep", "reason": "保持"})
        else:
            entity = payload["entities"][0]
            records = [{"type": "entity_knowledge", "entity": "alpha", "reason": "整理",
                        "items": [{"text": "合并说明", "refs": [f["id"] for f in entity["fragments"]]}]}]
        return httpx.Response(200, json={"choices": [{"message": {"content": llm_jsonl(records)}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await run_terminology_decision(project, http_client=client)
    run_dir = project / "runs" / result["run_id"]
    draft_path = run_dir / "terminology_decision_draft.json"
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    proposal = next(proposal for proposal in draft["proposals"] if "alpha" in proposal.get("knowledge_after", {}))
    assert proposal["knowledge_after"]["alpha"]["members"] == ["alpha", "beta"]
    source_library = draft["source_library"]
    legacy_source = deepcopy(source_library)
    final_terms = {term["normalized"]: term for term in legacy_source["terms"]}
    for state in proposal["after"]:
        final_terms[state["normalized"]].update(state)
    legacy_source["terms"] = list(final_terms.values())
    from app.term_knowledge import transfer_alias_evidence
    transfer_alias_evidence(legacy_source, term_normalization(load_project_config(project)))
    merged_view = entity_view(project, legacy_source, "alpha")
    legacy_hash = digest([merged_view["members"], [(f["id"], f["text"], f["source_range"])
                                                    for f in merged_view["fragments"] if f["state"] == "active"]])
    assert legacy_hash != proposal["knowledge_after"]["alpha"]["input_version"]

    def mark_legacy(library):
        for root in ("alpha", "beta"):
            view = entity_view(project, library, root)
            saved = library["knowledge"]["entities"][root]
            saved["input_version"] = digest([view["members"], [(f["id"], f["text"], f["source_range"])
                                                                  for f in view["fragments"] if f["state"] == "active"]])
            saved["source_order"] = view["source_order"]
        library["knowledge"]["version"] = 1

    mark_legacy(source_library)
    proposal["knowledge_after"]["alpha"]["input_version"] = legacy_hash
    draft["source_library"]["knowledge"]["version"] = 1
    atomic_write_json(draft_path, draft, compact=True)

    checkpoint_path = run_dir / "terminology_decision_checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    saved = checkpoint["phases"]["knowledge"]["alpha"]["decision"]
    saved["input_version"] = legacy_hash
    checkpoint_order = saved["source_order"]
    atomic_write_json(checkpoint_path, checkpoint, compact=True)
    manifest_path = project / "runs" / result["run_id"] / "manifest.json"
    manifest = read_json(project, manifest_path)
    manifest.update(status="running", decision_status="generating")
    write_json(project, manifest_path, manifest)

    legacy = read_json(project, project / "terminology/terms.json")
    mark_legacy(legacy)
    write_json(project, project / "terminology/terms.json", legacy)
    with sqlite3.connect(database_path(project)) as connection:
        connection.execute("UPDATE schema_meta SET value = ? WHERE key = 'schema_version'", (str(schema_version),))
    _SUPPORTED_CACHE.discard(database_path(project))

    original_draft = draft_path.read_bytes()
    from app import sqlite_storage
    migrate_main = sqlite_storage._migrate_term_knowledge_to_v9

    def fail_after_sidecars(*args, **kwargs):
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(sqlite_storage, "_migrate_term_knowledge_to_v9", fail_after_sidecars)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        ensure_supported(project)
    assert draft_path.read_bytes() == original_draft
    with sqlite3.connect(database_path(project)) as connection:
        assert connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == str(schema_version)
        assert connection.execute("SELECT status FROM runs WHERE run_id = ?", (result["run_id"],)).fetchone()[0] == "running"
    monkeypatch.setattr(sqlite_storage, "_migrate_term_knowledge_to_v9", migrate_main)

    assert ensure_supported(project) is not None
    migrated = json.loads(draft_path.read_text(encoding="utf-8"))
    assert migrated["source_library"]["knowledge"]["version"] == 2
    migrated_hash = migrated["proposals"][0]["knowledge_after"]["alpha"]["input_version"]
    assert migrated_hash != legacy_hash
    migrated_checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    migrated_decision = migrated_checkpoint["phases"]["knowledge"]["alpha"]["decision"]
    assert migrated_decision["input_version"] == legacy_hash
    assert migrated_decision["source_order"] == checkpoint_order
    manifest_path = run_dir / "manifest.json"
    manifest = read_json(project, manifest_path)
    assert manifest["status"] == "interrupted"
    assert manifest["decision_status"] == "pending"
    with pytest.raises(UsageError, match="旧 Run 已结束"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await run_terminology_decision(
                project, resume_run_id=result["run_id"], http_client=client,
            )
    assert calls == 3

    assert current_decision_draft(project)["needs_review"] == []

    apply_decision_draft(project, confirm_all=True)
    applied = load_terms(project)
    assert next(term for term in applied["terms"] if term["normalized"] == "beta")["group_primary"] == "alpha"
    assert entity_view(project, applied, "alpha")["status"] == "current"
    rollback_decision(project, confirm=True)
    rolled_back = load_terms(project)
    assert next(term for term in rolled_back["terms"] if term["normalized"] == "beta")["group_primary"] is None
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        next_run = await run_terminology_decision(project, replace_draft=True, http_client=client)
    assert next_run["run_id"] != result["run_id"]
    assert calls == 6


@pytest.mark.asyncio
async def test_knowledge_only_partial_failure_preserves_reviewable_success(tmp_path, monkeypatch):
    import httpx
    from app.errors import UsageError
    from app.sqlite_storage import read_json, write_json, record_header
    from app.term_knowledge import add_unsourced, empty_knowledge
    from app.term_decision import run_terminology_decision
    from app.term_decision_drafts import current_decision_draft, apply_decision_draft, manual_review_state

    project = make_project(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test")
    terms = [{"source": k, "normalized": k, "category": "name", "preferred_translation": k,
              "aliases": [], "group_primary": None, "conflicts": {}} for k in ("alpha", "beta")]
    data = {"terms_revision": 1, "terms": terms, "knowledge": empty_knowledge()}
    for term in terms:
        add_unsourced(data["knowledge"], term["normalized"], "身份说明", "manual")
    meta = read_json(project, project / "project.json")
    write_json(project, project / "terminology/terms.json", record_header("terminology_library", meta["project_id"], **data))

    async def handler(request):
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        records = [{"type": "entity_knowledge", "entity": e["entity"], "reason": "整理",
                    "items": [{"text": "更新说明", "refs": [e["fragments"][0]["id"] if e["entity"] == "alpha" else "unknown"]}]}
                   for e in payload["entities"]]
        return httpx.Response(200, json={"choices": [{"message": {"content": llm_jsonl(records)}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UsageError, match="实体说明聚合失败"):
            await run_terminology_decision(project, knowledge_only=True, http_client=client)
    draft = current_decision_draft(project)
    assert [p["normalized"] for p in draft["proposals"]] == [["alpha"]]
    assert [item["normalized"] for item in draft["needs_review"]] == ["beta"]
    assert draft["needs_review"][0]["evidence"] == {}
    apply_decision_draft(project, confirm_all=True)
    assert [item["normalized"] for item in manual_review_state(project)["items"]] == ["beta"]
