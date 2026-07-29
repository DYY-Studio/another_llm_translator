from __future__ import annotations

import io
import json
from email.message import Message
from pathlib import Path

import pytest

from app.editor import EDITOR_HTML, EditorStore, _handler
from app.errors import UsageError
from app.execution import latest_completed_by_segment, load_stage_history
from app.project import init_project
from app.stages import export_project, load_terms, match_terms
from app.storage import read_json
from tests.test_foundation import make_app_root


def create_editor_project(tmp_path: Path, text: str = "one\n\ntwo") -> Path:
    app_root = make_app_root(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text(text, encoding="utf-8-sig")
    project, _ = init_project(
        [str(source)],
        name="editor-demo",
        app_root=app_root,
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    return project


def test_editor_overview_excludes_empty_segments_and_preserves_order(
    tmp_path: Path,
) -> None:
    project = create_editor_project(tmp_path, "first\n\u3000\nsecond")
    overview = EditorStore(project).overview()
    assert overview["name"] == "editor-demo"
    assert [item["source"] for item in overview["segments"]] == ["first", "second"]
    assert [item["line_index"] for item in overview["segments"]] == [0, 2]


def test_editor_translation_appends_results_and_exports_latest(tmp_path: Path) -> None:
    project = create_editor_project(tmp_path, "one")
    store = EditorStore(project)
    before_runs = list((project / "runs").iterdir())
    first = store.save_translation(
        {"segment_id": "F0001-S000001", "text": "第一版"}
    )
    second = store.save_translation(
        {"segment_id": "F0001-S000001", "text": "第二版"}
    )

    history = load_stage_history(project, "translation")
    latest = latest_completed_by_segment(history)["F0001-S000001"]
    assert len(history) == 2
    assert first["record_id"] != second["record_id"]
    assert latest["text"] == "第二版"
    assert latest["origin"] == "project_editor"
    assert latest["run_id"] is None
    assert list((project / "runs").iterdir()) == before_runs

    export_project(project, "translated", bilingual=False, allow_missing=False)
    output = project / "output" / "translated" / "source.txt"
    assert output.read_text(encoding="utf-8-sig") == "第二版"


def test_editor_translation_records_enabled_validator_warning(tmp_path: Path) -> None:
    project = create_editor_project(tmp_path, "one")
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "japanese_kana = false", "japanese_kana = true"
        ),
        encoding="utf-8",
    )
    result = EditorStore(project).save_translation(
        {"segment_id": "F0001-S000001", "text": "残留カナ"}
    )
    assert result["validation_status"] == "warning"
    record = load_stage_history(project, "translation")[-1]
    assert record["validation_findings"][0]["validator"] == "japanese_kana"


def test_editor_saves_and_applies_review_results_with_current_lineage(
    tmp_path: Path,
) -> None:
    project = create_editor_project(tmp_path, "one")
    store = EditorStore(project)
    store.save_translation({"segment_id": "F0001-S000001", "text": "译文一"})
    proof = store.save_review(
        {
            "stage": "proofreading",
            "segment_id": "F0001-S000001",
            "review_status": "suggested",
            "suggested_text": "校对文本",
            "reason": "人工调整",
            "apply": True,
        }
    )
    assert proof["applied"]["text"] == "校对文本"
    assert proof["suggestion"]["origin"] == "project_editor"

    store.save_review(
        {
            "stage": "polishing",
            "segment_id": "F0001-S000001",
            "review_status": "suggested",
            "suggested_text": "润色文本",
            "reason": "",
            "apply": True,
        }
    )
    detail = store.segment_detail("F0001-S000001")
    assert detail["reviews"]["proofreading"]["applied"]["text"] == "校对文本"
    assert detail["reviews"]["polishing"]["base"]["text"] == "校对文本"
    assert detail["reviews"]["polishing"]["applied"]["text"] == "润色文本"

    store.save_translation({"segment_id": "F0001-S000001", "text": "译文二"})
    assert store.segment_detail("F0001-S000001")["reviews"]["proofreading"][
        "outdated"
    ]
    store.save_review(
        {
            "stage": "proofreading",
            "segment_id": "F0001-S000001",
            "review_status": "accepted",
            "suggested_text": "ignored",
            "reason": None,
            "apply": True,
        }
    )
    applied = latest_completed_by_segment(
        load_stage_history(project, "proofreading_applied")
    )["F0001-S000001"]
    assert applied["text"] == "译文二"


def test_editor_terms_update_library_and_overrides_immediately(tmp_path: Path) -> None:
    project = create_editor_project(tmp_path)
    store = EditorStore(project)
    added = store.save_term(
        {
            "old_normalized": None,
            "source": "Alice",
            "preferred_translation": "爱丽丝",
            "category": "人名",
            "description": "主角",
            "aliases": ["Alice A."],
            "disabled": False,
        }
    )
    assert added["terms_revision"] == 1
    assert match_terms("Alice arrived", load_terms(project), 10)[0][
        "preferred_translation"
    ] == "爱丽丝"

    renamed = store.save_term(
        {
            "old_normalized": "alice",
            "source": "Alicia",
            "preferred_translation": "艾丽西亚",
            "category": "人名",
            "description": "主角",
            "aliases": [],
            "disabled": False,
        }
    )
    assert renamed["terms_revision"] == 2
    overrides = read_json(project / "terminology" / "overrides.json")["overrides"]
    assert next(item for item in overrides if item["normalized"] == "alice")[
        "disabled"
    ]
    assert load_terms(project)["terms"][0]["normalized"] == "alicia"

    with pytest.raises(UsageError, match="已存在"):
        store.save_term(
            {
                "old_normalized": None,
                "source": "Alicia",
                "aliases": [],
                "disabled": False,
            }
        )
    disabled = store.save_term(
        {
            "old_normalized": "alicia",
            "source": "Alicia",
            "preferred_translation": "艾丽西亚",
            "category": "人名",
            "description": "主角",
            "aliases": [],
            "disabled": True,
        }
    )
    assert disabled["terms_revision"] == 3
    assert load_terms(project)["terms"] == []


def test_editor_rejects_unknown_segments_and_invalid_terms(tmp_path: Path) -> None:
    store = EditorStore(create_editor_project(tmp_path))
    with pytest.raises(UsageError, match="未知或空"):
        store.save_translation({"segment_id": "missing", "text": "x"})
    with pytest.raises(UsageError, match="source 不能为空"):
        store.save_term({"source": " ", "aliases": []})
    with pytest.raises(UsageError, match="aliases"):
        store.save_term({"source": "Alice", "aliases": "Alice"})


def _request_handler(
    store: EditorStore,
    *,
    method: str,
    path: str,
    body: dict[str, object] | None = None,
) -> bytes:
    payload = json.dumps(body).encode() if body is not None else b""
    handler_type = _handler(store, EDITOR_HTML.read_bytes())
    handler = handler_type.__new__(handler_type)
    handler.command = method
    handler.path = path
    handler.request_version = "HTTP/1.1"
    handler.requestline = f"{method} {path} HTTP/1.1"
    handler.client_address = ("127.0.0.1", 0)
    handler.rfile = io.BytesIO(payload)
    handler.wfile = io.BytesIO()
    handler.headers = Message()
    handler.headers["Content-Length"] = str(len(payload))
    if method == "GET":
        handler.do_GET()
    else:
        handler.do_POST()
    return handler.wfile.getvalue()


def test_editor_http_serves_page_and_json_errors(tmp_path: Path) -> None:
    store = EditorStore(create_editor_project(tmp_path))
    page = _request_handler(store, method="GET", path="/")
    assert b"200 OK" in page
    assert "项目结果编辑器" in page.decode("utf-8")

    project = _request_handler(store, method="GET", path="/api/project")
    assert b"200 OK" in project
    assert '"source": "one"' in project.decode("utf-8")

    error = _request_handler(
        store,
        method="POST",
        path="/api/translation/save",
        body={"segment_id": "missing", "text": "x"},
    )
    assert b"400 Bad Request" in error
    assert "未知或空" in error.decode("utf-8")
