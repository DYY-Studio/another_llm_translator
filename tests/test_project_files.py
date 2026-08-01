from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from app.errors import ProjectError, UsageError
from app.execution import Scope, stage_result_path
from app.main import build_parser
from app.project import (
    add_project_files,
    init_project,
    remove_project_files,
)
from app.stages import (
    export_project,
    inspect_full,
    run_apply,
    run_terminology,
)
from app.storage import (
    append_jsonl,
    atomic_write_json,
    read_json,
    read_jsonl,
    record_header,
)
from tests.test_documents import make_epub
from tests.test_foundation import make_app_root


def init_empty(
    tmp_path: Path, *, adapter_id: str = "txt"
) -> Path:
    project, summary = init_project(
        [],
        name="empty",
        document_adapter_id=adapter_id,
        empty=True,
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    assert summary["file_count"] == summary["segment_count"] == 0
    assert (project / "input").is_dir()
    return project


def test_export_cli_collects_repeated_file_ids() -> None:
    args = build_parser().parse_args(
        [
            "export",
            "sample",
            "--stage",
            "translated",
            "--file",
            "F0001",
            "--file",
            "F0003",
        ]
    )
    assert args.file_ids == ["F0001", "F0003"]


def test_empty_project_can_open_inspect_and_add_txt_files(
    tmp_path: Path,
) -> None:
    project = init_empty(tmp_path)
    assert read_jsonl(project / "source" / "files.jsonl") == []
    assert inspect_full(project)["next_command"].startswith(
        "python -m app.main files-add"
    )

    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("one\n", encoding="utf-8")
    second.write_text("two", encoding="utf-8")
    summary = add_project_files(project, [str(first), str(second)])

    assert summary["added_file_ids"] == ["F0001", "F0002"]
    assert [item["file_id"] for item in read_jsonl(
        project / "source" / "files.jsonl"
    )] == ["F0001", "F0002"]
    assert read_json(project / "project.json")["next_file_sequence"] == 3


def test_remove_retains_history_and_readd_does_not_reuse_ids(
    tmp_path: Path,
) -> None:
    project = init_empty(tmp_path)
    first = tmp_path / "first.txt"
    first.write_text("one", encoding="utf-8")
    add_project_files(project, [str(first)])
    metadata = read_json(project / "project.json")
    segment_id = read_jsonl(project / "source" / "segments.jsonl")[0][
        "segment_id"
    ]
    history_path = stage_result_path(project, "translation")
    append_jsonl(
        history_path,
        record_header(
            "stage_result",
            str(metadata["project_id"]),
            stage="translation",
            segment_id=segment_id,
            status="completed",
            text="一",
            validation_status="passed",
            validation_findings=[],
            stage_fingerprint="sha256:old",
            terms_revision=None,
            run_id="RUN-OLD",
            request_id="REQ-OLD",
        ),
    )
    history_before = history_path.read_bytes()

    removed = remove_project_files(project, ["F0001"])
    assert removed["removed_segments"] == 1
    assert history_path.read_bytes() == history_before
    assert inspect_full(project)["stages"]["translation"]["completed"] == 0

    add_project_files(project, [str(first)])
    files = read_jsonl(project / "source" / "files.jsonl")
    assert [item["file_id"] for item in files] == ["F0002"]
    assert read_jsonl(project / "source" / "segments.jsonl")[0][
        "segment_id"
    ].startswith("F0002-")


def test_add_rejects_active_name_collision_and_running_run(
    tmp_path: Path,
) -> None:
    project = init_empty(tmp_path)
    source = tmp_path / "A.txt"
    source.write_text("one", encoding="utf-8")
    add_project_files(project, [str(source)])
    duplicate_dir = tmp_path / "duplicate"
    duplicate_dir.mkdir()
    duplicate = duplicate_dir / "a.txt"
    duplicate.write_text("two", encoding="utf-8")
    with pytest.raises(UsageError, match="同名导出路径"):
        add_project_files(project, [str(duplicate)])

    metadata = read_json(project / "project.json")
    manifest = record_header(
        "run",
        str(metadata["project_id"]),
        record_id="RUN-ACTIVE",
        run_id="RUN-ACTIVE",
        stage="translation",
        status="running",
        started_at="2026-07-31T00:00:00Z",
    )
    atomic_write_json(project / "runs" / "RUN-ACTIVE" / "manifest.json", manifest)
    before = (project / "source" / "files.jsonl").read_bytes()
    with pytest.raises(UsageError, match="未完成 Run"):
        remove_project_files(project, ["F0001"])
    assert (project / "source" / "files.jsonl").read_bytes() == before


def test_epub_file_state_is_removed_and_new_id_is_allocated(
    tmp_path: Path,
) -> None:
    project = init_empty(tmp_path, adapter_id="epub")
    source = tmp_path / "book.epub"
    make_epub(source)
    add_project_files(project, [str(source)])
    file_record = read_jsonl(project / "source" / "files.jsonl")[0]
    state_path = project / str(file_record["document_adapter_state"])
    assert state_path.is_file()

    remove_project_files(project, ["F0001"])
    assert not state_path.exists()
    add_project_files(project, [str(source)])
    assert read_jsonl(project / "source" / "files.jsonl")[0]["file_id"] == "F0002"


def test_empty_project_accepts_mixed_txt_and_epub_files(tmp_path: Path) -> None:
    project = init_empty(tmp_path)
    text = tmp_path / "notes.txt"
    epub = tmp_path / "book.epub"
    text.write_text("plain text", encoding="utf-8")
    make_epub(epub)

    summary = add_project_files(project, [str(text), str(epub)])

    assert summary["added_files"] == 2
    files = read_jsonl(project / "source" / "files.jsonl")
    assert [item["document_adapter_id"] for item in files] == ["txt", "epub"]
    assert files[0]["document_adapter_state"] is None
    assert (project / str(files[1]["document_adapter_state"])).is_file()
    metadata = read_json(project / "project.json")
    assert "document_adapter_id" not in metadata


def test_mixed_project_exports_original_formats_or_txt(tmp_path: Path) -> None:
    project = init_empty(tmp_path)
    text = tmp_path / "notes.txt"
    epub = tmp_path / "book.epub"
    text.write_text("plain text", encoding="utf-8")
    make_epub(epub)
    add_project_files(project, [str(text), str(epub)])
    metadata = read_json(project / "project.json")
    for segment in read_jsonl(project / "source" / "segments.jsonl"):
        append_jsonl(
            stage_result_path(project, "translation"),
            record_header(
                "stage_result",
                str(metadata["project_id"]),
                stage="translation",
                segment_id=segment["segment_id"],
                status="completed",
                text=f"译：{segment['source']}",
                validation_status="passed",
                validation_findings=[],
                stage_fingerprint="sha256:mixed",
                terms_revision=0,
                run_id="RUN-MIXED",
                request_id="REQ-MIXED",
            ),
        )

    original = export_project(
        project,
        "translated",
        bilingual=False,
        allow_missing=False,
        output_format="original",
    )
    txt = export_project(
        project,
        "translated",
        bilingual=False,
        allow_missing=False,
        output_format="txt",
    )

    assert {Path(item).suffix for item in original["written"]} == {".txt", ".epub"}
    assert {Path(item).name for item in txt["written"]} == {"notes.txt", "book.txt"}


def test_implicit_file_format_rejects_unknown_extension(tmp_path: Path) -> None:
    project = init_empty(tmp_path)
    source = tmp_path / "notes.md"
    source.write_text("text", encoding="utf-8")
    with pytest.raises(UsageError, match="没有 Document Adapter"):
        add_project_files(project, [str(source)])


def test_auto_import_recurses_all_supported_formats_and_preserves_paths(
    tmp_path: Path,
) -> None:
    project = init_empty(tmp_path)
    source_root = tmp_path / "source-tree"
    (source_root / "chapters").mkdir(parents=True)
    (source_root / "chapters" / "one.text").write_text("one", encoding="utf-8")
    (source_root / "notes.bin").write_bytes(b"ignored")
    make_epub(source_root / "book.epub")

    summary = add_project_files(project, [str(source_root)], recursive=True)

    files = read_jsonl(project / "source" / "files.jsonl")
    assert [item["original_name"] for item in files] == [
        "book.epub",
        "chapters/one.text",
    ]
    assert [item["document_adapter_id"] for item in files] == ["epub", "txt"]
    assert summary["warnings"] == [
        f"{source_root}: 已忽略 1 个不支持的文件"
    ]


def test_auto_import_rejects_case_insensitive_effective_path_collision(
    tmp_path: Path,
) -> None:
    project = init_empty(tmp_path)
    first = tmp_path / "first" / "same.txt"
    second = tmp_path / "second" / "SAME.TXT"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")

    with pytest.raises(UsageError, match="重复导出相对路径"):
        add_project_files(project, [str(first), str(second)])
    assert read_jsonl(project / "source" / "files.jsonl") == []


def test_export_file_filter_limits_result_validation_and_output(
    tmp_path: Path,
) -> None:
    project = init_empty(tmp_path)
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")
    add_project_files(project, [str(first), str(second)])
    metadata = read_json(project / "project.json")
    first_segment = read_jsonl(project / "source" / "segments.jsonl")[0]
    append_jsonl(
        stage_result_path(project, "translation"),
        record_header(
            "stage_result",
            str(metadata["project_id"]),
            stage="translation",
            segment_id=first_segment["segment_id"],
            status="completed",
            text="一",
            validation_status="passed",
            validation_findings=[],
            stage_fingerprint="sha256:selected",
            terms_revision=0,
            run_id="RUN-SELECTED",
            request_id="REQ-SELECTED",
        ),
    )

    summary = export_project(
        project,
        "translated",
        bilingual=False,
        allow_missing=False,
        file_ids=["F0001"],
    )

    assert summary["selected_file_ids"] == ["F0001"]
    assert [Path(item).name for item in summary["written"]] == ["first.txt"]
    with pytest.raises(UsageError, match="未知文件 ID"):
        export_project(
            project,
            "translated",
            bilingual=False,
            allow_missing=False,
            file_ids=["F9999"],
        )
    with pytest.raises(UsageError, match="范围不能为空"):
        export_project(
            project,
            "translated",
            bilingual=False,
            allow_missing=False,
            file_ids=[],
        )


def test_txt_export_name_collision_fails_before_publish(tmp_path: Path) -> None:
    project = init_empty(tmp_path)
    text = tmp_path / "book.txt"
    epub = tmp_path / "book.epub"
    text.write_text("plain", encoding="utf-8")
    make_epub(epub)
    add_project_files(project, [str(text), str(epub)])

    output_dir = project / "output" / "translated"
    with pytest.raises(ProjectError, match="重复输出路径"):
        export_project(
            project,
            "translated",
            bilingual=False,
            allow_missing=True,
            output_format="txt",
        )
    assert not output_dir.exists()


@pytest.mark.parametrize("blank_text", [None, "", " \t\u3000"])
def test_main_workflows_fail_before_run_for_no_nonempty_segments(
    tmp_path: Path,
    blank_text: str | None,
) -> None:
    project = init_empty(tmp_path)
    if blank_text is not None:
        source = tmp_path / "blank.txt"
        source.write_text(blank_text, encoding="utf-8-sig")
        add_project_files(project, [str(source)])

    with pytest.raises(UsageError, match="没有可处理的非空 Segment"):
        asyncio.run(run_terminology(project, Scope()))
    with pytest.raises(UsageError, match="没有可处理的非空 Segment"):
        run_apply(
            project,
            "proofreading",
            Scope(),
            allow_outdated_base=False,
            confirmed_all=True,
        )
    with pytest.raises(UsageError, match="没有可处理的非空 Segment"):
        export_project(
            project,
            "translated",
            bilingual=False,
            allow_missing=True,
        )
    assert list((project / "runs").iterdir()) == []


def test_init_requires_exactly_input_or_empty(tmp_path: Path) -> None:
    app_root = make_app_root(tmp_path)
    with pytest.raises(UsageError, match="提供输入文件"):
        init_project(
            [],
            name="bad",
            app_root=app_root,
            projects_root=tmp_path / "projects",
        )
    source = tmp_path / "source.txt"
    source.write_text("one", encoding="utf-8")
    with pytest.raises(UsageError, match="提供输入文件"):
        init_project(
            [str(source)],
            name="bad",
            empty=True,
            app_root=app_root,
            projects_root=tmp_path / "projects",
        )


def test_add_and_remove_restore_inputs_when_staging_move_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = init_empty(tmp_path)
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")
    original_replace = os.replace
    add_moves = 0

    def fail_second_add_move(source: str | Path, target: str | Path) -> None:
        nonlocal add_moves
        if ".files-add." in str(source) and "/input/" in str(source):
            add_moves += 1
            if add_moves == 2:
                raise OSError("copy publish failed")
        original_replace(source, target)

    monkeypatch.setattr("app.project.os.replace", fail_second_add_move)
    with pytest.raises(OSError, match="copy publish failed"):
        add_project_files(project, [str(first), str(second)])
    assert read_jsonl(project / "source" / "files.jsonl") == []
    assert not list((project / "input").rglob("F*"))

    monkeypatch.setattr("app.project.os.replace", original_replace)
    add_project_files(project, [str(first), str(second)])
    stored = [
        project / "input" / str(item["stored_name"])
        for item in read_jsonl(project / "source" / "files.jsonl")
    ]
    remove_moves = 0

    def fail_second_remove_move(source: str | Path, target: str | Path) -> None:
        nonlocal remove_moves
        if "/input/" in str(source) and "/removed-input/" in str(target):
            remove_moves += 1
            if remove_moves == 2:
                raise OSError("remove staging failed")
        original_replace(source, target)

    monkeypatch.setattr("app.project.os.replace", fail_second_remove_move)
    with pytest.raises(OSError, match="remove staging failed"):
        remove_project_files(project, ["F0001", "F0002"])
    assert all(path.is_file() for path in stored)
    assert len(read_jsonl(project / "source" / "files.jsonl")) == 2
