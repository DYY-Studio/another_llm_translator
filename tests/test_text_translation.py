from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.config import dump_config, load_config
from app.errors import IncompleteError, UsageError
from app.sqlite_storage import list_run_index, read_segments, record_header
from tests.helpers import llm_jsonl, write_term_library
from tests.test_terminology_translation import create_project


@pytest.mark.parametrize("sourced", [False, True])
def test_temporary_segment_uses_project_terms_without_persisting(tmp_path: Path, sourced: bool) -> None:
    from app.text_translation import translate_text

    async def scenario() -> None:
        project = await create_project(tmp_path, "existing source")
        config = load_config(project / "config.toml")
        config["debug"]["enabled"] = True
        (project / "config.toml").write_text(dump_config(config), encoding="utf-8")
        write_term_library(project, project / "terminology/terms.json", record_header(
            "terminology_library", "test", terms_revision=1,
            terms=[dict(source="Alice", normalized="alice", preferred_translation="爱丽丝",
                        category="人物", description="旅行者", aliases=[])],
        ))
        before = read_segments(project)
        if sourced:
            from app.sqlite_storage import append_jsonl, write_json
            from app.term_library import load_terms, term_normalization
            from app.term_knowledge import publish_evidence, save_items, source_range
            library = load_terms(project)
            record = record_header("terminology_candidates", "test", active_task_id="scan", status="completed", terms=[dict(source="Alice", description="有出处的说明")],
                                   source_range=source_range(before, {s["segment_id"]: s for s in before}, {}))
            append_jsonl(project, project / "terminology/candidates.jsonl", record)
            publish_evidence(project, library, [record], term_normalization(config))
            save_items(project, library, "alice", [{"text": "有出处的说明", "fragment_ids": [record["record_id"] + ":0"]}])
            write_json(project, project / "terminology/terms.json", library)
        requests = []

        def respond(request: httpx.Request) -> httpx.Response:
            body = json.loads(json.loads(request.content)["messages"][1]["content"])
            requests.append(body)
            return httpx.Response(200, json={"choices": [{"message": {"content": llm_jsonl([
                dict(type="segment", id=body["segments"][0]["id"], translation="爱丽丝\n你好")
            ])}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await translate_text(project, "  Alice\nHello", http_client=client)
        assert result["text"] == "  爱丽丝\n你好"
        assert result["usage"]["total_tokens"] == 15
        assert len(requests) == 1
        assert len(requests[0]["segments"]) == 1
        assert requests[0]["terms"][0]["preferred_translation"] == "爱丽丝"
        assert requests[0]["terms"][0]["entity_knowledge"]["items"] == [{"text": "有出处的说明" if sourced else "旅行者"}]
        assert requests[0]["reference_context"] == []
        assert read_segments(project) == before
        assert list_run_index(project) == ([], 0)
        assert not list((project / "runs").glob("*"))

    asyncio.run(scenario())


@pytest.mark.parametrize("warning", [False, True])
def test_temporary_translation_repairs_and_reports_validation(tmp_path: Path, warning: bool) -> None:
    from app.text_translation import translate_text

    async def scenario() -> None:
        project = await create_project(tmp_path, "existing")
        config = load_config(project / "config.toml")
        config["validation"]["translation"]["validators"] = ["japanese_kana"]
        config["validation"]["translation"]["max_retry_attempts"] = 1
        config["validation"]["translation"]["exhausted_mode"] = "warning" if warning else "fail"
        (project / "config.toml").write_text(dump_config(config), encoding="utf-8")
        bodies = []

        def respond(request: httpx.Request) -> httpx.Response:
            body = json.loads(json.loads(request.content)["messages"][1]["content"])
            bodies.append(body)
            return httpx.Response(200, json={"choices": [{"message": {"content": llm_jsonl([
                dict(type="segment", id=body["segments"][0]["id"], translation="こんにちは")
            ])}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            if warning:
                result = await translate_text(project, "こんにちは", http_client=client)
                assert result["validation_findings"]
                assert result["warnings"]
            else:
                with pytest.raises(IncompleteError, match="校验"):
                    await translate_text(project, "こんにちは", http_client=client)
        assert len(bodies) == 2
        assert bodies[1]["validation_repair"]
        assert list_run_index(project) == ([], 0)

    asyncio.run(scenario())


def test_empty_temporary_translation_is_rejected(tmp_path: Path) -> None:
    from app.text_translation import translate_text

    with pytest.raises(UsageError, match="不能为空"):
        asyncio.run(translate_text(tmp_path, " \n "))


@pytest.mark.parametrize("rejection", ["context", "format"])
def test_temporary_translation_recovers_and_reassembles_one_segment(tmp_path: Path, rejection: str) -> None:
    from app.text_translation import translate_text

    async def scenario() -> None:
        project = await create_project(tmp_path, "existing")
        bodies = []

        def respond(request: httpx.Request) -> httpx.Response:
            body = json.loads(json.loads(request.content)["messages"][1]["content"])
            bodies.append(body)
            if len(bodies) == 1:
                if rejection == "format":
                    return httpx.Response(200, json={"choices": [{"message": {"content": llm_jsonl([])}}]})
                return httpx.Response(400, json={"error": {"message": "context length exceeded", "code": "context_length_exceeded"}})
            item = body["segments"][0]
            return httpx.Response(200, json={"choices": [{"message": {"content": llm_jsonl([
                dict(type="segment", id=item["id"], translation=item["source"].upper())
            ])}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await translate_text(project, "hello world", http_client=client)
        assert result["text"] == "HELLO WORLD"
        assert len(bodies) == (3 if rejection == "context" else 2)
        assert list_run_index(project) == ([], 0)

    asyncio.run(scenario())


def test_text_translation_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient
    from app.web import create_app
    from tests.test_web import make_project

    projects_root, project = make_project(tmp_path)
    seen = []

    async def translate(root: Path, source: str, **kwargs: object) -> dict:
        seen.append((root, source, kwargs))
        return dict(text="你好", usage=None, warnings=[], validation_findings=[])

    monkeypatch.setattr("app.text_translation.translate_text", translate)
    with TestClient(create_app(projects_root=projects_root, app_root=tmp_path / "app-root")) as client:
        response = client.post("/api/v1/projects/sample/text-translation", json={"source": "hello"})
        assert response.status_code == 200
        assert response.json()["text"] == "你好"
        assert seen[0][0:2] == (project, "hello")
        assert seen[0][2]["limiter"] is not None
        assert client.post("/api/v1/projects/sample/text-translation", json={"source": 3}).status_code == 400
