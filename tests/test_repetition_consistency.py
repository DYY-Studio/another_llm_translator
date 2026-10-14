from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app import config
from app.errors import ExternalError, UsageError
from app.execution import Scope
from app.project import init_project, load_segments
from app.sqlite_storage import append_stage_results, latest_stage_states, read_json, record_header
from tests.test_decision import preset
from tests.test_foundation import make_app_root


def repetition_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, source_text: str = "前文\n承诺\n后文\n承诺\n另一个场景\n承诺\n相同\n相同", texts: list[str] | None = None) -> Path:
    root = make_app_root(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text(source_text, encoding="utf-8-sig")
    project, _ = init_project([str(source)], name="repetition", app_root=root,
                              projects_root=tmp_path / "projects")
    assert project is not None
    path = project / "config.toml"
    path.write_text(path.read_text().replace('[decision]\npreset = ""', '[decision]\npreset = "local"'))
    decision_root = config.APP_ROOT / "decision_presets"
    decision_root.mkdir()
    (decision_root / "local.json").write_text(json.dumps(preset()))
    monkeypatch.setenv("LLM_API_KEY", "test")
    monkeypatch.setenv("DECISION_TEST_KEY", "test")
    texts = texts if texts is not None else ["前文译文", "我会来接你", "后文译文", "我会去接你", "另一个场景译文", "我会去接你", "相同译文", "相同译文"]
    project_id = read_json(project, project / "project.json")["project_id"]
    append_stage_results(project, [record_header("stage_result", project_id,
        stage="translation", segment_id=s["segment_id"], status="completed",
        text=text, origin="web", stage_fingerprint="test")
        for s, text in zip(load_segments(project), texts, strict=True)])
    return project


def transport(*, choice: str = "compare", invalid: bool = False, requests: list | None = None, apply_members: list[str] | None = None):
    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if requests is not None:
            requests.append(body)
        if request.url.path == "/custom/path":
            answer = {"type": "refusal"} if choice == "refusal" else {
                "type": "choice", "choice": choice, "confidence": 1,
                "probabilities": {"compare": int(choice == "compare"),
                                  "skip": int(choice == "skip")}}
            return httpx.Response(200, json={"model": "test-model", "answers": {"value": answer}})
        result = {"groups": [{"members": ["1", "2"], "target": "1", "apply": ["1", "2"], "reason": "同一承诺重现"}],
                  "keep": ["3"], "uncertain": []}
        if apply_members is not None:
            result["groups"][0]["apply"] = apply_members
        if invalid:
            result["groups"][0]["members"] = ["1", "unknown"]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"repetitions": [{"id": group["id"], **result} for group in json.loads(body["messages"][-1]["content"])["repetitions"]]})}, "finish_reason": "stop"}],
                                       "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}})
    return httpx.MockTransport(respond)


def test_generation_detects_groups_and_sends_shared_evidence(tmp_path, monkeypatch):
    from app.repetition_consistency import current_draft, repetition_plan, run_repetition
    project = repetition_project(tmp_path, monkeypatch)
    plan = repetition_plan(project, Scope(), screening_enabled=True)
    assert len(plan["groups"]) == 1
    assert plan["selected"] == 3
    requests = []
    async def run():
        async with httpx.AsyncClient(transport=transport(requests=requests)) as client:
            return await run_repetition(project, Scope(), http_client=client, screening_enabled=True)
    result = asyncio.run(run())
    assert result["completed"] == 3
    draft = current_draft(project)
    assert len(draft["groups"][0]["proposals"]) == 1
    payload = json.loads(requests[1]["messages"][-1]["content"])
    group = payload["repetitions"][0]
    assert group["source"] == "承诺"
    assert group["translations"] == ["我会来接你", "我会去接你"]
    assert len(group["occurrences"]) == 3
    assert payload["files"] == {"1": "source.txt"}
    assert [item["file"] for item in group["occurrences"]] == [1, 1, 1]
    assert [item["position"] for item in group["occurrences"]] == [1, 3, 5]
    from app.request_overview import llm_overview
    assert llm_overview(requests[1]["messages"])["input"] == payload
    assert all("source" not in item and "translation" not in item for item in group["occurrences"])


@pytest.mark.parametrize("choice", ["skip", "refusal"])
def test_screening_preserves_skip_and_refusal(tmp_path, monkeypatch, choice):
    from app.repetition_consistency import current_draft, run_repetition
    project = repetition_project(tmp_path, monkeypatch)
    requests = []
    async def run():
        async with httpx.AsyncClient(transport=transport(choice=choice, requests=requests)) as client:
            await run_repetition(project, Scope(), http_client=client, screening_enabled=True)
    asyncio.run(run())
    assert len(requests) == (1 if choice == "skip" else 2)
    assert current_draft(project)["groups"][0]["screening"] == choice


def test_invalid_group_fails_without_replacing_existing_draft(tmp_path, monkeypatch):
    from app.repetition_consistency import current_draft, run_repetition
    project = repetition_project(tmp_path, monkeypatch)
    async def run(invalid=False, replace=False):
        async with httpx.AsyncClient(transport=transport(invalid=invalid)) as client:
            return await run_repetition(project, Scope(), http_client=client, replace_draft=replace, screening_enabled=True)
    asyncio.run(run())
    old = current_draft(project)["run_id"]
    with pytest.raises(ExternalError):
        asyncio.run(run(invalid=True, replace=True))
    assert current_draft(project)["run_id"] == old


@pytest.mark.parametrize("invalid_content", ["{", json.dumps({"groups": [], "keep": ["1"], "uncertain": []})])
def test_comparison_format_retry_recovers_and_links_requests(tmp_path, monkeypatch, invalid_content):
    from app.repetition_consistency import current_draft, run_repetition
    from app.llm_client import LLMClient
    project = repetition_project(tmp_path, monkeypatch)
    requests, links = [], []
    valid_transport = transport(requests=requests)
    original_chat = LLMClient.chat
    async def chat(self, **kwargs):
        response, request_id = await original_chat(self, **kwargs)
        links.append((request_id, kwargs.get("parent_request_id")))
        return response, request_id
    monkeypatch.setattr(LLMClient, "chat", chat)
    def respond(request):
        response = valid_transport.handle_request(request)
        if len(requests) == 2:
            return httpx.Response(200, json={"choices": [{"message": {"content": invalid_content}, "finish_reason": "stop"}]})
        return response
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await run_repetition(project, Scope(), http_client=client, screening_enabled=True)
    result = asyncio.run(run())
    assert result["completed"] == 3
    assert len(requests) == 3  # One screening, then two comparison attempts.
    assert requests[1]["messages"] == requests[2]["messages"]
    assert links[0][1] is None and links[1][1] == links[0][0]
    assert len(current_draft(project)["groups"][0]["proposals"]) == 1


@pytest.mark.parametrize("limit", [0, 1])
def test_comparison_format_retry_exhaustion_preserves_screening(tmp_path, monkeypatch, limit):
    from app.repetition_consistency import repetition_plan, repetition_resume, run_repetition
    project = repetition_project(tmp_path, monkeypatch)
    plan = repetition_plan(project, Scope(), screening_enabled=True)
    plan["config"]["retry"]["format_max_attempts"] = limit
    requests = []
    async def run():
        async with httpx.AsyncClient(transport=transport(invalid=True, requests=requests)) as client:
            await run_repetition(project, Scope(), prepared_plan=plan, http_client=client, screening_enabled=True)
    with pytest.raises(ExternalError, match="响应无效"):
        asyncio.run(run())
    assert len(requests) == limit + 2
    assert repetition_resume(project, plan)["completed_steps"] == 1


def test_missing_translation_fails_preflight(tmp_path, monkeypatch):
    from app.repetition_consistency import repetition_plan
    project = repetition_project(tmp_path, monkeypatch)
    segment = load_segments(project)[0]
    project_id = read_json(project, project / "project.json")["project_id"]
    append_stage_results(project, [record_header("stage_result", project_id, stage="translation",
        segment_id=segment["segment_id"], status="reset")])
    with pytest.raises(UsageError, match="完整翻译"):
        repetition_plan(project, Scope(), screening_enabled=True)


def test_over_budget_group_is_visible_without_sending_requests(tmp_path, monkeypatch):
    from app.repetition_consistency import current_draft, run_repetition
    project = repetition_project(tmp_path, monkeypatch)
    path = config.APP_ROOT / "decision_presets" / "local.json"
    value = json.loads(path.read_text())
    value.update(context_window_tokens=2, context_safety_margin_tokens=1)
    path.write_text(json.dumps(value))
    def unexpected(request):
        pytest.fail("over-budget group must not send a request")
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
            await run_repetition(project, Scope(), http_client=client, screening_enabled=True)
    asyncio.run(run())
    group = current_draft(project)["groups"][0]
    assert group["screening"] == "over_budget"
    assert group["uncertain"] == ["1", "2", "3"]


def generate(project):
    from app.repetition_consistency import run_repetition
    async def run():
        async with httpx.AsyncClient(transport=transport()) as client:
            await run_repetition(project, Scope(), http_client=client, screening_enabled=True)
    asyncio.run(run())


def test_apply_undo_survives_history_maintenance(tmp_path, monkeypatch):
    from app.repetition_drafts import apply_draft, rollback_draft
    from app.sqlite_storage import maintain_project_database
    project = repetition_project(tmp_path, monkeypatch)
    generate(project)
    sid = load_segments(project)[3]["segment_id"]
    result = asyncio.run(apply_draft(project, confirm=True))
    assert result["changed"] == 1
    assert latest_stage_states(project, "translation", [sid])[sid]["completed"]["text"] == "我会来接你"
    maintain_project_database(project)
    rollback_draft(project, confirm=True)
    restored = latest_stage_states(project, "translation", [sid])[sid]["completed"]
    assert restored["text"] == "我会去接你"
    assert restored["restored_origin"] == "web"
    with pytest.raises(UsageError, match="撤销"):
        rollback_draft(project, confirm=True)


def test_later_affected_edit_blocks_undo_but_unrelated_edit_does_not(tmp_path, monkeypatch):
    from app.repetition_drafts import apply_draft, rollback_draft
    from app.web_store import WebStore
    project = repetition_project(tmp_path, monkeypatch)
    generate(project)
    asyncio.run(apply_draft(project, confirm=True))
    store = WebStore(project)
    store.save_translation({"segment_id": load_segments(project)[0]["segment_id"], "text": "修改无关译文"})
    assert rollback_draft(project, confirm=True)["changed"] == 1
    generate(project)
    asyncio.run(apply_draft(project, confirm=True))
    sid = load_segments(project)[3]["segment_id"]
    store.save_translation({"segment_id": sid, "text": "用户后来修改"})
    with pytest.raises(UsageError, match="变化"):
        rollback_draft(project, confirm=True)
    assert latest_stage_states(project, "translation", [sid])[sid]["completed"]["text"] == "用户后来修改"


@pytest.mark.parametrize("change", ["base", "context"])
def test_changed_group_base_or_context_blocks_entire_apply(tmp_path, monkeypatch, change):
    from app.repetition_drafts import apply_draft
    from app.web_store import WebStore
    import sqlite3
    project = repetition_project(tmp_path, monkeypatch)
    generate(project)
    sid = load_segments(project)[3]["segment_id"]
    before = latest_stage_states(project, "translation", [sid])[sid]["completed"]["record_id"]
    if change == "base":
        WebStore(project).save_translation({"segment_id": load_segments(project)[1]["segment_id"], "text": "新基准"})
    else:
        with sqlite3.connect(project / "project.sqlite") as connection:
            connection.execute("UPDATE segments SET source = ? WHERE segment_id = ?",
                               ("改变判断所用上下文", load_segments(project)[0]["segment_id"]))
    with pytest.raises(UsageError, match="过期"):
        asyncio.run(apply_draft(project, confirm=True))
    assert latest_stage_states(project, "translation", [sid])[sid]["completed"]["record_id"] == before


def test_application_and_draft_state_roll_back_together_on_storage_failure(tmp_path, monkeypatch):
    from app.repetition_drafts import apply_draft
    from app.repetition_consistency import current_draft
    import sqlite3
    project = repetition_project(tmp_path, monkeypatch)
    generate(project)
    sid = load_segments(project)[3]["segment_id"]
    with sqlite3.connect(project / "project.sqlite") as connection:
        connection.execute("CREATE TRIGGER fail_consistency_update BEFORE UPDATE ON runs "
                           "BEGIN SELECT RAISE(ABORT, 'transaction probe'); END")
    from app.errors import StorageError
    with pytest.raises(StorageError):
        asyncio.run(apply_draft(project, confirm=True))
    assert current_draft(project) is not None
    assert latest_stage_states(project, "translation", [sid])[sid]["completed"]["text"] == "我会去接你"


def test_saved_rejection_and_alternative_target(tmp_path, monkeypatch):
    from app.repetition_drafts import apply_draft, save_choices
    project = repetition_project(tmp_path, monkeypatch)
    generate(project)
    save_choices(project, {"1-1": {"accepted": False, "target": "2"}})
    assert asyncio.run(apply_draft(project, confirm=True))["changed"] == 0
    generate(project)
    save_choices(project, {"1-1": {"accepted": True, "target": "2"}})
    assert asyncio.run(apply_draft(project, confirm=True))["changed"] == 1
    sid = load_segments(project)[1]["segment_id"]
    assert latest_stage_states(project, "translation", [sid])[sid]["completed"]["text"] == "我会去接你"


def test_web_draft_flow_and_stale_review_identity(tmp_path, monkeypatch):
    from app.web import create_app
    from fastapi.testclient import TestClient
    project = repetition_project(tmp_path, monkeypatch)
    generate(project)
    with TestClient(create_app(projects_root=project.parent)) as client:
        base = "/api/v1/projects/repetition/repetition"
        options = client.get(base + "/options").json()
        assert options["candidate_groups"] == 1
        state = client.get(base).json()
        run_id = state["draft"]["run_id"]
        assert client.post(base + "/apply", json={"run_id": "old", "confirm": True, "choices": {}}).status_code == 400
        assert client.put(base + "/choices", json={"run_id": run_id, "choices": {
            "1-1": {"accepted": True, "target": "2"}}}).status_code == 200
        assert client.post(base + "/apply", json={"run_id": run_id, "confirm": True, "choices": {}}).status_code == 400
        assert client.post(base + "/apply", json={"run_id": run_id, "confirm": True,
            "choices": {"1-1": {"accepted": True, "target": "2"}}}).json()["changed"] == 1
        assert client.get(base).json()["can_rollback"]
        assert client.post(base + "/rollback", json={"run_id": run_id, "confirm": True}).json()["changed"] == 1


def test_repetition_tasks_use_existing_manager_and_cancel(tmp_path, monkeypatch):
    from app.web_tasks import WebTaskManager
    from app import repetition_consistency
    project = repetition_project(tmp_path, monkeypatch)
    started = asyncio.Event()
    async def held_run(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(repetition_consistency, "run_repetition", held_run)
    async def run():
        manager = WebTaskManager()
        state = await manager.start(project, "repetition_consistency", scope=Scope(),
                                    reuse_mixed_fingerprints=False, run_action=None)
        await asyncio.wait_for(started.wait(), 2)
        assert manager.is_project_running(project)
        await manager.cancel(state["task_id"])
        await manager.shutdown()
        assert manager.get(state["task_id"])["status"] == "cancelled"
    asyncio.run(run())


@pytest.mark.parametrize("exclude_punctuation_only", [True, False])
@pytest.mark.parametrize("screening_enabled", [True, False])
def test_managed_generation_publishes_draft_and_does_not_retain_queued_evidence(tmp_path, monkeypatch, exclude_punctuation_only, screening_enabled):
    from app.web_tasks import WebTaskManager
    from app.repetition_consistency import current_draft
    project = repetition_project(tmp_path, monkeypatch)
    original_client = httpx.AsyncClient
    def client(*args, **kwargs):
        return original_client(*args, **kwargs, transport=transport())
    monkeypatch.setattr(httpx, "AsyncClient", client)
    async def run():
        manager = WebTaskManager()
        state = await manager.start(project, "repetition_consistency", scope=Scope(),
                                    reuse_mixed_fingerprints=False, run_action=None, prompt_language="en", exclude_punctuation_only=exclude_punctuation_only, screening_enabled=screening_enabled)
        retained = manager.tasks[state["task_id"]]
        assert retained._start_decision.plan is None
        await retained.asyncio_task
        assert manager.get(state["task_id"])["status"] == "completed"
        draft = current_draft(project)
        assert draft["groups"][0]["proposals"]
        manifest = read_json(project, project / "runs" / draft["run_id"] / "manifest.json")
        assert manifest["prompt_language"] == "en"
        assert manifest["exclude_punctuation_only"] is exclude_punctuation_only
        assert manifest["screening_enabled"] is screening_enabled
        await manager.shutdown()
    asyncio.run(run())


def character_library(project, *, status="current", detail="角色设定", aliases=()):
    from app.term_knowledge import source_range
    segments = load_segments(project)
    refs = source_range([segments[5]], {s["segment_id"]: s for s in segments}, {})
    return {"terms_revision": 7, "terms": [{"source": "前文", "normalized": "前文",
        "aliases": list(aliases), "preferred_translation": "前文译文", "entity_key": "前文",
        "knowledge_status": status, "description_items": [{"text": detail, "sources": [refs]}]}]}


@pytest.mark.parametrize("aliases", [[], ["相同"]])
def test_comparison_injects_context_terms_with_local_relations_and_shared_snapshot(tmp_path, monkeypatch, aliases):
    from app import repetition_consistency as repetition
    project = repetition_project(tmp_path, monkeypatch)
    library = character_library(project, aliases=aliases)
    monkeypatch.setattr(repetition, "load_terms", lambda _: library)
    requests = []
    async def run():
        async with httpx.AsyncClient(transport=transport(requests=requests)) as client:
            return await repetition.run_repetition(project, Scope(), http_client=client, screening_enabled=True)
    result = asyncio.run(run())
    payload = json.loads(requests[1]["messages"][-1]["content"])
    first, second = payload["repetitions"][0]["occurrences"][:2]
    first_term = payload["terms"][first["terms"][0]["id"]]
    second_term = payload["terms"][second["terms"][0]["id"]]
    assert first_term["preferred_translation"] == "前文译文"
    assert first["terms"][0]["source_relations"][0] == ["after 1"]
    assert second["terms"][0]["source_relations"][0] == ["within"]
    assert first["terms"][0]["aliases"] == []
    assert second["terms"][0]["aliases"] == aliases
    assert first_term == second_term
    assert len(payload["terms"]) == 1
    snapshot = json.loads((project / "runs" / result["run_id"] / "repetition_terms.json").read_text())
    assert snapshot["terms_revision"] == 7
    assert len(snapshot["terms"]) == 1
    draft = repetition.current_draft(project)
    assert all("terms" not in o for g in draft["groups"] for o in g["occurrences"])


@pytest.mark.parametrize("status", ["pending", "stale"])
def test_comparison_obeys_character_knowledge_state(tmp_path, monkeypatch, status):
    from app import repetition_consistency as repetition
    project = repetition_project(tmp_path, monkeypatch)
    monkeypatch.setattr(repetition, "load_terms", lambda _: character_library(project, status=status))
    if status == "stale":
        with pytest.raises(UsageError, match="过期"):
            repetition.repetition_plan(project, Scope(), screening_enabled=True)
    else:
        async def run():
            async with httpx.AsyncClient(transport=transport()) as client:
                return await repetition.run_repetition(project, Scope(), http_client=client, screening_enabled=True)
        assert any("待聚合" in warning for warning in asyncio.run(run())["warnings"])


def test_term_injection_is_counted_in_preflight_budget(tmp_path, monkeypatch):
    from app import repetition_consistency as repetition
    project = repetition_project(tmp_path, monkeypatch)
    before = repetition.repetition_plan(project, Scope(), screening_enabled=True)
    monkeypatch.setattr(repetition, "load_terms", lambda _: character_library(project, detail="角色信息" * 20000))
    after = repetition.repetition_plan(project, Scope(), screening_enabled=True)
    assert not before["groups"][0]["over_budget"]
    assert after["groups"][0]["over_budget"]
    assert after["fingerprint"] != before["fingerprint"]


def test_repetition_prompts_are_editable_and_frozen_in_actual_requests(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.repetition_consistency import repetition_plan, run_repetition
    from app.web import create_app
    project = repetition_project(tmp_path, monkeypatch)
    app = create_app(projects_root=project.parent, app_root=config.APP_ROOT)
    with TestClient(app) as client:
        base = f"/api/v1/projects/{project.name}"
        prompts = client.get("/api/v1/decision-prompts").json()["prompts"]
        assert any(p["validator_id"] == "repetition_screening" for p in prompts)
        decision = client.get(f"{base}/decision-prompts/repetition_screening").json()
        assert decision["content"]["instructions"].startswith("Filter out")
        custom = {**decision["content"], "instructions": "这部作品中的承诺值得比较"}
        assert client.put(f"{base}/decision-prompts/repetition_screening", json={"content": custom}).status_code == 200
        assert client.put("/api/v1/decision-prompt-library/repetition_screening/story", json={"content": custom}).status_code == 200
        assert client.get("/api/v1/decision-prompt-library/repetition_screening/story").json()["content"] == custom
        initial = repetition_plan(project, Scope(), prompt_language="en", screening_enabled=True)["fingerprint"]
        path = f"{base}/prompts/repetition_consistency"
        assert client.put(path, json={"language": "en", "content": "Prefer recurring promises."}).status_code == 200
        preview = client.get(path, params={"language": "en"}).json()["assembled"]
    plan = repetition_plan(project, Scope(), prompt_language="en", screening_enabled=True)
    assert plan["fingerprint"] != initial
    requests = []
    async def run():
        async with httpx.AsyncClient(transport=transport(requests=requests)) as client:
            return await run_repetition(project, Scope(), prompt_language="en", http_client=client, screening_enabled=True)
    result = asyncio.run(run())
    assert requests[0]["questions"]["value"]["instructions"] == custom["instructions"]
    assert requests[1]["messages"][0]["content"] == preview
    run_dir = project / "runs" / result["run_id"]
    assert (run_dir / "prompt.txt").read_text() == preview
    assert json.loads((run_dir / "decision_prompts" / "repetition_screening.json").read_text())["content"]["instructions"] == custom["instructions"]


def test_repetition_prompt_missing_language_fails_without_fallback(tmp_path, monkeypatch):
    from app.repetition_consistency import repetition_plan
    project = repetition_project(tmp_path, monkeypatch)
    (project / "prompts" / "repetition_consistency.en.middle.txt").unlink()
    with pytest.raises(UsageError, match="Prompt"):
        repetition_plan(project, Scope(), prompt_language="en", screening_enabled=True)


def test_updated_terminology_does_not_invalidate_existing_repetition_draft(tmp_path, monkeypatch):
    from app import repetition_consistency as repetition, repetition_drafts
    project = repetition_project(tmp_path, monkeypatch)
    library = character_library(project)
    monkeypatch.setattr(repetition, "load_terms", lambda _: library)
    generate(project)
    updated = {**library, "terms_revision": 8}
    monkeypatch.setattr(repetition_drafts, "load_terms", lambda _: updated)
    result = asyncio.run(repetition_drafts.apply_draft(project, confirm=True))
    assert result["changed"] == 1
    sid = load_segments(project)[3]["segment_id"]
    assert latest_stage_states(project, "translation", [sid])[sid]["completed"]["terms_revision"] == 8


def test_apply_epub_uses_persisted_adapter_state_and_run_options(tmp_path, monkeypatch):
    from app import plugins
    from app.repetition_drafts import apply_draft, rollback_draft
    from tests.test_documents import make_epub
    repetition_project(tmp_path, monkeypatch)
    source = tmp_path / "repeated.epub"
    make_epub(source, xhtml=('<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Story</title></head>'
        '<body><p>承诺</p><p>承诺</p><p>承诺</p></body></html>').encode())
    run_options = {"ruby_mode": "short_xml", "inline_format_mode": "markers", "inline_format_policy": "strict"}
    project, _ = init_project([str(source)], name="epub-repetition", document_adapter_id="epub",
        adapter_options={"epub": run_options}, app_root=tmp_path / "app-root", projects_root=tmp_path / "projects")
    path = project / "config.toml"
    path.write_text(path.read_text().replace('[decision]\npreset = ""', '[decision]\npreset = "local"'))
    segments = load_segments(project)
    project_id = read_json(project, project / "project.json")["project_id"]
    texts = ["我会来接你", "我会去接你", "我会去接你"]
    append_stage_results(project, [record_header("stage_result", project_id, stage="translation",
        segment_id=s["segment_id"], status="completed", text=text, origin="web", stage_fingerprint="test")
        for s, text in zip(segments, texts, strict=True)])
    generate(project)
    observed = []
    original = plugins.normalize_document_output
    def capture(adapter, **kwargs):
        observed.append((kwargs["opaque_state"], kwargs["run_options"]))
        return original(adapter, **kwargs)
    monkeypatch.setattr(plugins, "normalize_document_output", capture)
    assert asyncio.run(apply_draft(project, confirm=True))["changed"] == 1
    assert len(observed) == 1 and observed[0][0] is not None
    assert observed[0][1] == run_options
    sid = segments[1]["segment_id"]
    assert latest_stage_states(project, "translation", [sid])[sid]["completed"]["text"] == texts[0]
    assert rollback_draft(project, confirm=True)["changed"] == 1
    assert latest_stage_states(project, "translation", [sid])[sid]["completed"]["text"] == texts[1]


@pytest.mark.parametrize("variants,excluded", [
    (["「一样。」", "一样", "一样！"], True),
    (["一样？", "一样…", "一样，"], True),
    (["一样", "一样 ", "一样"], False),
    (["一样", "一样。", "不同"], False),
    (["1+2", "12", "12"], False),
])
def test_optional_punctuation_filter_only_removes_whole_equal_body_groups(tmp_path, monkeypatch, variants, excluded):
    from app.repetition_consistency import repetition_plan
    project = repetition_project(tmp_path, monkeypatch)
    segments = [s for s in load_segments(project) if s["source"] == "承诺"]
    project_id = read_json(project, project / "project.json")["project_id"]
    append_stage_results(project, [record_header("stage_result", project_id, stage="translation",
        segment_id=s["segment_id"], status="completed", text=t, stage_fingerprint="test")
        for s, t in zip(segments, variants, strict=True)])
    filtered = repetition_plan(project, Scope(), screening_enabled=True)
    unfiltered = repetition_plan(project, Scope(), exclude_punctuation_only=False, screening_enabled=True)
    assert filtered["excluded_punctuation_groups"] == int(excluded)
    assert len(filtered["groups"]) == int(not excluded)
    assert len(unfiltered["groups"]) == 1 and len(unfiltered["groups"][0]["occurrences"]) == 3
    assert filtered["fingerprint"] != unfiltered["fingerprint"]
    if excluded:
        assert filtered["evidence"] == {} and filtered["terms"] == {}
        assert filtered["estimated_requests"] == 0
    assert [latest_stage_states(project, "translation", [s["segment_id"]])[s["segment_id"]]["completed"]["text"] for s in segments] == variants


def test_partial_application_and_manual_skipped_group(tmp_path, monkeypatch):
    from app.repetition_consistency import current_draft, run_repetition
    from app.repetition_drafts import apply_draft, save_choices, rollback_draft
    project = repetition_project(tmp_path, monkeypatch)
    generate(project)
    save_choices(project, {"1-1": {"accepted": True, "target": "1", "apply": []}})
    assert asyncio.run(apply_draft(project, confirm=True))["changed"] == 0
    async def skipped():
        async with httpx.AsyncClient(transport=transport(choice="skip")) as client:
            await run_repetition(project, Scope(), http_client=client, screening_enabled=True)
    asyncio.run(skipped())
    draft = current_draft(project)
    proposal = draft["groups"][0]["proposals"][0]
    assert proposal["manual"] and not proposal["default_accepted"]
    save_choices(project, {proposal["id"]: {"accepted": True, "target": "1", "apply": ["3"]}})
    assert asyncio.run(apply_draft(project, confirm=True))["changed"] == 1
    segments = load_segments(project)
    states = latest_stage_states(project, "translation", [s["segment_id"] for s in segments])
    assert states[segments[3]["segment_id"]]["completed"]["text"] == "我会去接你"
    assert states[segments[5]["segment_id"]]["completed"]["text"] == "我会来接你"
    assert rollback_draft(project, confirm=True)["changed"] == 1


def test_old_draft_remains_applicable_without_member_choices(tmp_path, monkeypatch):
    from app.repetition_consistency import current_draft, draft_path
    from app.repetition_drafts import apply_draft
    project = repetition_project(tmp_path, monkeypatch)
    generate(project)
    draft = current_draft(project)
    draft["groups"][0]["proposals"][0].pop("apply")
    draft_path(project, draft["run_id"]).write_text(json.dumps(draft))
    assert asyncio.run(apply_draft(project, confirm=True))["changed"] == 1


@pytest.mark.parametrize("members", [["unknown"], ["2", "2"]])
def test_invalid_partial_application_fails(tmp_path, monkeypatch, members):
    from app.repetition_drafts import save_choices
    project = repetition_project(tmp_path, monkeypatch)
    generate(project)
    with pytest.raises(UsageError, match="应用成员"):
        save_choices(project, {"1-1": {"accepted": True, "target": "1", "apply": members}})


def test_disabled_screening_needs_no_decision_resources_and_preserves_model_subset(tmp_path, monkeypatch):
    from app.repetition_consistency import repetition_plan, run_repetition, current_draft
    from app.repetition_drafts import apply_draft, save_choices
    project = repetition_project(tmp_path, monkeypatch)
    path = project / "config.toml"
    path.write_text(path.read_text().replace('[decision]\npreset = "local"', '[decision]\npreset = ""'))
    prompt = project / "decision_prompts" / "repetition_screening.json"
    prompt.parent.mkdir(exist_ok=True)
    prompt.write_text("invalid")
    plan = repetition_plan(project, Scope())
    assert plan["screen"] is None and plan["decision_preset"] is None
    assert plan["estimated_requests"] == 1
    requests = []
    async def run():
        async with httpx.AsyncClient(transport=transport(requests=requests, apply_members=[])) as client:
            return await run_repetition(project, Scope(), http_client=client)
    result = asyncio.run(run())
    assert len(requests) == 1 and "messages" in requests[0]
    assert not (project / "runs" / result["run_id"] / "decision_preset.json").exists()
    draft = current_draft(project)
    assert draft["groups"][0]["screening"] == "disabled"
    assert draft["groups"][0]["proposals"][0]["apply"] == []
    save_choices(project, {"1-1": {"accepted": True, "target": "1", "apply": ["2"]}})
    assert asyncio.run(apply_draft(project, confirm=True))["changed"] == 1


def test_resume_reuses_screening_and_keeps_original_snapshots(tmp_path, monkeypatch):
    from app.repetition_consistency import run_repetition, repetition_plan, repetition_resume, current_draft
    project = repetition_project(tmp_path, monkeypatch)
    async def run(invalid, action=None, requests=None):
        async with httpx.AsyncClient(transport=transport(invalid=invalid, requests=requests)) as client:
            return await run_repetition(project, Scope(), http_client=client, run_action=action, screening_enabled=True)
    with pytest.raises(ExternalError):
        asyncio.run(run(True))
    info = repetition_resume(project, repetition_plan(project, Scope(), screening_enabled=True))
    assert info["resume_compatible"] and info["completed_steps"] == 1
    run_dir = project / "runs" / info["run_id"]
    original_prompt = (run_dir / "prompt.txt").read_bytes()
    requests = []
    result = asyncio.run(run(False, "resume", requests))
    assert result["run_id"] == info["run_id"]
    assert len(requests) == 1 and "messages" in requests[0]
    assert (run_dir / "prompt.txt").read_bytes() == original_prompt
    assert len(read_json(project, run_dir / "manifest.json")["continuations"]) == 1
    assert current_draft(project)["run_id"] == info["run_id"]


def test_resume_rejects_changed_inputs_and_decline_starts_new_run(tmp_path, monkeypatch):
    from app.repetition_consistency import run_repetition, repetition_plan, repetition_resume
    project = repetition_project(tmp_path, monkeypatch)
    async def run(invalid, action=None):
        async with httpx.AsyncClient(transport=transport(invalid=invalid)) as client:
            return await run_repetition(project, Scope(), http_client=client, run_action=action, screening_enabled=True)
    with pytest.raises(ExternalError):
        asyncio.run(run(True))
    info = repetition_resume(project, repetition_plan(project, Scope(), screening_enabled=True))
    sid = load_segments(project)[1]["segment_id"]
    project_id = read_json(project, project / "project.json")["project_id"]
    append_stage_results(project, [record_header("stage_result", project_id, stage="translation", segment_id=sid,
        status="completed", text="改变基准", origin="web", stage_fingerprint="changed")])
    assert not repetition_resume(project, repetition_plan(project, Scope(), screening_enabled=True))["resume_compatible"]
    with pytest.raises(UsageError, match="已变化"):
        asyncio.run(run(False, "resume"))
    result = asyncio.run(run(False, "decline"))
    assert result["run_id"] != info["run_id"]
    old = read_json(project, project / "runs" / info["run_id"] / "manifest.json")
    assert old["status"] == "interrupted"


@pytest.mark.parametrize("shrink_budget", [False, True])
def test_completed_checkpoint_resume_needs_no_requests_and_keeps_usage(tmp_path, monkeypatch, shrink_budget):
    from app import repetition_consistency as repetition
    from app.errors import StorageError
    project = repetition_project(tmp_path, monkeypatch)
    original_write = repetition.atomic_write_text
    def fail_publish(path, text):
        if path.name == repetition.DRAFT_FILE:
            raise StorageError("publication probe")
        return original_write(path, text)
    monkeypatch.setattr(repetition, "atomic_write_text", fail_publish)
    async def run(action=None, requests=None):
        async with httpx.AsyncClient(transport=transport(requests=requests)) as client:
            return await repetition.run_repetition(project, Scope(), run_action=action, http_client=client, screening_enabled=True)
    with pytest.raises(StorageError, match="publication probe"):
        asyncio.run(run())
    if shrink_budget:
        from tests.helpers import use_llm_preset
        use_llm_preset(tmp_path, context_window_tokens=100, context_safety_margin_tokens=10)
    info = repetition.repetition_resume(project, repetition.repetition_plan(project, Scope(), screening_enabled=True))
    assert info["completed"] == 3 and info["estimated_requests"] == 0
    manifest_path = project / "runs" / info["run_id"] / "manifest.json"
    previous = read_json(project, manifest_path)
    previous_usage = previous["usage"]
    monkeypatch.setattr(repetition, "atomic_write_text", original_write)
    requests = []
    result = asyncio.run(run("resume", requests))
    assert requests == []
    assert result["usage"] == previous_usage
    current = read_json(project, manifest_path)
    assert current["usage_invocation_count"] == previous["usage_invocation_count"]
    assert current.get("key_audits") == previous.get("key_audits")


def test_web_resume_options_and_managed_start(tmp_path, monkeypatch):
    from app.web import create_app
    from app.web_tasks import WebTaskManager
    from app.repetition_consistency import run_repetition, current_draft
    from fastapi.testclient import TestClient
    project = repetition_project(tmp_path, monkeypatch)
    async def fail():
        async with httpx.AsyncClient(transport=transport(invalid=True)) as client:
            await run_repetition(project, Scope(), http_client=client, screening_enabled=False, prompt_language="en")
    with pytest.raises(ExternalError):
        asyncio.run(fail())
    with TestClient(create_app(projects_root=project.parent)) as client:
        options = client.get(f"/api/v1/projects/{project.name}/repetition/options?language=en").json()
        assert options["run_action"] == "resume" and options["screening_enabled"] is False
        assert options["running_run"]["resume_compatible"] and options["estimated_requests"] == 1
    original_client = httpx.AsyncClient
    requests = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: original_client(*args, **kwargs, transport=transport(requests=requests)))
    async def resume():
        manager = WebTaskManager()
        with pytest.raises(UsageError, match="必须选择"):
            await manager.start(project, "repetition_consistency", scope=Scope(), reuse_mixed_fingerprints=False,
                                run_action=None, prompt_language="en", screening_enabled=False)
        state = await manager.start(project, "repetition_consistency", scope=Scope(), reuse_mixed_fingerprints=False,
                                    run_action="resume", prompt_language="en", screening_enabled=False)
        await manager.tasks[state["task_id"]].asyncio_task
        assert manager.get(state["task_id"])["status"] == "completed"
        await manager.shutdown()
    asyncio.run(resume())
    assert len(requests) == 1
    assert current_draft(project)["run_id"] == options["running_run"]["run_id"]


def test_cancelled_comparison_resumes_after_screening(tmp_path, monkeypatch):
    from app.repetition_consistency import run_repetition, repetition_plan, repetition_resume
    project = repetition_project(tmp_path, monkeypatch)
    async def interrupted():
        comparing = asyncio.Event()
        respond = transport().handler
        async def handle(request):
            if request.url.path != "/custom/path":
                comparing.set()
                await asyncio.Future()
            return respond(request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            task = asyncio.create_task(run_repetition(project, Scope(), http_client=client, screening_enabled=True))
            await comparing.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    asyncio.run(interrupted())
    info = repetition_resume(project, repetition_plan(project, Scope(), screening_enabled=True))
    assert info["completed_steps"] == 1
    manifest = read_json(project, project / "runs" / info["run_id"] / "manifest.json")
    assert manifest["completed_at"] is None and manifest["last_interruption"]["reason"] == "cancelled"
    requests = []
    async def resume():
        async with httpx.AsyncClient(transport=transport(requests=requests)) as client:
            await run_repetition(project, Scope(), run_action="resume", http_client=client, screening_enabled=True)
    asyncio.run(resume())
    assert len(requests) == 1 and "messages" in requests[0]


def multiple_groups_project(tmp_path, monkeypatch):
    return repetition_project(tmp_path, monkeypatch, source_text="前文\n承诺\n独白\n承诺\n独白\n承诺\n独白",
        texts=["前文译文", "我会来接你", "独白甲", "我会去接你", "独白乙", "我会去接你", "独白乙"])


def test_multiple_groups_share_request_evidence_and_exact_budget(tmp_path, monkeypatch):
    from app import repetition_consistency as repetition
    from app.execution import estimate_messages
    project = multiple_groups_project(tmp_path, monkeypatch)
    monkeypatch.setattr(repetition, "load_terms", lambda _: character_library(project))
    plan = repetition.repetition_plan(project, Scope())
    requests = []
    async def run():
        async with httpx.AsyncClient(transport=transport(requests=requests)) as client:
            return await repetition.run_repetition(project, Scope(), prepared_plan=plan, http_client=client)
    result = asyncio.run(run())
    assert result["completed"] == 6
    assert len(requests) == plan["estimated_requests"] == 1
    payload = json.loads(requests[0]["messages"][-1]["content"])
    assert [g["id"] for g in payload["repetitions"]] == ["1", "2"]
    assert len(payload["terms"]) == 1 and len(payload["context"]) == len(set(payload["context"].values()))
    assert payload["files"] == {"1": "source.txt"}
    assert plan["estimated_input_tokens"] == estimate_messages(requests[0]["messages"], plan["config"]["execution"]["token_safety_factor"])
    draft = repetition.current_draft(project)
    assert draft["groups"][0]["proposals"][0]["id"] == "1-1"
    assert draft["groups"][1]["proposals"][0]["id"] == "2-1"


@pytest.mark.parametrize("failure", ["missing", "duplicate", "members"])
def test_batch_retry_preserves_valid_group_and_sends_only_invalid_group(tmp_path, monkeypatch, failure):
    from app.repetition_consistency import checkpoint_path, current_draft, run_repetition
    project = multiple_groups_project(tmp_path, monkeypatch)
    requests = []
    responder = transport(requests=requests)
    def respond(request):
        response = responder.handle_request(request)
        body = response.json()
        if len(requests) == 1:
            output = json.loads(body["choices"][0]["message"]["content"])
            if failure == "missing":
                output["repetitions"].pop()
            elif failure == "duplicate":
                output["repetitions"].append(output["repetitions"][1])
            else:
                output["repetitions"][1]["keep"] = ["unknown"]
            body["choices"][0]["message"]["content"] = json.dumps(output)
            return httpx.Response(200, json=body)
        run_dir = next((project / "runs").iterdir())
        saved = json.loads(checkpoint_path(run_dir, "1").read_text())
        assert "result" in saved and not checkpoint_path(run_dir, "2").exists()
        assert [g["id"] for g in json.loads(requests[-1]["messages"][-1]["content"])["repetitions"]] == ["2"]
        return response
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await run_repetition(project, Scope(), http_client=client)
    assert asyncio.run(run())["completed"] == 6
    assert len(requests) == 2 and len(current_draft(project)["groups"]) == 2


def test_batch_exhaustion_resumes_only_incomplete_group(tmp_path, monkeypatch):
    from app.repetition_consistency import repetition_plan, repetition_resume, run_repetition
    project = multiple_groups_project(tmp_path, monkeypatch)
    requests = []
    responder = transport(requests=requests)
    def fail_second(request):
        body = responder.handle_request(request).json()
        output = json.loads(body["choices"][0]["message"]["content"])
        for group in output["repetitions"]:
            if group["id"] == "2":
                group["keep"] = ["unknown"]
        body["choices"][0]["message"]["content"] = json.dumps(output)
        return httpx.Response(200, json=body)
    async def run(handler, action=None):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await run_repetition(project, Scope(), run_action=action, http_client=client)
    with pytest.raises(ExternalError):
        asyncio.run(run(fail_second))
    info = repetition_resume(project, repetition_plan(project, Scope()))
    assert info["completed"] == 3 and info["completed_steps"] == 1 and info["estimated_requests"] == 1
    requests.clear()
    result = asyncio.run(run(responder.handler, "resume"))
    assert result["run_id"] == info["run_id"] and result["completed"] == 6
    assert len(requests) == 1
    assert [g["id"] for g in json.loads(requests[0]["messages"][-1]["content"])["repetitions"]] == ["2"]


def test_batch_budget_keeps_groups_whole_and_reuses_configured_target(tmp_path, monkeypatch):
    from app import repetition_consistency as repetition
    project = multiple_groups_project(tmp_path, monkeypatch)
    initial = repetition.repetition_plan(project, Scope())
    config_value = initial["config"]
    config_value["execution"]["target_chunk_input_tokens"] = max(g["comparison_input_tokens"] for g in initial["groups"]) - 1
    monkeypatch.setattr(repetition, "load_project_config", lambda *args, **kwargs: config_value)
    plan = repetition.repetition_plan(project, Scope())
    assert plan["estimated_requests"] == 2 and plan["fingerprint"] != initial["fingerprint"]
    requests = []
    async def run():
        async with httpx.AsyncClient(transport=transport(requests=requests)) as client:
            return await repetition.run_repetition(project, Scope(), prepared_plan=plan, http_client=client)
    assert asyncio.run(run())["completed"] == 6
    assert len(requests) == 2
    for request in requests:
        groups = json.loads(request["messages"][-1]["content"])["repetitions"]
        assert len(groups) == 1 and len(groups[0]["occurrences"]) == 3


@pytest.mark.parametrize("change", ["model", "prompt", "adapter", "budget", "retry", "decision"])
def test_resume_changed_execution_preserves_phase_sources(tmp_path, monkeypatch, change):
    from app.repetition_consistency import checkpoint_path, repetition_plan, repetition_resume, run_repetition
    from tests.helpers import use_llm_preset
    project = repetition_project(tmp_path, monkeypatch)
    async def run(invalid=False, action=None, requests=None):
        async with httpx.AsyncClient(transport=transport(invalid=invalid, requests=requests)) as client:
            return await run_repetition(project, Scope(), screening_enabled=True, run_action=action, http_client=client)
    with pytest.raises(ExternalError):
        asyncio.run(run(invalid=True))
    old_plan = repetition_plan(project, Scope(), screening_enabled=True)
    info = repetition_resume(project, old_plan)
    run_dir = project / "runs" / info["run_id"]
    original_prompt = (run_dir / "prompt.txt").read_bytes()
    if change == "model":
        use_llm_preset(tmp_path, model="another-model")
    elif change == "prompt":
        path = project / "prompts" / "repetition_consistency.zh-CN.middle.txt"
        path.write_text(path.read_text() + "\n新的审查策略")
    elif change == "adapter":
        path = config.APP_ROOT / "llm_adapters" / "openai-compatible.json"
        value = json.loads(path.read_text())
        value["endpoint"] = "/alternate/chat"
        path.write_text(json.dumps(value))
    elif change == "budget":
        use_llm_preset(tmp_path, target_chunk_input_tokens=3000)
    elif change == "retry":
        path = project / "config.toml"
        path.write_text(path.read_text().replace("format_max_attempts = 2", "format_max_attempts = 1"))
    else:
        path = config.APP_ROOT / "decision_presets" / "local.json"
        value = json.loads(path.read_text())
        value.update(model="new-decision", context_window_tokens=2, context_safety_margin_tokens=1)
        path.write_text(json.dumps(value))
    plan = repetition_plan(project, Scope(), screening_enabled=True)
    assert plan["input_fingerprint"] == old_plan["input_fingerprint"]
    info = repetition_resume(project, plan)
    assert info["resume_compatible"] and info["execution_changed"] and info["estimated_requests"] == 1
    requests = []
    result = asyncio.run(run(action="resume", requests=requests))
    assert result["run_id"] == info["run_id"] and len(requests) == 1 and "messages" in requests[0]
    saved = json.loads(checkpoint_path(run_dir, "1").read_text())
    assert saved["screening_execution_index"] == 1 and saved["comparison_execution_index"] == 2
    assert (run_dir / "prompt.txt").read_bytes() == original_prompt
    snapshot = run_dir / "continuations" / "0001"
    assert (snapshot / "decision_presets" / "local.json").is_file()
    assert requests[0]["messages"][0]["content"] == (snapshot / "prompt.txt").read_text()


@pytest.mark.parametrize("change", ["source", "context", "terms", "target", "protocol", "old_checkpoint"])
def test_resume_rejects_evidence_or_protocol_changes(tmp_path, monkeypatch, change):
    from app import repetition_consistency as repetition
    from app.sqlite_storage import write_json
    project = repetition_project(tmp_path, monkeypatch)
    async def run():
        async with httpx.AsyncClient(transport=transport(invalid=True)) as client:
            await repetition.run_repetition(project, Scope(), screening_enabled=True, http_client=client)
    with pytest.raises(ExternalError):
        asyncio.run(run())
    original = repetition.repetition_plan(project, Scope(), screening_enabled=True)
    info = repetition.repetition_resume(project, original)
    if change in {"source", "context"}:
        segments = load_segments(project)
        segments[1 if change == "source" else 0]["source"] += "改变"
        monkeypatch.setattr(repetition, "load_segments", lambda _: segments)
    elif change == "terms":
        monkeypatch.setattr(repetition, "load_terms", lambda _: {"terms_revision": 999, "terms": []})
    elif change == "target":
        value = original["config"]
        value["project"]["target_language"] = "English"
        monkeypatch.setattr(repetition, "load_project_config", lambda *args, **kwargs: value)
    elif change == "protocol":
        monkeypatch.setattr(repetition, "PROTOCOL_VERSION", repetition.PROTOCOL_VERSION + 1)
    else:
        path = project / "runs" / info["run_id"] / "manifest.json"
        value = read_json(project, path)
        value["repetition_checkpoint"] = 1
        write_json(project, path, value)
    assert not repetition.repetition_resume(project, repetition.repetition_plan(project, Scope(), screening_enabled=True))["resume_compatible"]


def test_budget_increase_revisits_deferred_group_on_resume(tmp_path, monkeypatch):
    from app import repetition_consistency as repetition
    from app.errors import StorageError
    from tests.helpers import use_llm_preset
    project = repetition_project(tmp_path, monkeypatch)
    use_llm_preset(tmp_path, context_window_tokens=100, context_safety_margin_tokens=10)
    original_write = repetition.atomic_write_text
    def fail_publish(path, text):
        if path.name == repetition.DRAFT_FILE:
            raise StorageError("publish probe")
        return original_write(path, text)
    monkeypatch.setattr(repetition, "atomic_write_text", fail_publish)
    async def run(action=None, requests=None):
        async with httpx.AsyncClient(transport=transport(requests=requests)) as client:
            return await repetition.run_repetition(project, Scope(), run_action=action, http_client=client)
    with pytest.raises(StorageError):
        asyncio.run(run())
    use_llm_preset(tmp_path, context_window_tokens=64000, context_safety_margin_tokens=1000)
    plan = repetition.repetition_plan(project, Scope())
    info = repetition.repetition_resume(project, plan)
    assert info["resume_compatible"] and info["completed"] == 0 and info["estimated_requests"] == 1
    monkeypatch.setattr(repetition, "atomic_write_text", original_write)
    requests = []
    assert asyncio.run(run("resume", requests))["completed"] == 3
    assert len(requests) == 1


@pytest.mark.parametrize("kind", ["context", "truncated", "unknown"])
def test_comparison_splits_complete_groups_and_links_children(tmp_path, monkeypatch, kind):
    from app.errors import ContextLengthError
    from app.llm_client import LLMClient
    from app.repetition_consistency import run_repetition
    project = multiple_groups_project(tmp_path, monkeypatch)
    path = project / "config.toml"
    path.write_text(path.read_text().replace('empty_unknown_mode = "retry"', 'empty_unknown_mode = "split"'))
    requests, links = [], []
    respond_valid = transport(requests=requests)
    original_chat = LLMClient.chat
    async def chat(self, **kwargs):
        try:
            response, request_id = await original_chat(self, **kwargs)
        except ContextLengthError as error:
            links.append((error.request_id, kwargs.get("parent_request_id")))
            raise
        links.append((request_id, kwargs.get("parent_request_id")))
        return response, request_id
    monkeypatch.setattr(LLMClient, "chat", chat)
    def respond(request):
        response = respond_valid.handle_request(request)
        if len(requests) == 1:
            if kind == "context":
                return httpx.Response(400, text="context_length_exceeded: maximum context tokens")
            return httpx.Response(200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length" if kind == "truncated" else "stop"}]})
        return response
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await run_repetition(project, Scope(), http_client=client)
    assert asyncio.run(run())["completed"] == 6
    assert len(requests) == 3
    ids = [[g["id"] for g in json.loads(request["messages"][-1]["content"])["repetitions"]] for request in requests]
    assert ids == [["1", "2"], ["1"], ["2"]]
    assert links[0][1] is None and links[1][1] == links[2][1] == links[0][0]
    assert all(len(g["occurrences"]) == 3 for request in requests[1:] for g in json.loads(request["messages"][-1]["content"])["repetitions"])


@pytest.mark.parametrize("kind", ["context", "truncated"])
def test_single_comparison_group_is_not_split(tmp_path, monkeypatch, kind):
    from app.repetition_consistency import repetition_plan, repetition_resume, run_repetition
    project = repetition_project(tmp_path, monkeypatch)
    requests = []
    def respond(request):
        requests.append(request)
        if kind == "context":
            return httpx.Response(400, text="context_length_exceeded: maximum context tokens")
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await run_repetition(project, Scope(), http_client=client)
    with pytest.raises(ExternalError, match="不能继续拆分"):
        asyncio.run(run())
    assert len(requests) == 1
    assert repetition_resume(project, repetition_plan(project, Scope()))["completed"] == 0


def test_empty_split_budget_is_inherited(tmp_path, monkeypatch):
    from app.repetition_consistency import repetition_plan, run_repetition
    project = multiple_groups_project(tmp_path, monkeypatch)
    plan = repetition_plan(project, Scope())
    plan["config"]["retry"]["empty_truncated_max_attempts"] = 1
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await run_repetition(project, Scope(), prepared_plan=plan, http_client=client)
    with pytest.raises(ExternalError, match="预算已耗尽"):
        asyncio.run(run())
    assert len(calls) == 2


def test_hard_budget_accepts_exact_limit(tmp_path, monkeypatch):
    from app.repetition_consistency import repetition_plan
    from tests.helpers import use_llm_preset
    project = repetition_project(tmp_path, monkeypatch)
    initial = repetition_plan(project, Scope())
    limit = initial["groups"][0]["comparison_input_tokens"]
    use_llm_preset(tmp_path, context_window_tokens=limit + 10, context_safety_margin_tokens=10,
                   target_chunk_input_tokens=limit, input_tokens_per_minute=limit)
    plan = repetition_plan(project, Scope())
    assert not plan["groups"][0]["over_budget"] and plan["estimated_requests"] == 1


def test_cancelled_split_keeps_completed_child_for_resume(tmp_path, monkeypatch):
    from app.repetition_consistency import repetition_plan, repetition_resume, run_repetition
    project = multiple_groups_project(tmp_path, monkeypatch)
    requests = []
    respond_valid = transport(requests=requests)
    async def cancel_run():
        second_child = asyncio.Event()
        async def respond(request):
            response = respond_valid.handle_request(request)
            if len(requests) == 1:
                return httpx.Response(400, text="context_length_exceeded: maximum context tokens")
            if len(requests) == 3:
                second_child.set()
                await asyncio.Future()
            return response
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            task = asyncio.create_task(run_repetition(project, Scope(), http_client=client))
            await second_child.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    asyncio.run(cancel_run())
    info = repetition_resume(project, repetition_plan(project, Scope()))
    assert info["completed"] == 3 and info["estimated_requests"] == 1
    requests.clear()
    async def resume():
        async with httpx.AsyncClient(transport=transport(requests=requests)) as client:
            return await run_repetition(project, Scope(), run_action="resume", http_client=client)
    assert asyncio.run(resume())["completed"] == 6
    assert len(requests) == 1
    assert [g["id"] for g in json.loads(requests[0]["messages"][-1]["content"])["repetitions"]] == ["2"]


@pytest.mark.parametrize("invalid", [False, True])
def test_run_collects_warnings_usage_audit_and_interruption(tmp_path, monkeypatch, invalid):
    from app import repetition_consistency as repetition
    project = repetition_project(tmp_path, monkeypatch)
    monkeypatch.setattr(repetition, "load_terms", lambda _: character_library(project, status="pending"))
    path = project / "config.toml"
    path.write_text(path.read_text().replace('empty_truncated_mode = "split"', 'empty_truncated_mode = "retry"'))
    requests = []
    responder = transport(invalid=invalid, requests=requests)
    def respond(request):
        response = responder.handle_request(request)
        if len(requests) == 1:
            body = response.json()
            body["choices"][0].update(message={"content": ""}, finish_reason="length")
            return httpx.Response(200, json=body)
        return response
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await repetition.run_repetition(project, Scope(), http_client=client)
    if invalid:
        with pytest.raises(ExternalError):
            asyncio.run(run())
        info = repetition.repetition_resume(project, repetition.repetition_plan(project, Scope()))
        run_id = info["run_id"]
    else:
        result = asyncio.run(run())
        run_id = result["run_id"]
    manifest = read_json(project, project / "runs" / run_id / "manifest.json")
    assert any("待聚合" in value for value in manifest["warnings"])
    assert any("原样重试" in value for value in manifest["warnings"])
    assert len(manifest["warnings"]) == len(set(manifest["warnings"]))
    assert manifest["key_audits"][0]["execution_index"] == 1
    assert manifest["usage"]["available"] and manifest["usage"]["total_tokens"] == 30 * len(requests)
    assert manifest["usage_invocation_count"] == 1 and manifest["failed_segment_count"] == 0
    if invalid:
        assert manifest["status"] == "running" and manifest["completed_at"] is None
        assert manifest["last_interruption"]["reason"] == "format_error"
        assert manifest["last_interruption"]["request_id"].startswith("REQ-")
    else:
        assert manifest["warnings"] == result["warnings"]


def test_preflight_reports_execution_change_and_remaining_budget(tmp_path, monkeypatch):
    from app.repetition_consistency import run_repetition
    from app.web import create_app
    from fastapi.testclient import TestClient
    project = repetition_project(tmp_path, monkeypatch)
    async def run():
        async with httpx.AsyncClient(transport=transport(invalid=True)) as client:
            await run_repetition(project, Scope(), http_client=client)
    with pytest.raises(ExternalError):
        asyncio.run(run())
    path = project / "prompts" / "repetition_consistency.zh-CN.middle.txt"
    path.write_text(path.read_text() + "\n修改说明")
    with TestClient(create_app(projects_root=project.parent, app_root=config.APP_ROOT)) as client:
        value = client.get(f"/api/v1/projects/{project.name}/repetition/options").json()
    assert value["run_action"] == "resume"
    assert value["running_run"]["resume_compatible"] and value["running_run"]["execution_changed"]
    assert value["estimated_requests"] == 1


def test_failed_publication_transition_does_not_double_count_usage(tmp_path, monkeypatch):
    from app import repetition_consistency as repetition
    from app.errors import StorageError
    project = repetition_project(tmp_path, monkeypatch)
    original_write = repetition.write_repetition_state
    def fail_transition(*args, **kwargs):
        raise StorageError("transition probe")
    monkeypatch.setattr(repetition, "write_repetition_state", fail_transition)
    async def run(action=None):
        async with httpx.AsyncClient(transport=transport()) as client:
            return await repetition.run_repetition(project, Scope(), run_action=action, http_client=client)
    with pytest.raises(StorageError, match="transition probe"):
        asyncio.run(run())
    info = repetition.repetition_resume(project, repetition.repetition_plan(project, Scope()))
    manifest = read_json(project, project / "runs" / info["run_id"] / "manifest.json")
    assert manifest["usage"]["total_tokens"] == 30 and manifest["usage_invocation_count"] == 1
    assert len(manifest["key_audits"]) == 1
    monkeypatch.setattr(repetition, "write_repetition_state", original_write)
    result = asyncio.run(run("resume"))
    assert result["usage"] == manifest["usage"]

@pytest.mark.asyncio
@pytest.mark.parametrize("waiting", [False, True, "budget"])
async def test_continuous_repetition_applies_or_stops_for_review(tmp_path, monkeypatch, waiting):
    from app.web_tasks import WebTaskManager
    from app.repetition_consistency import run_repetition, current_draft
    project = repetition_project(tmp_path, monkeypatch)
    events = []
    if waiting == "budget":
        from tests.helpers import use_llm_preset
        use_llm_preset(tmp_path, context_window_tokens=100, context_safety_margin_tokens=10)

    async def translate(*args, **kwargs):
        events.append("translation")
        return {"selected": 8, "completed": 8, "failed": 0, "pending": 0}

    async def review(*args, **kwargs):
        events.append("proofreading")
        return {"selected": 8, "completed": 8, "failed": 0, "pending": 0}

    def respond(request):
        response = transport().handler(request)
        if waiting:
            body = response.json()
            data = json.loads(body["choices"][0]["message"]["content"])
            data["repetitions"][0].update(keep=[], uncertain=["3"])
            body["choices"][0]["message"]["content"] = json.dumps(data)
            return httpx.Response(200, json=body)
        return response

    async def repetition(*args, **kwargs):
        events.append("repetition")
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await run_repetition(*args, **kwargs, http_client=client)

    monkeypatch.setattr("app.web_continuous.run_translation", translate)
    monkeypatch.setattr("app.web_continuous.run_review", review)
    monkeypatch.setattr("app.web_continuous.run_repetition", repetition)
    manager = WebTaskManager()
    task = await manager.start(project, "continuous", scope=Scope(), run_action=None,
        reuse_mixed_fingerprints=True, continuous_stages=("translation", "proofreading"),
        include_repetition_consistency=True, apply_repetition_consistency=True)
    await manager.tasks[task["task_id"]].asyncio_task
    result = manager.get(task["task_id"])
    assert result["status"] == "completed", result.get("error")
    assert result["summary"]["waiting_review"] is bool(waiting)
    if waiting != "budget":
        assert result["summary"]["usage"]["available"] is True
        assert result["summary"]["usage"]["total_tokens"] == 30
    assert events == (["translation", "repetition"] if waiting else ["translation", "repetition", "proofreading"])
    assert (current_draft(project) is not None) is bool(waiting)
    step = next(s for s in result["steps"] if s["stage"] == "repetition_consistency")
    assert step["failed"] == 0
    assert step["status"] == ("waiting_review" if waiting else "completed")
    if waiting:
        assert result["current_stage"] == "repetition_consistency"
        assert result["steps"][-1]["status"] == "not_executed"
    states = latest_stage_states(project, "translation", [s["segment_id"] for s in load_segments(project)])
    translations = [s["completed"]["text"] for s in states.values()]
    assert translations.count("我会去接你") == (2 if waiting else 1)


def test_continuous_repetition_preflight_permissions_and_deferred_count(tmp_path, monkeypatch):
    from app.web_continuous import inspect_continuous
    project = repetition_project(tmp_path, monkeypatch)
    options = inspect_continuous(project, ("translation", "proofreading"), include_repetition_consistency=True,
        reuse_mixed_fingerprints=True)
    assert options["stages"] == ["translation", "repetition_consistency", "proofreading"]
    assert any(b["code"] == "apply_repetition_consistency_required" for b in options["blocking"])
    from app.repetition_consistency import run_repetition
    async def generate():
        async with httpx.AsyncClient(transport=transport()) as client:
            await run_repetition(project, Scope(), http_client=client)
    asyncio.run(generate())
    options = inspect_continuous(project, ("proofreading",), include_repetition_consistency=True,
        apply_repetition_consistency=True, reuse_mixed_fingerprints=True)
    assert any(b["code"] == "pending_repetition_draft" for b in options["blocking"])

@pytest.mark.asyncio
async def test_continuous_repetition_resume_restores_filters_and_screening(tmp_path, monkeypatch):
    from app.web_tasks import WebTaskManager
    from app.repetition_consistency import run_repetition, repetition_plan, repetition_resume
    from tests.helpers import use_llm_preset
    project = repetition_project(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=transport(invalid=True)) as client:
        with pytest.raises(ExternalError):
            await run_repetition(project, Scope(), screening_enabled=True, exclude_punctuation_only=False, http_client=client)
    use_llm_preset(tmp_path, model="changed")
    requests = []
    async def resume(*args, **kwargs):
        assert kwargs["prepared_plan"]["screening_enabled"] is True
        assert kwargs["prepared_plan"]["exclude_punctuation_only"] is False
        async with httpx.AsyncClient(transport=transport(requests=requests)) as client:
            return await run_repetition(*args, **kwargs, http_client=client)
    async def review(*args, **kwargs):
        return {"selected": 8, "completed": 8, "failed": 0, "pending": 0}
    monkeypatch.setattr("app.web_continuous.run_repetition", resume)
    monkeypatch.setattr("app.web_continuous.run_review", review)
    manager = WebTaskManager()
    task = await manager.start(project, "continuous", scope=Scope(), run_action=None, reuse_mixed_fingerprints=False,
        continuous_stages=("proofreading",), continuous_run_actions={"repetition_consistency": "resume"},
        include_repetition_consistency=True, apply_repetition_consistency=True)
    await manager.tasks[task["task_id"]].asyncio_task
    view = manager.get(task["task_id"])
    assert view["status"] == "completed", view.get("error")
    assert len(requests) == 1 and "messages" in requests[0]


def test_continuous_repetition_defers_evidence_until_translation(tmp_path, monkeypatch):
    from app.web_continuous import inspect_continuous
    project = repetition_project(tmp_path, monkeypatch)
    def forbidden(*args, **kwargs):
        pytest.fail("translation preflight must not assemble repetition evidence")
    monkeypatch.setattr("app.web_continuous.repetition_plan", forbidden)
    value = inspect_continuous(project, ("translation",), include_repetition_consistency=True,
        apply_repetition_consistency=True, reuse_mixed_fingerprints=True)
    step = value["steps"][-1]
    assert step["stage"] == "repetition_consistency" and step["count_deferred"] is True

@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_continuous_repetition_empty_or_apply_failure(tmp_path, monkeypatch, failure):
    from app.web_tasks import WebTaskManager
    from app.repetition_consistency import current_draft, run_repetition
    project = repetition_project(tmp_path, monkeypatch, texts=None if failure else ["同译文"] * 8)
    events = []
    async def repetition(*args, **kwargs):
        events.append("comparison")
        async with httpx.AsyncClient(transport=transport()) as client:
            return await run_repetition(*args, **kwargs, http_client=client)
    async def apply(*args, **kwargs):
        raise UsageError("application failure probe")
    async def review(*args, **kwargs):
        events.append("proofreading")
        return {"selected": 8, "completed": 8, "failed": 0, "pending": 0}
    monkeypatch.setattr("app.web_continuous.run_repetition", repetition)
    monkeypatch.setattr("app.web_continuous.apply_draft_locked", apply)
    monkeypatch.setattr("app.web_continuous.run_review", review)
    manager = WebTaskManager()
    task = await manager.start(project, "continuous", scope=Scope(), run_action=None, reuse_mixed_fingerprints=False,
        continuous_stages=("proofreading",), include_repetition_consistency=True, apply_repetition_consistency=True)
    await manager.tasks[task["task_id"]].asyncio_task
    view = manager.get(task["task_id"])
    assert view["status"] == ("failed" if failure else "completed")
    assert events == (["comparison"] if failure else ["proofreading"])
    assert (current_draft(project) is not None) is failure
    if failure:
        assert "application failure" in view["error"]["error"]
    else:
        assert view["steps"][0]["status"] == "skipped"


def test_continuous_term_application_checks_only_downstream_segment_stages(tmp_path, monkeypatch):
    from app import web_continuous
    project = repetition_project(tmp_path, monkeypatch)
    monkeypatch.setattr(web_continuous, "load_terms", lambda _: character_library(project))
    monkeypatch.setattr(web_continuous, "current_decision_draft", lambda _: {"proposals": [{"proposal_id": "1"}]})
    web_continuous._ensure_downstream_fingerprints_after_decision(project,
        ("repetition_consistency", "proofreading"), Scope(), reuse_mixed_fingerprints=False)

@pytest.mark.asyncio
async def test_continuous_repetition_checks_resume_evidence_after_translation(tmp_path, monkeypatch):
    from app.web_tasks import WebTaskManager
    from app.repetition_consistency import run_repetition, repetition_plan, repetition_resume
    from app.web_continuous import stage_fingerprint_snapshot
    project = repetition_project(tmp_path, monkeypatch)
    segments = load_segments(project)
    states = latest_stage_states(project, "translation", [s["segment_id"] for s in segments])
    project_id = read_json(project, project / "project.json")["project_id"]
    fingerprint = stage_fingerprint_snapshot(project, "translation")
    append_stage_results(project, [record_header("stage_result", project_id, stage="translation",
        segment_id=s["segment_id"], status="completed", text=states[s["segment_id"]]["completed"]["text"],
        origin="web", stage_fingerprint=fingerprint) for s in segments])
    async with httpx.AsyncClient(transport=transport(invalid=True)) as client:
        with pytest.raises(ExternalError):
            await run_repetition(project, Scope(), http_client=client)
    old = repetition_resume(project, repetition_plan(project, Scope()))
    async def translate(*args, **kwargs):
        append_stage_results(project, [record_header("stage_result", project_id, stage="translation",
            segment_id=segments[1]["segment_id"], status="completed", text="新的译文",
            origin="web", stage_fingerprint=fingerprint)])
        return {"selected": 8, "completed": 8, "failed": 0, "pending": 0}
    monkeypatch.setattr("app.web_continuous.run_translation", translate)
    manager = WebTaskManager()
    task = await manager.start(project, "continuous", scope=Scope(), run_action=None, reuse_mixed_fingerprints=False,
        continuous_stages=("translation",), continuous_run_actions={"repetition_consistency": "resume"},
        include_repetition_consistency=True, apply_repetition_consistency=True)
    await manager.tasks[task["task_id"]].asyncio_task
    view = manager.get(task["task_id"])
    assert view["status"] == "failed"
    assert "证据已变化" in view["error"]["error"]
    assert repetition_resume(project, repetition_plan(project, Scope()))["run_id"] == old["run_id"]

@pytest.mark.asyncio
async def test_continuous_terminal_repetition_generates_draft_without_apply_authorization(tmp_path, monkeypatch):
    from app.web_tasks import WebTaskManager
    from app.repetition_consistency import current_draft, run_repetition
    from app.web_continuous import inspect_continuous
    project = repetition_project(tmp_path, monkeypatch)
    options = inspect_continuous(project, ("translation",), include_repetition_consistency=True,
        reuse_mixed_fingerprints=True)
    assert not any(b["code"] == "apply_repetition_consistency_required" for b in options["blocking"])
    async def translate(*args, **kwargs):
        return {"selected": 8, "completed": 8, "failed": 0, "pending": 0}
    async def repetition(*args, **kwargs):
        async with httpx.AsyncClient(transport=transport()) as client:
            return await run_repetition(*args, **kwargs, http_client=client)
    async def forbidden_apply(*args, **kwargs):
        pytest.fail("terminal repetition review must not apply its draft")
    monkeypatch.setattr("app.web_continuous.run_translation", translate)
    monkeypatch.setattr("app.web_continuous.run_repetition", repetition)
    monkeypatch.setattr("app.web_continuous.apply_draft_locked", forbidden_apply)
    manager = WebTaskManager()
    task = await manager.start(project, "continuous", scope=Scope(), run_action=None,
        reuse_mixed_fingerprints=True, continuous_stages=("translation",), include_repetition_consistency=True)
    await manager.tasks[task["task_id"]].asyncio_task
    view = manager.get(task["task_id"])
    assert view["status"] == "completed", view.get("error")
    assert view["steps"][-1]["status"] == "completed"
    assert current_draft(project) is not None
    assert "applied" not in view["steps"][-1]["summary"]
