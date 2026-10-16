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
async def test_cancelled_retry_preserves_checkpoint_and_resume_requests_only_unfinished(tmp_path, monkeypatch):
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
            await run_terminology_decision(project, http_client=client)
        run_id = next((project / "runs").glob("*/terminology_decision_checkpoint.json")).parent.name
        assert decision_resume_compatibility(project, run_id, source_terms_revision=1)[0]
        resumed = True
        result = await run_terminology_decision(project, http_client=client, resume_run_id=run_id)
    assert calls == [["alpha", "beta"], ["alpha"], ["alpha"]]
    assert result["proposals"] == 2
    # Independent policy changes participate in the existing resume fingerprint.
    plan = decision_plan(project)
    before = _decision_fingerprint(plan["config"], plan["prompts"], plan["library"], final_review=False)
    path = project / "prompts/terminology_knowledge.zh-CN.middle.txt"
    path.write_text(path.read_text() + "\nchanged policy")
    plan = decision_plan(project)
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
    import sqlite3
    from app.sqlite_storage import database_path
    with sqlite3.connect(database_path(project)) as connection:
        connection.execute("UPDATE segments SET source = 'changed'")
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
