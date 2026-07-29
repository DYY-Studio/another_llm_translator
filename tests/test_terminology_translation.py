from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx
import pytest

from app.execution import Scope, latest_completed_by_segment, load_stage_history
from app.project import init_project
from app.stages import load_terms, match_terms, run_terminology, run_translation
from app.storage import read_jsonl
from tests.test_foundation import make_app_root


async def create_project(tmp_path: Path, text: str) -> Path:
    app_root = make_app_root(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text(text, encoding="utf-8")
    project, _ = init_project(
        [str(source)],
        name="demo",
        app_root=app_root,
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    os.environ["LLM_API_KEY"] = "test"
    return project


@pytest.mark.asyncio
async def test_terminology_publishes_and_translation_uses_terms(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "Alice entered.\nAlice waved.")
    seen_translation_payload: dict | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_translation_payload
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        payload = json.loads(body["messages"][1]["content"])
        if "提取 terms 数组" in system:
            content = {
                "terms": [
                    {
                        "source": "Alice",
                        "category": "女性人名",
                        "description": "人物",
                        "preferred_translation": "爱丽丝",
                        "aliases": [],
                    }
                ]
            }
        else:
            seen_translation_payload = payload
            content = {
                "segments": [
                    {"id": item["id"], "translation": f"译文:{item['source']}"}
                    for item in payload["segments"]
                ]
            }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(content)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        term_summary = await run_terminology(
            project, Scope(), http_client=client
        )
        translation_summary = await run_translation(
            project, Scope(), http_client=client
        )
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]

    assert term_summary["published"] is True
    assert term_summary["terms_revision"] == 1
    assert load_terms(project)["terms"][0]["preferred_translation"] == "爱丽丝"
    assert translation_summary["completed"] == 2
    assert seen_translation_payload is not None
    assert seen_translation_payload["terms"][0]["source"] == "Alice"


@pytest.mark.asyncio
async def test_completed_terminology_command_does_not_republish(tmp_path: Path) -> None:
    project = await create_project(tmp_path, "Alice")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        content = {
            "terms": [
                {
                    "source": "Alice",
                    "category": "人物",
                    "description": "人物",
                    "preferred_translation": "爱丽丝",
                    "aliases": [],
                }
            ]
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(content)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        first = await run_terminology(project, Scope(), http_client=client)
        second = await run_terminology(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    assert first["terms_revision"] == 1
    assert second["published"] is False
    assert second["terms_revision"] == 1
    assert calls == 1


@pytest.mark.asyncio
async def test_forced_terminology_scan_is_always_project_wide(tmp_path: Path) -> None:
    project = await create_project(tmp_path, "Alice\nBob")
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        requested.extend(item["id"] for item in payload["segments"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps({"terms": []})}}
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = await run_terminology(
            project,
            Scope(only_segment="F0001-S000001", force=True),
            http_client=client,
        )
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    assert set(requested) == {"F0001-S000001", "F0001-S000002"}
    assert summary["published"] is True


def test_term_matching_prefers_main_name_over_alias() -> None:
    library = {
        "terms": [
            {
                "source": "Alice",
                "aliases": [],
                "preferred_translation": "爱丽丝",
            },
            {
                "source": "Other",
                "aliases": ["Alice Wonderland"],
                "preferred_translation": "其他",
            },
        ]
    }
    matched = match_terms("Alice Wonderland arrived.", library, 10)
    assert [item["source"] for item in matched] == ["Alice", "Other"]


@pytest.mark.asyncio
async def test_translation_partial_response_retries_only_missing(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "one\ntwo")
    calls: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        ids = [item["id"] for item in payload["segments"]]
        calls.append(ids)
        returned = ids[:1]
        content = {
            "segments": [
                {"id": segment_id, "translation": f"ok:{segment_id}"}
                for segment_id in returned
            ]
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(content)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = await run_translation(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    assert summary["completed"] == 2
    assert calls == [
        ["F0001-S000001", "F0001-S000002"],
        ["F0001-S000002"],
    ]
    completed = latest_completed_by_segment(
        load_stage_history(project, "translation")
    )
    assert set(completed) == {"F0001-S000001", "F0001-S000002"}


@pytest.mark.asyncio
async def test_translation_reports_dynamic_output_budget_warning(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "one")
    config_path = project / "config.toml"
    text = config_path.read_text(encoding="utf-8")
    for key, value in (
        ("context_window_tokens", "1000"),
        ("context_safety_margin_tokens", "100"),
        ("max_output_tokens", "5000"),
    ):
        text = re.sub(rf"(?m)^{key}\s*=.*$", f"{key} = {value}", text)
    config_path.write_text(text, encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        content = {
            "segments": [
                {"id": item["id"], "translation": "译文"}
                for item in payload["segments"]
            ]
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(content)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = await run_translation(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    assert any("自动收窄" in warning for warning in summary["warnings"])
    run_dir = project / "runs" / summary["run_id"]
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert any("自动收窄" in warning for warning in manifest["warnings"])


@pytest.mark.asyncio
async def test_kana_validation_repairs_contiguous_failures(tmp_path: Path) -> None:
    project = await create_project(tmp_path, "first\nsecond\n\nthird")
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "japanese_kana = false", "japanese_kana = true"
        ),
        encoding="utf-8",
    )
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        calls.append(payload)
        repairing = "validation_repair" in payload
        content = {
            "segments": [
                {
                    "id": item["id"],
                    "translation": (
                        f"修复:{item['source']}"
                        if repairing
                        else f"候选カ:{item['source']}"
                    ),
                }
                for item in payload["segments"]
            ]
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(content)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = await run_translation(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    assert summary["completed"] == 3
    repair_payloads = [payload for payload in calls if "validation_repair" in payload]
    assert [
        [item["id"] for item in payload["segments"]] for payload in repair_payloads
    ] == [
        ["F0001-S000001", "F0001-S000002"],
        ["F0001-S000004"],
    ]
    records = read_jsonl(project / "stages" / "translation.jsonl")
    assert all(record["validation_status"] == "passed" for record in records)


@pytest.mark.asyncio
async def test_oversized_segment_is_split_and_saved_once(tmp_path: Path) -> None:
    project = await create_project(tmp_path, "A" * 5000)
    config_path = project / "config.toml"
    text = config_path.read_text(encoding="utf-8")
    for key, value in (
        ("context_window_tokens", "1200"),
        ("max_output_tokens", "300"),
        ("context_safety_margin_tokens", "100"),
        ("target_chunk_input_tokens", "700"),
    ):
        text = re.sub(rf"(?m)^{key}\s*=.*$", f"{key} = {value}", text)
    config_path.write_text(text, encoding="utf-8")
    requested_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        requested_ids.extend(item["id"] for item in payload["segments"])
        content = {
            "segments": [
                {"id": item["id"], "translation": item["source"].lower()}
                for item in payload["segments"]
            ]
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(content)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = await run_translation(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    assert summary["completed"] == 1
    assert len(requested_ids) > 1
    assert all("-P" in segment_id for segment_id in requested_ids)
    records = read_jsonl(project / "stages" / "translation.jsonl")
    completed = [record for record in records if record["status"] == "completed"]
    assert len(completed) == 1
    assert completed[0]["segment_id"] == "F0001-S000001"
    assert completed[0]["text"] == "a" * 5000


@pytest.mark.asyncio
async def test_model_context_error_triggers_runtime_segment_split(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "ABCDEFGH")
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "enabled = false",
            "enabled = true",
        ),
        encoding="utf-8",
    )
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        source = payload["segments"][0]["source"]
        requested.append(source)
        if len(source) > 3:
            return httpx.Response(
                400,
                text="context_length_exceeded: maximum context tokens",
            )
        content = {
            "segments": [
                {"id": payload["segments"][0]["id"], "translation": source.lower()}
            ]
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(content)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = await run_translation(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    assert summary["completed"] == 1
    assert any(len(source) > 3 for source in requested)
    assert sum(len(source) <= 3 for source in requested) == 4
    completed = latest_completed_by_segment(
        load_stage_history(project, "translation")
    )
    assert completed["F0001-S000001"]["text"] == "abcdefgh"
    run_dir = next((project / "runs").iterdir())
    attempts = read_jsonl(run_dir / "attempts.jsonl")
    assert any(item.get("parent_request_id") for item in attempts)


@pytest.mark.asyncio
async def test_validation_repair_context_error_splits_without_part_results(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "ABCDEFGH")
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "japanese_kana = false", "japanese_kana = true"
        ),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        item = payload["segments"][0]
        if "validation_repair" in payload and len(item["source"]) > 2:
            return httpx.Response(
                400,
                text="context_length_exceeded: maximum context tokens",
            )
        translation = (
            "好" * len(item["source"])
            if "validation_repair" in payload
            else "カ" * len(item["source"])
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "segments": [
                                        {"id": item["id"], "translation": translation}
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = await run_translation(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    assert summary["completed"] == 1
    records = read_jsonl(project / "stages" / "translation.jsonl")
    completed = [item for item in records if item["status"] == "completed"]
    assert len(completed) == 1
    assert completed[0]["segment_id"] == "F0001-S000001"
    assert completed[0]["text"] == "好" * 8
