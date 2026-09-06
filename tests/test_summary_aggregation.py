from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path

import httpx
import pytest

from app.errors import ExportError, UsageError
from app.execution import Scope, segment_model_source
from app.project import init_project
from app.sqlite_storage import (
    read_content_summaries,
    read_json,
    read_segments,
    read_summary_runs,
    record_header,
    write_content_summary,
)
from app.stage_terminology import _digest
from app.summary_aggregation import (
    aggregate_summaries,
    export_summary_markdown,
    full_summary_expired,
)
from app.summary_provenance import assess_full_summary
from app.web_tasks import WebTaskManager
from tests.helpers import llm_jsonl
from tests.test_foundation import make_app_root


def _project(tmp_path: Path, text: str = "Alice entered.\nBob waved.") -> Path:
    source = tmp_path / "source.txt"
    source.write_text(text, encoding="utf-8")
    project, _ = init_project(
        [str(source)],
        name="demo",
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    os.environ["LLM_API_KEY"] = "test"
    return project


def _fragment(
    project: Path,
    *,
    summary_id: str,
    segment_indexes: list[int],
    text: str,
    file_id: str = "F0001",
    part_id: str = "document",
    source_changed: bool = False,
) -> None:
    metadata = read_json(project, project / "project.json")
    segments = read_segments(project)
    boundary_segments = [
        segment
        for segment in segments
        if segment["file_id"] == file_id and segment["part_id"] == part_id
    ]
    selected = [boundary_segments[index] for index in segment_indexes]
    ranges = [
        {
            "segment_id": str(segment["segment_id"]),
            "original_segment_id": str(segment["segment_id"]),
            "slice_id": f"{segment['segment_id']}#slice-0000",
            "slice_index": 0,
            "source": str(segment["source"]),
            "source_digest": _digest(str(segment["source"])),
            "original_source_digest": _digest(str(segment["source"])),
            "model_text": segment_model_source(segment),
            "model_text_digest": _digest(segment_model_source(segment)),
            "original_model_text_digest": _digest(segment_model_source(segment)),
        }
        for segment in selected
    ]
    source_digest = _digest(ranges)
    input_digest = _digest(
        [
            {"segment_id": item["segment_id"], "model_text": item["model_text"]}
            for item in ranges
        ]
    )
    write_content_summary(
        project,
        record_header(
            "content_summary",
            str(metadata["project_id"]),
            record_id=summary_id,
            kind="fragment",
            file_id=file_id,
            part_id=part_id,
            status="completed",
            text=text,
            refs=[str(index + 1) for index in segment_indexes],
            source_range={
                "file_id": file_id,
                "part_id": part_id,
                "segment_ids": [item["segment_id"] for item in ranges],
                "segments": ranges,
            },
            source_digest=source_digest,
            input_digest=input_digest,
            prompt_digest="sha256:prompt",
            model="test-model",
            source_changed=source_changed,
        ),
    )


def _summary_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "content": llm_jsonl(
                            [
                                {
                                    "type": "summary",
                                    "text": "整合后的内容概括。",
                                }
                            ]
                        )
                    }
                }
            ]
        },
    )


def test_single_full_fragment_is_adopted_without_llm_call(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _fragment(
        project,
        summary_id="SUMMARY-FRAGMENT-ALL",
        segment_indexes=[0, 1],
        text="单片段概括。",
    )
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _summary_response(_)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = asyncio.run(
            aggregate_summaries(
                project,
                [{"file_id": "F0001", "part_id": "document"}],
                http_client=client,
            )
        )
    finally:
        asyncio.run(client.aclose())
        os.environ.pop("LLM_API_KEY", None)

    assert calls == 0
    assert result["boundaries"][0]["origin"] == "adopted"
    full = read_content_summaries(project, kind="full", status="completed")
    assert len(full) == 1
    assert full[0]["text"] == "单片段概括。"
    assert full[0]["provenance"]["origin"] == "adopted"


def test_adopted_full_expires_when_current_fragments_become_partitioned(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _fragment(
        project,
        summary_id="SUMMARY-FRAGMENT-OLD-ALL",
        segment_indexes=[0, 1],
        text="旧的直接采用概括。",
    )
    asyncio.run(
        aggregate_summaries(
            project,
            [{"file_id": "F0001", "part_id": "document"}],
        )
    )
    _fragment(
        project,
        summary_id="SUMMARY-FRAGMENT-NEW-1",
        segment_indexes=[0],
        text="新的片段一。",
    )
    _fragment(
        project,
        summary_id="SUMMARY-FRAGMENT-NEW-2",
        segment_indexes=[1],
        text="新的片段二。",
    )

    full = read_content_summaries(project, kind="full", status="completed")[0]
    current = [
        item
        for item in read_segments(project)
        if not item["is_empty"]
        and item["file_id"] == "F0001"
        and item["part_id"] == "document"
    ]
    fragments = read_content_summaries(
        project,
        file_id="F0001",
        part_id="document",
        kind="fragment",
        status="completed",
    )

    assert full_summary_expired(full, current, fragments) is True
    os.environ.pop("LLM_API_KEY", None)


def test_llm_aggregated_full_expires_when_fragment_text_changes(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _fragment(project, summary_id="SUMMARY-FRAGMENT-OLD-1", segment_indexes=[0], text="旧片段一。")
    _fragment(project, summary_id="SUMMARY-FRAGMENT-OLD-2", segment_indexes=[1], text="旧片段二。")
    client = httpx.AsyncClient(transport=httpx.MockTransport(_summary_response))
    try:
        asyncio.run(
            aggregate_summaries(
                project,
                [{"file_id": "F0001", "part_id": "document"}],
                http_client=client,
            )
        )
    finally:
        asyncio.run(client.aclose())
        os.environ.pop("LLM_API_KEY", None)

    full = read_content_summaries(project, kind="full", status="completed")[0]
    current = [item for item in read_segments(project) if not item["is_empty"]]
    artifacts = read_content_summaries(project)
    assert full_summary_expired(full, current, artifacts) is False

    _fragment(project, summary_id="SUMMARY-FRAGMENT-OLD-1", segment_indexes=[0], text="新片段一。")
    _fragment(project, summary_id="SUMMARY-FRAGMENT-OLD-2", segment_indexes=[1], text="新片段二。")

    artifacts = read_content_summaries(project)
    assert full_summary_expired(full, current, artifacts) is True
    exported = export_summary_markdown(
        project,
        [{"file_id": "F0001", "part_id": "document"}],
        "old-aggregated.md",
    )
    assert "整合后的内容概括。" in exported.read_text(encoding="utf-8")


def test_llm_aggregated_full_expires_when_fragment_ranges_change(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, "A\nB\nC\nD")
    _fragment(project, summary_id="SUMMARY-FRAGMENT-OLD-1", segment_indexes=[0, 1], text="前半段。")
    _fragment(project, summary_id="SUMMARY-FRAGMENT-OLD-2", segment_indexes=[2, 3], text="后半段。")
    client = httpx.AsyncClient(transport=httpx.MockTransport(_summary_response))
    try:
        asyncio.run(
            aggregate_summaries(
                project,
                [{"file_id": "F0001", "part_id": "document"}],
                http_client=client,
            )
        )
    finally:
        asyncio.run(client.aclose())
        os.environ.pop("LLM_API_KEY", None)

    full = read_content_summaries(project, kind="full", status="completed")[0]
    current = [item for item in read_segments(project) if not item["is_empty"]]
    artifacts = read_content_summaries(project)
    assert full_summary_expired(full, current, artifacts) is False

    _fragment(project, summary_id="SUMMARY-FRAGMENT-OLD-1", segment_indexes=[0], text="前半段。")
    _fragment(project, summary_id="SUMMARY-FRAGMENT-OLD-2", segment_indexes=[1, 2, 3], text="后半段。")

    artifacts = read_content_summaries(project)
    assert full_summary_expired(full, current, artifacts) is True


def test_recursive_full_expires_when_fragment_text_changes(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _fragment(project, summary_id="SUMMARY-FRAGMENT-OLD-1", segment_indexes=[0], text="甲" * 7500)
    _fragment(project, summary_id="SUMMARY-FRAGMENT-OLD-2", segment_indexes=[1], text="乙" * 7500)
    client = httpx.AsyncClient(transport=httpx.MockTransport(_summary_response))
    try:
        asyncio.run(
            aggregate_summaries(
                project,
                [{"file_id": "F0001", "part_id": "document"}],
                http_client=client,
            )
        )
    finally:
        asyncio.run(client.aclose())
        os.environ.pop("LLM_API_KEY", None)

    full = read_content_summaries(project, kind="full", status="completed")[0]
    assert read_content_summaries(project, kind="reduction", status="completed")
    current = [item for item in read_segments(project) if not item["is_empty"]]
    artifacts = read_content_summaries(project)
    assert full_summary_expired(full, current, artifacts) is False

    _fragment(project, summary_id="SUMMARY-FRAGMENT-OLD-1", segment_indexes=[0], text="新甲" * 3750)
    _fragment(project, summary_id="SUMMARY-FRAGMENT-OLD-2", segment_indexes=[1], text="新乙" * 3750)

    artifacts = read_content_summaries(project)
    assert full_summary_expired(full, current, artifacts) is True


def test_full_without_provenance_fails_closed_after_current_source_checks(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _fragment(project, summary_id="SUMMARY-FRAGMENT-LEGACY", segment_indexes=[0, 1], text="旧片段概括。")
    fragment = read_content_summaries(project, kind="fragment", status="completed")[0]
    legacy = {
        **fragment,
        "record_id": "SUMMARY-FULL-LEGACY",
        "kind": "full",
    }
    legacy.pop("provenance", None)
    write_content_summary(project, legacy)

    current = [item for item in read_segments(project) if not item["is_empty"]]
    artifacts = read_content_summaries(project)
    assessment = assess_full_summary(legacy, current, artifacts)
    assert assessment.expired is True
    assert assessment.expiry_reason == "provenance_unavailable"
    assert full_summary_expired(legacy, current, artifacts) is True


def test_export_keeps_a_stale_full_when_no_current_full_exists(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _fragment(
        project,
        summary_id="SUMMARY-FRAGMENT-ALL",
        segment_indexes=[0, 1],
        text="仍可使用的过期概括。",
    )
    asyncio.run(
        aggregate_summaries(
            project,
            [{"file_id": "F0001", "part_id": "document"}],
        )
    )
    full = read_content_summaries(project, kind="full", status="completed")[0]
    full["status"] = "stale"
    write_content_summary(project, full)

    output = export_summary_markdown(
        project,
        [{"file_id": "F0001", "part_id": "document"}],
        "stale-summary.md",
    )

    assert "仍可使用的过期概括。" in output.read_text(encoding="utf-8")
    os.environ.pop("LLM_API_KEY", None)


def test_multiple_fragments_are_aggregated_and_keep_references(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _fragment(project, summary_id="SUMMARY-FRAGMENT-1", segment_indexes=[0], text="Alice 出现。")
    _fragment(project, summary_id="SUMMARY-FRAGMENT-2", segment_indexes=[1], text="Bob 挥手。")
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _summary_response(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = asyncio.run(
            aggregate_summaries(
                project,
                [{"file_id": "F0001", "part_id": "document"}],
                http_client=client,
            )
        )
    finally:
        asyncio.run(client.aclose())
        os.environ.pop("LLM_API_KEY", None)

    assert result["boundaries"][0]["origin"] == "llm"
    full = read_content_summaries(project, kind="full", status="completed")
    assert full[0]["text"] == "整合后的内容概括。"
    assert full[0]["refs"] == ["F0001-S000001", "F0001-S000002"]
    assert len(full[0]["provenance"]["source_ranges"]) == 2
    payload = json.loads(requests[0]["messages"][1]["content"])
    assert payload["summaries"] == [
        {"id": "1", "text": "Alice 出现。"},
        {"id": "2", "text": "Bob 挥手。"},
    ]


def test_aggregation_rejects_multiple_summary_records(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _fragment(project, summary_id="SUMMARY-FRAGMENT-1", segment_indexes=[0], text="Alice 出现。")
    _fragment(project, summary_id="SUMMARY-FRAGMENT-2", segment_indexes=[1], text="Bob 挥手。")

    def handler(_: httpx.Request) -> httpx.Response:
        records = [
            {"type": "summary", "text": "Alice 出现。", "refs": ["1"]},
            {"type": "summary", "text": "Bob 挥手。", "refs": ["2"]},
        ]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UsageError, match="聚合失败：只允许一条 summary"):
            asyncio.run(
                aggregate_summaries(
                    project,
                    [{"file_id": "F0001", "part_id": "document"}],
                    http_client=client,
                )
            )
    finally:
        asyncio.run(client.aclose())
        os.environ.pop("LLM_API_KEY", None)

    assert read_content_summaries(project, kind="full", status="completed") == []


def test_multiple_boundaries_publish_full_results_atomically(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("Alice entered.\nAlice left.", encoding="utf-8")
    second.write_text("Bob waved.\nBob smiled.", encoding="utf-8")
    project, _ = init_project(
        [str(first), str(second)],
        name="demo",
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    os.environ["LLM_API_KEY"] = "test"
    for file_id in ("F0001", "F0002"):
        _fragment(project, summary_id=f"{file_id}-1", segment_indexes=[0], file_id=file_id, text="片段一。")
        _fragment(project, summary_id=f"{file_id}-2", segment_indexes=[1], file_id=file_id, text="片段二。")

    initial_client = httpx.AsyncClient(transport=httpx.MockTransport(_summary_response))
    try:
        asyncio.run(
            aggregate_summaries(
                project,
                [
                    {"file_id": "F0001", "part_id": "document"},
                    {"file_id": "F0002", "part_id": "document"},
                ],
                http_client=initial_client,
            )
        )
    finally:
        asyncio.run(initial_client.aclose())
    old_full = {
        (str(item["file_id"]), str(item["record_id"]))
        for item in read_content_summaries(project, kind="full", status="completed")
    }
    for file_id in ("F0001", "F0002"):
        _fragment(project, summary_id=f"{file_id}-1", segment_indexes=[0], file_id=file_id, text="旧片段一。", source_changed=True)
        _fragment(project, summary_id=f"{file_id}-2", segment_indexes=[1], file_id=file_id, text="旧片段二。", source_changed=True)
        _fragment(project, summary_id=f"{file_id}-new-1", segment_indexes=[0], file_id=file_id, text="新片段一。")
        _fragment(project, summary_id=f"{file_id}-new-2", segment_indexes=[1], file_id=file_id, text="新片段二。")

    calls = 0

    def fail_on_second_boundary(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _summary_response(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"type":"end"}'}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(fail_on_second_boundary))
    try:
        with pytest.raises(UsageError, match="聚合失败"):
            asyncio.run(
                aggregate_summaries(
                    project,
                    [
                        {"file_id": "F0001", "part_id": "document"},
                        {"file_id": "F0002", "part_id": "document"},
                    ],
                    http_client=client,
                )
            )
    finally:
        asyncio.run(client.aclose())
        os.environ.pop("LLM_API_KEY", None)
    completed_full = {
        (str(item["file_id"]), str(item["record_id"]))
        for item in read_content_summaries(project, kind="full", status="completed")
    }
    assert completed_full == old_full


def test_aggregation_rejects_incomplete_or_changed_coverage(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _fragment(project, summary_id="SUMMARY-FRAGMENT-1", segment_indexes=[0], text="Alice 出现。")
    with pytest.raises(UsageError, match="覆盖不完整"):
        asyncio.run(
            aggregate_summaries(
                project,
                [{"file_id": "F0001", "part_id": "document"}],
            )
        )

    _fragment(
        project,
        summary_id="SUMMARY-FRAGMENT-ALL",
        segment_indexes=[0, 1],
        text="过期概括。",
        source_changed=True,
    )
    with pytest.raises(UsageError, match="源已变化"):
        asyncio.run(
            aggregate_summaries(
                project,
                [{"file_id": "F0001", "part_id": "document"}],
            )
        )
    os.environ.pop("LLM_API_KEY", None)


def test_aggregation_rejects_overlapping_completed_fragment_ranges(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _fragment(project, summary_id="SUMMARY-FRAGMENT-1", segment_indexes=[0], text="Alice 出现。")
    _fragment(project, summary_id="SUMMARY-FRAGMENT-OVERLAP", segment_indexes=[0, 1], text="重复覆盖。")
    with pytest.raises(UsageError, match="重叠"):
        asyncio.run(
            aggregate_summaries(
                project,
                [{"file_id": "F0001", "part_id": "document"}],
            )
        )
    os.environ.pop("LLM_API_KEY", None)


def test_failed_aggregation_keeps_previous_full_result(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _fragment(project, summary_id="SUMMARY-FRAGMENT-1", segment_indexes=[0], text="Alice 出现。")
    _fragment(project, summary_id="SUMMARY-FRAGMENT-2", segment_indexes=[1], text="Bob 挥手。")
    metadata = read_json(project, project / "project.json")
    segments = read_segments(project)
    ranges = []
    for segment in segments:
        ranges.append(
            {
                "segment_id": segment["segment_id"],
                "source": segment["source"],
            }
        )
    write_content_summary(
        project,
        record_header(
            "content_summary",
            str(metadata["project_id"]),
            record_id="SUMMARY-FULL-OLD",
            kind="full",
            file_id="F0001",
            part_id="document",
            status="completed",
            text="旧的完整概括。",
            refs=["1", "2"],
            source_range={"segments": ranges},
            source_digest="sha256:old",
            input_digest="sha256:old-input",
            prompt_digest="sha256:prompt",
            model="test-model",
        ),
    )

    def malformed(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"type":"end"}'}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(malformed))
    try:
        with pytest.raises(UsageError, match="聚合失败"):
            asyncio.run(
                aggregate_summaries(
                    project,
                    [{"file_id": "F0001", "part_id": "document"}],
                    http_client=client,
                )
            )
    finally:
        asyncio.run(client.aclose())
        os.environ.pop("LLM_API_KEY", None)
    full = read_content_summaries(project, kind="full", status="completed")
    assert [item["text"] for item in full] == ["旧的完整概括。"]


def test_empty_export_selection_and_markdown_refs(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _fragment(
        project,
        summary_id="SUMMARY-FRAGMENT-ALL",
        segment_indexes=[0, 1],
        text="可导出的概括。",
    )
    asyncio.run(
        aggregate_summaries(
            project,
            [{"file_id": "F0001", "part_id": "document"}],
        )
    )
    with pytest.raises(ExportError, match="不能为空"):
        export_summary_markdown(project, [])
    output = export_summary_markdown(
        project,
        [{"file_id": "F0001", "part_id": "document"}],
        "notes/summary.md",
    )
    assert output.relative_to(project / "output").as_posix() == "notes/summary.md"
    markdown = output.read_text(encoding="utf-8")
    assert "# F0001 / document" in markdown
    assert "可导出的概括。" in markdown
    assert "F0001-S" in markdown
    os.environ.pop("LLM_API_KEY", None)


def test_export_keeps_full_when_adapter_model_text_changes(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _fragment(
        project,
        summary_id="SUMMARY-FRAGMENT-ALL",
        segment_indexes=[0, 1],
        text="可导出的概括。",
    )
    asyncio.run(
        aggregate_summaries(
            project,
            [{"file_id": "F0001", "part_id": "document"}],
        )
    )
    with sqlite3.connect(project / "project.sqlite") as connection:
        connection.execute(
            "UPDATE segments SET model_source = ? WHERE segment_id = ?",
            ("adapter remapped text", "F0001-S000001"),
        )
        connection.commit()
    output = export_summary_markdown(
        project,
        [{"file_id": "F0001", "part_id": "document"}],
    )
    assert "可导出的概括。" in output.read_text(encoding="utf-8")
    os.environ.pop("LLM_API_KEY", None)


def test_large_fragment_set_uses_reduction_checkpoints(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _fragment(project, summary_id="SUMMARY-FRAGMENT-1", segment_indexes=[0], text="甲" * 7500)
    _fragment(project, summary_id="SUMMARY-FRAGMENT-2", segment_indexes=[1], text="乙" * 7500)
    calls = 0
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        requests.append(json.loads(request.content))
        return _summary_response(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = asyncio.run(
            aggregate_summaries(
                project,
                [{"file_id": "F0001", "part_id": "document"}],
                http_client=client,
            )
        )
    finally:
        asyncio.run(client.aclose())
        os.environ.pop("LLM_API_KEY", None)
    assert calls >= 3
    assert result["boundaries"][0]["origin"] == "llm"
    assert len(read_content_summaries(project, kind="reduction", status="completed")) >= 2
    assert all(
        "refs" not in summary
        for request in requests
        for summary in json.loads(request["messages"][1]["content"])["summaries"]
    )


def test_minimal_aggregation_request_over_budget_fails_without_full_result(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _fragment(project, summary_id="SUMMARY-FRAGMENT-1", segment_indexes=[0], text="甲" * 100000)
    _fragment(project, summary_id="SUMMARY-FRAGMENT-2", segment_indexes=[1], text="乙" * 100000)
    with pytest.raises(UsageError, match="最小请求仍超过"):
        asyncio.run(
            aggregate_summaries(
                project,
                [{"file_id": "F0001", "part_id": "document"}],
            )
        )
    assert read_content_summaries(project, kind="full", status="completed") == []
    os.environ.pop("LLM_API_KEY", None)


def test_recursive_reduction_reports_non_convergence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    _fragment(project, summary_id="SUMMARY-FRAGMENT-1", segment_indexes=[0], text="甲" * 100)
    _fragment(project, summary_id="SUMMARY-FRAGMENT-2", segment_indexes=[1], text="乙" * 100)
    original_load = __import__("app.summary_aggregation", fromlist=["load_project_config"]).load_project_config

    def tiny_config(project_path: Path, *, stage: str) -> dict[str, object]:
        config = original_load(project_path, stage=stage)
        config["llm"]["context_window_tokens"] = 100
        config["llm"]["context_safety_margin_tokens"] = 0
        return config

    monkeypatch.setattr("app.summary_aggregation.load_project_config", tiny_config)
    with pytest.raises(UsageError, match="不收敛"):
        asyncio.run(
            aggregate_summaries(
                project,
                [{"file_id": "F0001", "part_id": "document"}],
            )
        )
    assert read_content_summaries(project, kind="full", status="completed") == []
    os.environ.pop("LLM_API_KEY", None)


@pytest.mark.asyncio
async def test_cancelled_aggregation_can_restart_without_damaging_old_full(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _fragment(project, summary_id="SUMMARY-FRAGMENT-1", segment_indexes=[0], text="Alice 出现。")
    _fragment(project, summary_id="SUMMARY-FRAGMENT-2", segment_indexes=[1], text="Bob 挥手。")

    async def slow(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return _summary_response(_)

    client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    try:
        task = asyncio.create_task(
            aggregate_summaries(
                project,
                [{"file_id": "F0001", "part_id": "document"}],
                http_client=client,
            )
        )
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await client.aclose()
    assert read_content_summaries(project, kind="full", status="completed") == []
    assert read_summary_runs(project, mode="aggregation")[0]["status"] == "interrupted"

    client = httpx.AsyncClient(transport=httpx.MockTransport(_summary_response))
    try:
        result = await aggregate_summaries(
            project,
            [{"file_id": "F0001", "part_id": "document"}],
            http_client=client,
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)
    assert result["completed"] == 1
    assert read_content_summaries(project, kind="full", status="completed")


@pytest.mark.asyncio
async def test_web_task_manager_runs_content_summary_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    _fragment(project, summary_id="SUMMARY-FRAGMENT-ALL", segment_indexes=[0, 1], text="已有概括。")

    async def fake_aggregate(project: Path, selected: list[dict[str, str]], **kwargs: object) -> dict[str, object]:
        del project, kwargs
        return {"selected": len(selected), "completed": len(selected), "failed": 0, "pending": 0}

    monkeypatch.setattr("app.web_tasks.aggregate_summaries", fake_aggregate)
    manager = WebTaskManager(max_active_projects=1)
    state = await manager.start(
        project,
        "content_summary",
        scope=Scope(),
        reuse_mixed_fingerprints=False,
        run_action=None,
        summary_selection=[{"file_id": "F0001", "part_id": "document"}],
    )
    await manager.tasks[state["task_id"]].asyncio_task
    assert manager.get(state["task_id"])["status"] == "completed"
