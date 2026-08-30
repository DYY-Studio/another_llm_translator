from __future__ import annotations

import asyncio
import io
import json
import os
import re
import sqlite3
import zipfile
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest
from fastapi.testclient import TestClient

import app.web as web_module
from app import sqlite_storage
from app.config import dump_config, load_config, load_project_config
from app.diagnostics import Diagnostics
from app.errors import UsageError
from app.execution import Scope, create_run
from app.locking import project_write_lock
from app.project import init_project
from app.sqlite_storage import (
    append_jsonl,
    read_files,
    read_json,
    read_jsonl,
    record_exists,
    record_header,
    write_json,
)
from app.web import create_app
from app.web_store import WebStore
from app.web_tasks import WebTaskManager
from tests.test_documents import RUBY_XHTML, add_translations, init_epub, make_epub
from tests.test_foundation import make_app_root
from tests.test_web_store import seed_conflicted_terms


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


def test_web_compacts_project_storage_and_blocks_running_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects_root, _ = make_project(tmp_path)
    app = create_app(projects_root=projects_root)
    client = TestClient(app)

    monkeypatch.setattr(app.state.tasks, "is_project_running", lambda _: True)
    blocked = client.post("/api/v1/projects/sample/storage/compact")
    assert blocked.status_code == 400

    monkeypatch.setattr(app.state.tasks, "is_project_running", lambda _: False)
    called: list[Path] = []

    def fake_compact(project: Path) -> dict[str, int]:
        called.append(project)
        return {"before_bytes": 100, "after_bytes": 64, "reclaimed_bytes": 36}

    monkeypatch.setattr(web_module, "compact_project_database", fake_compact)
    response = client.post("/api/v1/projects/sample/storage/compact")
    assert response.status_code == 200
    assert response.json() == {
        "before_bytes": 100,
        "after_bytes": 64,
        "reclaimed_bytes": 36,
    }
    assert called == [projects_root / "sample"]


def test_web_validation_errors_have_stable_safe_fields(tmp_path: Path) -> None:
    client = TestClient(create_app(projects_root=tmp_path / "projects"))

    response = client.post(
        "/api/v1/projects",
    )

    assert response.status_code == 400
    payload = response.json()
    assert payload["code"] == "request_validation_error"
    assert payload["params"]["fields"] == ["name"]
    assert "input" not in payload["params"]


def test_web_epub_export_error_preserves_language_tag_guidance(
    tmp_path: Path,
) -> None:
    project = init_epub(tmp_path)
    config_path = project / "config.toml"
    config = load_config(config_path)
    config["project"]["target_language_tag"] = ""
    config_path.write_text(dump_config(config), encoding="utf-8")
    add_translations(project)
    client = TestClient(create_app(projects_root=project.parent))

    response = client.post(
        "/api/v1/projects/book/export",
        json={"stage": "translated", "format": "original"},
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": "EPUB 导出需要 project.target_language_tag",
        "code": "export_error",
        "params": {
            "reason": "missing_target_language_tag",
            "adapter_id": "epub",
            "setting": "project.target_language_tag",
        },
    }
    assert not list((project / "output").rglob("*.epub"))


def test_web_unexpected_error_returns_safe_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects_root, _ = make_project(tmp_path)

    def unexpected(*_: object, **__: object) -> None:
        raise RuntimeError("secret diagnostic detail")

    monkeypatch.setattr(web_module, "resolve_project", unexpected)
    client = TestClient(
        create_app(projects_root=projects_root),
        raise_server_exceptions=False,
    )
    response = client.get("/api/v1/projects/sample")

    assert response.status_code == 500
    assert response.json() == {
        "error": "内部错误",
        "code": "internal_error",
        "params": {},
    }
    assert "secret" not in response.text


def test_web_creates_default_projects_root_at_startup(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    assert not projects_root.exists()
    client = TestClient(create_app(projects_root=projects_root))

    assert projects_root.is_dir()
    default = str(projects_root.resolve())
    assert client.post("/api/v1/directories", json={}).status_code == 200
    assert (
        client.post("/api/v1/directories", json={"path": default}).status_code
        == 200
    )
    created = client.post(
        "/api/v1/projects",
        data={"name": "empty", "empty": "true", "parent_dir": default},
    )
    assert created.status_code == 200
    assert created.json()["project_selector"] == "empty"


def test_web_long_query_inputs_use_post_bodies(tmp_path: Path) -> None:
    long_term = "长" * 3000
    projects_root, project = make_project(tmp_path, f"{long_term}\nother")
    client = TestClient(create_app(projects_root=projects_root))
    long_query = "筛选" * 3000

    index = client.post(
        "/api/v1/projects/sample/segments/ids",
        json={"stage": "translation", "q": long_query},
    )
    assert index.status_code == 200
    assert index.json()["total"] == 0
    page = client.post(
        "/api/v1/projects/sample/segments/query",
        json={"stage": "translation", "q": long_query, "offset": 0, "limit": 10},
    )
    assert page.status_code == 200
    assert page.json()["segments"] == []

    terms = client.post(
        "/api/v1/projects/sample/terms",
        json={"source": long_term, "aliases": [], "disabled": False},
    )
    assert terms.status_code == 200
    hits = client.post(
        "/api/v1/projects/sample/terms/hits",
        json={"normalized": long_term},
    )
    assert hits.status_code == 200
    assert hits.json()["total"] == 1

    diagnostics = client.post(
        "/api/v1/diagnostics",
        json={"q": long_query},
    )
    assert diagnostics.status_code == 200

    nested = project / "output" / ("译" * 20) / ("文" * 20)
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_bytes(b"long-path")
    downloaded = client.post(
        "/api/v1/projects/sample/exports/download",
        json={"file": nested.relative_to(project / "output").as_posix()},
    )
    assert downloaded.status_code == 200
    assert downloaded.content == b"long-path"

    directory = client.post(
        "/api/v1/directories",
        json={"path": str(nested.parent)},
    )
    assert directory.status_code == 200
    assert client.post(
        "/api/v1/directories",
        json={"path": "/" + ("深" * 3000)},
    ).status_code == 400

    assert client.get("/api/v1/directories", params={"path": long_query}).status_code != 200
    assert client.get(
        "/api/v1/projects/sample/segments/ids", params={"q": long_query}
    ).status_code != 200
    assert client.get(
        "/api/v1/projects/sample", params={"q": long_query}
    ).status_code == 400
    invalid_window = client.get(
        "/api/v1/projects/sample", params={"offset": "not-an-int"}
    )
    assert invalid_window.status_code == 400
    assert "窗口参数必须是整数" in invalid_window.json()["error"]
    assert client.get(
        "/api/v1/projects/sample/terms/hits", params={"normalized": long_term}
    ).status_code != 200
    assert client.get(
        "/api/v1/projects/sample/terms/related", params={"normalized": long_term}
    ).status_code != 200
    assert client.get("/api/v1/diagnostics", params={"q": long_query}).status_code != 200
    assert client.get(
        "/api/v1/projects/sample/exports/download", params={"file": "missing"}
    ).status_code != 200


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

    manifest = read_json(project, run_dir / "manifest.json")
    manifest["status"] = "failed"
    write_json(project, run_dir / "manifest.json", manifest)
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


def test_web_browses_server_directories_one_level_and_filters_symlinks(
    tmp_path: Path,
) -> None:
    projects_root, project = make_project(tmp_path)
    external = tmp_path / "external"
    nested = external / "nested"
    nested.mkdir(parents=True)
    (external / "note.txt").write_text("not a directory", encoding="utf-8")
    symlink = external / "project-link"
    symlink_created = True
    try:
        symlink.symlink_to(project, target_is_directory=True)
    except OSError:
        symlink_created = False
    client = TestClient(create_app(projects_root=projects_root))

    root_listing = client.post(
        "/api/v1/directories", json={"path": str(projects_root)}
    )
    assert root_listing.status_code == 200
    sample = next(
        item
        for item in root_listing.json()["directories"]
        if item["name"] == "sample"
    )
    assert sample["is_project"] is True

    listing = client.post(
        "/api/v1/directories", json={"path": str(external)}
    )
    assert listing.status_code == 200
    assert listing.json()["path"] == str(external.resolve())
    assert listing.json()["is_project"] is False
    assert listing.json()["drives"] == []
    assert [item["name"] for item in listing.json()["directories"]] == [
        "nested"
    ]

    project_listing = client.post(
        "/api/v1/directories", json={"path": str(project)}
    )
    assert project_listing.status_code == 200
    assert project_listing.json()["is_project"] is True

    assert client.post(
        "/api/v1/directories", json={"path": "relative/path"}
    ).status_code == 400
    assert client.post(
        "/api/v1/directories", json={"path": str(external / "note.txt")}
    ).status_code == 400
    if symlink_created:
        assert "project-link" not in {
            item["name"] for item in listing.json()["directories"]
        }
        assert client.post(
            "/api/v1/directories", json={"path": str(symlink)}
        ).status_code == 400


def test_windows_drive_probe_keeps_unavailable_drive_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeKernel32:
        def GetLogicalDrives(self) -> int:
            return (1 << 2) | (1 << 3)

        def GetDriveTypeW(self, path: str) -> int:
            return {"C:\\": 3, "D:\\": 5}[path]

    class FakeWindll:
        kernel32 = FakeKernel32()

    monkeypatch.setattr(web_module.os, "name", "nt")
    monkeypatch.setattr(web_module.ctypes, "windll", FakeWindll(), raising=False)
    monkeypatch.setattr(
        web_module.os.path,
        "isdir",
        lambda path: path == "C:\\",
    )

    assert web_module._windows_drive_entries() == [
        {
            "name": "C:",
            "path": "C:\\",
            "type": "fixed",
            "available": True,
        },
        {
            "name": "D:",
            "path": "D:\\",
            "type": "cdrom",
            "available": False,
        },
    ]


@pytest.mark.skipif(
    os.name != "nt",
    reason="Windows drive roots are only available on Windows",
)
def test_web_returns_drive_entries_at_a_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drives = [
        {
            "name": "C:",
            "path": "C:\\",
            "type": "fixed",
            "available": True,
        },
        {
            "name": "D:",
            "path": "D:\\",
            "type": "cdrom",
            "available": False,
        },
    ]
    monkeypatch.setattr(web_module, "_windows_drive_entries", lambda: drives)
    client = TestClient(create_app(projects_root=tmp_path / "projects"))

    root = Path(Path.cwd().anchor)
    response = client.post("/api/v1/directories", json={"path": str(root)})

    assert response.status_code == 200
    assert response.json()["drives"] == drives


WEB_DIST_PRESENT = (Path(__file__).parents[1] / "app" / "web_dist").is_dir()


@pytest.mark.skipif(
    not WEB_DIST_PRESENT,
    reason="web_dist 未构建；请先 npm run build --prefix web",
)
def test_web_build_serves_loadable_assets_with_core_contract(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(projects_root=tmp_path / "projects"))
    page = client.get("/")
    assert page.status_code == 200
    assert "document.documentElement.dataset.theme" in page.text
    asset = re.search(r'<script type="module"[^>]+src="([^"]+)"', page.text)
    assert asset is not None
    script = client.get(asset.group(1))
    assert script.status_code == 200
    stylesheet = re.search(r'<link rel="stylesheet"[^>]+href="([^"]+)"', page.text)
    assert stylesheet is not None
    css = client.get(stylesheet.group(1))
    assert css.status_code == 200


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
    assert [item["adapter_id"] for item in adapters] == ["epub", "srt", "txt"]
    assert next(item for item in adapters if item["adapter_id"] == "txt")[
        "extensions"
    ] == [".text", ".txt"]
    assert next(item for item in adapters if item["adapter_id"] == "epub")[
        "import_options"
    ][0]["default"] == "aozora"
    validators = client.get("/api/v1/translation-validators").json()[
        "validators"
    ]
    assert [item["validator_id"] for item in validators] == [
        "japanese_kana",
        "korean_hangul",
        "preferred_term_usage",
        "source_text_residual",
    ]


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
                {"epub": {"ruby_mode": "short_xml"}}
            ),
        },
        files=[
            ("files", ("ruby.epub", epub.read_bytes(), "application/epub+zip"))
        ],
    )

    assert response.status_code == 200
    overview = client.get("/api/v1/projects/ruby-option").json()
    assert [item["source"] for item in overview["segments"]] == [
        "彼は｜漢字《かんじ》を読む。",
        "｜特別《スペシャル／とくべつ》だ。",
    ]
    project = projects_root / "ruby-option"
    assert "adapter_options" not in read_json(project, project / "project.json")
    file_record = read_files(project)[0]
    state = read_json(project, project / str(file_record["document_adapter_state"]))
    assert state["state"]["ruby_mode"] == "short_xml"


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
    assert detail.json()["context"] == {"before": [], "after": []}


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


def test_web_exports_list_download_zip_and_remove(tmp_path: Path) -> None:
    projects_root, project = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root))

    exported = client.post(
        "/api/v1/projects/sample/export",
        json={"stage": "translated", "format": "txt", "allow_missing": True},
    )
    assert exported.status_code == 200
    assert exported.json()["written"] == ["output/translated/input.txt"]
    output_file = project / "output" / "translated" / "input.txt"
    expected_bytes = output_file.read_bytes()

    staging = project / "output" / ".staging"
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "temp.txt").write_text("temp", encoding="utf-8")
    (project / "output" / ".DS_Store").write_bytes(b"\x00\x00\x00\x01")
    (project / "output" / "translated" / ".DS_Store").write_bytes(b"\x00\x00\x00\x01")
    symlink_path = project / "output" / "translated" / "link.txt"
    symlink_created = False
    try:
        symlink_path.symlink_to(output_file)
        symlink_created = True
    except OSError:
        pass

    listing = client.get("/api/v1/projects/sample/exports")
    assert listing.status_code == 200
    files = listing.json()["files"]
    assert len(files) == 1
    assert files[0]["path"] == "translated/input.txt"
    assert files[0]["size"] == len(expected_bytes)
    assert files[0]["mtime"] > 0

    downloaded = client.post(
        "/api/v1/projects/sample/exports/download",
        json={"file": "translated/input.txt"},
    )
    assert downloaded.status_code == 200
    assert downloaded.headers["content-disposition"] == (
        'attachment; filename="input.txt"'
    )
    assert downloaded.content == expected_bytes

    unicode_file = project / "output" / "translated" / "译文.epub"
    unicode_file.write_bytes(b"epub")
    unicode_download = client.post(
        "/api/v1/projects/sample/exports/download",
        json={"file": "translated/译文.epub"},
    )
    assert unicode_download.status_code == 200
    disposition = unicode_download.headers["content-disposition"]
    assert disposition.startswith("attachment; filename*=utf-8''")
    assert unquote(disposition.partition("''")[2]) == "译文.epub"
    assert unicode_download.content == b"epub"
    unicode_file.unlink()

    nested_file = project / "output" / "proofread" / "nested.txt"
    nested_file.parent.mkdir(parents=True, exist_ok=True)
    nested_file.write_bytes(b"proofread")

    bundle = client.get(
        "/api/v1/projects/sample/exports/download-all"
    )
    assert bundle.status_code == 200
    assert bundle.headers["content-type"].startswith("application/zip")
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        assert archive.namelist() == [
            "proofread/nested.txt",
            "translated/input.txt",
        ]
        assert archive.read("translated/input.txt") == expected_bytes
        assert archive.read("proofread/nested.txt") == b"proofread"

    removed = client.post(
        "/api/v1/projects/sample/exports/remove",
        json={"files": ["translated/input.txt", "proofread/nested.txt"]},
    )
    assert removed.status_code == 200
    assert removed.json()["removed"] == [
        "translated/input.txt",
        "proofread/nested.txt",
    ]
    assert not output_file.exists()
    if symlink_created:
        symlink_path.unlink()
    assert client.get("/api/v1/projects/sample/exports").json()["files"] == []
    assert client.get(
        "/api/v1/projects/sample/exports/download-all"
    ).status_code == 400

    again = client.post(
        "/api/v1/projects/sample/exports/remove",
        json={"files": ["translated/input.txt"]},
    )
    assert again.status_code == 400
    assert client.post(
        "/api/v1/projects/sample/exports/remove", json={"files": []}
    ).status_code == 400
    assert client.post(
        "/api/v1/projects/sample/exports/remove", json={"files": "input.txt"}
    ).status_code == 400


def test_web_exports_reject_paths_outside_output(tmp_path: Path) -> None:
    projects_root, project = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root))
    client.post(
        "/api/v1/projects/sample/export",
        json={"stage": "translated", "format": "txt", "allow_missing": True},
    )
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")

    for raw in (
        "../secret.txt",
        "translated/../secret.txt",
        "translated/../../secret.txt",
        str(outside),
    ):
        assert client.post(
            "/api/v1/projects/sample/exports/download",
            json={"file": raw},
        ).status_code == 400
        assert client.post(
            "/api/v1/projects/sample/exports/remove",
            json={"files": [raw]},
        ).status_code == 400

    symlink = project / "output" / "translated" / "link.txt"
    try:
        symlink.symlink_to(outside)
    except OSError:
        return
    assert client.post(
        "/api/v1/projects/sample/exports/download",
        json={"file": "translated/link.txt"},
    ).status_code == 400
    assert client.post(
        "/api/v1/projects/sample/exports/remove",
        json={"files": ["translated/link.txt"]},
    ).status_code == 400


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
    config["project"]["target_language_tag"] = "zh-Hant"
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
        project / "prompts" / "translation.zh-CN.middle.txt"
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
        project / "prompts" / "translation.zh-CN.middle.txt"
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
        new_project / "prompts" / "translation.zh-CN.middle.txt"
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
        "stream": True,
        "stream_endpoint": "/v1/models/${model}:stream",
        "stream_read_timeout_enabled": False,
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
    assert client.get(
        "/api/v1/global/presets/openrouter"
    ).json()["stream_read_timeout_enabled"] is False
    preview = client.get(
        "/api/v1/global/presets/openrouter/preview"
    ).json()
    assert preview["headers"]["Authorization"] == "Bearer ***"
    assert preview["url"] == (
        "https://example.com/v1/v1/models/provider/model:stream"
    )
    assert preview["body"]["provider"] == custom["extra_body"]["provider"]
    assert preview["body"]["model"] == "provider/model"
    assert preview["transport"] == "sse"
    assert preview["body"]["stream"] is True
    assert preview["body"]["stream_options"] == {"include_usage": True}

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
        (
            tmp_path / "user-root" / "llm_adapters" / "alternate.json"
        ).read_text(encoding="utf-8")
    )["adapter_id"] == "alternate"
    assert not (app_root / "llm_adapters" / "alternate.json").exists()
    listed = client.get("/api/v1/global/adapters").json()
    assert {item["adapter_id"] for item in listed["adapters"]} >= {
        "openai-compatible",
        "alternate",
    }
    assert all(
        item["streaming_supported"] is True
        for item in listed["adapters"]
        if item["valid"]
    )
    wrong_id = client.put(
        "/api/v1/global/adapters/alternate",
        json={**global_adapter, "adapter_id": "other"},
    )
    assert wrong_id.status_code == 400


def test_web_rejects_inline_config(tmp_path: Path) -> None:
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


def test_web_file_removal_is_all_or_nothing(tmp_path: Path) -> None:
    projects_root, project = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root))
    before = read_files(project)
    response = client.post(
        "/api/v1/projects/sample/files/remove",
        json={"file_ids": ["F0001", "F9999"]},
    )
    assert response.status_code == 400
    assert read_files(project) == before


def test_web_reorders_all_project_files_and_rejects_invalid_permutations(
    tmp_path: Path,
) -> None:
    projects_root, project = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root))
    added = client.post(
        "/api/v1/projects/sample/files",
        files=[
            ("files", ("second.txt", b"second", "text/plain")),
            ("files", ("third.txt", b"third", "text/plain")),
        ],
    )
    assert added.status_code == 200

    reordered = client.post(
        "/api/v1/projects/sample/files/reorder",
        json={"file_ids": ["F0003", "F0001", "F0002"]},
    )
    assert reordered.status_code == 200
    assert reordered.json() == {
        "reordered_file_ids": ["F0003", "F0001", "F0002"],
        "file_count": 3,
    }
    overview = client.get("/api/v1/projects/sample").json()
    assert [item["file_id"] for item in overview["files"]] == [
        "F0003",
        "F0001",
        "F0002",
    ]
    before = read_files(project)

    for file_ids in (
        ["F0003", "F0001"],
        ["F0003", "F0001", "F0001"],
        ["F0003", "F0001", "F9999"],
    ):
        response = client.post(
            "/api/v1/projects/sample/files/reorder",
            json={"file_ids": file_ids},
        )
        assert response.status_code == 400
        assert read_files(project) == before


def test_web_rejects_unresolved_term_conflict_on_save(tmp_path: Path) -> None:
    projects_root, project = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root))

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
    assert read_json(project, project / "terminology" / "terms.json")["terms"] == []
    assert read_json(project, project / "terminology" / "overrides.json")["overrides"] == []


def test_web_clears_all_terminology_state_and_interrupts_stale_run(
    tmp_path: Path,
) -> None:
    projects_root, project = make_project(tmp_path, "Alice")
    client = TestClient(create_app(projects_root=projects_root))
    assert client.post(
        "/api/v1/projects/sample/terms",
        json={"source": "Alice", "preferred_translation": "爱丽丝"},
    ).status_code == 200
    assert client.post(
        "/api/v1/projects/sample/terms",
        json={"source": "Bob", "disabled": True},
    ).status_code == 200

    metadata = read_json(project, project / "project.json")
    task_id = "TERM-TASK-CLEAR"
    write_json(
        project,
        project / "terminology" / "active_task.json",
        record_header(
            "terminology_task",
            str(metadata["project_id"]),
            record_id=task_id,
            active_task_id=task_id,
            status="active",
        ),
    )
    append_jsonl(
        project,
        project / "terminology" / "scans.jsonl",
        record_header(
            "terminology_scan",
            str(metadata["project_id"]),
            active_task_id=task_id,
            segment_id="F0001-S000001",
            status="completed",
        ),
    )
    append_jsonl(
        project,
        project / "terminology" / "candidates.jsonl",
        record_header(
            "terminology_candidates",
            str(metadata["project_id"]),
            active_task_id=task_id,
            terms=[{"source": "Alice"}],
        ),
    )
    run_id, run_dir = create_run(
        project,
        config=load_project_config(project),
        stage="terminology",
        fingerprint="clear-test",
        prompt="test",
        selected_count=1,
        requested_count=1,
        reused_count=0,
    )
    append_jsonl(
        project,
        project / "stages" / "translation.jsonl",
        record_header(
            "stage_result",
            str(metadata["project_id"]),
            stage="translation",
            segment_id="F0001-S000001",
            status="completed",
            text="保留",
        ),
    )

    missing_confirmation = client.post(
        "/api/v1/projects/sample/terms/clear",
        json={"confirm": False},
    )
    assert missing_confirmation.status_code == 400
    assert client.get("/api/v1/projects/sample/terms").json()["terms"]

    cleared = client.post(
        "/api/v1/projects/sample/terms/clear",
        json={"confirm": True},
    )
    assert cleared.status_code == 200
    assert cleared.json()["terms_revision"] is None
    assert cleared.json()["terms"] == []
    assert cleared.json()["scan"]["status"] == "none"
    assert not record_exists(project, project / "terminology" / "terms.json")
    assert not record_exists(project, project / "terminology" / "active_task.json")
    assert read_json(project, project / "terminology" / "overrides.json")["overrides"] == []
    assert read_jsonl(project, project / "terminology" / "scans.jsonl") == []
    assert read_jsonl(project, project / "terminology" / "candidates.jsonl") == []
    assert read_json(project, run_dir / "manifest.json")["run_id"] == run_id
    assert read_json(project, run_dir / "manifest.json")["status"] == "interrupted"
    assert read_jsonl(project, project / "stages" / "translation.jsonl")[0]["text"] == "保留"


def test_web_rejects_terminology_clear_while_project_task_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects_root, _ = make_project(tmp_path)
    app = create_app(projects_root=projects_root)
    monkeypatch.setattr(app.state.tasks, "is_project_running", lambda _root: True)
    client = TestClient(app)

    blocked = client.post(
        "/api/v1/projects/sample/terms/clear",
        json={"confirm": True},
    )
    assert blocked.status_code == 400
    assert "运行中的任务" in blocked.json()["error"]


def test_web_materializes_alias_and_changes_group_primary(tmp_path: Path) -> None:
    projects_root, _ = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root))
    added = client.post(
        "/api/v1/projects/sample/terms",
        json={
            "source": "Alice",
            "preferred_translation": "爱丽丝",
            "category": "人物",
            "description": "主角",
            "aliases": ["Alicia"],
            "disabled": False,
        },
    )
    assert added.status_code == 200
    materialized = client.post(
        "/api/v1/projects/sample/terms/materialize",
        json={"normalized": "alice", "alias": "Alicia"},
    )
    assert materialized.status_code == 200
    assert materialized.json()["materialized"] == "alicia"
    member = next(
        item for item in materialized.json()["terms"] if item["normalized"] == "alicia"
    )
    assert member["group_primary"] == "alice"

    missing_confirmation = client.post(
        "/api/v1/projects/sample/terms/set-primary",
        json={"normalized": "alicia"},
    )
    assert missing_confirmation.status_code == 400
    switched = client.post(
        "/api/v1/projects/sample/terms/set-primary",
        json={"normalized": "alicia", "confirm": True},
    )
    assert switched.status_code == 200
    rows = {item["normalized"]: item for item in switched.json()["terms"]}
    assert rows["alicia"]["group_primary"] is None
    assert rows["alice"]["group_primary"] == "alicia"


def test_web_materializes_alias_by_restoring_removed_matching_entry(
    tmp_path: Path,
) -> None:
    projects_root, _ = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root))
    for payload in (
        {
            "source": "Alice",
            "preferred_translation": "爱丽丝",
            "category": "人物",
            "description": "主角",
            "aliases": ["Alicia"],
            "disabled": False,
        },
        {
            "source": "Alicia",
            "preferred_translation": "艾丽西亚",
            "category": "别名条目",
            "description": "已有人物资料",
            "aliases": ["Alicia Jr"],
            "disabled": False,
        },
    ):
        assert client.post("/api/v1/projects/sample/terms", json=payload).status_code == 200
    removed = client.post(
        "/api/v1/projects/sample/terms/remove",
        json={"normalized": ["alicia"]},
    )
    assert removed.status_code == 200
    restored = client.post(
        "/api/v1/projects/sample/terms/materialize",
        json={"normalized": "alice", "alias": "Alicia"},
    )
    assert restored.status_code == 200
    rows = {item["normalized"]: item for item in restored.json()["terms"]}
    assert rows["alicia"]["disabled"] is False
    assert rows["alicia"]["group_primary"] == "alice"
    assert rows["alicia"]["preferred_translation"] == "艾丽西亚"
    assert rows["alicia"]["category"] == "别名条目"
    assert rows["alicia"]["description"] == "已有人物资料"
    assert rows["alicia"]["aliases"] == ["Alicia Jr"]
    assert rows["alice"]["aliases"] == []


def test_web_term_group_member_can_leave_group(tmp_path: Path) -> None:
    projects_root, _ = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root))
    for source in ("John Smith", "John"):
        response = client.post(
            "/api/v1/projects/sample/terms",
            json={"source": source, "aliases": [], "disabled": False},
        )
        assert response.status_code == 200
    grouped = client.post(
        "/api/v1/projects/sample/terms/group-related",
        json={
            "normalized": "john smith",
            "related_normalized": "john",
            "primary_normalized": "john smith",
            "confirm": True,
        },
    )
    assert grouped.status_code == 200

    missing_confirmation = client.post(
        "/api/v1/projects/sample/terms/leave-group",
        json={"normalized": "john"},
    )
    assert missing_confirmation.status_code == 400
    left = client.post(
        "/api/v1/projects/sample/terms/leave-group",
        json={"normalized": "john", "confirm": True},
    )
    assert left.status_code == 200
    rows = {item["normalized"]: item for item in left.json()["terms"]}
    assert rows["john"]["group_primary"] is None
    assert rows["john smith"]["group_primary"] is None

    primary = client.post(
        "/api/v1/projects/sample/terms/leave-group",
        json={"normalized": "john smith", "confirm": True},
    )
    assert primary.status_code == 400
    assert primary.json()["code"] == "term_group_error"
    assert primary.json()["params"]["reason"] == "not_group_member"


def test_web_related_term_actions_are_exposed(tmp_path: Path) -> None:
    projects_root, _ = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root))
    assert client.post(
        "/api/v1/projects/sample/terms",
        json={"source": "John Smith", "aliases": [], "disabled": False},
    ).status_code == 200
    assert client.post(
        "/api/v1/projects/sample/terms",
        json={"source": "John", "aliases": [], "disabled": False},
    ).status_code == 200

    related = client.post(
        "/api/v1/projects/sample/terms/related",
        json={"normalized": "john smith"},
    )
    assert related.status_code == 200
    assert related.json()["related"][0]["normalized"] == "john"

    grouped = client.post(
        "/api/v1/projects/sample/terms/group-related",
        json={
            "normalized": "john smith",
            "related_normalized": "john",
            "primary_normalized": "john smith",
            "confirm": True,
        },
    )
    assert grouped.status_code == 200
    rows = {item["normalized"]: item for item in grouped.json()["terms"]}
    assert rows["john"]["group_primary"] == "john smith"


def test_web_related_alias_conversion_route_is_exposed(tmp_path: Path) -> None:
    projects_root, _ = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root))
    for source, aliases in (("John Smith", []), ("John", ["Johnny"])):
        response = client.post(
            "/api/v1/projects/sample/terms",
            json={"source": source, "aliases": aliases, "disabled": False},
        )
        assert response.status_code == 200

    converted = client.post(
        "/api/v1/projects/sample/terms/convert-to-alias",
        json={
            "normalized": "john smith",
            "related_normalized": "john",
            "confirm": True,
        },
    )
    assert converted.status_code == 200
    rows = {item["normalized"]: item for item in converted.json()["terms"]}
    assert set(rows["john smith"]["aliases"]) == {"John", "Johnny"}
    assert rows["john"]["disabled"] is True


def test_web_related_quick_remove_uses_reversible_remove_route(tmp_path: Path) -> None:
    projects_root, _ = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root))
    for source in ("John Smith", "John"):
        response = client.post(
            "/api/v1/projects/sample/terms",
            json={"source": source, "aliases": [], "disabled": False},
        )
        assert response.status_code == 200

    related = client.post(
        "/api/v1/projects/sample/terms/related",
        json={"normalized": "john smith"},
    )
    assert related.status_code == 200
    candidate = related.json()["related"][0]
    assert candidate["can_remove"] is True

    removed = client.post(
        "/api/v1/projects/sample/terms/remove",
        json={"normalized": [candidate["normalized"]]},
    )
    assert removed.status_code == 200
    assert removed.json()["removed"] == 1
    rows = {item["normalized"]: item for item in removed.json()["terms"]}
    assert rows["john"]["disabled"] is True


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
    metadata = read_json(project, project / "project.json")
    task_id = "TERM-TASK-WEB-PARTIAL"
    write_json(
        project,
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
        project,
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


def _seed_terms(project: Path, terms: list[dict]) -> None:
    project_id = str(read_json(project, project / "project.json")["project_id"])
    write_json(
        project,
        project / "terminology" / "terms.json",
        record_header(
            "terminology_library",
            project_id,
            record_id="TERMS-HITS",
            terms_revision=1,
            terms=terms,
        ),
    )


def test_web_terms_hits_count_order_and_pagination(tmp_path: Path) -> None:
    projects_root, project = make_project(
        tmp_path, "Alice walks alone\nBob sleeps\nAlice sings Alice"
    )
    _seed_terms(
        project,
        [
            {
                "record_id": "TERM-H-1",
                "source": "Alice",
                "normalized": "alice",
                "category": "人物",
                "description": None,
                "preferred_translation": "爱丽丝",
                "aliases": [],
                "conflicts": {
                    "categories": [],
                    "preferred_translations": [],
                    "alias_primaries": [],
                },
            }
        ],
    )
    client = TestClient(create_app(projects_root=projects_root))

    first = client.post(
        "/api/v1/projects/sample/terms/hits",
        json={"normalized": "alice", "offset": 0, "limit": 1},
    )
    assert first.status_code == 200
    payload = first.json()
    assert payload["source"] == "Alice"
    assert payload["total"] == 2
    assert [item["segment_id"] for item in payload["hits"]] == ["F0001-S000001"]
    assert set(payload["hits"][0]) == {
        "segment_id",
        "file_id",
        "line_index",
        "source",
    }
    assert payload["hits"][0]["source"] == "Alice walks alone"

    second = client.post(
        "/api/v1/projects/sample/terms/hits",
        json={"normalized": "alice", "offset": 1, "limit": 1},
    )
    assert second.status_code == 200
    assert second.json()["total"] == 2
    assert [item["segment_id"] for item in second.json()["hits"]] == [
        "F0001-S000003"
    ]
    assert second.json()["hits"][0]["source"] == "Alice sings Alice"


def test_web_terms_hits_for_group_member_does_not_require_primary(tmp_path: Path) -> None:
    projects_root, project = make_project(
        tmp_path,
        "Alice only\nAlicia walks\nAlly sings\nAlice and Alicia",
    )
    _seed_terms(
        project,
        [
            {
                "record_id": "TERM-H-MEMBER",
                "source": "Alice",
                "normalized": "alice",
                "category": None,
                "description": None,
                "preferred_translation": "爱丽丝",
                "aliases": [],
                "group_primary": None,
                "conflicts": {},
            },
            {
                "record_id": "TERM-H-MEMBER-CHILD",
                "source": "Alicia",
                "normalized": "alicia",
                "category": None,
                "description": None,
                "preferred_translation": "艾丽西亚",
                "aliases": ["Ally"],
                "group_primary": "alice",
                "conflicts": {},
            },
        ],
    )
    client = TestClient(create_app(projects_root=projects_root))

    response = client.post(
        "/api/v1/projects/sample/terms/hits",
        json={"normalized": "alicia"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "Alicia"
    assert payload["total"] == 3
    assert [item["source"] for item in payload["hits"]] == [
        "Alicia walks",
        "Ally sings",
        "Alice and Alicia",
    ]


def test_web_terms_hits_match_aliases_and_exclude_conflicted_aliases(
    tmp_path: Path,
) -> None:
    projects_root, project = make_project(tmp_path, "A walks\nAli sings\nAlice talks")
    _seed_terms(
        project,
        [
            {
                "record_id": "TERM-H-2",
                "source": "Alice",
                "normalized": "alice",
                "category": None,
                "description": None,
                "preferred_translation": "爱丽丝",
                "aliases": ["A", "Ali"],
                "conflicts": {
                    "categories": [],
                    "preferred_translations": [],
                    "alias_primaries": [
                        {"alias": "A", "primary_source": "Alpha"}
                    ],
                },
            }
        ],
    )
    client = TestClient(create_app(projects_root=projects_root))

    payload = client.post(
        "/api/v1/projects/sample/terms/hits",
        json={"normalized": "alice"},
    ).json()
    assert payload["total"] == 2
    assert [item["segment_id"] for item in payload["hits"]] == [
        "F0001-S000002",
        "F0001-S000003",
    ]


def test_web_terms_hits_apply_casefold_and_unicode_normalization(
    tmp_path: Path,
) -> None:
    projects_root, project = make_project(
        tmp_path, "alice writes\nＡｌｉｃｅ speaks\nbob listens"
    )
    _seed_terms(
        project,
        [
            {
                "record_id": "TERM-H-3",
                "source": "Alice",
                "normalized": "alice",
                "category": None,
                "description": None,
                "preferred_translation": "爱丽丝",
                "aliases": [],
                "conflicts": {
                    "categories": [],
                    "preferred_translations": [],
                    "alias_primaries": [],
                },
            }
        ],
    )
    client = TestClient(create_app(projects_root=projects_root))

    payload = client.post(
        "/api/v1/projects/sample/terms/hits",
        json={"normalized": "alice"},
    ).json()
    assert payload["total"] == 2
    assert [item["segment_id"] for item in payload["hits"]] == [
        "F0001-S000001",
        "F0001-S000002",
    ]


def test_web_terms_hits_cover_disabled_terms_and_validate_parameters(
    tmp_path: Path,
) -> None:
    projects_root, project = make_project(tmp_path, "Alpha first\nBeta second")
    _seed_terms(
        project,
        [
            {
                "record_id": "TERM-H-4",
                "source": "Alpha",
                "normalized": "alpha",
                "category": None,
                "description": None,
                "preferred_translation": "阿尔法",
                "aliases": [],
                "conflicts": {
                    "categories": [],
                    "preferred_translations": [],
                    "alias_primaries": [],
                },
            }
        ],
    )
    write_json(
        project,
        project / "terminology" / "overrides.json",
        record_header(
            "terminology_overrides",
            str(read_json(project, project / "project.json")["project_id"]),
            record_id="OVR-HITS",
            overrides=[{"normalized": "alpha", "source": "Alpha", "disabled": True}],
        ),
    )
    client = TestClient(create_app(projects_root=projects_root))

    assert (
        client.get("/api/v1/projects/sample/terms").json()["terms"][0]["disabled"]
        is True
    )
    payload = client.post(
        "/api/v1/projects/sample/terms/hits",
        json={"normalized": "alpha"},
    )
    assert payload.status_code == 200
    assert payload.json()["total"] == 1
    assert payload.json()["hits"][0]["segment_id"] == "F0001-S000001"

    missing = client.post(
        "/api/v1/projects/sample/terms/hits",
        json={"normalized": "nope"},
    )
    assert missing.status_code == 400
    assert "术语不存在" in missing.json()["error"]

    no_term = client.post("/api/v1/projects/sample/terms/hits", json={})
    assert no_term.status_code == 400

    invalid = client.post(
        "/api/v1/projects/sample/terms/hits",
        json={"normalized": "alpha", "offset": -1},
    )
    assert invalid.status_code == 400
    assert "窗口参数无效" in invalid.json()["error"]


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


def test_web_batches_translation_resets_and_deduplicates_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, project = make_project(tmp_path, "one\ntwo\nthree")
    store = WebStore(project)
    segment_ids = [
        "F0001-S000001",
        "F0001-S000002",
    ]
    for segment_id in segment_ids:
        store.save_translation({"segment_id": segment_id, "text": segment_id})

    connect_calls = 0
    original_connect = sqlite_storage._connect

    def counted_connect(path: Path) -> sqlite3.Connection:
        nonlocal connect_calls
        connect_calls += 1
        return original_connect(path)

    monkeypatch.setattr(sqlite_storage, "_connect", counted_connect)
    reset = store.reset_results(
        {"stage": "translation", "segment_ids": [segment_ids[0], *segment_ids]}
    )

    assert reset["stage"] == "translation"
    assert reset["selected"] == 2
    assert reset["cleared"] == 2
    assert reset["unchanged"] == 0
    assert reset["reset_records"] == 2
    assert reset["reset_batch_id"]
    assert connect_calls == 3
    pending = WebStore(project).segment_index(stage="translation", status="pending")
    assert pending["total"] == 3


def test_web_reset_without_results_does_not_write_stage_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, project = make_project(tmp_path)
    store = WebStore(project)
    path = project / "stages" / "translation.jsonl"
    before = read_jsonl(project, path)

    connect_calls = 0
    original_connect = sqlite_storage._connect

    def counted_connect(path: Path) -> sqlite3.Connection:
        nonlocal connect_calls
        connect_calls += 1
        return original_connect(path)

    monkeypatch.setattr(sqlite_storage, "_connect", counted_connect)
    reset = store.reset_results(
        {"stage": "translation", "segment_ids": ["F0001-S000001"]}
    )

    assert reset["cleared"] == 0
    assert reset["reset_records"] == 0
    assert reset["reset_batch_id"] is None
    assert connect_calls == 2
    assert read_jsonl(project, path) == before


def test_web_reset_invalid_segment_fails_before_writing_any_reset(
    tmp_path: Path,
) -> None:
    _, project = make_project(tmp_path)
    store = WebStore(project)
    store.save_translation(
        {"segment_id": "F0001-S000001", "text": "translated"}
    )
    path = project / "stages" / "translation.jsonl"
    before = read_jsonl(project, path)

    with pytest.raises(UsageError, match="未知或空 Segment"):
        store.reset_results(
            {
                "stage": "translation",
                "segment_ids": ["F0001-S000001", "missing"],
            }
        )

    assert read_jsonl(project, path) == before


def test_web_reset_polishing_batches_suggestion_and_applied_records(
    tmp_path: Path,
) -> None:
    _, project = make_project(tmp_path)
    store = WebStore(project)
    segment_id = "F0001-S000001"
    store.save_translation({"segment_id": segment_id, "text": "translated"})
    store.save_review(
        {
            "stage": "proofreading",
            "segment_id": segment_id,
            "review_status": "accepted",
            "apply": True,
        }
    )
    store.save_review(
        {
            "stage": "polishing",
            "segment_id": segment_id,
            "review_status": "accepted",
            "apply": True,
        }
    )

    reset = store.reset_results(
        {"stage": "polishing", "segment_ids": [segment_id]}
    )

    assert reset["cleared"] == 1
    assert reset["reset_records"] == 2
    overview = store.overview(stage="polishing", offset=0, limit=1)
    review = overview["segments"][0]["reviews"]["polishing"]
    assert review["suggestion"] is None
    assert review["applied"] is None


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
            tmp_path / "user-root" / "llm_adapters" / "openai-compatible.json"
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
    prompt_path = project / "prompts" / "translation.zh-CN.middle.txt"
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
    assert options.json()["preset"] == {"id": "default", "model": "example-model"}
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


def test_web_task_options_report_effective_stage_preset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects_root, project = make_project(tmp_path)
    app_root = tmp_path / "app-root"
    alternate = json.loads(
        (app_root / "llm_presets" / "default.json").read_text(encoding="utf-8")
    )
    alternate.update(preset_id="alternate", model="alternate-model")
    (app_root / "llm_presets" / "alternate.json").write_text(
        json.dumps(alternate), encoding="utf-8"
    )
    config = load_config(project / "config.toml")
    config["llm"]["preset_translation"] = "alternate"
    (project / "config.toml").write_text(dump_config(config), encoding="utf-8")

    monkeypatch.setattr("app.config.APP_ROOT", app_root)
    client = TestClient(create_app(projects_root=projects_root, app_root=app_root))
    translation = client.get(
        "/api/v1/projects/sample/task-options/translation"
    )
    terminology = client.get(
        "/api/v1/projects/sample/task-options/terminology"
    )

    assert translation.status_code == 200
    assert translation.json()["preset"] == {
        "id": "alternate",
        "model": "alternate-model",
    }
    assert terminology.status_code == 200
    assert terminology.json()["preset"] == {
        "id": "default",
        "model": "example-model",
    }


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
        assert options["preset"] == {
            "id": "google-gemini",
            "model": "gemini-2.5-flash",
        }

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
    prompt_path = project / "prompts" / "translation.zh-CN.middle.txt"
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
async def test_web_task_manager_preserves_expected_errors_and_hides_unexpected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, project = make_project(tmp_path)
    manager = WebTaskManager()

    async def expected_failure(*_: object, **__: object) -> dict[str, object]:
        raise UsageError("模型协议错误")

    monkeypatch.setattr("app.web_tasks.run_translation", expected_failure)
    expected = await manager.start(
        project,
        "translation",
        scope=Scope(),
        reuse_mixed_fingerprints=False,
        run_action=None,
    )
    await manager.tasks[expected["task_id"]].asyncio_task
    assert manager.get(expected["task_id"])["error"] == {
        "error": "模型协议错误",
        "code": "usage_error",
        "params": {},
    }

    async def unexpected_failure(*_: object, **__: object) -> dict[str, object]:
        raise RuntimeError("secret diagnostic detail")

    monkeypatch.setattr("app.web_tasks.run_translation", unexpected_failure)
    unexpected = await manager.start(
        project,
        "translation",
        scope=Scope(),
        reuse_mixed_fingerprints=False,
        run_action=None,
    )
    await manager.tasks[unexpected["task_id"]].asyncio_task
    state = manager.get(unexpected["task_id"])
    assert state["status"] == "failed"
    assert state["error"] == {
        "error": "内部错误",
        "code": "internal_error",
        "params": {},
    }
    assert "secret" not in str(state["error"])


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
                "partial": False,
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
                "partial": False,
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


@pytest.mark.asyncio
async def test_web_task_manager_active_tasks_excludes_terminal_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, project = make_project(tmp_path)
    entered = asyncio.Event()

    async def fake_translation(*_: object, **__: object) -> dict[str, object]:
        entered.set()
        await asyncio.Future()
        return {}

    monkeypatch.setattr("app.web_tasks.run_translation", fake_translation)
    manager = WebTaskManager()
    started = await manager.start(
        project,
        "translation",
        scope=Scope(),
        reuse_mixed_fingerprints=False,
        run_action=None,
    )
    await entered.wait()

    active = manager.active_tasks()
    assert len(active) == 1
    assert active[0]["task_id"] == started["task_id"]
    assert active[0]["project_id"] == read_json(
        project, project / "project.json"
    )["project_id"]

    await manager.cancel(started["task_id"])
    await manager.tasks[started["task_id"]].asyncio_task
    assert manager.active_tasks() == []


def test_web_task_options_include_completed_terminology_scans(
    tmp_path: Path,
) -> None:
    projects_root, project = make_project(tmp_path)
    project_id = str(read_json(project, project / "project.json")["project_id"])
    task_id = "TERM-TASK-COMPLETED"
    write_json(
        project,
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
        project,
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
    projects_root, project = make_project(tmp_path)
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
        active = client.get("/api/v1/tasks/active")
        assert active.status_code == 200
        assert [item["task_id"] for item in active.json()["tasks"]] == [task_id]
        assert active.json()["tasks"][0]["project_id"] == read_json(
            project, project / "project.json"
        )["project_id"]
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
        assert client.get("/api/v1/tasks/active").json()["tasks"] == []


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
            "credential": {
                "kind": "environment",
                "name": "DRAFT_LLM_API_KEY",
            },
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
    assert fake.request_url == "https://draft.example/v2/models"
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
    monkeypatch.delenv(preset["credential"]["name"], raising=False)
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


def test_web_creates_project_from_server_paths(tmp_path: Path) -> None:
    projects_root, _ = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root))
    single = tmp_path / "single.txt"
    single.write_text("one\ntwo", encoding="utf-8")
    folder = tmp_path / "book"
    folder.mkdir()
    (folder / "chapter10.txt").write_text("ten", encoding="utf-8")
    (folder / "chapter2.txt").write_text("two", encoding="utf-8")
    (folder / "chapter02.txt").write_text("zero two", encoding="utf-8")
    (folder / "nested").mkdir()
    (folder / "nested" / "chapter1.txt").write_text("nested", encoding="utf-8")
    (folder / "notes.md").write_text("ignored", encoding="utf-8")

    created = client.post(
        "/api/v1/projects",
        data={
            "name": "server-path-project",
            "empty": "false",
            "parent_dir": str(projects_root),
            "server_paths": [str(single), str(folder)],
            "server_input_kinds": ["file", "folder"],
            "adapter_options": "{}",
        },
    )
    assert created.status_code == 200, created.text
    overview = client.get("/api/v1/projects/server-path-project").json()
    assert [item["name"] for item in overview["files"]] == [
        "single.txt",
        "chapter02.txt",
        "chapter2.txt",
        "chapter10.txt",
        "nested/chapter1.txt",
    ]


def test_web_server_paths_reject_invalid_inputs(tmp_path: Path) -> None:
    projects_root, _ = make_project(tmp_path)
    client = TestClient(create_app(projects_root=projects_root))
    folder = tmp_path / "book"
    folder.mkdir()
    (folder / "notes.md").write_text("x", encoding="utf-8")

    def post(**fields: object):
        return client.post(
            "/api/v1/projects",
            data={
                "name": "bad-inputs",
                "empty": "false",
                "parent_dir": str(projects_root),
                "adapter_options": "{}",
                **fields,
            },
        )

    missing = post(server_paths=[str(tmp_path / "nope.txt")], server_input_kinds=["file"])
    assert missing.status_code == 400
    assert "不存在或无法访问" in missing.json()["error"]

    unsupported = post(server_paths=[str(tmp_path / "nope.txt")], server_input_kinds=["folder"])
    assert unsupported.status_code == 400

    empty_folder = post(server_paths=[str(folder)], server_input_kinds=["folder"])
    assert empty_folder.status_code == 400
    assert "没有受支持的输入文件" in empty_folder.json()["error"]

    relative = post(server_paths=["relative.txt"], server_input_kinds=["file"])
    assert relative.status_code == 400


def test_web_directory_browse_skips_unreadable_children(tmp_path: Path) -> None:
    import stat as stat_module

    projects_root, _ = make_project(tmp_path)
    base = tmp_path / "base"
    (base / "blocked").mkdir(parents=True)
    (base / "open").mkdir(parents=True)
    blocked = base / "blocked"
    blocked.chmod(0)
    try:
        client = TestClient(create_app(projects_root=projects_root))
        response = client.post("/api/v1/directories", json={"path": str(base)})
        assert response.status_code == 200
        by_name = {item["name"]: item for item in response.json()["directories"]}
        assert "open" in by_name
        assert by_name["blocked"]["is_project"] is False
    finally:
        blocked.chmod(stat_module.S_IRWXU)
