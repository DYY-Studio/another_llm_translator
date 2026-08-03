from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.web_store import WebStore
from app.config import load_config, load_project_config
from app.diagnostics import Diagnostics
from app.errors import UsageError
from app.execution import Scope, create_run
from app.locking import project_write_lock
from app.project import init_project
from app.storage import (
    append_jsonl,
    atomic_write_json,
    read_json,
    read_jsonl,
    record_header,
)
from app.web import create_app
from app.web_tasks import WebTaskManager
from tests.test_documents import RUBY_XHTML, make_epub
from tests.test_web_store import seed_conflicted_terms
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
    assert listed.json()["default_projects_path"] == str(projects_root.resolve())
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


def test_web_deletes_project_only_after_confirmation_and_finished_runs(
    tmp_path: Path,
) -> None:
    projects_root, project = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root))

    refused = client.request(
        "DELETE",
        "/api/v1/projects/sample",
        json={"confirm": False},
    )
    assert refused.status_code == 400
    assert project.exists()

    config = load_project_config(project)
    _, run_dir = create_run(
        project,
        config=config,
        stage="translation",
        fingerprint="test",
        prompt="test",
        selected_count=1,
        requested_count=1,
        reused_count=0,
    )
    blocked = client.request(
        "DELETE",
        "/api/v1/projects/sample",
        json={"confirm": True},
    )
    assert blocked.status_code == 400
    assert project.exists()

    manifest = read_json(run_dir / "manifest.json")
    manifest["status"] = "failed"
    atomic_write_json(run_dir / "manifest.json", manifest)
    deleted = client.request(
        "DELETE",
        "/api/v1/projects/sample",
        json={"confirm": True},
    )
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert not project.exists()
    assert client.get("/api/v1/projects").json()["projects"] == []



def test_web_creates_opens_and_remembers_external_projects(tmp_path: Path) -> None:
    projects_root, _ = make_project(tmp_path)
    app_root = tmp_path / "app-root"
    external_parent = tmp_path / "external"
    external_parent.mkdir()
    client = TestClient(
        create_app(projects_root=projects_root, app_root=app_root)
    )

    created = client.post(
        "/api/v1/projects",
        data={
            "name": "external-project",
            "document_adapter": "txt",
            "empty": "true",
            "parent_dir": str(external_parent),
        },
    )
    assert created.status_code == 200
    selector = created.json()["project_selector"]
    external_project = external_parent / "external-project"
    assert created.json()["project_path"] == str(external_project)
    listed = client.get("/api/v1/projects").json()["projects"]
    external = next(item for item in listed if item["selector"] == selector)
    assert external["external"] is True
    assert external["path"] == str(external_project)
    assert client.get(f"/api/v1/projects/{selector}").status_code == 200
    assert client.post(
        "/api/v1/projects",
        data={
            "name": "external-project",
            "document_adapter": "txt",
            "empty": "true",
            "parent_dir": str(external_parent),
        },
    ).status_code == 400

    hidden, _ = init_project(
        [],
        name="not-scanned",
        empty=True,
        app_root=app_root,
        projects_root=external_parent,
    )
    assert hidden is not None
    assert not any(
        item["name"] == "not-scanned"
        for item in client.get("/api/v1/projects").json()["projects"]
    )
    opened = client.post(
        "/api/v1/projects/open", json={"path": str(hidden)}
    )
    assert opened.status_code == 200
    assert opened.json()["path"] == str(hidden)

    assert client.post(
        "/api/v1/projects/open", json={"path": str(hidden)}
    ).status_code == 200
    names = [
        item["name"]
        for item in client.get("/api/v1/projects").json()["projects"]
    ]
    assert {"sample", "external-project", "not-scanned"} <= set(names)
    assert names.count("not-scanned") == 1

    moved = hidden.with_name("moved-project")
    hidden.rename(moved)
    assert client.post(
        "/api/v1/projects/open", json={"path": str(hidden)}
    ).status_code == 400
    assert all(
        item["name"] != "not-scanned"
        for item in client.get("/api/v1/projects").json()["projects"]
    )

    assert client.post(
        "/api/v1/projects/open", json={"path": "relative/project"}
    ).status_code == 400
    assert client.post(
        "/api/v1/projects/open", json={"path": str(tmp_path)}
    ).status_code == 400


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
    assert "minimal-llm-translator.recent-projects.v1" in script.text
    stylesheet = re.search(r'<link rel="stylesheet"[^>]+href="([^"]+)"', page.text)
    assert stylesheet is not None
    css = client.get(stylesheet.group(1))
    assert css.status_code == 200
    assert ".segment-batch-actions" in css.text
    assert script.text.count("segment-row-stack") == 1
    assert "segment-row-boundary" not in script.text
    assert (
        ".segment-row-stack{min-width:0;width:100%;max-width:100%;"
        "display:grid;grid-auto-rows:max-content;gap:2px;background:var(--border)}"
    ) in css.text
    assert ".segment-row-stack{min-width:0;width:100%;max-width:100%;" in css.text
    assert ".segment-list{min-width:0;min-height:0;width:100%;max-width:100%;" in css.text
    assert ".segment-row-boundary" not in css.text
    assert ".segment-row:after" not in css.text
    segment_row = re.search(r"\.segment-row\{([^}]*)\}", css.text)
    assert segment_row is not None
    assert "border-bottom" not in segment_row.group(1)
    assert "grid-template-columns:14px 72px minmax(0,1fr)" in segment_row.group(1)
    assert ".segment-row{content-visibility:auto;contain-intrinsic-block-size:70px;contain-intrinsic-inline-size:0}" in css.text
    assert "contain-intrinsic-size" not in css.text
    assert "rate_limit_waiting_requests" in script.text
    assert "rate_limit_wait_count" not in script.text
    assert "60 / RPM" in script.text
    assert "必须至少为 1" in script.text
    assert ".settings-navigation{position:sticky;top:58px" in css.text
    assert "grid-template-rows:auto minmax(0,1fr)" in css.text
    assert ".config-settings{height:100%;min-height:0;overflow:auto;padding:0 30px 30px" in css.text
    assert ".settings-sticky-heading{position:sticky;top:0;z-index:2;margin:0 -30px 24px" in css.text
    assert ".preset-list-body" in css.text
    assert ".preset-editor-body" in css.text
    assert 'grid-template:"list-heading editor-heading" auto "list-body editor-body"' in css.text
    assert ".preset-list-body{grid-area:list-body" in css.text
    assert "padding:0 0 24px" in css.text
    assert ".preset-list{" not in css.text
    assert "height:clamp(360px,52vh,520px)" in css.text
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
        "全局 LLM Preset",
        "使用全局 Preset",
        "翻译 Preset",
        "打开现有项目",
        "保存父目录",
        "只打开此目录",
        "执行与分块",
        "参考上下文",
        "翻译校验与重试",
        "调试与故障注入",
        "项目设置",
        "全局设置",
        "全局配置",
        "全局 Prompt",
        "LLM Preset",
        "附加 JSON Body",
        "最终请求预览",
        "同步全局模板",
        "全局请求模板",
        "不再保存副本",
        "获取模型",
        "搜索模型名称或 ID",
        "选择后仍需保存",
        "文件范围",
        "未选择时导出全部文件",
        "统一输出 TXT",
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
    assert next(item for item in adapters if item["adapter_id"] == "txt")[
        "extensions"
    ] == [".text", ".txt"]
    assert next(item for item in adapters if item["adapter_id"] == "epub")[
        "import_options"
    ][0]["default"] == "aozora"


def test_web_creates_mixed_project_from_queued_folder_inputs(
    tmp_path: Path,
) -> None:
    projects_root = tmp_path / "projects"
    epub = tmp_path / "book.epub"
    make_epub(epub)
    client = TestClient(create_app(projects_root=projects_root))

    response = client.post(
        "/api/v1/projects",
        data={
            "name": "mixed-upload",
            "relative_paths": [
                "chapters/one.txt",
                "assets/cover.bin",
                "book.epub",
            ],
            "input_kinds": ["folder", "folder", "folder"],
        },
        files=[
            ("files", ("one.txt", b"one", "text/plain")),
            ("files", ("cover.bin", b"image", "application/octet-stream")),
            ("files", ("book.epub", epub.read_bytes(), "application/epub+zip")),
        ],
    )

    assert response.status_code == 200
    assert response.json()["warnings"] == [
        "已忽略 1 个不支持的文件：assets/cover.bin"
    ]
    overview = client.get("/api/v1/projects/mixed-upload").json()
    assert [item["name"] for item in overview["files"]] == [
        "chapters/one.txt",
        "book.epub",
    ]
    assert [item["document_adapter_id"] for item in overview["files"]] == [
        "txt",
        "epub",
    ]


def test_web_applies_epub_import_options_without_project_level_settings(
    tmp_path: Path,
) -> None:
    projects_root = tmp_path / "projects"
    epub = tmp_path / "ruby.epub"
    make_epub(epub, xhtml=RUBY_XHTML)
    client = TestClient(create_app(projects_root=projects_root))

    response = client.post(
        "/api/v1/projects",
        data={
            "name": "ruby-option",
            "adapter_options": json.dumps(
                {"epub": {"ruby_mode": "parenthetical"}}
            ),
        },
        files=[
            ("files", ("ruby.epub", epub.read_bytes(), "application/epub+zip"))
        ],
    )

    assert response.status_code == 200
    overview = client.get("/api/v1/projects/ruby-option").json()
    assert [item["source"] for item in overview["segments"]] == [
        "彼は漢字（かんじ）を読む。",
        "特別（スペシャル／とくべつ）だ。",
    ]
    project = projects_root / "ruby-option"
    assert "adapter_options" not in read_json(project / "project.json")
    file_record = read_jsonl(project / "source" / "files.jsonl")[0]
    state = read_json(project / str(file_record["document_adapter_state"]))
    assert state["state"]["ruby_mode"] == "parenthetical"


def test_web_exposes_epub_xhtml_parts_without_splitting_the_file(
    tmp_path: Path,
) -> None:
    projects_root = tmp_path / "projects"
    epub = tmp_path / "chapters.epub"
    make_epub(
        epub,
        xhtmls=(
            b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>&#31532;&#19968;&#31456;</p></body></html>',
            b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>&#31532;&#20108;&#31456;</p></body></html>',
        ),
    )
    client = TestClient(create_app(projects_root=projects_root))

    response = client.post(
        "/api/v1/projects",
        data={"name": "chapter-parts"},
        files=[
            ("files", ("chapters.epub", epub.read_bytes(), "application/epub+zip"))
        ],
    )

    assert response.status_code == 200
    overview = client.get("/api/v1/projects/chapter-parts").json()
    assert len(overview["files"]) == 1
    assert [item["source"] for item in overview["segments"]] == [
        "第一章",
        "第二章",
    ]
    assert [item["part_id"] for item in overview["segments"]] == [
        "OEBPS/text/ch1.xhtml",
        "OEBPS/text/ch2.xhtml",
    ]
    detail = client.get(
        "/api/v1/projects/chapter-parts/segments/F0001-S000002"
    )
    assert detail.status_code == 200
    assert detail.json()["part_id"] == "OEBPS/text/ch2.xhtml"


def test_web_rejects_malformed_or_unknown_import_options(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    epub = tmp_path / "ruby.epub"
    make_epub(epub, xhtml=RUBY_XHTML)
    client = TestClient(create_app(projects_root=projects_root))

    malformed = client.post(
        "/api/v1/projects",
        data={"name": "bad-json", "adapter_options": "[]"},
        files=[("files", ("ruby.epub", epub.read_bytes()))],
    )
    unknown = client.post(
        "/api/v1/projects",
        data={
            "name": "bad-option",
            "adapter_options": json.dumps({"epub": {"unknown": "value"}}),
        },
        files=[("files", ("ruby.epub", epub.read_bytes()))],
    )

    assert malformed.status_code == 400
    assert "必须是对象" in malformed.json()["error"]
    assert unknown.status_code == 400
    assert "未知导入选项" in unknown.json()["error"]
    assert not (projects_root / "bad-json").exists()
    assert not (projects_root / "bad-option").exists()


def test_web_applies_import_options_when_adding_project_files(
    tmp_path: Path,
) -> None:
    projects_root, _ = make_project(tmp_path)
    epub = tmp_path / "ruby.epub"
    make_epub(epub, xhtml=RUBY_XHTML)
    client = TestClient(create_app(projects_root=projects_root))

    response = client.post(
        "/api/v1/projects/sample/files",
        data={
            "adapter_options": json.dumps(
                {"epub": {"ruby_mode": "base_only"}}
            )
        },
        files=[("files", ("ruby.epub", epub.read_bytes()))],
    )

    assert response.status_code == 200
    overview = client.get("/api/v1/projects/sample").json()
    assert [item["source"] for item in overview["segments"][-2:]] == [
        "彼は漢字を読む。",
        "特別だ。",
    ]


def test_web_rejects_queued_path_collision_before_creating_project(
    tmp_path: Path,
) -> None:
    projects_root = tmp_path / "projects"
    client = TestClient(create_app(projects_root=projects_root))

    response = client.post(
        "/api/v1/projects",
        data={
            "name": "collision",
            "relative_paths": ["chapter/one.txt", "CHAPTER/ONE.TXT"],
            "input_kinds": ["folder", "folder"],
        },
        files=[
            ("files", ("one.txt", b"one", "text/plain")),
            ("files", ("ONE.TXT", b"two", "text/plain")),
        ],
    )

    assert response.status_code == 400
    assert "重复相对路径" in response.json()["error"]
    assert not (projects_root / "collision").exists()


def test_web_export_accepts_explicit_file_range(tmp_path: Path) -> None:
    projects_root, _ = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root))

    response = client.post(
        "/api/v1/projects/sample/export",
        json={
            "stage": "translated",
            "format": "txt",
            "allow_missing": True,
            "file_ids": ["F0001"],
        },
    )

    assert response.status_code == 200
    assert response.json()["selected_file_ids"] == ["F0001"]
    invalid = client.post(
        "/api/v1/projects/sample/export",
        json={"stage": "translated", "file_ids": "F0001"},
    )
    assert invalid.status_code == 400
    invalid_format = client.post(
        "/api/v1/projects/sample/export",
        json={"stage": "translated", "format": "pdf"},
    )
    assert invalid_format.status_code == 400


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
    config = client.get("/api/v1/projects/sample/config").json()["config"]
    config["llm"]["preset_translation"] = "missing"
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
        "endpoint": "/v1/models/${model}:generate",
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
    assert preview["url"] == (
        "https://example.com/v1/v1/models/provider/model:generate"
    )
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


def test_web_manages_global_adapters(tmp_path: Path) -> None:
    projects_root, _ = make_project(tmp_path)
    app_root = tmp_path / "app-root"
    client = TestClient(create_app(
        projects_root=projects_root,
        app_root=app_root,
    ))
    global_adapter = client.get(
        "/api/v1/global/adapters/openai-compatible"
    ).json()
    global_adapter["adapter_id"] = "alternate"
    saved = client.put(
        "/api/v1/global/adapters/alternate",
        json=global_adapter,
    )
    assert saved.status_code == 200
    assert json.loads(
        (app_root / "llm_adapters" / "alternate.json").read_text("utf-8")
    )["adapter_id"] == "alternate"
    listed = client.get("/api/v1/global/adapters").json()
    assert {item["adapter_id"] for item in listed["adapters"]} >= {
        "openai-compatible",
        "alternate",
    }
    wrong_id = client.put(
        "/api/v1/global/adapters/alternate",
        json={**global_adapter, "adapter_id": "other"},
    )
    assert wrong_id.status_code == 400


def test_web_rejects_inline_config_and_has_no_migration_endpoint(
    tmp_path: Path,
) -> None:
    projects_root, project = make_project(tmp_path)
    app = create_app(projects_root=projects_root)
    client = TestClient(app)
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'preset = "default"', 'model = "legacy-model"'
        ),
        encoding="utf-8",
    )

    response = client.get("/api/v1/projects/sample/config")
    assert response.status_code == 400
    assert "未知配置键 config.llm: model" in response.json()["error"]
    route_paths = {getattr(route, "path", "") for route in app.routes}
    assert not any(
        path.startswith("/api/v1/projects/")
        and "adapters" in path
        for path in route_paths
    )
    assert "/api/v1/global/adapters" in route_paths


def test_web_file_removal_is_all_or_nothing(tmp_path: Path) -> None:
    projects_root, project = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root))
    before = read_jsonl(project / "source" / "files.jsonl")
    response = client.post(
        "/api/v1/projects/sample/files/remove",
        json={"file_ids": ["F0001", "F9999"]},
    )
    assert response.status_code == 400
    assert read_jsonl(project / "source" / "files.jsonl") == before


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


def test_web_can_permanently_delete_disabled_term(tmp_path: Path) -> None:
    projects_root, project = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root))
    added = client.post(
        "/api/v1/projects/sample/terms",
        json={
            "source": "Alice",
            "preferred_translation": "爱丽丝",
            "category": "人物",
            "description": "主角",
            "aliases": [],
            "disabled": True,
        },
    )
    assert added.status_code == 200
    deleted = client.post(
        "/api/v1/projects/sample/terms/delete",
        json={"normalized": ["alice"]},
    )
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] == 1
    assert read_json(project / "terminology" / "terms.json")["terms"] == []
    assert read_json(project / "terminology" / "overrides.json")["overrides"] == []


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


def test_web_exports_and_publishes_scanned_candidates(tmp_path: Path) -> None:
    projects_root, project = make_project(tmp_path, "Alice")
    metadata = read_json(project / "project.json")
    task_id = "TERM-TASK-WEB-PARTIAL"
    atomic_write_json(
        project / "terminology" / "active_task.json",
        record_header(
            "terminology_task",
            str(metadata["project_id"]),
            record_id=task_id,
            active_task_id=task_id,
            status="active",
            initial_stage_fingerprint="test",
        ),
    )
    append_jsonl(
        project / "terminology" / "candidates.jsonl",
        record_header(
            "terminology_candidates",
            str(metadata["project_id"]),
            active_task_id=task_id,
            run_id="RUN-WEB-PARTIAL",
            terms=[
                {
                    "source": "Alice",
                    "category": "人名",
                    "description": "人物",
                    "preferred_translation": "爱丽丝",
                    "aliases": [],
                }
            ],
        ),
    )
    client = TestClient(create_app(projects_root=projects_root))
    scan = client.get("/api/v1/projects/sample/terms").json()["scan"]
    assert scan["candidate_count"] == 1
    exported = client.get(
        "/api/v1/projects/sample/terms/export",
        params={"format": "json", "source": "scanned"},
    )
    assert exported.status_code == 200
    assert json.loads(exported.content)["terms"][0]["source"] == "Alice"
    published = client.post(
        "/api/v1/projects/sample/terms/publish-partial",
        json={"confirm": True},
    )
    assert published.status_code == 200
    assert published.json()["published"] is True
    assert client.get("/api/v1/projects/sample/terms").json()["scan"]["status"] == "partial_published"


def test_web_resets_results_and_applies_explicit_segments(tmp_path: Path) -> None:
    projects_root, project = make_project(tmp_path)
    store = WebStore(project)
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
    projects_root, _ = make_project(tmp_path)
    app_root = tmp_path / "app-root"
    client = TestClient(create_app(
        projects_root=projects_root,
        app_root=app_root,
    ))
    adapter = client.get(
        "/api/v1/global/adapters/openai-compatible"
    ).json()
    adapter["body"]["response_format"] = {"type": "json_object"}
    saved = client.put(
        "/api/v1/global/adapters/openai-compatible",
        json=adapter,
    )
    assert saved.status_code == 200
    assert json.loads(
        (
            app_root / "llm_adapters" / "openai-compatible.json"
        ).read_text(encoding="utf-8")
    )["body"]["response_format"] == {"type": "json_object"}
    preview = client.get(
        "/api/v1/global/adapters/openai-compatible/preview"
    ).json()
    assert preview["headers"]["Authorization"] == "Bearer ***"
    assert "***" not in json.dumps(preview["body"])


def test_web_task_options_report_mixed_fingerprints_and_reject_missing_choice(
    tmp_path: Path,
) -> None:
    projects_root, project = make_project(tmp_path)
    store = WebStore(project)
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
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'preset = "default"', 'preset = "google-gemini"'
        ),
        encoding="utf-8",
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
        assert options["running_run"]["current"]["endpoint"] == (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.5-flash:generateContent"
        )

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
    store = WebStore(project)
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


@pytest.mark.asyncio
async def test_web_task_exposes_live_progress_and_separate_token_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, project = make_project(tmp_path)

    async def fake_translation(
        _: Path,
        __: Scope,
        **kwargs: object,
    ) -> dict[str, object]:
        progress = kwargs["on_progress"]
        usage = kwargs["on_usage"]
        assert callable(progress)
        assert callable(usage)
        progress(1, 0, 2)
        usage(
            {
                "input_tokens": 12,
                "output_tokens": 5,
                "total_tokens": 17,
                "available": True,
            }
        )
        progress(2, 0, 2)
        return {
            "selected": 2,
            "completed": 2,
            "failed": 0,
            "pending": 0,
            "usage": {
                "input_tokens": 12,
                "output_tokens": 5,
                "total_tokens": 17,
                "available": True,
            },
        }

    monkeypatch.setattr("app.web_tasks.run_translation", fake_translation)
    diagnostics = Diagnostics(tmp_path / "logs" / "app.log")
    manager = WebTaskManager(diagnostics)
    started = await manager.start(
        project,
        "translation",
        scope=Scope(),
        reuse_mixed_fingerprints=False,
        run_action=None,
    )
    await manager.tasks[started["task_id"]].asyncio_task

    state = manager.get(started["task_id"])
    assert state["status"] == "completed"
    assert state["completed_segments"] == state["total_segments"] == 2
    assert state["failed_segments"] == state["pending_segments"] == 0
    assert state["usage"]["input_tokens"] == 12
    assert state["usage"]["output_tokens"] == 5
    assert diagnostics.snapshot()["metrics"]["input_tokens"] == 12
    assert diagnostics.snapshot()["metrics"]["output_tokens"] == 5


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


class FakeModelsResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        payload: object = None,
        json_error: bool = False,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self) -> object:
        if self._json_error:
            raise ValueError("bad json")
        return self._payload


class FakeModelsClient:
    instances: list["FakeModelsClient"] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.raise_error: Exception | None = None
        self.response = FakeModelsResponse()
        self.request_url = ""
        self.request_headers: dict[str, str] = {}
        FakeModelsClient.instances.append(self)

    async def __aenter__(self) -> "FakeModelsClient":
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    async def get(self, url: str, headers: dict[str, str] | None = None) -> FakeModelsResponse:
        self.request_url = url
        self.request_headers = headers or {}
        if self.raise_error is not None:
            raise self.raise_error
        return self.response


def test_web_preset_models_discovery_fetches_and_parses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects_root, _ = make_project(tmp_path)
    app_root = tmp_path / "app-root"
    fake = FakeModelsClient()
    fake.response = FakeModelsResponse(
        payload={
            "data": [
                {"id": "gpt-4o", "display_name": "GPT-4o"},
                {"id": "gpt-4.1"},
            ]
        }
    )
    def fake_client(**kwargs: object) -> FakeModelsClient:
        fake.kwargs = kwargs
        return fake

    monkeypatch.setattr("app.web.httpx.AsyncClient", fake_client)
    client = TestClient(create_app(projects_root=projects_root, app_root=app_root))
    preset = client.get("/api/v1/global/presets/default").json()
    saved_definition = dict(preset)
    preset.update(
        {
            "base_url": "https://draft.example/v2",
            "api_key_env": "DRAFT_LLM_API_KEY",
            "proxy_url": "https://proxy.example",
            "request_timeout_seconds": 45,
        }
    )
    os.environ["DRAFT_LLM_API_KEY"] = "draft-secret"
    try:
        result = client.post(
            "/api/v1/global/presets/default/models", json=preset
        )
    finally:
        del os.environ["DRAFT_LLM_API_KEY"]
    assert result.status_code == 200
    assert result.json() == {
        "models": [
            {"id": "gpt-4o", "display": "gpt-4o"},
            {"id": "gpt-4.1", "display": "gpt-4.1"},
        ],
        "count": 2,
    }
    assert fake.request_url == "https://draft.example/v2/v1/models"
    assert fake.request_headers["Authorization"] == "Bearer draft-secret"
    assert fake.kwargs == {
        "timeout": 45.0,
        "proxy": "https://proxy.example",
    }
    assert client.get(
        "/api/v1/global/presets/default"
    ).json() == saved_definition

    mismatched = {**preset, "preset_id": "other"}
    rejected = client.post(
        "/api/v1/global/presets/default/models", json=mismatched
    )
    assert rejected.status_code == 400
    assert "URL 中的 Preset ID" in rejected.json()["error"]


def test_web_preset_models_discovery_fails_fast(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects_root, _ = make_project(tmp_path)
    app_root = tmp_path / "app-root"
    adapter = json.loads(
        (app_root / "llm_adapters" / "openai-compatible.json").read_text("utf-8")
    )
    adapter["adapter_id"] = "minimal"
    adapter.pop("models")
    adapter.pop("usage")
    (app_root / "llm_adapters" / "minimal.json").write_text(
        json.dumps(adapter), encoding="utf-8"
    )
    preset = json.loads(
        (app_root / "llm_presets" / "default.json").read_text("utf-8")
    )
    preset["adapter_id"] = "minimal"
    client = TestClient(create_app(projects_root=projects_root, app_root=app_root))
    no_spec = client.post(
        "/api/v1/global/presets/default/models", json=preset
    )
    assert no_spec.status_code == 400
    assert "未声明模型发现规格" in no_spec.json()["error"]

    preset["adapter_id"] = "openai-compatible"
    monkeypatch.delenv(preset["api_key_env"], raising=False)
    missing_key = client.post(
        "/api/v1/global/presets/default/models", json=preset
    )
    assert missing_key.status_code == 400
    assert "缺少环境变量" in missing_key.json()["error"]

    os.environ["LLM_API_KEY"] = "test"
    try:
        fake = FakeModelsClient()
        fake.raise_error = httpx.ConnectError("no route")
        monkeypatch.setattr("app.web.httpx.AsyncClient", lambda **kwargs: fake)
        network = client.post(
            "/api/v1/global/presets/default/models", json=preset
        )
        assert network.status_code == 400
        assert "模型列表请求失败" in network.json()["error"]

        fake = FakeModelsClient()
        fake.response = FakeModelsResponse(status_code=500)
        monkeypatch.setattr("app.web.httpx.AsyncClient", lambda **kwargs: fake)
        http_error = client.post(
            "/api/v1/global/presets/default/models", json=preset
        )
        assert http_error.status_code == 400
        assert "HTTP 500" in http_error.json()["error"]

        fake = FakeModelsClient()
        fake.response = FakeModelsResponse(json_error=True)
        monkeypatch.setattr("app.web.httpx.AsyncClient", lambda **kwargs: fake)
        bad_json = client.post(
            "/api/v1/global/presets/default/models", json=preset
        )
        assert bad_json.status_code == 400
        assert "不是合法 JSON" in bad_json.json()["error"]

        fake = FakeModelsClient()
        fake.response = FakeModelsResponse(payload={"data": {"id": "x"}})
        monkeypatch.setattr("app.web.httpx.AsyncClient", lambda **kwargs: fake)
        bad_shape = client.post(
            "/api/v1/global/presets/default/models", json=preset
        )
        assert bad_shape.status_code == 400
        assert "不是数组" in bad_shape.json()["error"]
    finally:
        del os.environ["LLM_API_KEY"]
