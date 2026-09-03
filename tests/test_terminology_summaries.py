from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from app.errors import ConfigError, UsageError
from app.execution import Scope
from app.project import (
    add_project_files,
    init_project,
    remove_project_files,
    reorder_project_files,
)
from app.sqlite_storage import (
    read_content_summaries,
    read_files,
    read_json,
    read_jsonl,
    read_segments,
    read_summary_participation,
    read_summary_runs,
    replace_source,
    write_summary_participation,
    write_json,
    record_header,
    write_content_summary,
)
from app.stage_terminology import _digest, run_terminology
from app.term_library import load_terms, publish_partial_terms
from tests.helpers import llm_jsonl
from tests.test_foundation import make_app_root


def _project(tmp_path: Path, text: str = "Alice entered.\nBob waved.") -> Path:
    source = tmp_path / "source.txt"
    source.write_text(text, encoding="utf-8-sig")
    project, _ = init_project(
        [str(source)],
        name="demo",
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    os.environ["LLM_API_KEY"] = "test"
    return project


def _joint_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    payload = json.loads(body["messages"][1]["content"])
    source_segments = payload["source_segments"]
    records = [
        {
            "type": "summary",
            "text": "人物依次出现并行动。",
            "refs": [str(i) for i in range(1, len(source_segments) + 1)],
        },
        {"type": "term", "source": "Alice", "category": "人物"},
        {"type": "term", "source": "Bob", "category": "人物"},
    ]
    return httpx.Response(
        200, json={"choices": [{"message": {"content": llm_jsonl(records)}}]}
    )


def _write_summary(project: Path, file_id: str, summary_id: str) -> None:
    project_id = read_json(project, project / "project.json")["project_id"]
    write_content_summary(
        project,
        record_header(
            "content_summary",
            str(project_id),
            record_id=summary_id,
            kind="fragment",
            file_id=file_id,
            part_id="document",
            status="completed",
            text=f"Summary for {file_id}",
            source_range={"file_id": file_id, "part_id": "document", "segment_ids": []},
            source_digest=f"sha256:{summary_id}",
            input_digest=f"sha256:{summary_id}-input",
            prompt_digest="sha256:prompt",
            model="test-model",
        ),
    )


@pytest.mark.asyncio
async def test_summary_opt_in_uses_joint_request_and_persists_fragment(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "document", "selected": True}],
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(_joint_handler))
    try:
        result = await run_terminology(
            project,
            Scope(),
            http_client=client,
            include_summaries=True,
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)

    assert result["published"] is True
    summaries = read_content_summaries(project, kind="fragment")
    assert len(summaries) == 1
    assert summaries[0]["status"] == "completed"
    assert summaries[0]["file_id"] == "F0001"
    assert summaries[0]["part_id"] == "document"
    assert summaries[0]["refs"] == ["1", "2"]
    slices = summaries[0]["source_range"]["segments"]
    assert len({item["slice_id"] for item in slices}) == len(slices)
    assert all(item["segment_id"].startswith("F0001-S") for item in slices)
    assert all(item["original_segment_id"] == item["segment_id"] for item in slices)
    assert all(item["source_digest"].startswith("sha256:") for item in slices)
    assert all(item["model_text_digest"].startswith("sha256:") for item in slices)
    assert all("CHK-" not in item["slice_id"] for item in slices)
    manifest = read_json(project, project / "runs" / result["run_id"] / "manifest.json")
    assert manifest["summary_participation"] == [
        {"file_id": "F0001", "part_id": "document", "selected": True}
    ]
    assert load_terms(project)["terms"]


@pytest.mark.asyncio
async def test_summary_backfill_does_not_touch_terminology_records(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, "Alice entered.")

    def terms_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": llm_jsonl(
                                [
                                    {
                                        "type": "term",
                                        "source": "Alice",
                                        "category": "人物",
                                    }
                                ]
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(terms_handler))
    try:
        await run_terminology(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)
    before_scans = read_jsonl(project, project / "terminology" / "scans.jsonl")
    before_candidates = read_jsonl(
        project, project / "terminology" / "candidates.jsonl"
    )
    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "document", "selected": True}],
    )

    def summary_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert 'type="summary"' in body["messages"][0]["content"]
        assert 'type="term"' not in body["messages"][0]["content"]
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
                                        "text": "Alice 进入。",
                                        "refs": ["1"],
                                    }
                                ]
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(summary_handler))
    os.environ["LLM_API_KEY"] = "test"
    try:
        result = await run_terminology(
            project,
            Scope(),
            http_client=client,
            include_summaries=True,
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)

    assert result["published"] is False
    assert read_jsonl(project, project / "terminology" / "scans.jsonl") == before_scans
    assert (
        read_jsonl(project, project / "terminology" / "candidates.jsonl")
        == before_candidates
    )
    assert load_terms(project)["terms"][0]["source"] == "Alice"
    assert read_summary_participation(project)[0]["selected"] is True
    assert read_content_summaries(project, kind="fragment")[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_summary_opt_in_rejects_cross_boundary_batching(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "cross_boundary_batching = []",
            'cross_boundary_batching = ["terminology"]',
        ),
        encoding="utf-8",
    )
    with pytest.raises(UsageError, match="cross_boundary_batching"):
        await run_terminology(project, Scope(), include_summaries=True)
    os.environ.pop("LLM_API_KEY", None)


@pytest.mark.asyncio
async def test_standard_terminology_keeps_cross_boundary_batching(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, "Alice entered.")
    second = tmp_path / "second.txt"
    second.write_text("Bob waved.", encoding="utf-8")
    add_project_files(project, [str(second)])
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "cross_boundary_batching = []",
            'cross_boundary_batching = ["terminology"]',
        ),
        encoding="utf-8",
    )
    requested: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        payload = json.loads(body["messages"][1]["content"])
        requested.append(payload["source_segments"])
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl([])}}]},
        )

    os.environ["LLM_API_KEY"] = "test"
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await run_terminology(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)
    assert result["failed"] == 0
    assert requested == [["Alice entered.", "Bob waved."]]


@pytest.mark.asyncio
async def test_joint_partial_response_retries_only_failed_class(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, "Alice entered.")
    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "document", "selected": True}],
    )
    modes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt = body["messages"][0]["content"]
        if 'type="summary"' in prompt and 'type="term"' in prompt:
            modes.append("joint")
            records = [
                {"type": "summary", "text": "Alice 进入。", "refs": ["1"]},
                {"type": "term", "source": "Missing", "category": "无效"},
            ]
        else:
            modes.append("terms-only")
            records = [{"type": "term", "source": "Alice", "category": "人物"}]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await run_terminology(
            project,
            Scope(),
            http_client=client,
            include_summaries=True,
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)

    assert modes == ["joint", "terms-only"]
    assert result["failed"] == 0
    assert result["published"] is True
    assert len(read_content_summaries(project, status="completed")) == 1


@pytest.mark.asyncio
async def test_joint_summary_retry_updates_summary_run_prompt_digest(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, "Alice entered.")
    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "document", "selected": True}],
    )
    prompts: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt_text = body["messages"][0]["content"]
        if 'type="summary"' in prompt_text and 'type="term"' in prompt_text:
            prompts["joint"] = prompt_text
            records = [{"type": "term", "source": "Alice", "category": "人物"}]
        else:
            prompts["summary-only"] = prompt_text
            records = [{"type": "summary", "text": "Alice 进入。", "refs": ["1"]}]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
        )

    os.environ["LLM_API_KEY"] = "test"
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await run_terminology(
            project, Scope(), http_client=client, include_summaries=True
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)

    assert result["failed"] == 0
    assert prompts["joint"] != prompts["summary-only"]
    run = next(
        item
        for item in read_summary_runs(project)
        if item["run_id"] == result["run_id"]
    )
    assert run["prompt_digest"] == _digest(prompts["summary-only"])


@pytest.mark.asyncio
async def test_completed_terms_and_summary_are_reused_without_new_request(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, "Alice entered.")
    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "document", "selected": True}],
    )

    def first_handler(_request: httpx.Request) -> httpx.Response:
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
                                        "text": "Alice 进入。",
                                        "refs": ["1"],
                                    },
                                    {
                                        "type": "term",
                                        "source": "Alice",
                                        "category": "人物",
                                    },
                                ]
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(first_handler))
    try:
        await run_terminology(
            project, Scope(), http_client=client, include_summaries=True
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)

    os.environ["LLM_API_KEY"] = "test"
    calls = 0

    def fail_if_called(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("已完成的术语和概括不应再次请求模型")

    client = httpx.AsyncClient(transport=httpx.MockTransport(fail_if_called))
    try:
        result = await run_terminology(
            project, Scope(), http_client=client, include_summaries=True
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)
    assert calls == 0
    assert result["failed"] == 0
    assert result["pending"] == 0


def test_source_changes_keep_summary_artifacts_but_mark_deleted_boundary_stale(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, "first")
    _write_summary(project, "F0001", "SUMMARY-FIRST")
    second = tmp_path / "second.txt"
    second.write_text("second", encoding="utf-8")
    add_project_files(project, [str(second)])
    _write_summary(project, "F0002", "SUMMARY-SECOND")

    remove_project_files(project, ["F0002"])
    summaries = {item["record_id"]: item for item in read_content_summaries(project)}
    assert summaries["SUMMARY-FIRST"]["source_changed"] is False
    assert summaries["SUMMARY-SECOND"]["source_changed"] is True


def test_reordering_files_marks_existing_summary_boundaries_stale(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, "first")
    second = tmp_path / "second.txt"
    second.write_text("second", encoding="utf-8")
    add_project_files(project, [str(second)])
    _write_summary(project, "F0001", "SUMMARY-FIRST")
    _write_summary(project, "F0002", "SUMMARY-SECOND")

    reorder_project_files(project, ["F0002", "F0001"])
    summaries = {item["record_id"]: item for item in read_content_summaries(project)}
    assert summaries["SUMMARY-FIRST"]["source_changed"] is True
    assert summaries["SUMMARY-SECOND"]["source_changed"] is True


@pytest.mark.asyncio
async def test_summary_run_tracks_only_missing_summary_requests(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, "Alice entered.")
    second = tmp_path / "second.txt"
    second.write_text("Bob waved.", encoding="utf-8")
    add_project_files(project, [str(second)])

    def terms_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": llm_jsonl(
                                [
                                    {
                                        "type": "term",
                                        "source": "Alice",
                                        "category": "人物",
                                    }
                                ]
                            )
                        }
                    }
                ]
            },
        )

    os.environ["LLM_API_KEY"] = "test"
    client = httpx.AsyncClient(transport=httpx.MockTransport(terms_handler))
    try:
        await run_terminology(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)

    def summary_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        source_segments = json.loads(payload["messages"][1]["content"])[
            "source_segments"
        ]
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
                                        "text": "片段概括。",
                                        "refs": [
                                            str(i)
                                            for i in range(1, len(source_segments) + 1)
                                        ],
                                    }
                                ]
                            )
                        }
                    }
                ]
            },
        )

    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "document", "selected": True}],
    )
    os.environ["LLM_API_KEY"] = "test"
    client = httpx.AsyncClient(transport=httpx.MockTransport(summary_handler))
    try:
        await run_terminology(
            project, Scope(), http_client=client, include_summaries=True
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)

    write_summary_participation(
        project,
        [
            {"file_id": "F0001", "part_id": "document", "selected": True},
            {"file_id": "F0002", "part_id": "document", "selected": True},
        ],
    )
    os.environ["LLM_API_KEY"] = "test"
    client = httpx.AsyncClient(transport=httpx.MockTransport(summary_handler))
    try:
        result = await run_terminology(
            project, Scope(), http_client=client, include_summaries=True
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)

    run = next(
        item
        for item in read_summary_runs(project)
        if item["run_id"] == result["run_id"]
    )
    assert [item["file_id"] for item in run["source_ranges"]] == ["F0002"]
    assert [item["segment_id"] for item in run["source_ranges"][0]["segments"]] == [
        "F0002-S000001"
    ]


@pytest.mark.asyncio
async def test_summary_runtime_split_persists_stable_slice_provenance_and_reuses(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, "ABCDEFGH")
    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "document", "selected": True}],
    )
    requested_sources: list[str] = []

    def split_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        source = payload["source_segments"][0]
        requested_sources.append(source)
        if len(source) > 3:
            return httpx.Response(
                400,
                text="context_length_exceeded: maximum context tokens",
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": llm_jsonl(
                                [{"type": "summary", "text": "片段。", "refs": ["1"]}]
                            )
                        }
                    }
                ]
            },
        )

    os.environ["LLM_API_KEY"] = "test"
    client = httpx.AsyncClient(transport=httpx.MockTransport(split_handler))
    try:
        first = await run_terminology(
            project, Scope(), http_client=client, include_summaries=True
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)

    assert first["failed"] == 0
    assert len(requested_sources) > 1
    summaries = read_content_summaries(project, kind="fragment", status="completed")
    slices = [
        item for summary in summaries for item in summary["source_range"]["segments"]
    ]
    assert (
        "".join(
            item["source"]
            for item in sorted(slices, key=lambda value: value["slice_index"])
        )
        == "ABCDEFGH"
    )
    assert len({item["slice_id"] for item in slices}) == len(slices)
    assert all(item["segment_id"] == "F0001-S000001" for item in slices)
    assert all("CHK-" not in item["slice_id"] for item in slices)
    run = next(
        item for item in read_summary_runs(project) if item["run_id"] == first["run_id"]
    )
    run_slices = [
        item
        for source_range in run["source_ranges"]
        for item in source_range["segments"]
    ]
    assert {item["slice_id"] for item in run_slices} == {
        item["slice_id"] for item in slices
    }
    assert all(item["source"] != "ABCDEFGH" for item in run_slices)

    def fail_if_called(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("稳定摘要切片应被复用")

    client = httpx.AsyncClient(transport=httpx.MockTransport(fail_if_called))
    os.environ["LLM_API_KEY"] = "test"
    try:
        second = await run_terminology(
            project, Scope(), http_client=client, include_summaries=True
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)
    assert second["pending"] == 0


@pytest.mark.asyncio
async def test_external_model_source_refuses_unverifiable_runtime_split(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, "ABCDEFGH")
    files = read_files(project)
    segments = [dict(item) for item in read_segments(project)]
    segments[0]["model_source"] = "opaque model text"
    replace_source(
        project,
        files,
        segments,
        read_json(project, project / "project.json"),
    )
    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "document", "selected": True}],
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            text="context_length_exceeded: maximum context tokens",
        )

    os.environ["LLM_API_KEY"] = "test"
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ConfigError, match="model_source.*切片"):
            await run_terminology(
                project, Scope(), http_client=client, include_summaries=True
            )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)
    assert read_summary_runs(project)[0]["status"] == "failed"
    assert read_content_summaries(project, kind="fragment") == []


@pytest.mark.asyncio
async def test_summary_prompt_digest_changes_when_adapter_requirements_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path, "Alice entered.")
    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "document", "selected": True}],
    )
    from app import stage_terminology as terminology_module

    original_context = terminology_module._project_context
    requirement = "Adapter requirement A"

    def context_with_requirement(
        project_path: Path, *, stage: str | None = None
    ) -> tuple[dict, dict, list[dict], list[dict]]:
        config, metadata, files, segments = original_context(project_path, stage=stage)
        config["_document_adapter_prompt_requirements"] = {
            "F0001": {"zh-CN": requirement}
        }
        return config, metadata, files, segments

    monkeypatch.setattr(
        terminology_module, "_project_context", context_with_requirement
    )

    def summary_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        source_segments = json.loads(payload["messages"][1]["content"])[
            "source_segments"
        ]
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
                                        "text": "概括。",
                                        "refs": [
                                            str(i)
                                            for i in range(1, len(source_segments) + 1)
                                        ],
                                    }
                                ]
                            )
                        }
                    }
                ]
            },
        )

    os.environ["LLM_API_KEY"] = "test"
    client = httpx.AsyncClient(transport=httpx.MockTransport(summary_handler))
    try:
        first = await run_terminology(
            project, Scope(), http_client=client, include_summaries=True
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)

    requirement = "Adapter requirement B"
    calls = 0

    def changed_summary_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return summary_handler(request)

    os.environ["LLM_API_KEY"] = "test"
    client = httpx.AsyncClient(transport=httpx.MockTransport(changed_summary_handler))
    try:
        await run_terminology(
            project,
            Scope(),
            http_client=client,
            include_summaries=True,
            reuse_mixed_fingerprints=True,
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)
    assert calls == 1
    assert len(read_summary_runs(project)) == 2
    assert any(item["run_id"] != first["run_id"] for item in read_summary_runs(project))


@pytest.mark.asyncio
async def test_summary_external_failure_is_counted_as_external_error(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, "Alice entered.")

    def terms_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": llm_jsonl(
                                [
                                    {
                                        "type": "term",
                                        "source": "Alice",
                                        "category": "人物",
                                    }
                                ]
                            )
                        }
                    }
                ]
            },
        )

    os.environ["LLM_API_KEY"] = "test"
    client = httpx.AsyncClient(transport=httpx.MockTransport(terms_handler))
    try:
        await run_terminology(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)
    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "document", "selected": True}],
    )

    def failing_summary_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="upstream unavailable")

    os.environ["LLM_API_KEY"] = "test"
    client = httpx.AsyncClient(transport=httpx.MockTransport(failing_summary_handler))
    try:
        result = await run_terminology(
            project, Scope(), http_client=client, include_summaries=True
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)
    assert result["failed"] == 1
    assert result["failure_counts"] == {"external_error": 1}


@pytest.mark.asyncio
async def test_joint_external_failure_is_counted_once_per_requested_segment(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, "Alice entered.")
    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "document", "selected": True}],
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="upstream unavailable")

    os.environ["LLM_API_KEY"] = "test"
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await run_terminology(
            project, Scope(), http_client=client, include_summaries=True
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)
    assert result["failed"] == 1
    assert result["failure_counts"] == {"external_error": 1}


@pytest.mark.asyncio
async def test_partial_published_summary_backfill_reuses_existing_term_task(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, "Alice entered.")

    def terms_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": llm_jsonl(
                                [
                                    {
                                        "type": "term",
                                        "source": "Alice",
                                        "category": "人物",
                                    }
                                ]
                            )
                        }
                    }
                ]
            },
        )

    os.environ["LLM_API_KEY"] = "test"
    client = httpx.AsyncClient(transport=httpx.MockTransport(terms_handler))
    try:
        await run_terminology(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)
    active = read_json(project, project / "terminology" / "active_task.json")
    active["status"] = "active"
    write_json(project, project / "terminology" / "active_task.json", active)
    published = publish_partial_terms(project)
    active_before = read_json(project, project / "terminology" / "active_task.json")
    candidates_before = read_jsonl(
        project, project / "terminology" / "candidates.jsonl"
    )
    terms_before = load_terms(project)

    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "document", "selected": True}],
    )
    modes: list[str] = []

    def summary_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        modes.append(body["messages"][0]["content"])
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
                                        "text": "Alice 进入。",
                                        "refs": ["1"],
                                    }
                                ]
                            )
                        }
                    }
                ]
            },
        )

    os.environ["LLM_API_KEY"] = "test"
    client = httpx.AsyncClient(transport=httpx.MockTransport(summary_handler))
    try:
        result = await run_terminology(
            project, Scope(), http_client=client, include_summaries=True
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)

    active_after = read_json(project, project / "terminology" / "active_task.json")
    assert published["active_task_id"] == active_before["active_task_id"]
    assert active_after["active_task_id"] == active_before["active_task_id"]
    assert len(modes) == 1
    assert 'type="summary"' in modes[0]
    assert 'type="term"' not in modes[0]
    assert result["failed"] == 0
    assert (
        read_jsonl(project, project / "terminology" / "candidates.jsonl")
        == candidates_before
    )
    assert load_terms(project) == terms_before


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "error"])
async def test_summary_run_is_not_left_running_on_cancel_or_exception(
    tmp_path: Path,
    failure: str,
) -> None:
    project = _project(tmp_path, "Alice entered.")
    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "document", "selected": True}],
    )

    def failing_handler(_request: httpx.Request) -> httpx.Response:
        if failure == "cancel":
            raise asyncio.CancelledError
        raise RuntimeError("test summary failure")

    os.environ["LLM_API_KEY"] = "test"
    client = httpx.AsyncClient(transport=httpx.MockTransport(failing_handler))
    try:
        with pytest.raises(
            asyncio.CancelledError if failure == "cancel" else RuntimeError
        ):
            await run_terminology(
                project, Scope(), http_client=client, include_summaries=True
            )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)

    runs = read_summary_runs(project)
    assert len(runs) == 1
    assert runs[0]["status"] == ("interrupted" if failure == "cancel" else "failed")
