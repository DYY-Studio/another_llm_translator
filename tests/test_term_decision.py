from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import load_config, load_project_config
from app.errors import StorageError, UsageError
from app.execution import create_run
from app.main import build_parser
from app.project import init_project
from app.sqlite_storage import atomic_write_json, read_json, record_header, write_json
from app.term_decision import (
    CHECKPOINT_FILE,
    apply_decision_draft,
    collect_term_evidence,
    current_decision_draft,
    rollback_decision,
    run_terminology_decision,
    save_decision_rejections,
)
from app.web import create_app
from app.web_store import WebStore
from tests.helpers import llm_jsonl
from tests.test_foundation import make_app_root


def create_decision_project(tmp_path: Path, text: str = "Alice Ally\nBob") -> Path:
    app_root = make_app_root(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text(text, encoding="utf-8-sig")
    project, _ = init_project(
        [str(source)],
        name="decision-demo",
        app_root=app_root,
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    metadata = read_json(project, project / "project.json")
    write_json(
        project,
        project / "terminology" / "terms.json",
        record_header(
            "terminology_library",
            str(metadata["project_id"]),
            record_id="TERMS-1",
            terms_revision=1,
            published_run_id="RUN-TERM",
            active_task_id="TASK-TERM",
            terms=[
                {
                    "record_id": "TERM-000001",
                    "source": "Alice",
                    "normalized": "alice",
                    "category": "人物",
                    "description": "unhelpful",
                    "preferred_translation": None,
                    "aliases": ["Ally"],
                    "group_primary": None,
                    "conflicts": {
                        "categories": [],
                        "preferred_translations": [],
                        "alias_primaries": [],
                        "group_claims": [],
                    },
                },
                {
                    "record_id": "TERM-000002",
                    "source": "Bob",
                    "normalized": "bob",
                    "category": "人物",
                    "description": "",
                    "preferred_translation": "鲍勃",
                    "aliases": [],
                    "group_primary": None,
                    "conflicts": {
                        "categories": [],
                        "preferred_translations": [],
                        "alias_primaries": [],
                        "group_claims": [],
                    },
                },
            ],
        ),
    )
    return project


def decision_response(payload: dict) -> list[dict]:
    values = []
    for term in payload["terms"]:
        if term["normalized"] == "alice" and payload["phase"] == "adjudication":
            values.append(
                {
                    "type": "decision",
                    "normalized": "alice",
                    "action": "update",
                    "reason": "补全译名并清理说明",
                    "category": "女性人名",
                    "description": None,
                    "preferred_translation": "爱丽丝",
                    "aliases": ["Ally"],
                    "group_primary": None,
                }
            )
        else:
            values.append(
                {
                    "type": "decision",
                    "normalized": term["normalized"],
                    "action": "keep",
                    "reason": "保持当前决定",
                }
            )
    return values


def single_term_batches(states: list[dict], **_: object) -> tuple[list, int]:
    return [([state], []) for state in states], len(states)


def test_decision_config_migrates_defaults_and_cli_contract(tmp_path: Path) -> None:
    config_path = make_app_root(tmp_path) / "config" / "config.toml"
    source = config_path.read_text(encoding="utf-8")
    source = "\n".join(
        line
        for line in source.splitlines()
        if not line.startswith("preset_terminology_decision")
        and not line.startswith("temperature_terminology_decision")
    )
    config_path.write_text(source + "\n", encoding="utf-8")

    config = load_config(config_path)
    assert config["llm"]["preset_terminology_decision"] == ""
    assert config["llm"]["temperature_terminology_decision"] == 0.1

    parser = build_parser()
    generated = parser.parse_args(["terms-decide", "demo", "--replace-draft"])
    assert generated.command == "terms-decide"
    assert generated.replace_draft is True
    resumed = parser.parse_args(["terms-decide", "demo", "--resume-run"])
    assert resumed.resume_run is True
    forced = parser.parse_args(["terms-decide", "demo", "--force"])
    assert forced.force is True
    applied = parser.parse_args(
        ["terms-decide-apply", "demo", "--all", "--reject", "TDP-1"]
    )
    assert applied.rejected_proposal_ids == ["TDP-1"]


@pytest.mark.asyncio
async def test_decision_generates_persistent_two_pass_draft_and_applies(
    tmp_path: Path,
) -> None:
    project = create_decision_project(tmp_path)
    phases: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        payload = json.loads(body["messages"][1]["content"])
        phases.append(payload["phase"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": llm_jsonl(decision_response(payload))}}
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
        )

    os.environ["LLM_API_KEY"] = "test"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        summary = await run_terminology_decision(project, http_client=client)
    del os.environ["LLM_API_KEY"]

    assert phases == ["adjudication", "consistency"]
    assert summary["proposals"] == 1
    draft = current_decision_draft(project)
    assert draft is not None
    assert draft["source_terms_revision"] == 1
    assert draft["proposals"][0]["after"][0]["preferred_translation"] == "爱丽丝"

    applied = apply_decision_draft(project, confirm_all=True)
    assert applied["terms_revision"] == 2
    alice = next(
        item
        for item in read_json(project, project / "terminology" / "terms.json")[
            "terms"
        ]
        if item["normalized"] == "alice"
    )
    assert alice["preferred_translation"] == "爱丽丝"
    assert alice["description"] == ""
    assert current_decision_draft(project) is None

    rolled_back = rollback_decision(project, confirm=True)
    assert rolled_back["terms_revision"] == 3
    restored = read_json(project, project / "terminology" / "terms.json")
    alice = next(item for item in restored["terms"] if item["normalized"] == "alice")
    assert alice["preferred_translation"] is None
    assert alice["description"] == "unhelpful"


@pytest.mark.asyncio
async def test_decision_runs_batches_concurrently_with_phase_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create_decision_project(tmp_path)
    preset_path = tmp_path / "app-root" / "llm_presets" / "default.json"
    preset = json.loads(preset_path.read_text(encoding="utf-8"))
    preset["max_parallel"] = 2
    preset_path.write_text(json.dumps(preset), encoding="utf-8")
    monkeypatch.setattr("app.term_decision._pack_batches", single_term_batches)
    phase_one_started = asyncio.Event()
    release_phase_one = asyncio.Event()
    active = 0
    maximum = 0
    phase_one_finished = 0

    async def request_batch(*_: object, **kwargs: object) -> dict[str, dict]:
        nonlocal active, maximum, phase_one_finished
        phase = str(kwargs["phase"])
        focus = kwargs["focus"]
        normalized = str(focus[0]["normalized"])
        if phase == "consistency":
            assert phase_one_finished == 2
        active += 1
        maximum = max(maximum, active)
        if phase == "adjudication":
            if active == 2:
                phase_one_started.set()
            await release_phase_one.wait()
        await asyncio.sleep(0)
        active -= 1
        if phase == "adjudication":
            phase_one_finished += 1
        return {normalized: {"action": "keep", "reason": "保持"}}

    monkeypatch.setattr("app.term_decision._request_batch", request_batch)
    async with httpx.AsyncClient() as client:
        task = asyncio.create_task(
            run_terminology_decision(project, http_client=client)
        )
        await asyncio.wait_for(phase_one_started.wait(), timeout=1)
        release_phase_one.set()
        await task

    assert maximum == 2
    assert current_decision_draft(project) is not None


@pytest.mark.asyncio
async def test_decision_cancel_checkpoints_completed_batches_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create_decision_project(tmp_path)
    monkeypatch.setattr("app.term_decision._pack_batches", single_term_batches)
    alice_done = asyncio.Event()
    hold_bob = asyncio.Event()
    calls: list[tuple[str, str]] = []

    async def interrupted_batch(*_: object, **kwargs: object) -> dict[str, dict]:
        phase = str(kwargs["phase"])
        normalized = str(kwargs["focus"][0]["normalized"])
        calls.append((phase, normalized))
        if phase == "adjudication" and normalized == "bob":
            await hold_bob.wait()
        if phase == "adjudication" and normalized == "alice":
            alice_done.set()
        return {normalized: {"action": "keep", "reason": "保持"}}

    monkeypatch.setattr("app.term_decision._request_batch", interrupted_batch)
    async with httpx.AsyncClient() as client:
        task = asyncio.create_task(
            run_terminology_decision(project, http_client=client)
        )
        await asyncio.wait_for(alice_done.wait(), timeout=1)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        runs = [
            item
            for item in (project / "runs").iterdir()
            if (item / CHECKPOINT_FILE).is_file()
        ]
        assert len(runs) == 1
        run_id = runs[0].name
        manifest = read_json(project, runs[0] / "manifest.json")
        assert manifest["status"] == "running"
        checkpoint = json.loads(
            (runs[0] / CHECKPOINT_FILE).read_text(encoding="utf-8")
        )
        assert set(checkpoint["phases"]["adjudication"]) == {"alice"}

        library_path = project / "terminology" / "terms.json"
        library = read_json(project, library_path)
        library["terms_revision"] = 2
        write_json(project, library_path, library)
        with pytest.raises(UsageError, match="revision 已变化"):
            await run_terminology_decision(
                project, resume_run_id=run_id, http_client=client
            )
        library["terms_revision"] = 1
        write_json(project, library_path, library)

        prompt_path = project / "prompts" / "terminology_decision.zh-CN.middle.txt"
        prompt_path.write_text(
            prompt_path.read_text(encoding="utf-8") + "\n继续时使用当前 Prompt。\n",
            encoding="utf-8",
        )

        resumed_calls: list[tuple[str, str]] = []

        async def resumed_batch(*_: object, **kwargs: object) -> dict[str, dict]:
            phase = str(kwargs["phase"])
            normalized = str(kwargs["focus"][0]["normalized"])
            resumed_calls.append((phase, normalized))
            return {normalized: {"action": "keep", "reason": "保持"}}

        monkeypatch.setattr("app.term_decision._request_batch", resumed_batch)
        await run_terminology_decision(
            project, resume_run_id=run_id, http_client=client
        )

    assert ("adjudication", "alice") not in resumed_calls
    assert ("adjudication", "bob") in resumed_calls
    assert {value for value in resumed_calls if value[0] == "consistency"} == {
        ("consistency", "alice"),
        ("consistency", "bob"),
    }
    manifest = read_json(project, project / "runs" / run_id / "manifest.json")
    assert len(manifest["continuations"]) == 1
    assert manifest["usage_invocation_count"] == 2
    assert current_decision_draft(project)["prompt_fingerprint"].startswith(
        "sha256:"
    )


@pytest.mark.asyncio
async def test_web_decision_review_rejections_and_apply(tmp_path: Path) -> None:
    project = create_decision_project(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": llm_jsonl(decision_response(payload))}}
                ]
            },
        )

    os.environ["LLM_API_KEY"] = "test"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await run_terminology_decision(project, http_client=client)
    del os.environ["LLM_API_KEY"]

    client = TestClient(create_app(projects_root=project.parent))
    options = client.get(
        "/api/v1/projects/decision-demo/task-options/terminology_decision"
    )
    assert options.status_code == 200
    assert options.json()["selected"] == 2
    assert options.json()["has_pending_draft"] is True
    review = client.get(
        "/api/v1/projects/decision-demo/terms/decision"
    ).json()
    proposal_id = review["draft"]["proposals"][0]["proposal_id"]
    rejected = client.put(
        "/api/v1/projects/decision-demo/terms/decision/rejections",
        json={"rejected_proposal_ids": [proposal_id]},
    )
    assert rejected.status_code == 200
    assert rejected.json()["draft"]["rejected_proposal_ids"] == [proposal_id]

    applied = client.post(
        "/api/v1/projects/decision-demo/terms/decision/apply",
        json={"confirm": True},
    )
    assert applied.status_code == 200
    assert applied.json()["applied"] == 0
    assert applied.json()["terms"]["terms_revision"] == 1


def test_web_starts_terminology_decision_task_without_options_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create_decision_project(tmp_path)

    async def fake_decision(_: Path, **kwargs: object) -> dict[str, object]:
        progress = kwargs["on_progress"]
        assert callable(progress)
        progress(4, 0, 4)
        return {"completed": 4, "failed": 0, "pending": 0}

    monkeypatch.setattr("app.web_tasks.run_terminology_decision", fake_decision)
    client = TestClient(create_app(projects_root=project.parent))
    started = client.post(
        "/api/v1/projects/decision-demo/tasks",
        json={"stage": "terminology_decision"},
    )
    assert started.status_code == 200
    assert started.json()["stage"] == "terminology_decision"
    assert started.json()["total_segments"] == 0

    task_id = started.json()["task_id"]
    state = client.get(f"/api/v1/tasks/{task_id}").json()
    assert state["status"] == "completed"
    assert state["completed_segments"] == 4
    assert state["total_segments"] == 4


def test_web_decision_exposes_checkpoint_and_supports_resume_or_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create_decision_project(tmp_path)
    config = load_project_config(project, stage="terminology_decision")
    run_id, run_dir = create_run(
        project,
        config=config,
        stage="terminology_decision",
        fingerprint="sha256:test",
        prompt="test",
        selected_count=2,
        requested_count=2,
        reused_count=0,
        details={
            "source_terms_revision": 1,
            "decision_status": "generating",
            "rejected_proposal_ids": [],
            "prompt_language": "zh-CN",
        },
    )
    atomic_write_json(
        run_dir / CHECKPOINT_FILE,
        record_header(
            "terminology_decision_checkpoint",
            str(read_json(project, project / "project.json")["project_id"]),
            run_id=run_id,
            source_terms_revision=1,
            phases={
                "adjudication": {
                    "alice": {
                        "decision": {"action": "keep", "reason": "保持"},
                        "decision_fingerprint": "sha256:test",
                        "model_fingerprint": "sha256:model",
                        "prompt_fingerprint": "sha256:prompt",
                    }
                },
                "consistency": {},
            },
        ),
    )
    received: list[str | None] = []

    async def fake_decision(_: Path, **kwargs: object) -> dict[str, object]:
        received.append(kwargs.get("resume_run_id"))
        progress = kwargs["on_progress"]
        assert callable(progress)
        progress(4, 0, 4)
        return {"completed": 4, "failed": 0, "pending": 0}

    monkeypatch.setattr("app.web_tasks.run_terminology_decision", fake_decision)
    client = TestClient(create_app(projects_root=project.parent))
    options = client.get(
        "/api/v1/projects/decision-demo/task-options/terminology_decision"
    )
    assert options.status_code == 200
    assert options.json()["running_run"]["run_id"] == run_id
    assert options.json()["running_run"]["completed_steps"] == 1
    assert options.json()["running_run"]["total_steps"] == 4
    assert client.post(
        "/api/v1/projects/decision-demo/tasks",
        json={"stage": "terminology_decision"},
    ).status_code == 400
    assert client.post(
        "/api/v1/projects/decision-demo/tasks",
        json={
            "stage": "terminology_decision",
            "run_action": "resume",
            "force": True,
        },
    ).status_code == 400
    assert client.post(
        "/api/v1/projects/decision-demo/tasks",
        json={"stage": "terminology_decision", "run_action": "decline"},
    ).status_code == 400

    resumed = client.post(
        "/api/v1/projects/decision-demo/tasks",
        json={"stage": "terminology_decision", "run_action": "resume"},
    )
    assert resumed.status_code == 200
    client.get(f"/api/v1/tasks/{resumed.json()['task_id']}")
    assert received == [run_id]

    forced = client.post(
        "/api/v1/projects/decision-demo/tasks",
        json={
            "stage": "terminology_decision",
            "run_action": "decline",
            "force": True,
        },
    )
    assert forced.status_code == 200
    client.get(f"/api/v1/tasks/{forced.json()['task_id']}")
    assert received == [run_id, None]
    assert read_json(project, run_dir / "manifest.json")["status"] == "interrupted"


def test_evidence_counts_source_alias_and_aozora_views(tmp_path: Path) -> None:
    project = create_decision_project(tmp_path, "｜Alice《アリス》 Ally\nBob")
    library = read_json(project, project / "terminology" / "terms.json")

    evidence = collect_term_evidence(project, library["terms"])

    assert evidence["alice"]["hit_count"] == 1
    assert evidence["alice"]["source_hit_count"] == 1
    assert evidence["alice"]["alias_hit_counts"] == {"Ally": 1}
    assert evidence["bob"]["hit_count"] == 1


@pytest.mark.asyncio
async def test_decision_protects_override_and_replacement_failure_keeps_draft(
    tmp_path: Path,
) -> None:
    project = create_decision_project(tmp_path)
    WebStore(project).save_term(
        {
            "old_normalized": "bob",
            "source": "Bob",
            "category": "男性人名",
            "description": "人工确认",
            "preferred_translation": "鲍勃",
            "aliases": [],
        }
    )
    seen_terms: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        seen_terms.append([item["normalized"] for item in payload["terms"]])
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(decision_response(payload))}}]},
        )

    os.environ["LLM_API_KEY"] = "test"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await run_terminology_decision(project, http_client=client)
    assert all(values == ["alice"] for values in seen_terms)
    old = current_decision_draft(project)
    assert old is not None

    def invalid_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": llm_jsonl([])}}]}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(invalid_handler)
    ) as client:
        with pytest.raises(UsageError, match="重试耗尽"):
            await run_terminology_decision(
                project, replace_draft=True, http_client=client
            )
    del os.environ["LLM_API_KEY"]
    assert current_decision_draft(project)["run_id"] == old["run_id"]


@pytest.mark.asyncio
async def test_alias_transfer_and_disable_form_one_relationship_proposal(
    tmp_path: Path,
) -> None:
    project = create_decision_project(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        records = []
        for term in payload["terms"]:
            if term["normalized"] == "alice":
                records.append(
                    {
                        "type": "decision",
                        "normalized": "alice",
                        "action": "update",
                        "reason": "转移简称",
                        "category": "人物",
                        "description": None,
                        "preferred_translation": "爱丽丝",
                        "aliases": ["Ally", "Bob"],
                        "group_primary": None,
                    }
                )
            else:
                records.append(
                    {
                        "type": "decision",
                        "normalized": "bob",
                        "action": "disable",
                        "reason": "并入 Alice",
                    }
                )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": llm_jsonl(records)}}]}
        )

    os.environ["LLM_API_KEY"] = "test"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await run_terminology_decision(project, http_client=client)
    del os.environ["LLM_API_KEY"]

    draft = current_decision_draft(project)
    assert draft is not None
    assert draft["model_fingerprint"].startswith("sha256:")
    assert draft["prompt_fingerprint"].startswith("sha256:")
    assert len(draft["proposals"]) == 1
    assert draft["proposals"][0]["kind"] == "relationship"
    assert draft["proposals"][0]["normalized"] == ["alice", "bob"]


def test_decision_rejections_and_stale_revision(tmp_path: Path) -> None:
    project = create_decision_project(tmp_path)
    metadata = read_json(project, project / "project.json")
    run_id = "RUN-DECISION"
    run_dir = project / "runs" / run_id
    run_dir.mkdir()
    before = read_json(project, project / "terminology" / "terms.json")
    overrides = read_json(project, project / "terminology" / "overrides.json")
    draft = record_header(
        "terminology_decision_draft",
        str(metadata["project_id"]),
        record_id="DRAFT-1",
        run_id=run_id,
        status="pending",
        source_terms_revision=1,
        decision_fingerprint="sha256:test",
        proposals=[
            {
                "proposal_id": "TDP-1",
                "kind": "term_update",
                "normalized": ["alice"],
                "before": [],
                "after": [
                    {
                        "normalized": "alice",
                        "source": "Alice",
                        "category": "人物",
                        "description": None,
                        "preferred_translation": "爱丽丝",
                        "aliases": ["Ally"],
                        "group_primary": None,
                        "disabled": False,
                    }
                ],
                "changes": ["preferred_translation"],
                "reason": "补全",
                "evidence": {},
            }
        ],
        needs_review=[],
        rejected_proposal_ids=[],
        source_library=before,
        source_overrides=overrides,
    )
    (run_dir / "terminology_decision_draft.json").write_text(
        json.dumps(draft), encoding="utf-8"
    )
    write_json(
        project,
        run_dir / "manifest.json",
        record_header(
            "run",
            str(metadata["project_id"]),
            record_id=run_id,
            run_id=run_id,
            stage="terminology_decision",
            status="completed",
            started_at="2026-01-01T00:00:00+00:00",
            decision_status="pending",
            rejected_proposal_ids=[],
        ),
    )

    save_decision_rejections(project, ["TDP-1"])
    result = apply_decision_draft(project, confirm_all=True)
    assert result["applied"] == 0
    assert result["terms_revision"] == 1

    # Re-open the draft and then change the library revision to prove stale checks.
    manifest = read_json(project, run_dir / "manifest.json")
    manifest["decision_status"] = "pending"
    write_json(project, run_dir / "manifest.json", manifest)
    library = read_json(project, project / "terminology" / "terms.json")
    library["terms_revision"] = 2
    write_json(project, project / "terminology" / "terms.json", library)
    with pytest.raises(UsageError, match="已过期"):
        save_decision_rejections(project, [])
    with pytest.raises(UsageError, match="已过期"):
        apply_decision_draft(project, confirm_all=True)


def test_atomic_apply_failure_preserves_terms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create_decision_project(tmp_path)
    before = read_json(project, project / "terminology" / "terms.json")
    # Reuse the draft fixture builder from the rejection test by producing a tiny Run.
    metadata = read_json(project, project / "project.json")
    run_id = "RUN-ATOMIC"
    run_dir = project / "runs" / run_id
    run_dir.mkdir()
    draft = record_header(
        "terminology_decision_draft",
        str(metadata["project_id"]),
        run_id=run_id,
        status="pending",
        source_terms_revision=1,
        proposals=[
            {
                "proposal_id": "TDP-A",
                "after": [
                    {
                        "normalized": "alice",
                        "source": "Alice",
                        "category": "人物",
                        "description": None,
                        "preferred_translation": "爱丽丝",
                        "aliases": ["Ally"],
                        "group_primary": None,
                        "disabled": False,
                    }
                ],
            }
        ],
        rejected_proposal_ids=[],
        source_library=before,
        source_overrides=read_json(
            project, project / "terminology" / "overrides.json"
        ),
    )
    (run_dir / "terminology_decision_draft.json").write_text(
        json.dumps(draft), encoding="utf-8"
    )
    write_json(
        project,
        run_dir / "manifest.json",
        record_header(
            "run",
            str(metadata["project_id"]),
            record_id=run_id,
            run_id=run_id,
            stage="terminology_decision",
            status="completed",
            started_at="2026-01-01T00:00:00+00:00",
            decision_status="pending",
            rejected_proposal_ids=[],
        ),
    )

    def fail(*_: object, **__: object) -> None:
        raise StorageError("injected")

    monkeypatch.setattr("app.term_decision.write_terminology_decision_state", fail)
    with pytest.raises(StorageError, match="injected"):
        apply_decision_draft(project, confirm_all=True)
    assert read_json(project, project / "terminology" / "terms.json") == before
