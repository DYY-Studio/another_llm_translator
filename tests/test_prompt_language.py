from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.execution import full_prompt, stage_fingerprint
from app.main import run
from app.project import init_project
from app.sqlite_storage import read_json
from app.stages import _prompt, _prompt_language, prompt_middle_digests, run_translation
from app.web import create_app
from tests.helpers import llm_jsonl, use_llm_preset
from tests.test_foundation import make_app_root


async def create_project(tmp_path: Path) -> Path:
    app_root = make_app_root(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text("one\ntwo", encoding="utf-8")
    project, _ = init_project(
        [str(source)],
        name="demo",
        app_root=app_root,
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    os.environ["LLM_API_KEY"] = "test"
    return project


def test_full_prompt_assembles_rules_and_middle_per_language() -> None:
    zh = full_prompt("translation", " 中段 ", "zh-CN")
    en = full_prompt("translation", "middle", "en")
    assert "只处理 user 消息" in zh
    assert "中段" in zh
    assert "Process only the pending content" in en
    assert "middle" in en
    assert "只处理 user 消息" not in en


def test_full_prompt_rejects_unknown_language() -> None:
    with pytest.raises(Exception):
        full_prompt("translation", "middle", "fr")


def test_prompt_language_resolution_falls_back_to_zh_cn(
    tmp_path: Path,
) -> None:
    project = create_project_sync(tmp_path)
    en_file = project / "prompts" / "translation.en.middle.txt"
    en_file.unlink()
    assert _prompt_language(project, "translation", "en") == "zh-CN"
    prompt = _prompt(project, "translation", "en")
    assert "只处理 user 消息" in prompt
    assert "忠实翻译" in prompt


def test_fingerprint_is_language_agnostic_and_tracks_any_language_change(
    tmp_path: Path,
) -> None:
    from app.config import load_project_config

    project = create_project_sync(tmp_path)
    config = load_project_config(project)
    digests = prompt_middle_digests(project, "translation")
    assert set(digests) == {"zh-CN", "en"}
    baseline = stage_fingerprint(config, "translation", digests)

    zh_digest = digests["zh-CN"]
    en_digest = digests["en"]
    assert stage_fingerprint(
        config, "translation", {"zh-CN": zh_digest, "en": en_digest}
    ) == baseline
    assert stage_fingerprint(
        config, "translation", {"zh-CN": zh_digest, "en": "x"}
    ) != baseline
    assert stage_fingerprint(
        config, "translation", {"zh-CN": "x", "en": en_digest}
    ) != baseline


@pytest.mark.asyncio
async def test_run_translation_uses_requested_language_and_records_it(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path)
    seen_system: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_system.append(body["messages"][0]["content"])
        records = [
            {"type": "segment", "id": item["id"], "translation": f"译:{item['source']}"}
            for item in json.loads(body["messages"][1]["content"])["segments"]
        ]
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": llm_jsonl(records)}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = await run_translation(
            project,
            __import__("app.execution", fromlist=["Scope"]).Scope(),
            http_client=client,
            prompt_language="en",
        )
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    assert summary["completed"] == 2
    assert seen_system and "Process only the pending content" in seen_system[0]
    manifest = read_json(
        project, project / "runs" / summary["run_id"] / "manifest.json"
    )
    assert manifest["prompt_language"] == "en"


def test_cli_language_follows_minimal_llm_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_llm_preset(tmp_path, monkeypatch)
    monkeypatch.setenv("MINIMAL_LLM_LANGUAGE", "en")
    project = create_project_sync(tmp_path)
    assert _prompt_language(project, "translation", None) == "en"


def test_web_prompt_endpoints_serve_language_views_and_reject_unknown(
    tmp_path: Path,
) -> None:
    projects_root, _ = make_project(tmp_path)
    client = TestClient(create_app(
        projects_root=projects_root,
        app_root=tmp_path / "app-root",
    ))
    zh = client.get("/api/v1/global/prompts/translation").json()
    en = client.get(
        "/api/v1/global/prompts/translation", params={"language": "en"}
    ).json()
    assert zh["language"] == "zh-CN"
    assert en["language"] == "en"
    assert "只处理 user 消息" in zh["assembled"]
    assert "Process only the pending content" in en["assembled"]
    assert set(en["languages"]) == {"zh-CN", "en"}

    assert client.put(
        "/api/v1/global/prompts/translation",
        json={"language": "fr", "content": "x"},
    ).status_code == 400

    assert client.put(
        "/api/v1/global/prompts/translation",
        json={"language": "en", "content": "EN MIDDLE"},
    ).status_code == 200
    saved = client.get(
        "/api/v1/global/prompts/translation", params={"language": "en"}
    ).json()
    assert saved["content"] == "EN MIDDLE"
    assert (
        tmp_path / "user-root" / "prompts" / "translation.en.middle.txt"
    ).read_text(encoding="utf-8") == "EN MIDDLE"


def test_web_task_start_forwards_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects_root, project = make_project(tmp_path)
    calls: list[dict[str, object]] = []

    async def fake_translation(
        _: Path,
        scope: object,
        **kwargs: object,
    ) -> dict[str, object]:
        calls.append(kwargs)
        return {"failed": 0, "pending": 0}

    monkeypatch.setattr("app.web_tasks.run_translation", fake_translation)
    app = create_app(projects_root=projects_root)
    with TestClient(app) as client:
        client.post(
            "/api/v1/projects/sample/tasks",
            json={"stage": "translation", "language": "en", "force": True},
        )
        rejected = client.post(
            "/api/v1/projects/sample/tasks",
            json={"stage": "translation", "language": "fr", "force": True},
        )
    assert rejected.status_code == 400
    assert calls and calls[0]["prompt_language"] == "en"


def create_project_sync(tmp_path: Path) -> Path:
    app_root = make_app_root(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text("one", encoding="utf-8")
    project, _ = init_project(
        [str(source)],
        name="demo",
        app_root=app_root,
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    return project


def make_project(tmp_path: Path) -> tuple[Path, Path]:
    app_root = make_app_root(tmp_path)
    input_path = tmp_path / "input.txt"
    input_path.write_text("one\ntwo", encoding="utf-8")
    projects_root = tmp_path / "projects"
    project, _ = init_project(
        [str(input_path)],
        name="sample",
        app_root=app_root,
        projects_root=projects_root,
    )
    assert project is not None
    return projects_root, project
