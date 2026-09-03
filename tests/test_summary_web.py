from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from app.project import init_project
from app.web_tasks import task_options
from app.sqlite_storage import read_json, read_segments, record_header, write_content_summary
from app.stage_terminology import _digest
from app.execution import segment_model_source
from app.web import create_app
from app.summary_aggregation import aggregate_summaries
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


def test_open_project_restores_only_missing_summary_prompt(tmp_path: Path):
    project = _project(tmp_path)
    app_root = tmp_path / "app-root"
    missing = project / "prompts" / "content_summary.zh-CN.middle.txt"
    existing = project / "prompts" / "content_summary.en.middle.txt"
    custom = "用户自定义概括提示词。"
    existing.write_text(custom, encoding="utf-8")
    missing.unlink()

    client = TestClient(create_app(projects_root=project.parent, app_root=app_root))
    opened = client.post("/api/v1/projects/open", json={"path": str(project)})

    assert opened.status_code == 200
    assert missing.read_text(encoding="utf-8") == (
        app_root / "prompts" / missing.name
    ).read_text(encoding="utf-8")
    assert existing.read_text(encoding="utf-8") == custom
    assert any("content_summary.zh-CN.middle.txt" in item for item in opened.json()["warnings"])


def test_summary_task_options_exclude_source_changed_full(tmp_path: Path):
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
    assert options["completed"] == 0


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
    assert "summary_selection" in response.json()["error"]
