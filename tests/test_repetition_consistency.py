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
                                  "skip": int(choice == "skip"), "uncertain": int(choice == "uncertain")}}
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


@pytest.mark.parametrize("choice", ["skip", "uncertain", "refusal"])
def test_screening_preserves_uncertain_and_refusal(tmp_path, monkeypatch, choice):
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
    assert latest_stage_states(project, "translation", [sid])[sid]["completed"]["text"] == "我会去接你"
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
