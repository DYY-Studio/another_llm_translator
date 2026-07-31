from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.editor import EditorStore
from app.config import dump_config, load_config, load_project_config
from app.errors import UsageError
from app.execution import Scope, create_run
from app.locking import project_write_lock
from app.project import init_project
from app.storage import append_jsonl, atomic_write_json, read_json, record_header
from app.web import create_app
from app.web_tasks import WebTaskManager
from tests.test_editor import seed_conflicted_terms
from tests.test_foundation import make_app_root


def make_project(tmp_path: Path, source: str = "one\ntwo") -> tuple[Path, Path]:
    app_root = make_app_root(tmp_path)
    input_path = tmp_path / "input.txt"
    input_path.write_text(source, encoding="utf-8")
    projects_root = tmp_path / "projects"
    project, _ = init_project(
        [str(input_path)],
        name="sample",
        app_root=app_root,
        projects_root=projects_root,
    )
    assert project is not None
    return projects_root, project


def test_web_lists_project_edits_translation_and_rejects_remote_origin(
    tmp_path: Path,
) -> None:
    projects_root, _ = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root))

    listed = client.get("/api/v1/projects")
    assert listed.status_code == 200
    assert listed.json()["projects"][0]["name"] == "sample"
    overview = client.get("/api/v1/projects/sample").json()
    segment_id = overview["segments"][0]["segment_id"]
    saved = client.post(
        "/api/v1/projects/sample/translations",
        json={"segment_id": segment_id, "text": "一"},
    )
    assert saved.status_code == 200
    assert saved.json()["text"] == "一"
    assert (
        client.get(
            "/api/v1/projects",
            headers={"Origin": "https://remote.example"},
        ).status_code
        == 403
    )


def test_web_build_includes_editor_layout_context_and_theme_controls(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(projects_root=tmp_path / "projects"))
    page = client.get("/")
    assert page.status_code == 200
    assert "document.documentElement.dataset.theme" in page.text
    assert "minimal-llm-translator.theme.v1" in page.text
    asset = re.search(r'<script type="module"[^>]+src="([^"]+)"', page.text)
    assert asset is not None
    script = client.get(asset.group(1))
    assert script.status_code == 200
    stylesheet = re.search(r'<link rel="stylesheet"[^>]+href="([^"]+)"', page.text)
    assert stylesheet is not None
    css = client.get(stylesheet.group(1))
    assert css.status_code == 200
    assert ".segment-batch-actions" in css.text
    assert ".settings-tabs{position:sticky;top:58px" in css.text
    for text in (
        "terms-workspace",
        "segment-batch-actions",
        "只看冲突",
        "显示已移除",
        "全部状态",
        "上下文",
        "当前外观",
        "跟随系统",
        "运行当前阶段",
        "复用已有结果",
        "强制重做全部",
        "续用原 Run",
        "移除所选",
        "清除所选",
        "全部清除",
        "应用所选",
        "全部应用",
        "导入术语表",
        "导出术语表",
        "项目与输入",
        "LLM 与采样",
        "执行与分块",
        "参考上下文",
        "翻译校验与重试",
        "调试与故障注入",
        "全局配置",
        "全局 Prompt",
        "LLM Preset",
        "附加 JSON Body",
        "最终请求预览",
        "同步全局模板",
        "保存当前连接并切换",
        "复制全局 Adapter",
    ):
        assert text in script.text
    assert "保存前会严格验证完整 TOML" not in script.text


def test_web_creates_project_from_uploaded_files(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    app = create_app(projects_root=projects_root)
    client = TestClient(app)
    response = client.post(
        "/api/v1/projects",
        data={"name": "uploaded"},
        files=[
            ("files", ("first.txt", b"one", "text/plain")),
            ("files", ("second.txt", b"two", "text/plain")),
        ],
    )
    # The endpoint uses the repository global template bundle.
    assert response.status_code == 200
    overview = client.get("/api/v1/projects/uploaded").json()
    assert [item["name"] for item in overview["files"]] == [
        "first.txt",
        "second.txt",
    ]
    adapters = client.get("/api/v1/document-adapters").json()["adapters"]
    assert [item["adapter_id"] for item in adapters] == ["epub", "txt"]


def test_web_creates_empty_project_and_manages_source_files(
    tmp_path: Path,
) -> None:
    projects_root = tmp_path / "projects"
    app = create_app(projects_root=projects_root)
    client = TestClient(app)
    created = client.post(
        "/api/v1/projects",
        data={
            "name": "empty",
            "document_adapter": "txt",
            "empty": "true",
        },
    )
    assert created.status_code == 200
    overview = client.get("/api/v1/projects/empty").json()
    assert overview["nonempty_segment_count"] == 0
    assert overview["files"] == []
    assert (
        client.get(
            "/api/v1/projects/empty/task-options/translation"
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/v1/projects/empty/tasks",
            json={"stage": "translation"},
        ).status_code
        == 400
    )
    assert app.state.tasks.tasks == {}

    added = client.post(
        "/api/v1/projects/empty/files",
        files=[
            ("files", ("first.txt", b"one", "text/plain")),
            ("files", ("second.txt", b"two", "text/plain")),
        ],
    )
    assert added.status_code == 200
    assert added.json()["added_file_ids"] == ["F0001", "F0002"]
    overview = client.get("/api/v1/projects/empty").json()
    assert [item["document_adapter_id"] for item in overview["files"]] == [
        "txt",
        "txt",
    ]
    removed = client.post(
        "/api/v1/projects/empty/files/remove",
        json={"file_ids": ["F0001", "F0002"]},
    )
    assert removed.status_code == 200
    overview = client.get("/api/v1/projects/empty").json()
    assert overview["files"] == []
    assert overview["segments"] == []


def test_web_reads_and_saves_typed_project_config(tmp_path: Path) -> None:
    projects_root, project = make_project(tmp_path)
    client = TestClient(create_app(
        projects_root=projects_root,
        app_root=tmp_path / "app-root",
    ))

    response = client.get("/api/v1/projects/sample/config")
    assert response.status_code == 200
    config = response.json()["config"]
    config["project"]["target_language"] = "繁体中文"
    config["input"]["encoding_confidence_threshold"] = 0.6
    config["llm"]["temperature_translation"] = 0.25
    config["execution"]["scheduling_mode"] = "parallel"
    config["chunking"]["allow_split_oversized_segment"] = False
    config["context"]["translation"]["previous_segments"] = 4
    config["terminology"]["alias_primary_collision"] = "merge"
    config["validation"]["translation"]["exhausted_mode"] = "warning"
    config["retry"]["jitter_seconds"] = 0.2
    config["debug"]["inject_429_every"] = 7

    saved = client.put(
        "/api/v1/projects/sample/config",
        json={"config": config},
    )
    assert saved.status_code == 200
    assert client.get("/api/v1/projects/sample/config").json()["config"] == config
    assert load_config(project / "config.toml") == config


@pytest.mark.parametrize(
    "mutate",
    [
        lambda config: config["retry"].__setitem__("http_max_attempts", 1.5),
        lambda config: config["retry"].__setitem__("max_delay_seconds", -1),
        lambda config: config["project"].__setitem__("unknown", True),
        lambda config: config.__delitem__("chunking"),
    ],
)
def test_web_rejects_invalid_project_config_without_changing_file(
    tmp_path: Path,
    mutate: object,
) -> None:
    projects_root, project = make_project(tmp_path)
    client = TestClient(create_app(
        projects_root=projects_root,
        app_root=tmp_path / "app-root",
    ))
    original = (project / "config.toml").read_bytes()
    config = client.get("/api/v1/projects/sample/config").json()["config"]
    mutate(config)  # type: ignore[operator]

    response = client.put(
        "/api/v1/projects/sample/config",
        json={"config": config},
    )

    assert response.status_code == 400
    assert (project / "config.toml").read_bytes() == original


def test_web_config_requires_object_and_existing_matching_adapter(
    tmp_path: Path,
) -> None:
    projects_root, project = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root))
    original = (project / "config.toml").read_bytes()

    assert client.put(
        "/api/v1/projects/sample/config",
        json={"config": "raw toml"},
    ).status_code == 400
    config = client.get("/api/v1/projects/sample/config").json()["config"]
    config["llm"]["preset"] = "missing"
    assert client.put(
        "/api/v1/projects/sample/config",
        json={"config": config},
    ).status_code == 400
    assert (project / "config.toml").read_bytes() == original


def test_web_edits_global_templates_without_changing_existing_project(
    tmp_path: Path,
) -> None:
    projects_root, project = make_project(tmp_path)
    app_root = tmp_path / "app-root"
    client = TestClient(create_app(
        projects_root=projects_root,
        app_root=app_root,
    ))
    project_config = (project / "config.toml").read_bytes()
    project_prompt = (
        project / "prompts" / "translation.middle.txt"
    ).read_bytes()
    config = client.get("/api/v1/global/config").json()["config"]
    config["project"]["target_language"] = "繁體中文"

    assert client.put(
        "/api/v1/global/config", json={"config": config}
    ).status_code == 200
    assert client.put(
        "/api/v1/global/prompts/translation",
        json={"content": "GLOBAL TRANSLATION PROMPT"},
    ).status_code == 200
    assert (project / "config.toml").read_bytes() == project_config
    assert (
        project / "prompts" / "translation.middle.txt"
    ).read_bytes() == project_prompt

    created = client.post(
        "/api/v1/projects",
        data={"name": "new-project", "document_adapter": "txt", "empty": "true"},
    )
    assert created.status_code == 200
    new_project = projects_root / "new-project"
    assert load_config(new_project / "config.toml")["project"][
        "target_language"
    ] == "繁體中文"
    assert (
        new_project / "prompts" / "translation.middle.txt"
    ).read_text("utf-8") == "GLOBAL TRANSLATION PROMPT"


def test_web_manages_presets_and_previews_merged_extra_body(
    tmp_path: Path,
) -> None:
    projects_root, _ = make_project(tmp_path)
    app_root = tmp_path / "app-root"
    client = TestClient(create_app(
        projects_root=projects_root,
        app_root=app_root,
    ))
    default = client.get("/api/v1/global/presets/default").json()
    custom = {
        **default,
        "preset_id": "openrouter",
        "model": "provider/model",
        "extra_body": {
            "provider": {
                "order": ["anthropic", "google"],
                "allow_fallbacks": False,
            }
        },
    }
    saved = client.put("/api/v1/global/presets/openrouter", json=custom)
    assert saved.status_code == 200
    preview = client.get(
        "/api/v1/global/presets/openrouter/preview"
    ).json()
    assert preview["headers"]["Authorization"] == "Bearer ***"
    assert preview["body"]["provider"] == custom["extra_body"]["provider"]
    assert preview["body"]["model"] == "provider/model"

    conflict = {**custom, "preset_id": "conflict", "extra_body": {"model": "x"}}
    assert client.put(
        "/api/v1/global/presets/conflict", json=conflict
    ).status_code == 400
    secret = {**custom, "preset_id": "secret", "extra_body": {"x": "${api_key}"}}
    assert client.put(
        "/api/v1/global/presets/secret", json=secret
    ).status_code == 400
    assert not (app_root / "llm_presets" / "conflict.json").exists()
    assert not (app_root / "llm_presets" / "secret.json").exists()

    config = client.get("/api/v1/global/config").json()["config"]
    config["llm"]["preset"] = "openrouter"
    assert client.put(
        "/api/v1/global/config", json={"config": config}
    ).status_code == 200
    rejected = client.delete("/api/v1/global/presets/openrouter")
    assert rejected.status_code == 400
    assert "全局配置正在使用" in rejected.json()["error"]


def test_web_explicitly_copies_adapter_and_migrates_legacy_config(
    tmp_path: Path,
) -> None:
    projects_root, project = make_project(tmp_path)
    app_root = tmp_path / "app-root"
    client = TestClient(create_app(
        projects_root=projects_root,
        app_root=app_root,
    ))
    raw = load_config(project / "config.toml")
    resolved = load_project_config(project, presets_root=app_root)
    legacy = deepcopy(raw)
    legacy["llm"].pop("preset")
    legacy["llm"].update(
        {
            key: resolved["llm"][key]
            for key in (
                "adapter",
                "base_url",
                "endpoint",
                "model",
                "api_key_env",
                "proxy_url",
                "max_output_tokens",
                "context_window_tokens",
                "context_safety_margin_tokens",
            )
        }
    )
    legacy["execution"].update(
        {
            key: resolved["execution"][key]
            for key in (
                "max_parallel",
                "requests_per_minute",
                "input_tokens_per_minute",
                "request_timeout_seconds",
                "token_safety_factor",
            )
        }
    )
    (project / "config.toml").write_text(dump_config(legacy), encoding="utf-8")
    migrated = client.post(
        "/api/v1/projects/sample/migrate-llm-preset",
        json={"preset_id": "legacy-saved"},
    )
    assert migrated.status_code == 200
    assert (app_root / "llm_presets" / "legacy-saved.json").is_file()
    migrated_config = load_config(project / "config.toml")
    assert migrated_config["llm"]["preset"] == "legacy-saved"
    assert "model" not in migrated_config["llm"]

    global_adapter = json.loads(
        (app_root / "llm_adapters" / "openai-compatible.json").read_text("utf-8")
    )
    global_adapter["adapter_id"] = "alternate"
    (app_root / "llm_adapters" / "alternate.json").write_text(
        json.dumps(global_adapter), encoding="utf-8"
    )
    copied = client.post(
        "/api/v1/projects/sample/adapters/copy-global",
        json={"adapter_id": "alternate"},
    )
    assert copied.status_code == 200
    assert (project / "llm_adapters" / "alternate.json").is_file()


def test_web_file_removal_is_all_or_nothing(tmp_path: Path) -> None:
    projects_root, project = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root))
    before = (project / "source" / "files.jsonl").read_bytes()
    response = client.post(
        "/api/v1/projects/sample/files/remove",
        json={"file_ids": ["F0001", "F9999"]},
    )
    assert response.status_code == 400
    assert (project / "source" / "files.jsonl").read_bytes() == before


def test_web_edits_removes_restores_and_validates_terms(tmp_path: Path) -> None:
    projects_root, project = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root))

    empty = client.get("/api/v1/projects/sample/terms")
    assert empty.status_code == 200
    assert empty.json()["terms"] == []

    added = client.post(
        "/api/v1/projects/sample/terms",
        json={
            "source": "Alice",
            "preferred_translation": "爱丽丝",
            "category": "人物",
            "description": "主角",
            "aliases": ["A"],
            "disabled": False,
        },
    )
    assert added.status_code == 200
    assert added.json()["terms_revision"] == 1
    assert added.json()["terms"][0]["preferred_translation"] == "爱丽丝"

    renamed = client.post(
        "/api/v1/projects/sample/terms",
        json={
            "old_normalized": "alice",
            "source": "Alice Liddell",
            "preferred_translation": "爱丽丝·利德尔",
            "category": "人物",
            "description": "主角",
            "aliases": ["Alice"],
            "disabled": False,
        },
    )
    assert renamed.status_code == 200
    assert any(
        item["normalized"] == "alice liddell" and not item["disabled"]
        for item in renamed.json()["terms"]
    )

    removed = client.post(
        "/api/v1/projects/sample/terms",
        json={
            "old_normalized": "alice liddell",
            "source": "Alice Liddell",
            "preferred_translation": "爱丽丝·利德尔",
            "category": "人物",
            "description": "主角",
            "aliases": ["Alice"],
            "disabled": True,
        },
    )
    assert removed.status_code == 200
    assert next(
        item
        for item in removed.json()["terms"]
        if item["normalized"] == "alice liddell"
    )["disabled"]

    restored = client.post(
        "/api/v1/projects/sample/terms",
        json={
            "old_normalized": "alice liddell",
            "source": "Alice Liddell",
            "preferred_translation": "爱丽丝·利德尔",
            "category": "人物",
            "description": "主角",
            "aliases": ["Alice"],
            "disabled": False,
        },
    )
    assert restored.status_code == 200
    assert not next(
        item
        for item in restored.json()["terms"]
        if item["normalized"] == "alice liddell"
    )["disabled"]

    seed_conflicted_terms(project)
    unresolved = client.post(
        "/api/v1/projects/sample/terms",
        json={
            "old_normalized": "alpha",
            "source": "Alpha",
            "preferred_translation": "阿尔法",
            "category": "",
            "description": "category conflict",
            "aliases": [],
            "disabled": False,
        },
    )
    assert unresolved.status_code == 400
    assert "类别冲突尚未裁决" in unresolved.json()["error"]


def test_web_imports_exports_and_bulk_removes_terms(tmp_path: Path) -> None:
    projects_root, _ = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root))
    document = {
        "schema_version": 1,
        "record_type": "terminology_exchange",
        "terms": [
            {
                "source": source,
                "preferred_translation": translated,
                "category": "人物",
                "description": "imported",
                "aliases": [],
                "disabled": False,
                "conflicts": {
                    "categories": [],
                    "preferred_translations": [],
                },
            }
            for source, translated in (("Alice", "爱丽丝"), ("Bob", "鲍勃"))
        ],
    }
    imported = client.post(
        "/api/v1/projects/sample/terms/import",
        files={
            "file": (
                "terms.json",
                json.dumps(document).encode(),
                "application/json",
            )
        },
    )
    assert imported.status_code == 200
    assert imported.json()["imported"] == 2

    removed = client.post(
        "/api/v1/projects/sample/terms/remove",
        json={"normalized": ["alice", "bob"]},
    )
    assert removed.status_code == 200
    assert removed.json()["removed"] == 2
    assert removed.json()["terms_revision"] == 2

    visible = client.get(
        "/api/v1/projects/sample/terms/export",
        params={"format": "json"},
    )
    assert visible.status_code == 200
    assert json.loads(visible.content)["terms"] == []
    complete = client.get(
        "/api/v1/projects/sample/terms/export",
        params={"format": "csv", "include_disabled": "true"},
    )
    assert complete.status_code == 200
    assert "Alice" in complete.content.decode("utf-8-sig")


def test_web_resets_results_and_applies_explicit_segments(tmp_path: Path) -> None:
    projects_root, project = make_project(tmp_path)
    store = EditorStore(project)
    for segment_id in ("F0001-S000001", "F0001-S000002"):
        store.save_translation({"segment_id": segment_id, "text": segment_id})
        store.save_review(
            {
                "stage": "proofreading",
                "segment_id": segment_id,
                "review_status": "accepted",
                "apply": False,
            }
        )
    client = TestClient(create_app(projects_root=projects_root))
    applied = client.post(
        "/api/v1/projects/sample/apply",
        json={
            "stage": "proofreading",
            "segment_ids": ["F0001-S000001"],
            "all": True,
            "allow_outdated_base": False,
        },
    )
    assert applied.status_code == 200
    overview = client.get("/api/v1/projects/sample").json()
    assert overview["segments"][0]["reviews"]["proofreading"]["applied"] is not None
    assert overview["segments"][1]["reviews"]["proofreading"]["applied"] is None

    reset = client.post(
        "/api/v1/projects/sample/results/reset",
        json={
            "stage": "proofreading",
            "segment_ids": ["F0001-S000001"],
        },
    )
    assert reset.status_code == 200
    assert reset.json()["reset_records"] == 2
    overview = client.get("/api/v1/projects/sample").json()
    assert overview["segments"][0]["reviews"]["proofreading"]["suggestion"] is None
    assert overview["segments"][0]["reviews"]["proofreading"]["applied"] is None


def test_web_adapter_validation_preview_and_secret_redaction(
    tmp_path: Path,
) -> None:
    projects_root, project = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root))
    adapter = client.get(
        "/api/v1/projects/sample/adapters/openai-compatible"
    ).json()
    adapter["body"]["response_format"] = {"type": "json_object"}
    saved = client.put(
        "/api/v1/projects/sample/adapters/openai-compatible",
        json=adapter,
    )
    assert saved.status_code == 200
    assert json.loads(
        (
            project / "llm_adapters" / "openai-compatible.json"
        ).read_text(encoding="utf-8")
    )["body"]["response_format"] == {"type": "json_object"}
    preview = client.get(
        "/api/v1/projects/sample/adapter-preview"
    ).json()
    assert preview["headers"]["Authorization"] == "Bearer ***"
    assert "***" not in json.dumps(preview["body"])


def test_web_task_options_report_mixed_fingerprints_and_reject_missing_choice(
    tmp_path: Path,
) -> None:
    projects_root, project = make_project(tmp_path)
    store = EditorStore(project)
    segment_id = store.overview()["segments"][0]["segment_id"]
    store.save_translation({"segment_id": segment_id, "text": "一"})
    prompt_path = project / "prompts" / "translation.middle.txt"
    prompt_path.write_text(
        prompt_path.read_text(encoding="utf-8") + "\nchanged",
        encoding="utf-8",
    )
    app = create_app(projects_root=projects_root)
    client = TestClient(app)

    options = client.get(
        "/api/v1/projects/sample/task-options/translation"
    )
    assert options.status_code == 200
    assert options.json()["selected"] == 2
    assert options.json()["completed"] == 1
    assert options.json()["current_fingerprint_completed"] == 0
    assert options.json()["mismatched_fingerprint_completed"] == 1
    assert options.json()["running_run"] is None

    undecided = client.post(
        "/api/v1/projects/sample/tasks",
        json={"stage": "translation"},
    )
    assert undecided.status_code == 400
    assert "必须明确选择复用或 force" in undecided.json()["error"]
    assert app.state.tasks.tasks == {}

    invalid_boolean = client.post(
        "/api/v1/projects/sample/tasks",
        json={"stage": "translation", "force": "true"},
    )
    assert invalid_boolean.status_code == 400
    assert "force 必须是布尔值" in invalid_boolean.json()["error"]
    conflicting = client.post(
        "/api/v1/projects/sample/tasks",
        json={
            "stage": "translation",
            "force": True,
            "reuse_mixed_fingerprints": True,
        },
    )
    assert conflicting.status_code == 400
    assert "不能同时使用" in conflicting.json()["error"]
    assert app.state.tasks.tasks == {}


def test_web_task_options_require_explicit_running_run_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects_root, project = make_project(tmp_path)
    run_id, _ = create_run(
        project,
        config=load_project_config(project),
        stage="translation",
        fingerprint="old",
        prompt="old prompt",
        selected_count=2,
        requested_count=2,
        reused_count=0,
        details={
            "scope": {
                "all_nonempty": True,
                "from_file": None,
                "only_file": None,
                "only_segment": None,
                "force": False,
            }
        },
    )
    calls: list[dict[str, object]] = []

    async def fake_translation(
        _: Path,
        scope: Scope,
        **kwargs: object,
    ) -> dict[str, object]:
        calls.append({"force": scope.force, **kwargs})
        return {"failed": 0, "pending": 0}

    monkeypatch.setattr("app.web_tasks.run_translation", fake_translation)
    app = create_app(projects_root=projects_root)
    with TestClient(app) as client:
        options = client.get(
            "/api/v1/projects/sample/task-options/translation"
        ).json()
        assert options["running_run"]["run_id"] == run_id
        assert options["running_run"]["scope"]["all_nonempty"] is True
        assert options["running_run"]["previous"]["model"]
        assert options["running_run"]["current"]["endpoint"]

        undecided = client.post(
            "/api/v1/projects/sample/tasks",
            json={"stage": "translation"},
        )
        assert undecided.status_code == 400
        assert "必须选择续用或结束并新建" in undecided.json()["error"]
        assert app.state.tasks.tasks == {}

        ignored_options = client.post(
            "/api/v1/projects/sample/tasks",
            json={
                "stage": "translation",
                "run_action": "resume",
                "force": True,
            },
        )
        assert ignored_options.status_code == 400
        assert "续用 Run 时不能" in ignored_options.json()["error"]
        assert app.state.tasks.tasks == {}

        resumed = client.post(
            "/api/v1/projects/sample/tasks",
            json={"stage": "translation", "run_action": "resume"},
        )
        assert resumed.status_code == 200
        task_id = resumed.json()["task_id"]
        for _ in range(20):
            state = client.get(f"/api/v1/tasks/{task_id}").json()
            if state["status"] == "completed":
                break
        assert state["status"] == "completed"
        assert calls[0]["resume_run_id"] == run_id
        assert calls[0]["force"] is False


@pytest.mark.asyncio
async def test_web_task_manager_forwards_force_and_fingerprint_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, project = make_project(tmp_path)
    store = EditorStore(project)
    segment_id = store.overview()["segments"][0]["segment_id"]
    store.save_translation({"segment_id": segment_id, "text": "一"})
    prompt_path = project / "prompts" / "translation.middle.txt"
    prompt_path.write_text(
        prompt_path.read_text(encoding="utf-8") + "\nchanged",
        encoding="utf-8",
    )
    calls: list[tuple[bool, bool]] = []

    async def fake_translation(
        _: Path,
        scope: Scope,
        **kwargs: object,
    ) -> dict[str, object]:
        calls.append(
            (scope.force, bool(kwargs["reuse_mixed_fingerprints"]))
        )
        return {"failed": 0, "pending": 0}

    monkeypatch.setattr("app.web_tasks.run_translation", fake_translation)
    manager = WebTaskManager()
    forced = await manager.start(
        project,
        "translation",
        scope=Scope(force=True),
        reuse_mixed_fingerprints=False,
        run_action=None,
    )
    await manager.tasks[forced["task_id"]].asyncio_task
    reused = await manager.start(
        project,
        "translation",
        scope=Scope(),
        reuse_mixed_fingerprints=True,
        run_action=None,
    )
    await manager.tasks[reused["task_id"]].asyncio_task
    assert calls == [(True, False), (False, True)]


def test_web_task_options_include_completed_terminology_scans(
    tmp_path: Path,
) -> None:
    projects_root, project = make_project(tmp_path)
    project_id = str(read_json(project / "project.json")["project_id"])
    task_id = "TERM-TASK-COMPLETED"
    atomic_write_json(
        project / "terminology" / "active_task.json",
        record_header(
            "terminology_task",
            project_id,
            record_id=task_id,
            active_task_id=task_id,
            status="completed",
            initial_stage_fingerprint="old",
        ),
    )
    append_jsonl(
        project / "terminology" / "scans.jsonl",
        record_header(
            "terminology_scan",
            project_id,
            stage="terminology",
            segment_id="F0001-S000001",
            status="completed",
            active_task_id=task_id,
            stage_fingerprint="old",
            run_id="RUN-OLD",
            request_id="REQ-OLD",
        ),
    )
    client = TestClient(create_app(projects_root=projects_root))
    options = client.get(
        "/api/v1/projects/sample/task-options/terminology"
    ).json()
    assert options["completed"] == 1
    assert options["pending"] == 1
    assert options["mismatched_fingerprint_completed"] == 1


def test_project_write_lock_rejects_second_writer(tmp_path: Path) -> None:
    _, project = make_project(tmp_path)
    with project_write_lock(project):
        with pytest.raises(UsageError, match="另一个写入任务"):
            with project_write_lock(project):
                pass


def test_web_task_manager_allows_one_task_and_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects_root, _ = make_project(tmp_path)
    entered = asyncio.Event()

    async def fake_translation(*_: object, **__: object) -> dict[str, object]:
        entered.set()
        await asyncio.Future()
        return {}

    monkeypatch.setattr("app.web_tasks.run_translation", fake_translation)
    with TestClient(create_app(projects_root=projects_root)) as client:
        first = client.post(
            "/api/v1/projects/sample/tasks",
            json={"stage": "translation"},
        )
        assert first.status_code == 200
        task_id = first.json()["task_id"]
        second = client.post(
            "/api/v1/projects/sample/tasks",
            json={"stage": "translation"},
        )
        assert second.status_code == 400
        cancelled = client.post(f"/api/v1/tasks/{task_id}/cancel")
        assert cancelled.status_code == 200
        for _ in range(20):
            state = client.get(f"/api/v1/tasks/{task_id}").json()
            if state["status"] == "cancelled":
                break
        assert state["status"] == "cancelled"
