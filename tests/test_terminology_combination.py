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
        assert "source_refs" not in payload
        assert ("segments" in payload) == ("segment" in kinds)
        if "segment" not in kinds:
            assert all(
                "id" in item and "text" in item for item in payload["source_segments"]
            )
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
                    "refs": [item["id"] for item in payload["source_segments"]],
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


@pytest.mark.asyncio
async def test_supplement_adapter_requirements_follow_active_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = await create_project(tmp_path, "A\nB")
    write_summary_participation(
        project, [{"file_id": "F0001", "part_id": "document", "selected": True}]
    )
    rules = {
        "terminology": "术语文档规则。",
        "translation": "翻译文档规则。",
        "fragment_summary": "概括文档规则。",
    }
    monkeypatch.setattr(
        "app.stage_runtime.document_prompt_requirements",
        lambda adapter, state, options, stage: {
            "zh-CN": rules[stage],
            "en": rules[stage],
        },
    )
    missing = {"summary"}
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        payload = json.loads(body["messages"][1]["content"])
        mode = payload["response_mode"]
        kinds = response_record_types(mode)
        seen.append((mode, body["messages"][0]["content"]))
        assert "source_refs" not in payload
        assert ("segments" in payload) == ("segment" in kinds)
        if "segment" in kinds:
            assert payload["segments"] == [
                {"id": "1", "source": "A"},
                {"id": "2", "source": "B"},
            ]
        if "summary" in kinds or ("segment" in kinds and "term" in kinds):
            assert payload["source_segments"] == [
                {"id": "1", "text": "A"},
                {"id": "2", "text": "B"},
            ]
        elif "term" in kinds:
            assert payload["source_segments"] == ["A", "B"]
        else:
            assert "source_segments" not in payload
        records = []
        if "summary" in kinds:
            records.append(
                {
                    "type": "summary",
                    "text": "甲",
                    "refs": [item["id"] for item in payload["source_segments"]],
                }
                if "summary" not in missing
                else {"type": "summary", "text": 1}
            )
        if "term" in kinds:
            records.append(
                {"type": "no_terms"}
                if "term" not in missing
                else {"type": "term", "source": "A", "category": 1}
            )
        if "segment" in kinds and "segment" not in missing:
            records.extend(
                {"type": "segment", "id": item["id"], "translation": "甲"}
                for item in payload["segments"]
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": llm_jsonl(records)}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for missing in (
            {"summary"},
            {"term"},
            {"segment"},
            {"summary", "term"},
            {"summary", "segment"},
            {"term", "segment"},
        ):
            await run_terminology(
                project,
                Scope(force=True),
                http_client=client,
                include_summaries=True,
                include_draft_translation=True,
            )
    from app.llm_response import TerminologyResponseMode

    assert {mode for mode, _ in seen} == {
        mode.value for mode in TerminologyResponseMode
    }
    for mode, prompt in seen:
        kinds = response_record_types(mode)
        for stage, kind in (
            ("terminology", "term"),
            ("translation", "segment"),
            ("fragment_summary", "summary"),
        ):
            assert (rules[stage] in prompt) == (kind in kinds)


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
@pytest.mark.parametrize("include_draft", [False, True])
async def test_triple_resume_keeps_summary_selection_and_options(
    tmp_path: Path,
    include_draft: bool,
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
                include_draft_translation=include_draft,
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
        project,
        "terminology",
        include_summaries=True,
        include_draft_translation=include_draft,
    )
    assert options["summary_selected_boundaries"] == 1
    progress = (
        options["draft_progress"]["content_summary"]
        if include_draft
        else options["summary_progress"]
    )
    assert progress["total"] == 1

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
                                    *(
                                        [
                                            {
                                                "type": "segment",
                                                "id": "1",
                                                "translation": "爱丽丝进来了。",
                                            }
                                        ]
                                        if include_draft
                                        else []
                                    ),
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
            include_draft_translation=include_draft,
        )
    assert result["failed"] == 0
    assert (
        len(read_content_summaries(project, kind="fragment", status="completed")) == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_terms", [False, True])
@pytest.mark.parametrize("error_kind", ["context", "empty"])
async def test_summary_supplement_records_unsplittable_failure(
    tmp_path: Path,
    missing_terms: bool,
    error_kind: str,
) -> None:
    from app.sqlite_storage import read_json

    project = await create_project(tmp_path, "A")
    write_summary_participation(
        project,
        [{"file_id": "F0001", "part_id": "document", "selected": True}],
    )
    failing = False
    modes = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        mode = payload["response_mode"]
        if failing:
            modes.append(mode)
            if error_kind == "context":
                return httpx.Response(
                    400, text="context_length_exceeded: maximum context tokens"
                )
            return httpx.Response(
                200,
                json={
                    "choices": [{"finish_reason": "length", "message": {"content": ""}}]
                },
            )
        records = []
        if "term" in response_record_types(mode):
            records.append(
                {"type": "term", "source": "A", "category": 1}
                if missing_terms
                else {"type": "no_terms"}
            )
        if "segment" in response_record_types(mode):
            records.append({"type": "segment", "id": "1", "translation": "甲"})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": llm_jsonl(records)}}]}
        )

    progress = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await run_terminology(
            project,
            Scope(),
            http_client=client,
            include_summaries=True,
            include_draft_translation=True,
        )
        failing = True
        result = await run_terminology(
            project,
            Scope(),
            http_client=client,
            reuse_mixed_fingerprints=True,
            include_summaries=True,
            include_draft_translation=True,
            on_progress=lambda done, failed, total: progress.append(
                (done, failed, total)
            ),
        )
    assert modes == ["terms+fragment-summary" if missing_terms else "summary-only"]
    assert result["failed"] == 1
    assert result["draft_progress"]["content_summary"] == {
        "completed": 0,
        "failed": 1,
        "total": 1,
    }
    assert progress[-1] == (0, 1, 1)
    assert len(load_stage_history(project, "translation")) == 1
    manifest = read_json(project, project / "runs" / result["run_id"] / "manifest.json")
    assert manifest["status"] == "failed"
    failures = [
        row
        for row in read_content_summaries(project, kind="fragment", status="failed")
        if row["run_id"] == result["run_id"]
    ]
    assert len(failures) == 1
    assert failures[0]["error_class"] == (
        "context_error" if error_kind == "context" else "empty_response"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("error_kind", ["context", "empty"])
async def test_summary_supplement_split_preserves_successful_draft(
    tmp_path: Path, error_kind: str
) -> None:
    project = await create_project(tmp_path, "Alice entered.\nBob left.")
    write_summary_participation(
        project, [{"file_id": "F0001", "part_id": "document", "selected": True}]
    )
    modes = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        modes.append(payload["response_mode"])
        if len(modes) == 2:
            if error_kind == "context":
                return httpx.Response(
                    400, text="context_length_exceeded: maximum context tokens"
                )
            return httpx.Response(
                200,
                json={
                    "choices": [{"finish_reason": "length", "message": {"content": ""}}]
                },
            )
        records = []
        kinds = response_record_types(payload["response_mode"])
        if "summary" in kinds and len(modes) > 2:
            records.append({"type": "summary", "text": "人物进出。"})
        if "term" in kinds:
            records.append({"type": "no_terms"})
        if "segment" in kinds:
            records.extend(
                {
                    "type": "segment",
                    "id": item["id"],
                    "translation": f"译文{len(modes)}",
                }
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
    assert modes == [
        "terms+translation+fragment-summary",
        "summary-only",
        "summary-only",
        "summary-only",
    ]
    translations = load_stage_history(project, "translation")
    assert len(translations) == 2
    assert {item["text"] for item in translations} == {"译文1"}
    assert result["failed"] == 0
    assert all(
        value["completed"] == value["total"]
        for value in result["draft_progress"].values()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("error_kind", ["context", "empty"])
async def test_supplement_split_continues_remaining_groups(
    tmp_path: Path, error_kind: str
) -> None:
    project = await create_project(tmp_path, "Alice entered.\nBob left.")
    modes = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        modes.append(payload["response_mode"])
        if len(modes) == 1:
            records = [
                {"type": "no_terms"},
                {"type": "segment", "id": "1", "translation": "爱丽丝进来了。"},
            ]
            content = "\n".join(
                json.dumps(item, ensure_ascii=False) for item in records
            )
        elif len(modes) == 2:
            if error_kind == "context":
                return httpx.Response(
                    400, text="context_length_exceeded: maximum context tokens"
                )
            return httpx.Response(
                200,
                json={
                    "choices": [{"finish_reason": "length", "message": {"content": ""}}]
                },
            )
        else:
            records = [{"type": "no_terms"}]
            if "segment" in response_record_types(payload["response_mode"]):
                records.extend(
                    {"type": "segment", "id": item["id"], "translation": "鲍勃离开了。"}
                    for item in payload["segments"]
                )
            content = llm_jsonl(records)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await run_terminology(
            project,
            Scope(),
            http_client=client,
            include_draft_translation=True,
        )
    assert modes == [
        "terms+translation",
        "terms-only",
        "terms-only",
        "terms-only",
        "terms+translation",
    ]
    translations = load_stage_history(project, "translation")
    assert len(translations) == 2
    assert translations[0]["text"] == "爱丽丝进来了。"
    assert result["failed"] == 0
    assert all(
        value["completed"] == value["total"]
        for value in result["draft_progress"].values()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pending_kind,include_draft",
    [
        ("term", True),
        ("segment", True),
        ("summary", True),
        ("term", False),
        ("summary", False),
    ],
)
@pytest.mark.parametrize("error_kind", ["context", "empty"])
async def test_nested_supplement_split_keeps_other_results_complete_and_reusable(
    tmp_path: Path, pending_kind: str, include_draft: bool, error_kind: str
) -> None:
    from app.sqlite_storage import read_json, terminology_scan_state

    project = await create_project(tmp_path, "ABCD")
    write_summary_participation(
        project, [{"file_id": "F0001", "part_id": "document", "selected": True}]
    )
    modes = []
    only_mode = {
        "term": "terms-only",
        "segment": "translation-only",
        "summary": "summary-only",
    }[pending_kind]

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        modes.append(
            payload.get(
                "response_mode",
                only_mode if len(modes) in (2, 3, 4) else "terms+fragment-summary",
            )
        )
        if len(modes) in (1, 3):
            if error_kind == "context":
                return httpx.Response(
                    400, text="context_length_exceeded: maximum context tokens"
                )
            return httpx.Response(
                200,
                json={
                    "choices": [{"finish_reason": "length", "message": {"content": ""}}]
                },
            )
        kinds = response_record_types(modes[-1])
        records = []
        if "summary" in kinds and not (len(modes) == 2 and pending_kind == "summary"):
            records.append({"type": "summary", "text": "内容概括。"})
        if "term" in kinds:
            records.append(
                {"type": "term", "source": "AB", "category": 1}
                if len(modes) == 2 and pending_kind == "term"
                else {"type": "no_terms"}
            )
        if "segment" in kinds and not (len(modes) == 2 and pending_kind == "segment"):
            records.extend(
                {"type": "segment", "id": item["id"], "translation": "译文"}
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
            include_draft_translation=include_draft,
        )
        assert result["failed"] == 0
        assert result["completed"] == 1
        if include_draft:
            assert all(
                value["completed"] == value["total"] == 1
                for value in result["draft_progress"].values()
            )
        active = read_json(project, project / "terminology" / "active_task.json")
        scanned, _ = terminology_scan_state(
            project, active["active_task_id"], {"F0001-S000001"}
        )
        assert scanned == {"F0001-S000001"}
        assert modes[2:5] == [only_mode] * 3
        modes.clear()
        repeated = await run_terminology(
            project,
            Scope(),
            http_client=client,
            reuse_mixed_fingerprints=True,
            include_summaries=True,
            include_draft_translation=include_draft,
        )
        assert modes == []
        assert repeated["failed"] == 0
        if include_draft:
            assert repeated["requested"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("include_draft", [False, True])
async def test_parallel_summary_split_keeps_saved_slices_reusable(
    tmp_path: Path, include_draft: bool
) -> None:
    import asyncio
    from tests.helpers import use_llm_preset

    project = await create_project(tmp_path, "A" * 8000 + "B" * 8000)
    path = project / "config.toml"
    path.write_text(
        path.read_text().replace(
            'scheduling_mode = "ordered_by_file"', 'scheduling_mode = "parallel"'
        )
    )
    use_llm_preset(
        tmp_path,
        context_window_tokens=3000,
        context_safety_margin_tokens=100,
        target_chunk_input_tokens=1,
        max_output_tokens=200,
    )
    write_summary_participation(
        project, [{"file_id": "F0001", "part_id": "document", "selected": True}]
    )
    seen = []
    repeat = False

    async def handler(request: httpx.Request) -> httpx.Response:
        assert not repeat, "完整概括不应在再次补缺时重新请求"
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        seen.append(payload)
        if len(seen) == 1:
            # Let later preflight slices finish before the first slice subdivides.
            await asyncio.sleep(0.03)
            return httpx.Response(
                400, text="context_length_exceeded: maximum context tokens"
            )
        records = [{"type": "summary", "text": "概括"}, {"type": "no_terms"}]
        if include_draft:
            records.extend(
                {"type": "segment", "id": item["id"], "translation": "译"}
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
            include_draft_translation=include_draft,
        )
        assert result["completed"] == 1
        assert result["failed"] == 0
        assert len(seen) > 3
        repeat = True
        repeated = await run_terminology(
            project,
            Scope(),
            http_client=client,
            reuse_mixed_fingerprints=True,
            include_summaries=True,
            include_draft_translation=include_draft,
        )
        assert repeated["failed"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("include_draft", [False, True])
async def test_complete_summary_recovery_ignores_old_partial_partition(
    tmp_path: Path, include_draft: bool
) -> None:
    from app.errors import FatalExternalError

    project = await create_project(tmp_path, "ABCD")
    write_summary_participation(
        project, [{"file_id": "F0001", "part_id": "document", "selected": True}]
    )
    phase = 0
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert phase != 2, "补缺成功后应复用完整概括"
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        seen.append(payload)
        if phase == 0 and len(seen) == 1:
            return httpx.Response(
                400, text="context_length_exceeded: maximum context tokens"
            )
        if phase == 0 and len(seen) == 3:
            return httpx.Response(400, text="bad request")
        records = [{"type": "summary", "text": "概括"}, {"type": "no_terms"}]
        if include_draft:
            records.extend(
                {"type": "segment", "id": item["id"], "translation": "译"}
                for item in payload["segments"]
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": llm_jsonl(records)}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FatalExternalError):
            await run_terminology(
                project,
                Scope(),
                http_client=client,
                include_summaries=True,
                include_draft_translation=include_draft,
            )
        phase = 1
        seen.clear()
        recovered = await run_terminology(
            project,
            Scope(),
            http_client=client,
            reuse_mixed_fingerprints=True,
            include_summaries=True,
            include_draft_translation=include_draft,
        )
        assert recovered["completed"] == 1
        assert recovered["failed"] == 0
        assert len(seen) == 1
        phase = 2
        await run_terminology(
            project,
            Scope(),
            http_client=client,
            reuse_mixed_fingerprints=True,
            include_summaries=True,
            include_draft_translation=include_draft,
        )
