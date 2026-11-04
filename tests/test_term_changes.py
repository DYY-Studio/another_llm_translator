from pathlib import Path

import pytest

from app.config import load_project_config
from app.errors import UsageError
from app.sqlite_storage import append_jsonl, read_json, read_segments, record_header, write_json
from app.term_library import _merge_and_publish_terms, load_terms
from app.term_knowledge import source_range
from app.web_store import WebStore
from tests.test_web_store import create_web_store_project


def publish(project: Path, task_id: str, source: str, translation: str) -> dict:
    segments = read_segments(project)
    segment = segments[0]
    candidate = record_header("terminology_candidates", "test", active_task_id=task_id,
        source_order={"segment_id": segment["segment_id"], "offset": 0},
        source_range=source_range([segment], {s["segment_id"]: s for s in segments}, {}),
        terms=[{"source": source, "category": "人物", "preferred_translation": translation, "aliases": []}])
    append_jsonl(project, project / "terminology" / "candidates.jsonl", candidate)
    write_json(project, project / "terminology" / "active_task.json",
               record_header("terminology_task", "test", active_task_id=task_id, status="active"))
    return _merge_and_publish_terms(project, task_id=task_id, project_id="test", published_run_id=task_id)


def test_changes_accumulate_and_preserve_overridden_candidate(tmp_path: Path) -> None:
    project = create_web_store_project(tmp_path, "Alice Bob")
    publish(project, "first", "Alice", "爱丽丝")
    WebStore(project).save_term({"source": "Alice", "old_normalized": "alice", "preferred_translation": "艾莉丝", "category": "人物", "aliases": []})
    publish(project, "second", "Bob", "鲍勃")
    library = publish(project, "third", "Alice", "爱丽丝")
    pending = library["knowledge"]["pending_changes"]
    assert set(pending) == {"alice", "bob"}
    assert pending["alice"]["candidate_refs"]
    assert next(term for term in library["terms"] if term["normalized"] == "alice")["preferred_translation"] == "艾莉丝"
    rows = WebStore(project).terms()["terms"]
    alice = next(row for row in rows if row["normalized"] == "alice")
    assert len(alice["pending_change"]["candidates"]) == 1
    assert alice["pending_change"]["candidates"][-1]["preferred_translation"] == "爱丽丝"
    refs = pending["alice"]["candidate_refs"][:]
    repeated = _merge_and_publish_terms(project, task_id="third", project_id="test", published_run_id="third")
    assert repeated["knowledge"]["pending_changes"]["alice"]["candidate_refs"] == refs


def test_pending_draft_blocks_scan_preflight_and_publication(tmp_path: Path) -> None:
    from app.execution import create_run
    from app.sqlite_storage import atomic_write_json
    from app.web_tasks import task_options
    project = create_web_store_project(tmp_path, "Alice")
    publish(project, "first", "Alice", "爱丽丝")
    run_id, directory = create_run(project, config=load_project_config(project, stage="terminology_decision"), stage="terminology_decision", fingerprint="test", prompt=None,
                                   selected_count=1, requested_count=1, reused_count=0)
    path = directory / "manifest.json"
    manifest = read_json(project, path)
    manifest.update(status="completed", decision_status="pending")
    write_json(project, path, manifest)
    atomic_write_json(directory / "terminology_decision_draft.json", {"run_id": run_id})
    with pytest.raises(UsageError, match="先应用或丢弃"):
        task_options(project, "terminology")
    with pytest.raises(UsageError, match="先应用或丢弃"):
        _merge_and_publish_terms(project, task_id="first", project_id="test", published_run_id="first")


def test_keeping_current_term_acknowledges_only_selected_changes(tmp_path: Path) -> None:
    project = create_web_store_project(tmp_path, "Alice Bob")
    publish(project, "first", "Alice", "爱丽丝")
    library = publish(project, "second", "Bob", "鲍勃")
    WebStore(project).resolve_term_changes({"normalized": ["alice", "alice"], "expected_revision": library["terms_revision"], "confirm": True})
    assert set(load_terms(project)["knowledge"]["pending_changes"]) == {"bob"}


def test_incremental_plan_can_update_protected_targets_without_unrelated_terms(tmp_path: Path) -> None:
    from app.term_decision import decision_plan
    project = create_web_store_project(tmp_path, "Alice Bob")
    publish(project, "first", "Alice", "爱丽丝")
    library = publish(project, "second", "Bob", "鲍勃")
    WebStore(project).resolve_term_changes({"normalized": ["bob"], "expected_revision": library["terms_revision"], "confirm": True})
    WebStore(project).save_term({"old_normalized": "alice", "source": "Alice", "preferred_translation": "艾莉丝", "aliases": []})
    plan = decision_plan(project, prepare_requests=False, estimate_usage=False)
    assert {item["normalized"] for item in plan["eligible"]} == {"alice"}
    assert "bob" in plan["protected"]
    full = decision_plan(project, all_terms=True, prepare_requests=False, estimate_usage=False)
    assert {item["normalized"] for item in full["eligible"]} == {"alice", "bob"}


def test_pending_knowledge_mode_skips_current_explanations(tmp_path: Path) -> None:
    from app.term_decision import decision_plan
    from app.term_knowledge import save_items
    project = create_web_store_project(tmp_path, "Alice Bob")
    publish(project, "first", "Alice", "爱丽丝")
    library = publish(project, "second", "Bob", "鲍勃")
    save_items(project, library, "alice", [])
    write_json(project, project / "terminology" / "terms.json", library)
    pending = decision_plan(project, knowledge_only=True)
    assert pending["knowledge_pending"] == 1
    full = decision_plan(project, knowledge_only=True, all_terms=True)
    assert full["knowledge_pending"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change_name", [False, True, "review", "alias_review"])
async def test_decision_acknowledges_changes_only_on_apply_and_rollback_restores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change_name: bool | str,
) -> None:
    import json
    import httpx
    from app.term_decision import run_terminology_decision
    from app.term_decision_drafts import apply_decision_draft, current_decision_draft, rollback_decision
    from app.term_knowledge import save_items
    from tests.helpers import llm_jsonl
    monkeypatch.setenv("LLM_API_KEY", "test")
    project = create_web_store_project(tmp_path, "Alice Smith")
    library = publish(project, "first", "Alice", "爱丽丝")
    WebStore(project).save_term({"old_normalized": "alice", "source": "Alice", "preferred_translation": "艾莉丝", "aliases": []})
    library = load_terms(project, resolve_knowledge=False)
    save_items(project, library, "alice", [])
    write_json(project, project / "terminology" / "terms.json", library)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        decisions = [{"type": "decision", "normalized": item["normalized"], "action": "keep", "reason": "保持当前"} for item in payload["terms"]]
        if change_name in {"review", "alias_review"}:
            decisions[0].update(action="needs_review", reason="需要人工处理")
        elif change_name and payload["phase"] == "adjudication":
            decisions[0].update(action="update", changes={"preferred_translation": "爱丽丝"})
        return httpx.Response(200, json={"choices": [{"message": {"content": llm_jsonl(decisions)}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await run_terminology_decision(project, http_client=client)
    assert "alice" in load_terms(project)["knowledge"]["pending_changes"]
    draft = current_decision_draft(project)
    if change_name is True:
        assert draft["proposals"][0]["modifies_confirmed"]
    apply_decision_draft(project, confirm_all=True)
    if change_name in {"review", "alias_review"}:
        from app.term_decision_drafts import set_manual_review_resolved
        assert "alice" in load_terms(project)["knowledge"]["pending_changes"]
        publish(project, "later", "Alice", "爱丽丝")
        later_refs = load_terms(project)["knowledge"]["pending_changes"]["alice"]["candidate_refs"]
        key = "alice"
        if change_name == "alias_review":
            store = WebStore(project)
            store.save_term({"source": "Alice Smith", "preferred_translation": "艾莉丝·史密斯"})
            store.convert_related_to_alias({"normalized": "alice smith", "related_normalized": "alice", "confirm": True})
            key = "alice smith"
        set_manual_review_resolved(project, run_id=draft["run_id"], normalized="alice", resolved=True)
        assert load_terms(project)["knowledge"]["pending_changes"][key]["candidate_refs"] == later_refs[1:]
        set_manual_review_resolved(project, run_id=draft["run_id"], normalized="alice", resolved=False)
        pending = load_terms(project)["knowledge"]["pending_changes"]
        assert set(map(tuple, pending[key]["candidate_refs"])) == set(map(tuple, later_refs))
        if change_name == "alias_review":
            assert "alice" not in pending
        return
    assert load_terms(project)["knowledge"]["pending_changes"] == {}
    rollback_decision(project, confirm=True)
    assert "alice" in load_terms(project)["knowledge"]["pending_changes"]


def test_continuous_decision_includes_confirmed_terms_with_pending_changes(tmp_path: Path) -> None:
    from app.web_continuous import _decision_plan_or_skip
    project = create_web_store_project(tmp_path, "Alice")
    publish(project, "first", "Alice", "爱丽丝")
    WebStore(project).save_term({"old_normalized": "alice", "source": "Alice", "preferred_translation": "艾莉丝", "aliases": []})
    plan, reason = _decision_plan_or_skip(project, None, final_review=False)
    assert reason is None
    assert {term["normalized"] for term in plan["eligible"]} == {"alice"}
