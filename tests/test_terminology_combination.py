from pathlib import Path
import json

import httpx
import pytest

from app.execution import Scope, load_stage_history
from app.llm_response import response_record_types
from app.sqlite_storage import read_content_summaries, write_summary_participation
from app.stage_terminology import run_terminology
from tests.test_terminology_translation import create_project, llm_jsonl


@pytest.mark.asyncio
async def test_triple_scan_retries_missing_summary_and_preserves_draft(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "Alice entered.")
    write_summary_participation(
        project, [{"file_id": "F0001", "part_id": "document", "selected": True}]
    )
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        seen.append(payload)
        kinds = response_record_types(payload["response_mode"])
        records = []
        if "summary" in kinds and len(seen) > 1:
            records.append({"type": "summary", "text": "爱丽丝进来了。"})
        if "term" in kinds:
            records.append({"type": "no_terms"})
        if "segment" in kinds:
            records.extend(
                {"type": "segment", "id": item["id"], "translation": "爱丽丝进来了。"}
                for item in payload["segments"]
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": llm_jsonl(records)}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await run_terminology(
            project,
            Scope(),
            http_client=client,
            include_summaries=True,
            include_draft_translation=True,
        )
        assert result["failed"] == 0
        assert [item["response_mode"] for item in seen] == [
            "terms+translation+fragment-summary",
            "summary-only",
        ]
        assert len(load_stage_history(project, "translation")) == 1
        assert (
            len(read_content_summaries(project, kind="fragment", status="completed"))
            == 1
        )
        seen.clear()
        repeated = await run_terminology(
            project,
            Scope(),
            reuse_mixed_fingerprints=True,
            http_client=client,
            include_summaries=True,
            include_draft_translation=True,
        )
        assert repeated["failed"] == 0
        assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing,expected",
    [("terms", "terms-only"), ("translation", "translation-only"), (None, None)],
)
async def test_triple_scan_preserves_independent_results_and_force(
    tmp_path: Path, missing: str | None, expected: str | None
) -> None:
    project = await create_project(tmp_path, "Alice entered.\nBob left.")
    write_summary_participation(
        project, [{"file_id": "F0001", "part_id": "document", "selected": True}]
    )
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        seen.append(payload)
        kinds = response_record_types(payload["response_mode"])
        records = []
        if "summary" in kinds:
            records.append(
                {
                    "type": "summary",
                    "text": "人物进出。",
                    "refs": payload["source_refs"],
                }
            )
        if "term" in kinds:
            records.append(
                {
                    "type": "term",
                    "source": "Alice",
                    "category": 1 if missing == "terms" and len(seen) == 1 else "人名",
                }
            )
        if "segment" in kinds:
            records.extend(
                {"type": "segment", "id": item["id"], "translation": "人物移动。"}
                for item in payload["segments"]
                if not (missing == "translation" and len(seen) == 1)
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": llm_jsonl(records)}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await run_terminology(
            project,
            Scope(),
            http_client=client,
            include_summaries=True,
            include_draft_translation=True,
        )
        assert result["failed"] == 0
        assert [item["response_mode"] for item in seen] == [
            "terms+translation+fragment-summary"
        ] + ([expected] if expected else [])
        assert len(load_stage_history(project, "translation")) == 2
        assert (
            len(read_content_summaries(project, kind="fragment", status="completed"))
            == 1
        )
        seen.clear()
        result = await run_terminology(
            project,
            Scope(force=True),
            http_client=client,
            include_summaries=True,
            include_draft_translation=True,
        )
        assert result["failed"] == 0
        assert len(load_stage_history(project, "translation")) == 4
        assert (
            len(read_content_summaries(project, kind="fragment", status="completed"))
            == 1
        )


def test_combined_prompt_middles_are_used_in_preview_and_execution(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient
    from app.web import create_app
    from app.stage_runtime import _prompt_factory
    from tests.test_web import make_project

    projects, project = make_project(tmp_path)
    with TestClient(
        create_app(projects_root=projects, app_root=tmp_path / "app-root")
    ) as client:
        middles = {
            "terminology": "只提取作品专有名称。",
            "translation": "保持原文叙述视角。",
            "fragment_summary": "简洁概括本次事件。",
        }
        for stage, middle in middles.items():
            saved = client.put(
                f"/api/v1/projects/sample/prompts/{stage}",
                json={"language": "zh-CN", "content": middle},
            )
            assert saved.status_code == 200
        view = client.get(
            "/api/v1/projects/sample/prompts/terminology?language=zh-CN"
        ).json()
        preview = view["assembled_modes"]["terms+translation+fragment-summary"]
        actual = _prompt_factory(
            project,
            "terminology",
            "zh-CN",
            response_mode="terms+translation+fragment-summary",
        )(())
        assert actual == preview
        assert all(middle in actual for middle in middles.values())
        assert "no_terms" in actual and '末行精确为{"type":"end"}' in actual


@pytest.mark.asyncio
async def test_triple_resume_keeps_summary_selection_and_options(
    tmp_path: Path,
) -> None:
    import asyncio
    from app.web_tasks import task_options

    project = await create_project(tmp_path, "Alice entered.")
    boundary = {"file_id": "F0001", "part_id": "document"}
    write_summary_participation(project, [{**boundary, "selected": True}])

    def interrupted(_request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    async with httpx.AsyncClient(transport=httpx.MockTransport(interrupted)) as client:
        with pytest.raises(asyncio.CancelledError):
            await run_terminology(
                project,
                Scope(),
                http_client=client,
                include_summaries=True,
                include_draft_translation=True,
            )
    from app.sqlite_storage import read_summary_runs, read_json, write_json

    run_id = read_summary_runs(project)[-1]["run_id"]
    # A process crash leaves a running manifest for explicit continuation.
    manifest_path = project / "runs" / run_id / "manifest.json"
    manifest = read_json(project, manifest_path)
    manifest["status"] = "running"
    write_json(project, manifest_path, manifest)
    write_summary_participation(project, [{**boundary, "selected": False}])
    options = task_options(
        project, "terminology", include_summaries=True, include_draft_translation=True
    )
    assert options["summary_selected_boundaries"] == 1
    assert options["draft_progress"]["content_summary"]["total"] == 1

    def completed(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": llm_jsonl(
                                [
                                    {"type": "summary", "text": "爱丽丝进来了。"},
                                    {"type": "no_terms"},
                                    {
                                        "type": "segment",
                                        "id": "1",
                                        "translation": "爱丽丝进来了。",
                                    },
                                ]
                            )
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(completed)) as client:
        result = await run_terminology(
            project,
            Scope(),
            http_client=client,
            resume_run_id=run_id,
            include_summaries=True,
            include_draft_translation=True,
        )
    assert result["failed"] == 0
    assert result["draft_progress"]["content_summary"]["completed"] == 1
