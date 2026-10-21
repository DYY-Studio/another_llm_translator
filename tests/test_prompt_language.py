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


def test_translation_prompt_describes_summary_context_relation() -> None:
    for language in ("zh-CN", "en"):
        prompt = full_prompt("translation", "middle", language)
        assert "summary_context_relation" in prompt
        assert "previous_only" in prompt
        assert "partial_overlap" in prompt
        assert "contains_all_current" in prompt


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


@pytest.mark.parametrize("language", ["zh-CN", "en"])
def test_repetition_prompt_previews_use_execution_prompts(tmp_path: Path, language: str) -> None:
    from app.repetition_consistency import final_review_prompt

    projects_root, _ = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root, app_root=tmp_path / "app-root"))
    stage = "repetition_consistency"
    library = f"/api/v1/prompt-library/{stage}/{language}/policy"
    assert client.put(library, json={"content": "Custom review policy"}).status_code == 200
    for path in (
        f"/api/v1/global/prompts/{stage}",
        f"/api/v1/projects/sample/prompts/{stage}",
        f"/api/v1/prompts/{stage}/default",
        library,
    ):
        response = client.get(path, params={"language": language})
        assert response.status_code == 200
        view = response.json()
        comparison = full_prompt(stage, view["content"], language)
        assert view["assembled_phases"] == {
            "comparison": comparison,
            "final_review": final_review_prompt(comparison, language),
        }
        assert view["assembled"] == comparison


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
        "terms+translation",
        "summary-only",
        "terms+translation+fragment-summary",
    }
    assert terminology["assembled_mode_languages"] == {
        "terms-only": "zh-CN",
        "terms+fragment-summary": "zh-CN",
        "terms+translation": "zh-CN",
        "summary-only": "zh-CN",
        "terms+translation+fragment-summary": "zh-CN",
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
    assert set(decision["assembled_phases"]) == {
        "adjudication",
        "consistency",
        "final_review", "containment",
    }
    assert "当前是第一阶段“术语裁决”" in decision["assembled_phases"]["adjudication"]
    assert (
        "当前是第二阶段“跨术语一致性复核”"
        in decision["assembled_phases"]["consistency"]
    )
    assert "当前是第三阶段“术语自动终审”" in decision["assembled_phases"]["final_review"]

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
    assert value["assembled_mode_languages"] == {
        "terms-only": "zh-CN",
        "terms+translation": "zh-CN",
    }
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
        "terms+translation": "en",
        "summary-only": "en",
    }
    assert set(modes) == {
        "terms-only",
        "terms+fragment-summary",
        "terms+translation",
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
        "terms+translation": "en",
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
        started = client.post(
            "/api/v1/projects/sample/tasks",
            json={"stage": "translation", "language": "en", "force": True},
        )
        rejected = client.post(
            "/api/v1/projects/sample/tasks",
            json={"stage": "translation", "language": "fr", "force": True},
        )
        from tests.helpers import wait_for_web_task
        wait_for_web_task(client, started.json()["task_id"])
    assert rejected.status_code == 400
    assert calls and calls[0]["prompt_language"] == "en"


@pytest.mark.asyncio
async def test_english_format_retry_does_not_add_instructions(tmp_path: Path) -> None:
    project = await create_project(tmp_path)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        if calls == 1:
            content = json.dumps({"segments": []})
        else:
            assert "format_correction" not in payload
            assert "validation_repair" not in payload
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
async def test_english_validation_repair_sends_self_contained_guidance(
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
            assert instruction == "Translate Japanese kana into the target language."
            assert all(set(item) == {"id", "source"} for item in payload["segments"])
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


def test_draft_prompt_preview_uses_runtime_contract_and_current_language(
    tmp_path: Path,
) -> None:
    projects_root, project = make_project(tmp_path)
    language = "en"
    translation = project / "prompts" / "translation.en.middle.txt"
    translation.write_text("Current project translation policy.", encoding="utf-8")
    client = TestClient(create_app(projects_root=projects_root))
    value = client.get(
        "/api/v1/projects/sample/prompts/terminology", params={"language": language}
    ).json()
    expected = full_prompt(
        "terminology",
        value["content"],
        language,
        response_mode="terms+translation",
        translation_middle=translation.read_text(),
    )
    assert value["assembled_modes"]["terms+translation"] == expected
    assert "no_terms" in expected
    assert "Current project translation policy." in expected
    translation.unlink()
    value = client.get(
        "/api/v1/projects/sample/prompts/terminology", params={"language": language}
    ).json()
    assert "terms+translation" not in value["assembled_modes"]
    assert "terms+translation" in value["assembled_mode_errors"]


def test_project_prompt_preview_includes_selected_adapter_requirements(
    tmp_path: Path,
) -> None:
    from tests.test_documents import make_epub
    from app.execution import Scope
    from app.project import load_source_files, update_file_run_options
    from app.stage_runtime import (
        _project_context,
        _prompt_factory,
        _document_prompt_requirement_helpers,
    )
    from app.stage_terminology_draft import DraftTerminologyScan, draft_run_context

    app_root = make_app_root(tmp_path)
    txt = tmp_path / "plain.txt"
    txt.write_text("one", encoding="utf-8")
    epub = tmp_path / "ruby.epub"
    make_epub(epub)
    second_txt = tmp_path / "second.txt"
    second_txt.write_text("two", encoding="utf-8")
    second_epub = tmp_path / "second.epub"
    make_epub(second_epub)
    projects = tmp_path / "projects"
    project, _ = init_project(
        [str(txt), str(second_txt), str(epub), str(second_epub)],
        document_adapter_id=None,
        name="preview",
        app_root=app_root,
        projects_root=projects,
    )
    assert project is not None
    files = load_source_files(project)
    txt_id = next(
        item["file_id"] for item in files if item["document_adapter_id"] == "txt"
    )
    epub_id = next(
        item["file_id"] for item in files if item["document_adapter_id"] == "epub"
    )
    second_epub_id = next(
        item["file_id"] for item in files if item["original_name"] == "second.epub"
    )
    with TestClient(create_app(projects_root=projects, app_root=app_root)) as client:
        endpoint = "/api/v1/projects/preview/prompts/translation"
        txt_view = client.get(endpoint, params={"file_id": txt_id}).json()
        epub_view = client.get(endpoint, params={"file_id": epub_id}).json()
        assert "Ruby" not in txt_view["assembled"]
        assert "｜已译base《" in epub_view["assembled"]
        assert epub_view["document_context"]["file_id"] == epub_id
        groups = epub_view["document_context"]["groups"]
        assert len(groups) == 2
        assert [group["file_count"] for group in groups] == [2, 2]
        assert [group["has_requirements"] for group in groups] == [False, True]
        update_file_run_options(project, second_epub_id, {"ruby_mode": "compact"})
        compact = client.get(endpoint, params={"file_id": second_epub_id}).json()
        assert len(compact["document_context"]["groups"]) == 3
        assert "⟦R:base|Y:reading⟧" in compact["assembled"]
        assert "｜已译base《" not in compact["assembled"]
        config, _, _, segments = _project_context(project, stage="translation")
        selected = [item for item in segments if item["file_id"] == epub_id]
        requirements = _document_prompt_requirement_helpers(config, "zh-CN")[0](
            selected
        )
        assert epub_view["assembled"] == _prompt_factory(project, "translation")(
            requirements
        )
        combo = client.get(
            "/api/v1/projects/preview/prompts/terminology", params={"file_id": epub_id}
        ).json()
        draft_config, _, _, draft_segments = draft_run_context(
            project, include_summaries=True
        )
        draft_requirements = _document_prompt_requirement_helpers(
            draft_config, "zh-CN"
        )[0]([item for item in draft_segments if item["file_id"] == epub_id])
        assert combo["assembled_modes"][
            "terms+translation+fragment-summary"
        ] == _prompt_factory(
            project, "terminology", response_mode="terms+translation+fragment-summary"
        )(draft_requirements)
        for language in ("zh-CN", "en"):
            preview = client.get(
                "/api/v1/projects/preview/prompts/terminology",
                params={"file_id": epub_id, "language": language},
            ).json()
            draft_config, metadata, _, draft_segments = draft_run_context(
                project, include_summaries=True
            )
            selected = [item for item in draft_segments if item["file_id"] == epub_id]
            scan = DraftTerminologyScan(
                project,
                metadata,
                draft_config,
                Scope(),
                None,
                language,
                selected,
                set(),
            )
            for mode in preview["assembled_modes"]:
                if mode not in {item.value for item in TerminologyResponseMode}:
                    continue
                runtime = _prompt_factory(
                    project,
                    "terminology",
                    language,
                    response_mode=None if mode == "terms-only" else mode,
                )(scan.requirements(selected, TerminologyResponseMode(mode)))
                assert runtime == preview["assembled_modes"][mode]
        update_file_run_options(project, epub_id, {"ruby_mode": "base_only"})
        plain = client.get(endpoint, params={"file_id": epub_id}).json()
        assert "Ruby" not in plain["assembled"]
        assert len(plain["document_context"]["groups"]) == 2
        empty_group = next(
            group
            for group in plain["document_context"]["groups"]
            if not group["has_requirements"]
        )
        assert empty_group["file_count"] == 3
        assert empty_group["adapter_ids"] == ["txt", "epub"]
        assert client.get(endpoint, params={"file_id": "missing"}).status_code == 400

@pytest.mark.parametrize("language", ["zh-CN", "en"])
@pytest.mark.parametrize("mode", list(TerminologyResponseMode))
def test_combined_prefix_explains_only_active_fields_once(language, mode):
    from app.llm_response import response_record_types

    types = response_record_types(mode)
    prefix = full_prompt(
        "terminology",
        "term middle",
        language,
        response_mode=mode,
        fragment_summary_middle="summary middle",
        translation_middle="translation middle",
    ).split("\n\n", 1)[0]
    fields = {"target_language", "reference_context"}
    if "term" in types or "summary" in types:
        fields.add("source_segments")
    if "summary" in types:
        fields.add("source_segments[].id")
    if "segment" in types:
        fields |= {
            "segments[].source",
            "terms",
            "summary_context",
            "summary_context_relation",
            "validation_repair",
        }
    for field in fields:
        assert prefix.count(field + ":") == 1
    for field in {"segments[].current_text", "summaries"}:
        assert field + ":" not in prefix
    if "segment" not in types:
        assert "summary_context:" not in prefix
        assert "terms:" not in prefix


@pytest.mark.parametrize("language", ["zh-CN", "en"])
@pytest.mark.parametrize("mode", list(TerminologyResponseMode))
def test_result_protocols_share_one_end_rule(language, mode):
    from app.llm_response import response_record_types

    prompt = full_prompt(
        "terminology",
        "term middle",
        language,
        response_mode=mode,
        fragment_summary_middle="summary middle",
        translation_middle="translation middle",
    )
    assert prompt.count('{"type":"end"}') == 1
    assert ('{"type":"no_terms"}' in prompt) == (
        "term" in response_record_types(mode)
        and mode != TerminologyResponseMode.TERMS_ONLY
    )
    if "summary" in response_record_types(mode):
        assert (
            'one or more type="summary"' in prompt
            if language == "en"
            else '一条或多条 type="summary"' in prompt
        )

    types = response_record_types(mode)
    for record_type, middle in (("term", "term middle"), ("summary", "summary middle"), ("segment", "translation middle")):
        assert (middle in prompt) == (record_type in types)
    order_rule = "概括记录必须在术语记录之前。" if language == "zh-CN" else "Summary records must precede terminology records."
    assert (order_rule in prompt) == ("summary" in types and "term" in types)


@pytest.mark.parametrize("language", ["zh-CN", "en"])
def test_decision_prompt_previews_match_runtime_phase_contracts(tmp_path, language):
    from app.term_decision import _prompt

    projects_root, project = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root))
    runtime = _prompt(project, language, final_review=True)
    preview = client.get(
        "/api/v1/projects/sample/prompts/terminology_decision",
        params={"language": language},
    ).json()["assembled_phases"]
    knowledge = client.get(
        "/api/v1/projects/sample/prompts/terminology_knowledge",
        params={"language": language},
    ).json()["assembled"]
    assert {**preview, "knowledge": knowledge} == runtime
    for phase, prompt in runtime.items():
        prefix, _, suffix = prompt.split("\n\n", 2)
        assert prompt.count('{"type":"end"}') == 1
        assert "target_language:" in prefix
        if phase == "knowledge":
            assert "validation_feedback" in prefix
            assert "validation_repair" not in prefix
            assert "entities:" in prefix and "source_range:" in prefix
            assert '"refs":["K1"]' in suffix
            assert "decision_terms" not in prefix
        else:
            assert "validation_repair" in prefix
            assert "description" in prefix and "applied_description" in prefix
            assert "current_items:" not in prefix
            assert '"type":"decision"' in suffix
            assert "alias、description" not in prefix
            assert "alias, description" not in prefix
        if phase == "final_review":
            restriction = "禁止输出 needs_review" if language == "zh-CN" else "never output needs_review"
            assert restriction in suffix
    assert full_prompt("terminology_decision", "middle", language) == full_prompt(
        "terminology_decision", "middle", language, phase="adjudication",
    )


@pytest.mark.parametrize("language", ["zh-CN", "en"])
def test_forced_term_declaration_keeps_terms_only_protocol(language):
    prompt = full_prompt("terminology", "custom policy", language, require_term_declaration=True)
    assert prompt.count('{"type":"no_terms"}') == 1
    assert prompt.count('{"type":"end"}') == 1
    assert 'type="term"' in prompt
    assert 'type="summary"' not in prompt


@pytest.mark.parametrize("language", ["zh-CN", "en"])
def test_editable_policies_are_not_forced_by_fixed_prompt(tmp_path, language):
    project = create_project_sync(tmp_path)
    policy_texts = {
        "terminology": "身份、属性、关系及变化" if language == "zh-CN" else "identity, attributes, relationships and changes",
        "translation": "全篇实体说明" if language == "zh-CN" else "full-work entity explanations",
        "proofreading": "全篇实体说明" if language == "zh-CN" else "full-work entity explanations",
        "polishing": "全篇实体说明" if language == "zh-CN" else "full-work entity explanations",
        "content_summary": "一段简洁、连贯" if language == "zh-CN" else "one concise, coherent",
    }
    for stage, policy in policy_texts.items():
        middle = (project / "prompts" / f"{stage}.{language}.middle.txt").read_text().strip()
        custom = full_prompt(stage, "CUSTOM_POLICY", language)
        assert policy in middle and policy not in custom
        assert full_prompt(stage, middle, language) == custom.replace("CUSTOM_POLICY", middle)
    scan = full_prompt("terminology", "CUSTOM_POLICY", language)
    gender_policy = "人物性别仅在可靠时" if language == "zh-CN" else "Put gender in category only when reliable"
    assert gender_policy not in scan
    scan_middle = (project / "prompts" / f"terminology.{language}.middle.txt").read_text()
    assert gender_policy in scan_middle
    naming_middle = (project / "prompts" / f"terminology_decision.{language}.middle.txt").read_text()
    assert ("合并重复或语义等价的描述" if language == "zh-CN" else "Merge duplicate or semantically equivalent descriptions") not in naming_middle
    summary_middle = (project / "prompts" / f"fragment_summary.{language}.middle.txt").read_text()
    assert "refs" not in summary_middle
    summary = full_prompt("fragment_summary", summary_middle, language)
    assert "source_segments[].id:" in summary and "refs" in summary


@pytest.mark.parametrize("stage,language", [("translation", "zh-CN"), ("translation", "en"), ("terminology_decision", "en"), ("repetition_consistency", "en")])
def test_explicit_prompt_inheritance_is_shared_by_preview_runtime_and_fingerprint(tmp_path, stage, language):
    from app.stage_runtime import _prompt
    from app.project import ensure_missing_summary_prompts, sync_global_templates
    project = create_project_sync(tmp_path)
    app = create_app(projects_root=project.parent, app_root=tmp_path / "app-root")
    path = f"/api/v1/projects/{project.name}/prompts/{stage}"
    global_path = f"/api/v1/global/prompts/{stage}"
    local_file = project / "prompts" / f"{stage}.{language}.middle.txt"
    original = local_file.read_text()
    with TestClient(app) as client:
        assert client.request("DELETE", path, json={"language": language, "confirm": False}).status_code == 400
        assert local_file.read_text() == original
        assert client.put(global_path, json={"language": language, "content": "Global policy one"}).status_code == 200
        assert client.request("DELETE", path, json={"language": language, "confirm": True}).status_code == 200
        view = client.get(path, params={"language": language}).json()
        assert view["inherited"] and view["content"] == "Global policy one"
        assert not local_file.exists()
        assert _prompt(project, stage, language) == view["assembled"]
        digest = prompt_middle_digests(project, stage)[language]
        assert ensure_missing_summary_prompts(project, app_root=tmp_path / "app-root") == []
        sync_global_templates(project, app_root=tmp_path / "app-root", choice="update")
        assert not local_file.exists()
        assert client.put(global_path, json={"language": language, "content": "Global policy two"}).status_code == 200
        assert prompt_middle_digests(project, stage)[language] != digest
        assert "Global policy two" in _prompt(project, stage, language)
        assert client.get(path, params={"language": language}).json()["content"] == "Global policy two"
        assert client.put(path, json={"language": language, "content": "Local policy"}).status_code == 200
        assert not client.get(path, params={"language": language}).json()["inherited"]
        assert local_file.read_text() == "Local policy"
        assert client.put(global_path, json={"language": language, "content": "Global policy three"}).status_code == 200
        assert "Local policy" in _prompt(project, stage, language)


@pytest.mark.asyncio
async def test_inherited_translation_request_freezes_prompt_across_global_updates(tmp_path):
    from app.execution import Scope
    from app.prompt_resources import ProjectPrompts
    from app.user_config import write_user
    from app.locking import project_write_lock
    project = await create_project(tmp_path)
    global_file = write_user("prompts/translation.en.middle.txt")
    global_file.write_text("Global policy one")
    with project_write_lock(project):
        ProjectPrompts(project).restore("translation", "en")
    observed = []
    def handler(request):
        body = json.loads(request.content)
        observed.append(body["messages"][0]["content"])
        records = [{"type": "segment", "id": item["id"], "translation": f"译:{item['source']}"}
            for item in json.loads(body["messages"][1]["content"])["segments"]]
        return httpx.Response(200, json={"choices": [{"message": {"content": llm_jsonl(records)}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await run_translation(project, Scope(), http_client=client, prompt_language="en")
    assert observed and all("Global policy one" in prompt for prompt in observed)
    snapshot = project / "runs" / result["run_id"] / "prompt.txt"
    original = snapshot.read_text()
    global_file.write_text("Global policy two")
    assert snapshot.read_text() == original and "Global policy one" in original


def test_invalid_global_prompt_blocks_restore_without_removing_project_override(tmp_path):
    from app.user_config import write_user
    project = create_project_sync(tmp_path)
    local = project / "prompts" / "translation.en.middle.txt"
    original = local.read_bytes()
    write_user("prompts/translation.en.middle.txt").write_text("")
    with TestClient(create_app(projects_root=project.parent, app_root=tmp_path / "app-root")) as client:
        path = f"/api/v1/projects/{project.name}/prompts/translation"
        assert client.request("DELETE", path, json={"language": "en", "confirm": True}).status_code == 400
        assert local.read_bytes() == original
        assert not client.get(path, params={"language": "en"}).json()["inherited"]
