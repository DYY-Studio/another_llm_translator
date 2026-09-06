from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.errors import UsageError
from app.execution import full_prompt, stage_fingerprint
from app.llm_response import TerminologyResponseMode
from app.project import init_project
from app.sqlite_storage import read_json
from app.stage_runtime import _prompt_language, prompt_middle_digests
from app.stage_translation import run_translation
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


def test_summary_response_modes_require_independent_prompt_middle() -> None:
    with pytest.raises(UsageError, match="独立的片段概括 Prompt"):
        full_prompt(
            "terminology",
            "Project policy.",
            "en",
            response_mode=TerminologyResponseMode.SUMMARY_ONLY,
        )


def test_fragment_prompt_changes_do_not_invalidate_terminology_fingerprint(
    tmp_path: Path,
) -> None:
    from app.config import load_project_config

    project = create_project_sync(tmp_path)
    config = load_project_config(project, stage="terminology")
    baseline = stage_fingerprint(
        config,
        "terminology",
        prompt_middle_digests(project, "terminology"),
    )

    fragment = project / "prompts" / "fragment_summary.zh-CN.middle.txt"
    fragment.write_text(
        fragment.read_text(encoding="utf-8") + "\nchanged", encoding="utf-8"
    )
    assert stage_fingerprint(
        config,
        "terminology",
        prompt_middle_digests(project, "terminology"),
    ) == baseline

    terminology = project / "prompts" / "terminology.zh-CN.middle.txt"
    terminology.write_text(
        terminology.read_text(encoding="utf-8") + "\nchanged", encoding="utf-8"
    )
    assert stage_fingerprint(
        config,
        "terminology",
        prompt_middle_digests(project, "terminology"),
    ) != baseline


def test_full_prompt_rejects_unknown_language() -> None:
    from app.errors import UsageError

    with pytest.raises(UsageError):
        full_prompt("translation", "middle", "fr")


def test_prompt_language_resolution_rejects_missing_requested_language(
    tmp_path: Path,
) -> None:
    project = create_project_sync(tmp_path)
    en_file = project / "prompts" / "translation.en.middle.txt"
    en_file.unlink()
    with pytest.raises(UsageError) as error:
        _prompt_language(project, "translation", "en")
    assert error.value.params["reason"] == "prompt_language_missing"
    assert "translation.en.middle.txt" in str(error.value)


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
    assert (
        stage_fingerprint(config, "translation", {"zh-CN": zh_digest, "en": en_digest})
        == baseline
    )
    assert (
        stage_fingerprint(config, "translation", {"zh-CN": zh_digest, "en": "x"})
        != baseline
    )
    assert (
        stage_fingerprint(config, "translation", {"zh-CN": "x", "en": en_digest})
        != baseline
    )


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
    assert seen_system and "The user message is JSON" in seen_system[0]
    manifest = read_json(
        project, project / "runs" / summary["run_id"] / "manifest.json"
    )
    assert manifest["prompt_language"] == "en"


def test_cli_language_follows_another_llm_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_llm_preset(tmp_path)
    monkeypatch.setenv("ANOTHER_LLM_LANGUAGE", "en")
    project = create_project_sync(tmp_path)
    assert _prompt_language(project, "translation", None) == "en"


def test_web_prompt_endpoints_serve_language_views_and_reject_unknown(
    tmp_path: Path,
) -> None:
    projects_root, _ = make_project(tmp_path)
    client = TestClient(
        create_app(
            projects_root=projects_root,
            app_root=tmp_path / "app-root",
        )
    )
    zh = client.get("/api/v1/global/prompts/translation").json()
    en = client.get(
        "/api/v1/global/prompts/translation", params={"language": "en"}
    ).json()
    assert zh["language"] == "zh-CN"
    assert en["language"] == "en"
    assert "用户消息为 JSON" in zh["assembled"]
    assert "The user message is JSON" in en["assembled"]
    assert set(en["languages"]) == {"zh-CN", "en"}

    fragment = client.get("/api/v1/global/prompts/fragment_summary")
    assert fragment.status_code == 200
    assert fragment.json()["language"] == "zh-CN"
    assert fragment.json()["assembled_mode_languages"] == {"summary-only": "zh-CN"}
    assert "概括" in fragment.json()["assembled"]
    assert "术语候选提取器" not in fragment.json()["assembled"]

    terminology = client.get("/api/v1/global/prompts/terminology").json()
    assert set(terminology["assembled_modes"]) == {
        "terms-only",
        "terms+fragment-summary",
        "summary-only",
    }
    assert terminology["assembled_mode_languages"] == {
        "terms-only": "zh-CN",
        "terms+fragment-summary": "zh-CN",
        "summary-only": "zh-CN",
    }
    assert terminology["assembled_modes"]["terms+fragment-summary"].index(
        (tmp_path / "app-root" / "prompts" / "terminology.zh-CN.middle.txt")
        .read_text(encoding="utf-8")
        .strip()
    ) < terminology["assembled_modes"]["terms+fragment-summary"].index(
        (tmp_path / "app-root" / "prompts" / "fragment_summary.zh-CN.middle.txt")
        .read_text(encoding="utf-8")
        .strip()
    )
    assert "术语候选提取器" not in terminology["assembled_modes"]["summary-only"]

    decision = client.get("/api/v1/global/prompts/terminology_decision").json()
    assert set(decision["assembled_phases"]) == {"adjudication", "consistency"}
    assert "当前是第一阶段“术语裁决”" in decision["assembled_phases"]["adjudication"]
    assert (
        "当前是第二阶段“跨术语一致性复核”"
        in decision["assembled_phases"]["consistency"]
    )

    assert (
        client.put(
            "/api/v1/global/prompts/translation",
            json={"language": "fr", "content": "x"},
        ).status_code
        == 400
    )

    assert (
        client.put(
            "/api/v1/global/prompts/translation",
            json={"language": "en", "content": "EN MIDDLE"},
        ).status_code
        == 200
    )
    saved = client.get(
        "/api/v1/global/prompts/translation", params={"language": "en"}
    ).json()
    assert saved["content"] == "EN MIDDLE"
    assert (tmp_path / "user-root" / "prompts" / "translation.en.middle.txt").read_text(
        encoding="utf-8"
    ) == "EN MIDDLE"


def test_project_terms_only_prompt_preview_survives_missing_fragment_prompt(
    tmp_path: Path,
) -> None:
    projects_root, project = make_project(tmp_path)
    (project / "prompts" / "fragment_summary.zh-CN.middle.txt").unlink()
    app_root = tmp_path / "empty-global"
    (app_root / "prompts").mkdir(parents=True)
    client = TestClient(
        create_app(projects_root=projects_root, app_root=app_root)
    )

    response = client.get("/api/v1/projects/sample/prompts/terminology")

    assert response.status_code == 200
    value = response.json()
    assert "terms-only" in value["assembled_modes"]
    assert value["assembled_mode_languages"] == {"terms-only": "zh-CN"}
    assert "terms+fragment-summary" not in value["assembled_modes"]
    assert "summary-only" not in value["assembled_modes"]


def test_project_prompt_languages_exclude_missing_project_resources(
    tmp_path: Path,
) -> None:
    projects_root, project = make_project(tmp_path)
    app_root = tmp_path / "app-root"
    (project / "prompts" / "fragment_summary.en.middle.txt").unlink()
    client = TestClient(create_app(projects_root=projects_root, app_root=app_root))

    response = client.get("/api/v1/projects/sample/prompts/fragment_summary")

    assert response.status_code == 200
    assert response.json()["languages"] == ["zh-CN"]


def test_project_prompt_preview_rejects_missing_requested_language(
    tmp_path: Path,
) -> None:
    projects_root, project = make_project(tmp_path)
    app_root = tmp_path / "app-root"
    (project / "prompts" / "terminology.en.middle.txt").unlink()
    (project / "prompts" / "fragment_summary.en.middle.txt").unlink()
    (app_root / "prompts" / "terminology.en.middle.txt").write_text(
        "__GLOBAL_TERMINOLOGY__", encoding="utf-8"
    )
    (app_root / "prompts" / "fragment_summary.en.middle.txt").write_text(
        "__GLOBAL_FRAGMENT__", encoding="utf-8"
    )
    client = TestClient(create_app(projects_root=projects_root, app_root=app_root))

    terminology = client.get(
        "/api/v1/projects/sample/prompts/terminology",
        params={"language": "en"},
    )
    fragment = client.get(
        "/api/v1/projects/sample/prompts/fragment_summary",
        params={"language": "en"},
    )

    assert terminology.status_code == 400
    assert terminology.json()["params"]["reason"] == "prompt_language_missing"
    assert "terminology.en.middle.txt" in terminology.json()["error"]
    assert fragment.status_code == 400
    assert fragment.json()["params"]["reason"] == "prompt_language_missing"
    assert "fragment_summary.en.middle.txt" in fragment.json()["error"]


def test_project_summary_preview_rejects_missing_requested_terms_language(
    tmp_path: Path,
) -> None:
    projects_root, project = make_project(tmp_path)
    (project / "prompts" / "terminology.en.middle.txt").unlink()
    (project / "prompts" / "fragment_summary.en.middle.txt").write_text(
        "__EN_FRAGMENT__", encoding="utf-8"
    )
    (project / "prompts" / "fragment_summary.zh-CN.middle.txt").write_text(
        "__ZH_FRAGMENT__", encoding="utf-8"
    )
    app_root = tmp_path / "empty-global"
    (app_root / "prompts").mkdir(parents=True)
    client = TestClient(
        create_app(projects_root=projects_root, app_root=app_root)
    )

    response = client.get(
        "/api/v1/projects/sample/prompts/terminology",
        params={"language": "en"},
    )

    assert response.status_code == 400
    assert response.json()["params"]["reason"] == "prompt_language_missing"
    assert "terminology.en.middle.txt" in response.json()["error"]


def test_prompt_library_supports_fragment_summary_resource(
    tmp_path: Path,
) -> None:
    projects_root, _ = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root))

    saved = client.put(
        "/api/v1/prompt-library/fragment_summary/en/concise",
        json={"content": "Library fragment policy."},
    )
    assert saved.status_code == 200
    detail = client.get(
        "/api/v1/prompt-library/fragment_summary/en/concise"
    )
    assert detail.status_code == 200
    assert detail.json()["content"] == "Library fragment policy."
    assert "Library fragment policy." in detail.json()["assembled"]
    assert detail.json()["assembled_mode_languages"] == {"summary-only": "en"}
    assert detail.json()["assembled_modes"] == {
        "summary-only": detail.json()["assembled"]
    }


def test_prompt_library_terminology_entry_previews_all_response_modes(
    tmp_path: Path,
) -> None:
    projects_root, _ = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root))

    saved = client.put(
        "/api/v1/prompt-library/terminology/en/concise",
        json={"content": "Library terminology policy."},
    )
    assert saved.status_code == 200

    detail = client.get("/api/v1/prompt-library/terminology/en/concise")
    assert detail.status_code == 200
    modes = detail.json()["assembled_modes"]
    assert detail.json()["assembled_mode_languages"] == {
        "terms-only": "en",
        "terms+fragment-summary": "en",
        "summary-only": "en",
    }
    assert set(modes) == {
        "terms-only",
        "terms+fragment-summary",
        "summary-only",
    }
    assert "Library terminology policy." in modes["terms-only"]
    assert "Library terminology policy." in modes["terms+fragment-summary"]
    assert "Library terminology policy." not in modes["summary-only"]


def test_prompt_library_joint_preview_falls_back_to_one_language_pair(
    tmp_path: Path,
) -> None:
    projects_root, _ = make_project(tmp_path)
    app_root = tmp_path / "app-root"
    client = TestClient(
        create_app(projects_root=projects_root, app_root=app_root)
    )

    assert client.put(
        "/api/v1/prompt-library/terminology/en/paired",
        json={"content": "__EN_TERMINOLOGY__"},
    ).status_code == 200
    assert client.put(
        "/api/v1/prompt-library/terminology/zh-CN/paired",
        json={"content": "__ZH_TERMINOLOGY__"},
    ).status_code == 200
    assert client.put(
        "/api/v1/prompt-library/fragment_summary/zh-CN/paired",
        json={"content": "__ZH_FRAGMENT__"},
    ).status_code == 200
    (app_root / "prompts" / "fragment_summary.en.middle.txt").unlink()

    detail = client.get("/api/v1/prompt-library/terminology/en/paired")

    assert detail.status_code == 200
    value = detail.json()
    assert value["assembled_mode_languages"] == {
        "terms-only": "en",
        "terms+fragment-summary": "zh-CN",
        "summary-only": "zh-CN",
    }
    assert "__ZH_TERMINOLOGY__" in value["assembled_modes"]["terms+fragment-summary"]
    assert "__ZH_FRAGMENT__" in value["assembled_modes"]["terms+fragment-summary"]
    assert "__EN_TERMINOLOGY__" not in value["assembled_modes"]["terms+fragment-summary"]


def test_web_task_start_forwards_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects_root, _ = make_project(tmp_path)
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


@pytest.mark.asyncio
async def test_english_format_correction_contains_no_chinese(tmp_path: Path) -> None:
    project = await create_project(tmp_path)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        if calls == 1:
            content = json.dumps({"segments": []})
        else:
            correction = payload["format_correction"]
            assert "current pending content" in correction
            assert "JSONL structure" in correction
            assert "fixed fields" in correction
            assert "complete" in correction
            assert "previous response" not in correction
            assert not any("\u4e00" <= char <= "\u9fff" for char in correction)
            content = llm_jsonl(
                [
                    {
                        "type": "segment",
                        "id": item["id"],
                        "translation": f"fixed:{item['source']}",
                    }
                    for item in payload["segments"]
                ]
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
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
    assert calls == 2


@pytest.mark.asyncio
async def test_english_validation_repair_defines_candidate_scope(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path)
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "validators = []", 'validators = ["japanese_kana"]'
        ),
        encoding="utf-8",
    )
    repairs = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal repairs
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        if "validation_repair" in payload:
            repairs += 1
            instruction = payload["validation_repair"]
            assert "Use failed_candidate as the base" in instruction
            assert "fix only the issues in validation_matches" in instruction
            assert not any("\u4e00" <= char <= "\u9fff" for char in instruction)
            assert all("failed_candidate" in item for item in payload["segments"])
            assert all("validation_matches" in item for item in payload["segments"])
            prefix = "repaired:"
        else:
            prefix = "candidateカ:"
        records = [
            {
                "type": "segment",
                "id": item["id"],
                "translation": f"{prefix}{item['source']}",
            }
            for item in payload["segments"]
        ]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
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
    assert repairs == 1


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
