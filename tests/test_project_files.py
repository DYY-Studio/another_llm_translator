from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path

import pytest

from app.errors import ProjectError, StorageError, UsageError
from app.execution import Scope, stage_result_path
from app.main import build_parser, parse_adapter_option_args
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
from app.sqlite_storage import (
    append_jsonl,
    read_files,
    read_json,
    read_jsonl,
    read_segments,
    record_header,
    write_json,
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


def rewrite_segment_payload(project: Path, segment: dict[str, object]) -> None:
    with sqlite3.connect(project / "project.sqlite") as connection:
        connection.execute(
            "UPDATE segments SET payload_json = ? WHERE segment_id = ?",
            (json.dumps(segment, ensure_ascii=False), segment["segment_id"]),
        )


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


def test_adapter_options_are_collected_for_cli_imports() -> None:
    init_args = build_parser().parse_args(
        [
            "init",
            "book.epub",
            "--name",
            "book",
            "--document-adapter",
            "epub",
            "--adapter-option",
            "epub.ruby_mode=parenthetical",
        ]
    )
    add_args = build_parser().parse_args(
        [
            "files-add",
            "book",
            "next.epub",
            "--adapter-option",
            "epub.ruby_mode=base_only",
        ]
    )

    assert init_args.adapter_options == ["epub.ruby_mode=parenthetical"]
    assert add_args.adapter_options == ["epub.ruby_mode=base_only"]


def test_parse_adapter_option_args_builds_adapter_dict() -> None:
    assert parse_adapter_option_args(
        ["record.source_style=marked", "record.line_ending=crlf", "a.b=x"]
    ) == {
        "record": {"source_style": "marked", "line_ending": "crlf"},
        "a": {"b": "x"},
    }
    with pytest.raises(UsageError, match="重复"):
        parse_adapter_option_args(["a.b=1", "a.b=2"])
    for value in ("a.b", "=x", "a=x", ".b=x", "a.=x", "a.b.c=x"):
        with pytest.raises(UsageError, match="格式无效"):
            parse_adapter_option_args([value])


def test_empty_project_can_open_inspect_and_add_txt_files(
    tmp_path: Path,
) -> None:
    project = init_empty(tmp_path)
    assert read_files(project) == []
    assert inspect_full(project)["next_command"].startswith(
        "python -m app.main files-add"
    )

    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("one\n", encoding="utf-8")
    second.write_text("two", encoding="utf-8")
    summary = add_project_files(project, [str(first), str(second)])

    assert summary["added_file_ids"] == ["F0001", "F0002"]
    assert [item["file_id"] for item in read_files(project)] == ["F0001", "F0002"]
    assert {
        item["part_id"]
        for item in read_segments(project)
    } == {"document"}
    assert read_json(project, project / "project.json")["next_file_sequence"] == 3


def test_old_project_without_part_id_requires_rebuild(tmp_path: Path) -> None:
    project = init_empty(tmp_path)
    source = tmp_path / "old.txt"
    source.write_text("one", encoding="utf-8")
    add_project_files(project, [str(source)])
    segments = read_segments(project)
    segments[0].pop("part_id")
    rewrite_segment_payload(project, segments[0])

    with pytest.raises(ProjectError, match="part_id.*重新创建"):
        inspect_full(project)


def test_remove_retains_history_and_readd_does_not_reuse_ids(
    tmp_path: Path,
) -> None:
    project = init_empty(tmp_path)
    first = tmp_path / "first.txt"
    first.write_text("one", encoding="utf-8")
    add_project_files(project, [str(first)])
    metadata = read_json(project, project / "project.json")
    segment_id = read_segments(project)[0][
        "segment_id"
    ]
    history_path = stage_result_path(project, "translation")
    append_jsonl(
        project,
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
    history_before = read_jsonl(project, history_path)

    removed = remove_project_files(project, ["F0001"])
    assert removed["removed_segments"] == 1
    assert read_jsonl(project, history_path) == history_before
    assert inspect_full(project)["stages"]["translation"]["completed"] == 0

    add_project_files(project, [str(first)])
    files = read_files(project)
    assert [item["file_id"] for item in files] == ["F0002"]
    assert read_segments(project)[0][
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

    metadata = read_json(project, project / "project.json")
    manifest = record_header(
        "run",
        str(metadata["project_id"]),
        record_id="RUN-ACTIVE",
        run_id="RUN-ACTIVE",
        stage="translation",
        status="running",
        started_at="2026-07-31T00:00:00Z",
    )
    write_json(project, project / "runs" / "RUN-ACTIVE" / "manifest.json", manifest)
    before = read_files(project)
    with pytest.raises(UsageError, match="未完成 Run"):
        remove_project_files(project, ["F0001"])
    assert read_files(project) == before


def test_epub_file_state_is_removed_and_new_id_is_allocated(
    tmp_path: Path,
) -> None:
    project = init_empty(tmp_path, adapter_id="epub")
    source = tmp_path / "book.epub"
    make_epub(source)
    add_project_files(project, [str(source)])
    file_record = read_files(project)[0]
    state_path = project / str(file_record["document_adapter_state"])
    assert read_json(project, state_path)["file_id"] == "F0001"

    remove_project_files(project, ["F0001"])
    with pytest.raises(StorageError, match="记录不存在"):
        read_json(project, state_path)
    add_project_files(project, [str(source)])
    assert read_files(project)[0]["file_id"] == "F0002"


def test_empty_project_accepts_mixed_txt_and_epub_files(tmp_path: Path) -> None:
    project = init_empty(tmp_path)
    text = tmp_path / "notes.txt"
    epub = tmp_path / "book.epub"
    text.write_text("plain text", encoding="utf-8")
    make_epub(epub)

    summary = add_project_files(project, [str(text), str(epub)])

    assert summary["added_files"] == 2
    files = read_files(project)
    assert [item["document_adapter_id"] for item in files] == ["txt", "epub"]
    assert files[0]["document_adapter_state"] is None
    assert read_json(project, project / str(files[1]["document_adapter_state"]))["file_id"] == files[1]["file_id"]
    metadata = read_json(project, project / "project.json")
    assert "document_adapter_id" not in metadata


def test_mixed_project_exports_original_formats_or_txt(tmp_path: Path) -> None:
    project = init_empty(tmp_path)
    text = tmp_path / "notes.txt"
    epub = tmp_path / "book.epub"
    text.write_text("plain text", encoding="utf-8")
    make_epub(epub)
    add_project_files(project, [str(text), str(epub)])
    metadata = read_json(project, project / "project.json")
    for segment in read_segments(project):
        append_jsonl(
            project,
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

    files = read_files(project)
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
    assert read_files(project) == []


def test_export_file_filter_limits_result_validation_and_output(
    tmp_path: Path,
) -> None:
    project = init_empty(tmp_path)
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")
    add_project_files(project, [str(first), str(second)])
    metadata = read_json(project, project / "project.json")
    first_segment = read_segments(project)[0]
    append_jsonl(
        project,
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
    assert read_files(project) == []
    assert not list((project / "input").rglob("F*"))

    monkeypatch.setattr("app.project.os.replace", original_replace)
    add_project_files(project, [str(first), str(second)])
    stored = [
        project / "input" / str(item["stored_name"])
        for item in read_files(project)
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
    assert len(read_files(project)) == 2
