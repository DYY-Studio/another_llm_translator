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
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
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
