from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from app.errors import ConfigError, UsageError
from app.execution import Scope
from app.main import run
from app.project import (
    add_project_files,
    init_project,
    remove_project_files,
    reorder_project_files,
)
from app.sqlite_storage import (
    read_content_summaries,
    read_json,
    read_jsonl,
    read_summary_participation,
    read_summary_runs,
    record_header,
    write_content_summary,
    write_json,
    write_summary_participation,
)
from app.stage_terminology import _digest, run_terminology
from app.summary_aggregation import export_summary_markdown
from app.term_library import load_terms, publish_partial_terms
from app.web_tasks import task_options
from tests.helpers import llm_jsonl
from tests.test_document_adapter_contract import (
    RecordDocumentAdapter,
    register_plugin,
    write_record,
)
from tests.test_documents import make_epub
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
    summary_text = (
        "<em1>Alice</em1> 进入。"
        if any("<em1>" in source for source in source_segments)
        else "人物依次出现并行动。"
    )
    records = [
        {
            "type": "summary",
            "text": summary_text,
            "refs": [str(i) for i in range(1, len(source_segments) + 1)],
        }
    ]
    for source in ("Alice", "Bob"):
        if any(source in value for value in source_segments):
            records.append({"type": "term", "source": source, "category": "人物"})
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


def _epub_marker_project(tmp_path: Path) -> Path:
    source = tmp_path / "markers.epub"
    make_epub(
        source,
        xhtml=(
            b'<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            b"<p><em>Alice</em> entered.</p>"
            b"</body></html>"
        ),
    )
    project, _ = init_project(
        [str(source)],
        name="markers",
        document_adapter_id="epub",
        adapter_options={
            "epub": {
                "inline_format_mode": "markers",
                "inline_format_policy": "strict",
            }
        },
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    return project


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
    full = read_content_summaries(project, kind="full", status="completed")
    assert len(full) == 1
    assert full[0]["text"] == summaries[0]["text"]
    assert full[0]["refs"] == ["F0001-S000001", "F0001-S000002"]
    assert full[0]["provenance"] == {
        "origin": "adopted_fragment",
        "artifact_ids": [summaries[0]["record_id"]],
        "source_ranges": [summaries[0]["source_range"]],
    }
    assert task_options(project, "content_summary")["completed"] == 1
    exported = export_summary_markdown(
        project,
        [{"file_id": "F0001", "part_id": "document"}],
        "auto-summary.md",
    )
    assert exported.read_text(encoding="utf-8").startswith("# 内容概括")
    manifest = read_json(project, project / "runs" / result["run_id"] / "manifest.json")
    assert manifest["summary_participation"] == [
        {"file_id": "F0001", "part_id": "document", "selected": True}
    ]
    assert load_terms(project)["terms"]


@pytest.mark.asyncio
async def test_joint_partitioned_summaries_persist_separate_fragments(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "document", "selected": True}],
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        records = [
            {"type": "summary", "text": "Alice 进入。", "refs": ["1"]},
            {"type": "summary", "text": "Bob 挥手。", "refs": ["2"]},
        ]
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

    assert result["failed"] == 0
    summaries = read_content_summaries(project, kind="fragment", status="completed")
    summaries.sort(key=lambda item: item["source_range"]["segment_ids"][0])
    assert [item["text"] for item in summaries] == ["Alice 进入。", "Bob 挥手。"]
    assert [item["refs"] for item in summaries] == [["1"], ["1"]]
    assert [
        item["source_range"]["segment_ids"] for item in summaries
    ] == [["F0001-S000001"], ["F0001-S000002"]]
    assert read_content_summaries(project, kind="full", status="completed") == []


@pytest.mark.asyncio
async def test_forced_full_cover_summary_replaces_auto_adopted_full(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "document", "selected": True}],
    )

    first_client = httpx.AsyncClient(transport=httpx.MockTransport(_joint_handler))
    try:
        await run_terminology(
            project,
            Scope(),
            http_client=first_client,
            include_summaries=True,
        )
    finally:
        await first_client.aclose()

    def forced_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        payload = json.loads(body["messages"][1]["content"])
        source_segments = payload["source_segments"]
        records: list[dict[str, object]] = [
            {
                "type": "summary",
                "text": "强制重做后的概括。",
                "refs": [str(index) for index in range(1, len(source_segments) + 1)],
            }
        ]
        for source in ("Alice", "Bob"):
            if any(source in value for value in source_segments):
                records.append({"type": "term", "source": source, "category": "人物"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(forced_handler))
    try:
        result = await run_terminology(
            project,
            Scope(force=True),
            http_client=client,
            include_summaries=True,
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)

    assert result["failed"] == 0
    full = read_content_summaries(project, kind="full")
    assert [(item["status"], item["text"]) for item in full] == [
        ("stale", "人物依次出现并行动。"),
        ("completed", "强制重做后的概括。"),
    ]


@pytest.mark.asyncio
async def test_forced_summary_redo_clears_old_fragments_before_partitioned_response(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "document", "selected": True}],
    )

    first_client = httpx.AsyncClient(transport=httpx.MockTransport(_joint_handler))
    try:
        await run_terminology(
            project,
            Scope(),
            http_client=first_client,
            include_summaries=True,
        )
    finally:
        await first_client.aclose()

    old = read_content_summaries(project, kind="fragment", status="completed")
    assert [item["text"] for item in old] == ["人物依次出现并行动。"]

    modes: list[str] = []

    def forced_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt = body["messages"][0]["content"]
        payload = json.loads(body["messages"][1]["content"])
        source_segments = payload["source_segments"]
        joint = 'type="summary"' in prompt
        modes.append("joint" if joint else "terms-only")
        records: list[dict[str, object]] = []
        if joint:
            records.extend(
                [
                    {"type": "summary", "text": "Alice 新概括。", "refs": ["1"]},
                    {"type": "summary", "text": "Bob 新概括。", "refs": ["2"]},
                ]
            )
        for source in ("Alice", "Bob"):
            if any(source in value for value in source_segments):
                records.append({"type": "term", "source": source, "category": "人物"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(forced_handler))
    try:
        result = await run_terminology(
            project,
            Scope(force=True),
            http_client=client,
            include_summaries=True,
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)

    assert result["failed"] == 0
    assert "joint" in modes
    summaries = read_content_summaries(project, kind="fragment", status="completed")
    assert {item["text"] for item in summaries} == {"Alice 新概括。", "Bob 新概括。"}


@pytest.mark.asyncio
async def test_forced_summary_redo_clears_only_selected_boundaries(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("Alice entered.", encoding="utf-8-sig")
    second.write_text("Bob waved.", encoding="utf-8-sig")
    project, _ = init_project(
        [str(first), str(second)],
        name="demo",
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    os.environ["LLM_API_KEY"] = "test"
    write_summary_participation(
        project,
        [
            {"file_id": "F0001", "part_id": "document", "selected": True},
            {"file_id": "F0002", "part_id": "document", "selected": True},
        ],
    )

    def initial_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        payload = json.loads(body["messages"][1]["content"])
        source_segments = payload["source_segments"]
        records: list[dict[str, object]] = [
            {
                "type": "summary",
                "text": "旧概括。",
                "refs": [str(index) for index in range(1, len(source_segments) + 1)],
            }
        ]
        for source in ("Alice", "Bob"):
            if any(source in value for value in source_segments):
                records.append({"type": "term", "source": source, "category": "人物"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(initial_handler))
    try:
        await run_terminology(
            project,
            Scope(),
            http_client=client,
            include_summaries=True,
        )
    finally:
        await client.aclose()

    write_summary_participation(
        project,
        [
            {"file_id": "F0001", "part_id": "document", "selected": True},
            {"file_id": "F0002", "part_id": "document", "selected": False},
        ],
    )

    def forced_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt = body["messages"][0]["content"]
        payload = json.loads(body["messages"][1]["content"])
        source_segments = payload["source_segments"]
        if 'type="summary"' in prompt:
            records: list[dict[str, object]] = [
                {"type": "summary", "text": "新概括。", "refs": ["1"]}
            ]
        else:
            records = []
        for source in ("Alice", "Bob"):
            if any(source in value for value in source_segments):
                records.append({"type": "term", "source": source, "category": "人物"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(forced_handler))
    try:
        result = await run_terminology(
            project,
            Scope(force=True),
            http_client=client,
            include_summaries=True,
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)

    assert result["failed"] == 0
    summaries = read_content_summaries(project, kind="fragment", status="completed")
    assert {
        (item["file_id"], item["part_id"], item["text"])
        for item in summaries
    } == {
        ("F0001", "document", "新概括。"),
        ("F0002", "document", "旧概括。"),
    }


@pytest.mark.asyncio
async def test_forced_summary_redo_does_not_restore_old_fragments_after_failure(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "document", "selected": True}],
    )
    first_client = httpx.AsyncClient(transport=httpx.MockTransport(_joint_handler))
    try:
        await run_terminology(
            project,
            Scope(),
            http_client=first_client,
            include_summaries=True,
        )
    finally:
        await first_client.aclose()

    def invalid_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl([{"type": "end"}])}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(invalid_handler))
    try:
        result = await run_terminology(
            project,
            Scope(force=True),
            http_client=client,
            include_summaries=True,
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)

    assert result["failed"] > 0
    assert read_content_summaries(
        project,
        kind="fragment",
        status="completed",
    ) == []
    failed = read_content_summaries(project, kind="fragment", status="failed")
    assert failed
    assert all(item.get("text") is None for item in failed)


@pytest.mark.asyncio
async def test_forced_summary_dry_run_keeps_existing_fragments(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "document", "selected": True}],
    )
    first_client = httpx.AsyncClient(transport=httpx.MockTransport(_joint_handler))
    try:
        await run_terminology(
            project,
            Scope(),
            http_client=first_client,
            include_summaries=True,
        )
    finally:
        await first_client.aclose()

    def fail_if_called(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("dry-run 不应调用模型")

    client = httpx.AsyncClient(transport=httpx.MockTransport(fail_if_called))
    try:
        await run_terminology(
            project,
            Scope(force=True, dry_run=True),
            http_client=client,
            include_summaries=True,
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)

    summaries = read_content_summaries(project, kind="fragment", status="completed")
    assert [item["text"] for item in summaries] == ["人物依次出现并行动。"]


@pytest.mark.asyncio
async def test_epub_summary_only_uses_fragment_adapter_requirements(
    tmp_path: Path,
) -> None:
    project = _epub_marker_project(tmp_path)

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
        first = await run_terminology(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)
    assert first["failed"] == 0
    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "OEBPS/text/ch1.xhtml", "selected": True}],
    )
    prompts: list[str] = []

    def summary_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompts.append(body["messages"][0]["content"])
        payload = json.loads(body["messages"][1]["content"])
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
                                        "refs": [
                                            str(i)
                                            for i in range(
                                                1, len(payload["source_segments"]) + 1
                                            )
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
        result = await run_terminology(
            project,
            Scope(),
            http_client=client,
            include_summaries=True,
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)
    assert result["failed"] == 0
    assert prompts
    assert "必须保留所有已有标记" not in prompts[0]
    assert "Keep every existing marker" not in prompts[0]
    metadata = json.loads(
        (project / "runs" / result["run_id"] / "prompt_variants.json").read_text(
            encoding="utf-8"
        )
    )
    assert metadata["summary-only"]["requirements"] == []
    snapshot = json.loads(
        (
            project
            / "runs"
            / result["run_id"]
            / "document_adapter_prompt_requirements.json"
        ).read_text(encoding="utf-8")
    )
    assert "必须保留所有已有标记" not in json.dumps(snapshot, ensure_ascii=False)


@pytest.mark.asyncio
async def test_summary_only_fragment_context_uses_terminology_preset(
    tmp_path: Path,
) -> None:
    project = _epub_marker_project(tmp_path)
    app_root = tmp_path / "runtime-global"
    default_preset = json.loads(
        (app_root / "llm_presets" / "default.json").read_text(encoding="utf-8")
    )
    default_preset["preset_id"] = "terminology-only"
    (app_root / "llm_presets" / "terminology-only.json").write_text(
        json.dumps(default_preset), encoding="utf-8"
    )
    config_path = project / "config.toml"
    config_text = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        config_text.replace(
            'preset_terminology = ""', 'preset_terminology = "terminology-only"'
        ),
        encoding="utf-8",
    )

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
        first = await run_terminology(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)
    assert first["failed"] == 0

    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'preset = "default"', 'preset = "missing-global"'
        ),
        encoding="utf-8",
    )

    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "OEBPS/text/ch1.xhtml", "selected": True}],
    )

    def summary_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
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
                                        "refs": [
                                            str(i)
                                            for i in range(
                                                1, len(payload["source_segments"]) + 1
                                            )
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
        result = await run_terminology(
            project,
            Scope(),
            http_client=client,
            include_summaries=True,
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)
    assert result["failed"] == 0
    run_dir = project / "runs" / result["run_id"]
    preset = json.loads((run_dir / "llm_preset.json").read_text(encoding="utf-8"))
    assert preset["preset_id"] == "terminology-only"


@pytest.mark.asyncio
async def test_epub_joint_prompt_combines_only_terminology_adapter_requirements(
    tmp_path: Path,
) -> None:
    project = _epub_marker_project(tmp_path)
    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "OEBPS/text/ch1.xhtml", "selected": True}],
    )
    prompts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompts.append(body["messages"][0]["content"])
        return _joint_handler(request)

    os.environ["LLM_API_KEY"] = "test"
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
    assert result["failed"] == 0
    assert prompts and "必须保留所有已有标记" in prompts[0]
    run_dir = project / "runs" / result["run_id"]
    metadata = json.loads(
        (run_dir / "prompt_variants.json").read_text(encoding="utf-8")
    )
    joint_variant = next(
        value
        for key, value in metadata.items()
        if key.startswith("terms+fragment-summary")
    )
    assert set(metadata) == {
        next(key for key in metadata if key.startswith("terms+fragment-summary"))
    }
    assert joint_variant["requirements"]
    assert "必须保留所有已有标记" in (
        run_dir / joint_variant["path"]
    ).read_text(encoding="utf-8")
    assert not (run_dir / "prompt_variants" / "terms-only.txt").exists()
    assert not (run_dir / "prompt_variants" / "summary-only.txt").exists()
    snapshot = json.loads(
        (run_dir / "document_adapter_prompt_requirements.json").read_text(
            encoding="utf-8"
        )
    )
    assert "必须保留所有已有标记" in json.dumps(snapshot, ensure_ascii=False)


@pytest.mark.asyncio
async def test_joint_run_snapshots_only_requested_prompt_variant(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, "Alice entered.")
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

    run_dir = project / "runs" / result["run_id"]
    variants = run_dir / "prompt_variants"
    assert (variants / "terms-and-fragment-summary.txt").is_file()
    assert not (variants / "terms-only.txt").exists()
    assert not (variants / "summary-only.txt").exists()
    assert (run_dir / "prompt.txt").read_text("utf-8") == (
        variants / "terms-and-fragment-summary.txt"
    ).read_text("utf-8")
    assert (run_dir / "document_adapter_prompt_requirements.json").is_file()
    manifest = read_json(project, run_dir / "manifest.json")
    assert manifest["prompt_languages"] == {"terms+fragment-summary": "zh-CN"}
    summary_run = next(
        item
        for item in read_summary_runs(project)
        if item["run_id"] == result["run_id"]
    )
    assert summary_run["prompt_languages"] == {"terms+fragment-summary": "zh-CN"}


@pytest.mark.asyncio
async def test_mixed_terms_and_joint_run_prefers_joint_primary_snapshot(
    tmp_path: Path,
) -> None:
    project = _epub_marker_project(tmp_path)
    second = tmp_path / "second.epub"
    make_epub(
        second,
        xhtml=(
            b'<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            b"<p><em>Bob</em> waved.</p>"
            b"</body></html>"
        ),
    )
    add_project_files(
        project,
        [str(second)],
        document_adapter_id="epub",
        adapter_options={
            "epub": {
                "inline_format_mode": "markers",
                "inline_format_policy": "strict",
            }
        },
    )
    write_summary_participation(
        project,
        [
            {
                "file_id": "F0002",
                "part_id": "OEBPS/text/ch1.xhtml",
                "selected": True,
            }
        ],
    )
    request_modes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system_prompt = body["messages"][0]["content"]
        payload = json.loads(body["messages"][1]["content"])
        is_joint = 'type="summary"' in system_prompt
        request_modes.append("joint" if is_joint else "terms-only")
        source_segments = payload["source_segments"]
        records: list[dict[str, object]] = []
        if is_joint:
            records.append(
                {
                    "type": "summary",
                    "text": (
                        "<em1>Bob</em1> 挥手。"
                        if "<em1>" in source_segments[0]
                        else "片段概括。"
                    ),
                    "refs": [str(i) for i in range(1, len(source_segments) + 1)],
                }
            )
        records.append(
            {
                "type": "term",
                "source": "Alice" if "Alice" in source_segments[0] else "Bob",
                "category": "人物",
            }
        )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": llm_jsonl(records)}}]}
        )

    os.environ["LLM_API_KEY"] = "test"
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

    assert result["failed"] == 0
    assert sorted(request_modes) == ["joint", "terms-only"]
    run_dir = project / "runs" / result["run_id"]
    manifest = read_json(project, run_dir / "manifest.json")
    assert manifest["primary_mode"] == "terms+fragment-summary"
    metadata = json.loads((run_dir / "prompt_variants.json").read_text("utf-8"))
    joint_key = next(
        key for key in metadata if key.startswith("terms+fragment-summary")
    )
    terms_key = next(key for key in metadata if key.startswith("terms-only"))
    joint_variant = (run_dir / metadata[joint_key]["path"]).read_text(
        encoding="utf-8"
    )
    assert (run_dir / "prompt.txt").read_text(encoding="utf-8") == joint_variant
    assert set(metadata) == {joint_key, terms_key}
    assert {
        entry["primary_mode"] for entry in metadata.values()
    } == {"terms+fragment-summary"}
    assert metadata[joint_key]["requirements"]
    assert metadata[terms_key]["requirements"]
    adapter_snapshot = json.loads(
        (run_dir / "document_adapter_prompt_requirements.json").read_text("utf-8")
    )
    assert "必须保留所有已有标记" in json.dumps(
        adapter_snapshot, ensure_ascii=False
    )


@pytest.mark.asyncio
async def test_joint_run_prompt_variants_rebuild_actual_adapter_prompt(
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
    seen_prompts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_prompts.append(body["messages"][0]["content"])
        return _joint_handler(request)

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

    run_dir = project / "runs" / result["run_id"]
    metadata = json.loads(
        (run_dir / "prompt_variants.json").read_text(encoding="utf-8")
    )
    matching = {
        key: value
        for key, value in metadata.items()
        if value["requirements"] == [requirement]
    }
    assert len(matching) == 1
    for value in matching.values():
        variant = (run_dir / value["path"]).read_text(encoding="utf-8")
        assert requirement in variant
    joint_key = next(
        key for key in matching if key.startswith("terms+fragment-summary")
    )
    joint_variant = (run_dir / matching[joint_key]["path"]).read_text(
        encoding="utf-8"
    )
    variant_texts = {
        (run_dir / value["path"]).read_text(encoding="utf-8")
        for value in matching.values()
    }
    assert set(seen_prompts) <= variant_texts
    assert joint_variant in seen_prompts


@pytest.mark.asyncio
async def test_preflight_interception_does_not_create_unused_prompt_variants(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, "Alice entered.")
    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "document", "selected": True}],
    )
    from app import config as config_module

    preset_path = config_module.APP_ROOT / "llm_presets" / "default.json"
    preset = json.loads(preset_path.read_text(encoding="utf-8"))
    preset["context_window_tokens"] = 128
    preset["context_safety_margin_tokens"] = 0
    preset["max_output_tokens"] = 1
    preset_path.write_text(json.dumps(preset), encoding="utf-8")
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "allow_split_oversized_segment = true",
            "allow_split_oversized_segment = false",
        ),
        encoding="utf-8",
    )
    calls = 0

    def fail_if_called(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("前置校验拦截后不应请求模型")

    os.environ["LLM_API_KEY"] = "test"
    client = httpx.AsyncClient(transport=httpx.MockTransport(fail_if_called))
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

    assert calls == 0
    assert result["failed"] == 1
    run_dir = project / "runs" / result["run_id"]
    assert not (run_dir / "prompt_variants.json").exists()
    assert not (run_dir / "prompt.txt").exists()
    manifest = read_json(project, run_dir / "manifest.json")
    assert "prompt_variants" not in manifest


@pytest.mark.asyncio
async def test_preflight_interception_selects_primary_mode_from_actual_requests(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, ("Alice " * 5000).strip())
    second = tmp_path / "second.txt"
    second.write_text("Bob waved.", encoding="utf-8")
    add_project_files(project, [str(second)])
    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "document", "selected": True}],
    )
    from app import config as config_module

    preset_path = config_module.APP_ROOT / "llm_presets" / "default.json"
    preset = json.loads(preset_path.read_text(encoding="utf-8"))
    preset["context_window_tokens"] = 4096
    preset["context_safety_margin_tokens"] = 0
    preset["max_output_tokens"] = 1
    preset_path.write_text(json.dumps(preset), encoding="utf-8")
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "allow_split_oversized_segment = true",
            "allow_split_oversized_segment = false",
        ),
        encoding="utf-8",
    )
    request_modes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system_prompt = body["messages"][0]["content"]
        request_modes.append(
            "joint" if 'type="summary"' in system_prompt else "terms-only"
        )
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
                                        "source": "Bob",
                                        "category": "人物",
                                    }
                                ]
                            )
                        }
                    }
                ]
            },
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

    assert request_modes == ["terms-only"]
    assert result["failed"] == 1
    run_dir = project / "runs" / result["run_id"]
    manifest = read_json(project, run_dir / "manifest.json")
    assert manifest["primary_mode"] == "terms-only"
    metadata = json.loads((run_dir / "prompt_variants.json").read_text("utf-8"))
    assert set(metadata) == {"terms-only"}
    assert metadata["terms-only"]["primary_mode"] == "terms-only"
    assert (run_dir / "prompt.txt").read_text("utf-8") == (
        run_dir / metadata["terms-only"]["path"]
    ).read_text("utf-8")


@pytest.mark.asyncio
async def test_ordinary_terminology_without_pending_work_does_not_write_empty_prompt(
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
        first = await run_terminology(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)
    assert first["failed"] == 0
    assert first["pending"] == 0
    first_manifest = read_json(
        project, project / "runs" / first["run_id"] / "manifest.json"
    )

    calls = 0

    def fail_if_called(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("没有待处理术语时不应请求模型")

    os.environ["LLM_API_KEY"] = "test"
    client = httpx.AsyncClient(transport=httpx.MockTransport(fail_if_called))
    try:
        second = await run_terminology(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)

    assert calls == 0
    assert second["pending"] == 0
    assert second["run_id"]
    second_manifest = read_json(
        project, project / "runs" / second["run_id"] / "manifest.json"
    )
    assert (
        second_manifest["stage_fingerprint"]
        == first_manifest["stage_fingerprint"]
    )
    assert not (
        project / "runs" / second["run_id"] / "prompt.txt"
    ).exists()


@pytest.mark.asyncio
async def test_summary_only_run_does_not_read_missing_terminology_prompt(
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
        first = await run_terminology(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)
    assert first["failed"] == 0

    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "document", "selected": True}],
    )
    for language in ("zh-CN", "en"):
        (project / "prompts" / f"terminology.{language}.middle.txt").unlink()
    seen_prompts: list[str] = []

    def summary_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_prompts.append(body["messages"][0]["content"])
        assert 'type="term"' not in seen_prompts[-1]
        source_segments = json.loads(body["messages"][1]["content"])[
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
                                        "text": "Alice 进入。",
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
        result = await run_terminology(
            project,
            Scope(),
            http_client=client,
            reuse_mixed_fingerprints=True,
            include_summaries=True,
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)

    assert result["failed"] == 0
    assert seen_prompts
    assert read_content_summaries(project, kind="fragment", status="completed")
    manifest = read_json(project, project / "runs" / result["run_id"] / "manifest.json")
    assert set(manifest["prompt_variants"]) == {"summary-only"}
    assert manifest["prompt_languages"] == {"summary-only": "zh-CN"}


@pytest.mark.asyncio
async def test_summary_only_backfill_does_not_read_existing_terminology_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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

    client = httpx.AsyncClient(transport=httpx.MockTransport(terms_handler))
    try:
        first = await run_terminology(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)
    assert first["failed"] == 0
    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "document", "selected": True}],
    )

    original_read_bytes = Path.read_bytes
    original_read_text = Path.read_text

    def forbid_terminology_bytes(path: Path, *args: object, **kwargs: object) -> bytes:
        if path.parent.name == "prompts" and path.name.startswith("terminology."):
            raise AssertionError("summary-only 不应读取 terminology Prompt")
        return original_read_bytes(path, *args, **kwargs)

    def forbid_terminology_text(path: Path, *args: object, **kwargs: object) -> str:
        if path.parent.name == "prompts" and path.name.startswith("terminology."):
            raise AssertionError("summary-only 不应读取 terminology Prompt")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", forbid_terminology_bytes)
    monkeypatch.setattr(Path, "read_text", forbid_terminology_text)

    os.environ["LLM_API_KEY"] = "test"

    def summary_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
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
                                        "refs": [
                                            str(i)
                                            for i in range(
                                                1, len(payload["source_segments"]) + 1
                                            )
                                        ],
                                    }
                                ]
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(summary_handler))
    try:
        result = await run_terminology(
            project,
            Scope(),
            http_client=client,
            include_summaries=True,
            reuse_mixed_fingerprints=True,
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)
    assert result["failed"] == 0
    manifest = read_json(project, project / "runs" / result["run_id"] / "manifest.json")
    assert set(manifest["prompt_variants"]) == {"summary-only"}
    assert manifest["prompt_languages"] == {"summary-only": "zh-CN"}


@pytest.mark.asyncio
async def test_terms_only_run_does_not_read_fragment_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, "Alice entered.")
    original_read_bytes = Path.read_bytes
    original_read_text = Path.read_text

    def forbid_fragment_bytes(path: Path, *args: object, **kwargs: object) -> bytes:
        if path.parent.name == "prompts" and path.name.startswith("fragment_summary."):
            raise AssertionError("terms-only 不应读取 fragment_summary Prompt")
        return original_read_bytes(path, *args, **kwargs)

    def forbid_fragment_text(path: Path, *args: object, **kwargs: object) -> str:
        if path.parent.name == "prompts" and path.name.startswith("fragment_summary."):
            raise AssertionError("terms-only 不应读取 fragment_summary Prompt")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", forbid_fragment_bytes)
    monkeypatch.setattr(Path, "read_text", forbid_fragment_text)

    def terms_handler(request: httpx.Request) -> httpx.Response:
        del request
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
        result = await run_terminology(
            project,
            Scope(),
            http_client=client,
            include_summaries=True,
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)
    assert result["failed"] == 0
    manifest = read_json(project, project / "runs" / result["run_id"] / "manifest.json")
    assert set(manifest["prompt_variants"]) == {"terms-only"}
    assert manifest["prompt_languages"] == {"terms-only": "zh-CN"}


@pytest.mark.asyncio
async def test_summary_format_correction_uses_requested_language(
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
        first = await run_terminology(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)
    assert first["failed"] == 0
    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "document", "selected": True}],
    )
    calls = 0

    def summary_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        payload = json.loads(body["messages"][1]["content"])
        if calls == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "type": "summary",
                                        "text": "未闭合。",
                                        "refs": ["1"],
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                },
            )
        correction = payload["format_correction"]
        assert "只处理当前待处理内容" in correction
        assert "current pending content" not in correction
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
                                        "refs": [
                                            str(i)
                                            for i in range(1, len(payload["source_segments"]) + 1)
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
        result = await run_terminology(
            project,
            Scope(),
            http_client=client,
            prompt_language="zh-CN",
            reuse_mixed_fingerprints=True,
            include_summaries=True,
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)

    assert result["failed"] == 0
    assert calls == 2
    summary_run = next(
        item for item in read_summary_runs(project) if item["run_id"] == result["run_id"]
    )
    assert summary_run["prompt_languages"] == {"summary-only": "zh-CN"}


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
                {"type": "summary", "text": "Alice 进入。"},
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
    summaries = read_content_summaries(project, kind="fragment", status="completed")
    assert len(summaries) == 1
    assert summaries[0]["refs"] == ["1"]
    metadata = json.loads(
        (
            project
            / "runs"
            / result["run_id"]
            / "prompt_variants.json"
        ).read_text(encoding="utf-8")
    )
    assert {key.split("__", 1)[0] for key in metadata} == {
        "terms+fragment-summary",
        "terms-only",
    }


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


@pytest.mark.asyncio
async def test_summary_reuse_includes_project_target_language(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, "Alice entered.")
    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "document", "selected": True}],
    )

    def summary_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        payload = json.loads(body["messages"][1]["content"])
        records = [
            {
                "type": "summary",
                "text": f"Summary in {payload['target_language']}.",
                "refs": ["1"],
            }
        ]
        if 'type="term"' in body["messages"][0]["content"]:
            records.append(
                {"type": "term", "source": "Alice", "category": "人物"}
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": llm_jsonl(records)
                        }
                    }
                ]
            },
        )

    os.environ["LLM_API_KEY"] = "test"
    client = httpx.AsyncClient(transport=httpx.MockTransport(summary_handler))
    try:
        _first = await run_terminology(
            project, Scope(), http_client=client, include_summaries=True
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)
    first_summary = read_content_summaries(project, kind="fragment", status="completed")
    assert first_summary[0]["target_language"] == "简体中文"

    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'target_language = "简体中文"', 'target_language = "English"'
        ),
        encoding="utf-8",
    )
    calls = 0

    def changed_language_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert payload["target_language"] == "English"
        return summary_handler(request)

    os.environ["LLM_API_KEY"] = "test"
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(changed_language_handler)
    )
    try:
        result = await run_terminology(
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
    assert result["failed"] == 0
    assert any(
        item["target_language"] == "English"
        for item in read_content_summaries(project, kind="fragment", status="completed")
    )


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_plugin(monkeypatch, RecordDocumentAdapter())
    source = tmp_path / "book.rec"
    write_record(
        source,
        "# name: demo\nABCDEFGH\nIJKL\n---\nother part",
    )
    project, _ = init_project(
        [str(source)],
        name="record-demo",
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
        document_adapter_id="record",
        adapter_options={"record": {"source_style": "marked"}},
    )
    assert project is not None
    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "a", "selected": True}],
    )

    requests: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        requests.append(payload["source_segments"])
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
    assert requests[0] == ["<k1>ABCDEFGH</k1>", "<k2>IJKL</k2>"]
    assert requests[1:] == [["<k1>ABCDEFGH</k1>"]]
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


@pytest.mark.asyncio
async def test_external_adapter_parts_use_generic_summary_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An external-like adapter proves summaries do not depend on TXT/EPUB."""
    register_plugin(monkeypatch, RecordDocumentAdapter())
    source = tmp_path / "book.rec"
    write_record(source, "# name: demo\nAlice entered.\n---\nBob waved.")
    project, _ = init_project(
        [str(source)],
        name="record-demo",
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
        document_adapter_id="record",
        adapter_options={"record": {"source_style": "marked"}},
    )
    assert project is not None
    write_summary_participation(
        project,
        [
            {"file_id": "F0001", "part_id": "a", "selected": True},
            {"file_id": "F0001", "part_id": "b", "selected": True},
        ],
    )
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        requests.append(payload)
        source_segments = payload["source_segments"]
        records = [
            {
                "type": "summary",
                "text": f"概括：{source_segments[0]}",
                "refs": ["1"],
            },
            {
                "type": "term",
                "source": "Alice" if "Alice" in source_segments[0] else "Bob",
                "category": "人物",
            },
        ]
        return httpx.Response(
            200, json={"choices": [{"message": {"content": llm_jsonl(records)}}]}
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
    assert [payload["source_segments"] for payload in requests] == [
        ["<k1>Alice entered.</k1>"],
        ["<k2>Bob waved.</k2>"],
    ]
    summaries = read_content_summaries(project, kind="fragment", status="completed")
    assert {(item["file_id"], item["part_id"]) for item in summaries} == {
        ("F0001", "a"),
        ("F0001", "b"),
    }
    assert all(
        item["source_range"]["file_id"] == item["file_id"]
        and item["source_range"]["part_id"] == item["part_id"]
        for item in summaries
    )
    ranges = {
        item["part_id"]: item["source_range"]["segments"][0]
        for item in summaries
    }
    assert ranges["a"]["source"] == "Alice entered."
    assert ranges["a"]["model_text"] == "<k1>Alice entered.</k1>"
    assert ranges["b"]["source"] == "Bob waved."
    assert ranges["b"]["model_text"] == "<k2>Bob waved.</k2>"


def test_cli_terminology_keeps_summary_opt_in_out_of_standard_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path, "Alice entered.")
    calls: list[dict[str, object]] = []

    async def fake_run_terminology(
        _project: Path, _scope: Scope, **kwargs: object
    ) -> dict[str, object]:
        calls.append(kwargs)
        return {
            "stage": "terminology",
            "completed": 1,
            "failed": 0,
            "pending": 0,
            "warnings": [],
        }

    monkeypatch.setattr("app.main.run_terminology", fake_run_terminology)
    try:
        assert run(["terminology", str(project)]) == 0
    finally:
        os.environ.pop("LLM_API_KEY", None)
    capsys.readouterr()

    assert calls == [{"resume_run_id": None, "reuse_mixed_fingerprints": False}]
