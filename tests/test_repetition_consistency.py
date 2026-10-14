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


def repetition_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = make_app_root(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text("前文\n承诺\n后文\n承诺\n另一个场景\n承诺\n相同\n相同", encoding="utf-8-sig")
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
    texts = ["前文译文", "我会来接你", "后文译文", "我会去接你", "另一个场景译文", "我会去接你", "相同译文", "相同译文"]
    project_id = read_json(project, project / "project.json")["project_id"]
    append_stage_results(project, [record_header("stage_result", project_id,
        stage="translation", segment_id=s["segment_id"], status="completed",
        text=text, origin="web", stage_fingerprint="test")
        for s, text in zip(load_segments(project), texts, strict=True)])
    return project


def transport(*, choice: str = "compare", invalid: bool = False, requests: list | None = None):
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
        result = {"groups": [{"members": ["1", "2"], "target": "1", "reason": "同一承诺重现"}],
                  "keep": ["3"], "uncertain": []}
        if invalid:
            result["groups"][0]["members"] = ["1", "unknown"]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(result)}, "finish_reason": "stop"}],
                                       "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}})
    return httpx.MockTransport(respond)


def test_generation_detects_groups_and_sends_shared_evidence(tmp_path, monkeypatch):
    from app.repetition_consistency import current_draft, repetition_plan, run_repetition
    project = repetition_project(tmp_path, monkeypatch)
    plan = repetition_plan(project, Scope())
    assert len(plan["groups"]) == 1
    assert plan["selected"] == 3
    requests = []
    async def run():
        async with httpx.AsyncClient(transport=transport(requests=requests)) as client:
            return await run_repetition(project, Scope(), http_client=client)
    result = asyncio.run(run())
    assert result["completed"] == 3
    draft = current_draft(project)
    assert len(draft["groups"][0]["proposals"]) == 1
    payload = json.loads(requests[1]["messages"][-1]["content"])
    assert payload["source"] == "承诺"
    assert payload["translations"] == ["我会来接你", "我会去接你"]
    assert len(payload["occurrences"]) == 3
    assert all("source" not in item and "translation" not in item for item in payload["occurrences"])


@pytest.mark.parametrize("choice", ["skip", "refusal"])
def test_screening_preserves_skip_and_refusal(tmp_path, monkeypatch, choice):
    from app.repetition_consistency import current_draft, run_repetition
    project = repetition_project(tmp_path, monkeypatch)
    requests = []
    async def run():
        async with httpx.AsyncClient(transport=transport(choice=choice, requests=requests)) as client:
            await run_repetition(project, Scope(), http_client=client)
    asyncio.run(run())
    assert len(requests) == (1 if choice == "skip" else 2)
    assert current_draft(project)["groups"][0]["screening"] == choice


def test_invalid_group_fails_without_replacing_existing_draft(tmp_path, monkeypatch):
    from app.repetition_consistency import current_draft, run_repetition
    project = repetition_project(tmp_path, monkeypatch)
    async def run(invalid=False, replace=False):
        async with httpx.AsyncClient(transport=transport(invalid=invalid)) as client:
            return await run_repetition(project, Scope(), http_client=client, replace_draft=replace)
    asyncio.run(run())
    old = current_draft(project)["run_id"]
    with pytest.raises(ExternalError):
        asyncio.run(run(invalid=True, replace=True))
    assert current_draft(project)["run_id"] == old


def test_missing_translation_fails_preflight(tmp_path, monkeypatch):
    from app.repetition_consistency import repetition_plan
    project = repetition_project(tmp_path, monkeypatch)
    segment = load_segments(project)[0]
    project_id = read_json(project, project / "project.json")["project_id"]
    append_stage_results(project, [record_header("stage_result", project_id, stage="translation",
        segment_id=segment["segment_id"], status="reset")])
    with pytest.raises(UsageError, match="完整翻译"):
        repetition_plan(project, Scope())


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
            await run_repetition(project, Scope(), http_client=client)
    asyncio.run(run())
    group = current_draft(project)["groups"][0]
    assert group["screening"] == "over_budget"
    assert group["uncertain"] == ["1", "2", "3"]


def generate(project):
    from app.repetition_consistency import run_repetition
    async def run():
        async with httpx.AsyncClient(transport=transport()) as client:
            await run_repetition(project, Scope(), http_client=client)
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
def test_managed_generation_publishes_draft_and_does_not_retain_queued_evidence(tmp_path, monkeypatch, exclude_punctuation_only):
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
                                    reuse_mixed_fingerprints=False, run_action=None, prompt_language="en", exclude_punctuation_only=exclude_punctuation_only)
        retained = manager.tasks[state["task_id"]]
        assert retained._start_decision.plan is None
        await retained.asyncio_task
        assert manager.get(state["task_id"])["status"] == "completed"
        draft = current_draft(project)
        assert draft["groups"][0]["proposals"]
        manifest = read_json(project, project / "runs" / draft["run_id"] / "manifest.json")
        assert manifest["prompt_language"] == "en"
        assert manifest["exclude_punctuation_only"] is exclude_punctuation_only
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
            return await repetition.run_repetition(project, Scope(), http_client=client)
    result = asyncio.run(run())
    payload = json.loads(requests[1]["messages"][-1]["content"])
    first, second = payload["occurrences"][:2]
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
            repetition.repetition_plan(project, Scope())
    else:
        async def run():
            async with httpx.AsyncClient(transport=transport()) as client:
                return await repetition.run_repetition(project, Scope(), http_client=client)
        assert any("待聚合" in warning for warning in asyncio.run(run())["warnings"])


def test_term_injection_is_counted_in_preflight_budget(tmp_path, monkeypatch):
    from app import repetition_consistency as repetition
    project = repetition_project(tmp_path, monkeypatch)
    before = repetition.repetition_plan(project, Scope())
    monkeypatch.setattr(repetition, "load_terms", lambda _: character_library(project, detail="角色信息" * 20000))
    after = repetition.repetition_plan(project, Scope())
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
        initial = repetition_plan(project, Scope(), prompt_language="en")["fingerprint"]
        path = f"{base}/prompts/repetition_consistency"
        assert client.put(path, json={"language": "en", "content": "Prefer recurring promises."}).status_code == 200
        preview = client.get(path, params={"language": "en"}).json()["assembled"]
    plan = repetition_plan(project, Scope(), prompt_language="en")
    assert plan["fingerprint"] != initial
    requests = []
    async def run():
        async with httpx.AsyncClient(transport=transport(requests=requests)) as client:
            return await run_repetition(project, Scope(), prompt_language="en", http_client=client)
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
        repetition_plan(project, Scope(), prompt_language="en")


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
    filtered = repetition_plan(project, Scope())
    unfiltered = repetition_plan(project, Scope(), exclude_punctuation_only=False)
    assert filtered["excluded_punctuation_groups"] == int(excluded)
    assert len(filtered["groups"]) == int(not excluded)
    assert len(unfiltered["groups"]) == 1 and len(unfiltered["groups"][0]["occurrences"]) == 3
    assert filtered["fingerprint"] != unfiltered["fingerprint"]
    if excluded:
        assert filtered["evidence"] == {} and filtered["terms"] == {}
        assert filtered["estimated_requests"] == 0
    assert [latest_stage_states(project, "translation", [s["segment_id"]])[s["segment_id"]]["completed"]["text"] for s in segments] == variants
