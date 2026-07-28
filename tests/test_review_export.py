from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from app.errors import IncompleteError
from app.execution import Scope
from app.stages import (
    export_project,
    run_all,
    run_apply,
    run_review,
    run_translation,
)
from app.storage import read_jsonl
from tests.test_terminology_translation import create_project


def workflow_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    system = body["messages"][0]["content"]
    payload = json.loads(body["messages"][1]["content"])
    if "提取 terms 数组" in system:
        content = {"terms": []}
    elif "完整 translation" in system:
        content = {
            "segments": [
                {"id": item["id"], "translation": f"译:{item['source']}"}
                for item in payload["segments"]
            ]
        }
    else:
        content = {
            "segments": [
                {
                    "id": item["id"],
                    "status": "suggested",
                    "suggested_text": (
                        f"润:{item['current_text']}"
                        if "改善译文表达" in system
                        else f"校:{item['current_text']}"
                    ),
                    "reason": "test",
                }
                for item in payload["segments"]
            ]
        }
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps(content)}}]},
    )


@pytest.mark.asyncio
async def test_review_apply_and_bilingual_export(tmp_path: Path) -> None:
    project = await create_project(tmp_path, "one\n\ntwo")
    client = httpx.AsyncClient(transport=httpx.MockTransport(workflow_handler))
    try:
        await run_translation(project, Scope(), http_client=client)
        proof = await run_review(
            project, "proofreading", Scope(), http_client=client
        )
        applied = run_apply(
            project,
            "proofreading",
            Scope(),
            allow_outdated_base=False,
            confirmed_all=True,
        )
        polish = await run_review(
            project, "polishing", Scope(), http_client=client
        )
        polished = run_apply(
            project,
            "polishing",
            Scope(),
            allow_outdated_base=False,
            confirmed_all=True,
        )
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    assert proof["completed"] == 2
    assert applied["completed"] == 2
    assert polish["completed"] == 2
    assert polished["completed"] == 2

    mono = export_project(
        project, "proofread", bilingual=False, allow_missing=False
    )
    bilingual = export_project(
        project, "polished", bilingual=True, allow_missing=False
    )
    assert mono["files"] == bilingual["files"] == 1
    mono_text = (
        project / "output" / "proofread" / "source.txt"
    ).read_text(encoding="utf-8-sig")
    bilingual_text = (
        project / "output" / "bilingual" / "polished" / "source.txt"
    ).read_text(encoding="utf-8-sig")
    assert mono_text.splitlines() == ["校:译:one", "", "校:译:two"]
    assert bilingual_text.splitlines() == [
        "one",
        "润:校:译:one",
        "",
        "two",
        "润:校:译:two",
    ]


@pytest.mark.asyncio
async def test_apply_rejects_outdated_base(tmp_path: Path) -> None:
    project = await create_project(tmp_path, "one")
    client = httpx.AsyncClient(transport=httpx.MockTransport(workflow_handler))
    try:
        await run_translation(project, Scope(), http_client=client)
        await run_review(project, "proofreading", Scope(), http_client=client)
        await run_translation(project, Scope(force=True), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    with pytest.raises(IncompleteError, match="旧上游"):
        run_apply(
            project,
            "proofreading",
            Scope(),
            allow_outdated_base=False,
            confirmed_all=True,
        )
    summary = run_apply(
        project,
        "proofreading",
        Scope(),
        allow_outdated_base=True,
        confirmed_all=True,
    )
    assert summary["completed"] == 1
    assert summary["warnings"]


@pytest.mark.asyncio
async def test_run_all_generates_suggestions_without_apply(tmp_path: Path) -> None:
    project = await create_project(tmp_path, "one\ntwo")
    client = httpx.AsyncClient(transport=httpx.MockTransport(workflow_handler))
    try:
        summary = await run_all(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    assert [step["stage"] for step in summary["steps"]] == [
        "terminology",
        "translation",
        "proofreading",
        "polishing",
    ]
    assert read_jsonl(project / "stages" / "proofreading.jsonl")
    assert read_jsonl(project / "stages" / "polishing.jsonl")
    assert not (project / "stages" / "proofreading_applied.jsonl").exists()
    assert not (project / "stages" / "polishing_applied.jsonl").exists()


@pytest.mark.asyncio
async def test_export_allow_missing_falls_back_and_reports(tmp_path: Path) -> None:
    project = await create_project(tmp_path, "one")
    summary = export_project(
        project, "translated", bilingual=False, allow_missing=True
    )
    assert summary["fallback_segments"] == ["F0001-S000001"]
    output = project / "output" / "translated" / "source.txt"
    assert output.read_text(encoding="utf-8-sig") == "one"
    del os.environ["LLM_API_KEY"]
