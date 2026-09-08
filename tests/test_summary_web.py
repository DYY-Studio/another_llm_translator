from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import load_project_config
from app.errors import UsageError
from app.execution import create_run, segment_model_source
from app.project import init_project
from app.sqlite_storage import (
    read_content_summaries,
    read_json,
    read_segments,
    record_header,
    write_content_summary,
    write_summary_participation,
)
from app.stage_terminology import _digest
from app.summary_aggregation import aggregate_summaries
from app.web import create_app
from app.web_tasks import task_options
from tests.test_foundation import make_app_root


def _project(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("Alice entered.\nBob waved.", encoding="utf-8")
    project, _ = init_project(
        [str(source)],
        name="demo",
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    return project


def _full_fragment(project):
    metadata = read_json(project, project / "project.json")
    segments = read_segments(project)
    ranges = []
    for segment in segments:
        model_text = segment_model_source(segment)
        ranges.append(
            {
                "segment_id": segment["segment_id"],
                "original_segment_id": segment["segment_id"],
                "slice_id": f"{segment['segment_id']}#slice-0000",
                "slice_index": 0,
                "source": segment["source"],
                "source_digest": _digest(segment["source"]),
                "original_source_digest": _digest(segment["source"]),
                "model_text": model_text,
                "model_text_digest": _digest(model_text),
                "original_model_text_digest": _digest(model_text),
            }
        )
    source_range = {
        "file_id": "F0001",
        "part_id": "document",
        "segment_ids": [item["segment_id"] for item in ranges],
        "segments": ranges,
    }
    write_content_summary(
        project,
        record_header(
            "content_summary",
            metadata["project_id"],
            record_id="SUMMARY-FRAGMENT-WEB",
            kind="fragment",
            file_id="F0001",
            part_id="document",
            status="completed",
            text="Web 概括。",
            refs=["1", "2"],
            source_range=source_range,
            source_digest=_digest(ranges),
            input_digest=_digest(
                [{"segment_id": item["segment_id"], "model_text": item["model_text"]} for item in ranges]
            ),
            prompt_digest="sha256:prompt",
            model="test-model",
        ),
    )


def test_summary_routes_expose_selection_preflight_and_export(tmp_path):
    project = _project(tmp_path)
    _full_fragment(project)
    client = TestClient(create_app(projects_root=project.parent))

    listing = client.get("/api/v1/projects/demo/summaries")
    assert listing.status_code == 200
    assert listing.json()["boundaries"] == [
        {"file_id": "F0001", "part_id": "document", "segment_count": 2}
    ]

    selection = client.put(
        "/api/v1/projects/demo/summaries/participation",
        json={"boundaries": [{"file_id": "F0001", "part_id": "document"}], "selected": True},
    )
    assert selection.status_code == 200
    assert selection.json()["participation"][0]["selected"] is True

    preflight = client.post(
        "/api/v1/projects/demo/summaries/aggregation-preflight",
        json={"boundaries": [{"file_id": "F0001", "part_id": "document"}]},
    )
    assert preflight.status_code == 200
    assert preflight.json()["boundaries"][0]["can_adopt"] is True

    asyncio.run(
        aggregate_summaries(
            project,
            [{"file_id": "F0001", "part_id": "document"}],
        )
    )

    exported = client.post(
        "/api/v1/projects/demo/summaries/export",
        json={
            "boundaries": [{"file_id": "F0001", "part_id": "document"}],
            "path": "notes/summary.md",
        },
    )
    assert exported.status_code == 200
    assert exported.json()["path"] == "notes/summary.md"


def test_summary_payload_schema_aliases_and_safe_errors(tmp_path: Path) -> None:
    project = _project(tmp_path)
    client = TestClient(create_app(projects_root=project.parent))
    boundary = {"file_id": "F0001", "part_id": "document"}

    aliased = client.put(
        "/api/v1/projects/demo/summaries/participation",
        json={"selection": [boundary], "selected": False, "unknown": "ignored"},
    )
    assert aliased.status_code == 200
    assert aliased.json()["participation"][0]["selected"] is False

    missing = client.post(
        "/api/v1/projects/demo/summaries/aggregation-preflight",
        json={},
    )
    assert missing.status_code == 400
    assert missing.json() == {
        "error": "请求参数无效",
        "code": "request_validation_error",
        "params": {"fields": ["boundaries"]},
    }

    malformed = client.post(
        "/api/v1/projects/demo/summaries/aggregation-preflight",
        json={"boundaries": [{"file_id": "F0001"}]},
    )
    assert malformed.status_code == 400
    assert malformed.json()["code"] == "request_validation_error"
    assert malformed.json()["params"]["fields"] == ["boundaries.0.part_id"]

    invalid_selected = client.put(
        "/api/v1/projects/demo/summaries/participation",
        json={"boundaries": [boundary], "selected": "false"},
    )
    assert invalid_selected.status_code == 400
    assert invalid_selected.json()["code"] == "request_validation_error"
    assert invalid_selected.json()["params"]["fields"] == ["selected"]


def test_open_project_restores_missing_summary_prompts(tmp_path: Path):
    project = _project(tmp_path)
    app_root = tmp_path / "app-root"
    missing = project / "prompts" / "content_summary.zh-CN.middle.txt"
    missing_fragment = project / "prompts" / "fragment_summary.zh-CN.middle.txt"
    missing_terminology = project / "prompts" / "terminology.en.middle.txt"
    existing = project / "prompts" / "content_summary.en.middle.txt"
    custom = "用户自定义概括提示词。"
    existing.write_text(custom, encoding="utf-8")
    missing.unlink()
    missing_fragment.unlink()
    missing_terminology.unlink()

    client = TestClient(create_app(projects_root=project.parent, app_root=app_root))
    opened = client.post("/api/v1/projects/open", json={"path": str(project)})

    assert opened.status_code == 200
    assert missing.read_text(encoding="utf-8") == (
        app_root / "prompts" / missing.name
    ).read_text(encoding="utf-8")
    assert missing_fragment.read_text(encoding="utf-8") == (
        app_root / "prompts" / missing_fragment.name
    ).read_text(encoding="utf-8")
    assert missing_terminology.read_text(encoding="utf-8") == (
        app_root / "prompts" / missing_terminology.name
    ).read_text(encoding="utf-8")
    assert existing.read_text(encoding="utf-8") == custom
    assert any(
        "content_summary.zh-CN.middle.txt" in item
        for item in opened.json()["warnings"]
    )
    assert any(
        "fragment_summary.zh-CN.middle.txt" in item
        for item in opened.json()["warnings"]
    )
    assert any(
        "terminology.en.middle.txt" in item
        for item in opened.json()["warnings"]
    )


def test_summary_task_options_keep_source_changed_full_usable(tmp_path: Path):
    project = _project(tmp_path)
    _full_fragment(project)
    metadata = read_json(project, project / "project.json")
    segments = read_segments(project)
    ranges = [
        {
            "segment_id": item["segment_id"],
            "original_segment_id": item["segment_id"],
            "source": item["source"],
            "original_source_digest": _digest(item["source"]),
            "original_model_text_digest": _digest(segment_model_source(item)),
        }
        for item in segments
    ]
    source_range = {"segments": ranges}
    write_content_summary(
        project,
        record_header(
            "content_summary",
            metadata["project_id"],
            record_id="SUMMARY-FULL-STALE-SOURCE",
            kind="full",
            file_id="F0001",
            part_id="document",
            status="completed",
            text="过期概括。",
            source_range=source_range,
            source_digest=_digest(source_range),
            input_digest=_digest(ranges),
            prompt_digest="sha256:prompt",
            model="test-model",
            source_changed=True,
        ),
    )
    options = task_options(project, "content_summary")
    assert options["completed"] == 1

    client = TestClient(create_app(projects_root=project.parent))
    listing = client.get("/api/v1/projects/demo/summaries")
    assert listing.status_code == 200
    full = [
        item
        for item in listing.json()["artifacts"]
        if item["kind"] == "full"
    ]
    assert full[0]["expired"] is True
    assert full[0]["expiry_reason"] == "source_changed"


def test_summary_route_marks_llm_full_expired_after_fragment_text_changes(tmp_path: Path):
    project = _project(tmp_path)
    _full_fragment(project)
    fragment = read_content_summaries(project, kind="fragment", status="completed")[0]
    legacy_full = {
        **fragment,
        "record_id": "SUMMARY-FULL-LLM-WEB",
        "kind": "full",
        "input_digest": _digest(
            [{"summary_id": fragment["record_id"], "text": fragment["text"]}]
        ),
        "provenance": {
            "origin": "llm",
            "artifact_ids": [fragment["record_id"]],
            "source_ranges": [fragment["source_range"]],
        },
    }
    write_content_summary(project, legacy_full)
    fragment["text"] = "片段文本已重新生成。"
    write_content_summary(project, fragment)

    client = TestClient(create_app(projects_root=project.parent))
    response = client.get("/api/v1/projects/demo/summaries")
    assert response.status_code == 200
    full = [item for item in response.json()["artifacts"] if item["record_id"] == "SUMMARY-FULL-LLM-WEB"]
    assert full[0]["expired"] is True
    assert full[0]["expiry_reason"] == "provenance_unavailable"


def test_tasks_reject_summary_selection_for_non_summary_stage(tmp_path: Path):
    project = _project(tmp_path)
    client = TestClient(create_app(projects_root=project.parent))
    response = client.post(
        "/api/v1/projects/demo/tasks",
        json={
            "stage": "terminology",
            "summary_selection": [{"file_id": "F0001", "part_id": "document"}],
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == "usage_error"
    assert "summary_selection" in response.json()["error"]


def test_terminology_summary_start_rejects_partial_scope_before_queueing(
    tmp_path: Path,
):
    project = _project(tmp_path)
    client = TestClient(create_app(projects_root=project.parent))

    response = client.post(
        "/api/v1/projects/demo/tasks",
        json={
            "stage": "terminology",
            "include_summaries": True,
            "only_segment": "F0001-S000001",
        },
    )

    assert response.status_code == 400
    assert "完整项目范围" in response.json()["error"]
    assert client.get("/api/v1/tasks/active").json()["tasks"] == []


def test_terminology_task_options_expose_summary_preflight(tmp_path: Path):
    project = _project(tmp_path)
    _full_fragment(project)
    from app.sqlite_storage import write_summary_participation

    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "document", "selected": True}],
    )
    client = TestClient(create_app(projects_root=project.parent))

    with_flag = client.get(
        "/api/v1/projects/demo/task-options/terminology",
        params={"include_summaries": "true"},
    )
    assert with_flag.status_code == 200
    body = with_flag.json()
    assert body["summary_selected_boundaries"] == 1
    assert body["summary_only_work"] is False

    without_flag = client.get(
        "/api/v1/projects/demo/task-options/terminology"
    )
    assert "summary_selected_boundaries" not in without_flag.json()


def test_terminology_summary_prompt_preflight_and_start_fail_consistently(
    tmp_path: Path,
):
    project = _project(tmp_path)
    (project / "prompts" / "fragment_summary.en.middle.txt").unlink()
    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "document", "selected": True}],
    )
    client = TestClient(create_app(projects_root=project.parent))

    options = client.get(
        "/api/v1/projects/demo/task-options/terminology",
        params={"include_summaries": "true", "language": "en"},
    )

    assert options.status_code == 200
    assert options.json()["summary_prompt_preflight"] == {
        "ok": False,
        "language": "en",
        "required_stages": ["terminology", "fragment_summary"],
        "missing": ["fragment_summary.en.middle.txt"],
    }

    started = client.post(
        "/api/v1/projects/demo/tasks",
        json={"stage": "terminology", "language": "en", "include_summaries": True},
    )

    assert started.status_code == 400
    assert started.json()["params"]["reason"] == "summary_prompt_missing"
    assert "fragment_summary.en.middle.txt" in started.json()["error"]
    assert client.get("/api/v1/tasks/active").json()["tasks"] == []


def test_summary_participation_rejects_duplicate_boundaries(tmp_path: Path):
    project = _project(tmp_path)
    client = TestClient(create_app(projects_root=project.parent))

    response = client.put(
        "/api/v1/projects/demo/summaries/participation",
        json={
            "boundaries": [
                {"file_id": "F0001", "part_id": "document"},
                {"file_id": "F0001", "part_id": "document"},
            ],
            "selected": True,
        },
    )

    assert response.status_code == 400
    assert "不能重复" in response.json()["error"]


def test_terminology_start_reports_machine_readable_conflict_reasons(
    tmp_path: Path,
):
    project = _project(tmp_path)
    create_run(
        project,
        config=load_project_config(project, stage="terminology"),
        stage="terminology",
        fingerprint="old",
        prompt="old prompt",
        selected_count=2,
        requested_count=2,
        reused_count=0,
        details={
            "scope": {
                "all_nonempty": True,
                "from_file": None,
                "only_file": None,
                "only_segment": None,
                "force": False,
            }
        },
    )
    client = TestClient(create_app(projects_root=project.parent))

    plain = client.post(
        "/api/v1/projects/demo/tasks",
        json={"stage": "terminology"},
    )
    assert plain.status_code == 400
    assert plain.json()["params"]["reason"] == "unfinished_run"

    response = client.post(
        "/api/v1/projects/demo/tasks",
        json={"stage": "terminology", "summary_selection": []},
    )
    assert response.status_code == 400
    assert "summary_selection" in response.json()["error"]


def test_content_summary_task_options_and_start_report_unfinished_run(
    tmp_path: Path,
):
    project = _project(tmp_path)
    _full_fragment(project)
    create_run(
        project,
        config=load_project_config(project, stage="content_summary"),
        stage="content_summary",
        fingerprint="old",
        prompt="old prompt",
        selected_count=1,
        requested_count=1,
        reused_count=0,
        details={
            "scope": {
                "all_nonempty": True,
                "from_file": None,
                "only_file": None,
                "only_segment": None,
                "force": False,
            }
        },
    )
    options = task_options(project, "content_summary")
    assert options["running_run"]["resume_compatible"] is False

    client = TestClient(create_app(projects_root=project.parent))
    started = client.post(
        "/api/v1/projects/demo/summaries/aggregate",
        json={"boundaries": [{"file_id": "F0001", "part_id": "document"}]},
    )
    assert started.status_code == 400
    assert started.json()["code"] == "usage_error"
    assert started.json()["params"]["reason"] == "unfinished_run"


def test_content_summary_task_options_preflights_requested_prompt_language(
    tmp_path: Path,
):
    project = _project(tmp_path)
    (project / "prompts" / "content_summary.en.middle.txt").unlink()

    with pytest.raises(UsageError, match="en Prompt"):
        task_options(project, "content_summary", prompt_language="en")


def test_content_summary_start_preflights_requested_prompt_language(
    tmp_path: Path,
):
    project = _project(tmp_path)
    _full_fragment(project)
    (project / "prompts" / "content_summary.en.middle.txt").unlink()
    client = TestClient(create_app(projects_root=project.parent))

    response = client.post(
        "/api/v1/projects/demo/tasks",
        json={
            "stage": "content_summary",
            "language": "en",
            "summary_selection": [{"file_id": "F0001", "part_id": "document"}],
        },
    )

    assert response.status_code == 400
    assert response.json()["params"]["reason"] == "prompt_language_missing"
    assert client.get("/api/v1/tasks/active").json()["tasks"] == []


def test_specialized_summary_aggregate_route_passes_prompt_language(
    tmp_path: Path,
):
    project = _project(tmp_path)
    _full_fragment(project)
    (project / "prompts" / "content_summary.en.middle.txt").unlink()
    client = TestClient(create_app(projects_root=project.parent))

    response = client.post(
        "/api/v1/projects/demo/summaries/aggregate",
        json={
            "language": "en",
            "boundaries": [{"file_id": "F0001", "part_id": "document"}],
        },
    )

    assert response.status_code == 400
    assert response.json()["params"]["reason"] == "prompt_language_missing"
