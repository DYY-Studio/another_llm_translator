from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import zipfile
from pathlib import Path

import pytest

import app.main as main_module
import app.project as project_module
from app.errors import ProjectError, StorageError, UsageError
from app.execution import Scope, select_scope, stage_result_path
from app.file_replacement import align_segments
from app.main import build_parser, parse_adapter_option_args, run
from app.project import (
    add_project_files,
    apply_file_replacement,
    init_project,
    prepare_file_replacement,
    remove_project_files,
    reorder_project_files,
)
from app.sqlite_storage import (
    append_jsonl,
    read_files,
    read_json,
    read_jsonl,
    read_segments,
    record_header,
    replace_source,
    write_json,
)
from app.stages import (
    export_project,
    inspect_full,
    run_apply,
    run_terminology,
)
from tests.test_documents import RUBY_XHTML, make_epub
from tests.test_foundation import make_app_root


def replacement_segment(
    segment_id: str,
    source: str,
    *,
    part_id: str = "document",
    model_source: str | None = None,
) -> dict[str, object]:
    return {
        "segment_id": segment_id,
        "file_id": "F0001",
        "line_index": 0,
        "part_id": part_id,
        "source": source,
        "model_source": model_source,
        "is_empty": source == "",
    }


def test_segment_alignment_preserves_ordered_matches_across_insertions() -> None:
    old = [
        replacement_segment("old-a", "A"),
        replacement_segment("old-anchor", "ANCHOR"),
        replacement_segment("old-b", "B"),
    ]
    new = [
        replacement_segment("new-a", "A"),
        replacement_segment("new-extra", "EXTRA"),
        replacement_segment("new-anchor", "ANCHOR"),
        replacement_segment("new-b", "B"),
    ]

    result = align_segments(old, new)

    assert result.preserved_new_to_old == {0: 0, 2: 1, 3: 2}
    assert result.ambiguous_old_indices == ()
    assert result.ambiguous_new_indices == ()


def test_segment_alignment_reuses_multiple_anchors_and_each_gap() -> None:
    old = [
        replacement_segment("old-prefix", "PREFIX"),
        replacement_segment("old-one", "ONE"),
        replacement_segment("old-two", "TWO"),
        replacement_segment("old-three", "THREE"),
        replacement_segment("old-suffix", "SUFFIX"),
    ]
    new = [
        replacement_segment("new-prefix", "PREFIX"),
        replacement_segment("new-insert-one", "INSERT-ONE"),
        replacement_segment("new-one", "ONE"),
        replacement_segment("new-insert-two", "INSERT-TWO"),
        replacement_segment("new-two", "TWO"),
        replacement_segment("new-three", "THREE"),
        replacement_segment("new-suffix", "SUFFIX"),
    ]

    result = align_segments(old, new)

    assert result.preserved_new_to_old == {
        0: 0,
        2: 1,
        4: 2,
        5: 3,
        6: 4,
    }
    assert result.ambiguous_old_indices == ()
    assert result.ambiguous_new_indices == ()


def test_segment_alignment_does_not_reuse_ambiguous_duplicate_text() -> None:
    old = [
        replacement_segment("old-dup-1", "DUP"),
        replacement_segment("old-anchor", "ANCHOR"),
        replacement_segment("old-dup-2", "DUP"),
    ]
    new = [
        replacement_segment("new-dup-1", "DUP"),
        replacement_segment("new-anchor", "ANCHOR"),
        replacement_segment("new-dup-2", "DUP"),
        replacement_segment("new-dup-3", "DUP"),
    ]

    result = align_segments(old, new)

    assert result.preserved_new_to_old == {1: 1}
    assert result.ambiguous_old_indices == (0, 2)
    assert result.ambiguous_new_indices == (0, 2, 3)


def test_segment_alignment_marks_reordered_duplicate_occurrences_ambiguous() -> None:
    old = [
        replacement_segment("old-dup-before", "DUP"),
        replacement_segment("old-anchor", "ANCHOR"),
        replacement_segment("old-dup-after", "DUP"),
    ]
    new = [
        replacement_segment("new-dup-before", "DUP"),
        replacement_segment("new-dup-after", "DUP"),
        replacement_segment("new-anchor", "ANCHOR"),
    ]

    result = align_segments(old, new)

    assert result.preserved_new_to_old == {2: 1}
    assert result.ambiguous_old_indices == (0, 2)
    assert result.ambiguous_new_indices == (0, 1)


def test_segment_alignment_reuses_exact_duplicate_run_around_change() -> None:
    old = [
        replacement_segment("old-dup-1", "DUP"),
        replacement_segment("old-dup-2", "DUP"),
        replacement_segment("old-change", "OLD"),
    ]
    new = [
        replacement_segment("new-dup-1", "DUP"),
        replacement_segment("new-dup-2", "DUP"),
        replacement_segment("new-change", "NEW"),
    ]

    result = align_segments(old, new)

    assert result.preserved_new_to_old == {0: 0, 1: 1}
    assert result.ambiguous_old_indices == ()
    assert result.ambiguous_new_indices == ()


def test_segment_alignment_matches_reordered_parts_independently() -> None:
    old = [
        replacement_segment("old-a", "A", part_id="a"),
        replacement_segment("old-b", "B", part_id="a"),
        replacement_segment("old-c", "C", part_id="b"),
    ]
    new = [
        replacement_segment("new-c", "C", part_id="b"),
        replacement_segment("new-a", "A", part_id="a"),
        replacement_segment("new-b", "B", part_id="a"),
    ]

    result = align_segments(old, new)

    assert result.preserved_new_to_old == {0: 2, 1: 0, 2: 1}


def test_segment_alignment_requires_effective_model_source_match() -> None:
    old = [replacement_segment("old", "A", model_source="model A")]
    new = [replacement_segment("new", "A", model_source="model B")]

    result = align_segments(old, new)

    assert result.preserved_new_to_old == {}
    assert result.ambiguous_old_indices == ()
    assert result.ambiguous_new_indices == ()


def test_file_replacement_preserves_ids_and_progress_when_inserting_segment(
    tmp_path: Path,
) -> None:
    project = init_empty(tmp_path)
    original = tmp_path / "book.txt"
    replacement = tmp_path / "book-revised.txt"
    original.write_text("A\nANCHOR\nB", encoding="utf-8")
    replacement.write_text("A\nEXTRA\nANCHOR\nB", encoding="utf-8")
    add_project_files(project, [str(original)])
    old_segments = read_segments(project)
    metadata = read_json(project, project / "project.json")
    append_jsonl(
        project,
        stage_result_path(project, "translation"),
        record_header(
            "stage_result",
            str(metadata["project_id"]),
            stage="translation",
            segment_id=str(old_segments[2]["segment_id"]),
            status="completed",
            text="B译文",
            validation_status="passed",
            validation_findings=[],
            stage_fingerprint="sha256:test",
            terms_revision=None,
            run_id="RUN-OLD",
            request_id="REQ-OLD",
        ),
    )

    plan = prepare_file_replacement(project, "F0001", replacement)

    assert plan.impact["preserved_segment_count"] == 3
    assert plan.impact["added_segment_count"] == 1
    assert plan.impact["removed_segment_count"] == 0
    apply_file_replacement(project, plan)

    segments = read_segments(project)
    assert [item["source"] for item in segments] == ["A", "EXTRA", "ANCHOR", "B"]
    assert [item["segment_id"] for item in segments] == [
        old_segments[0]["segment_id"],
        "F0001-S000004",
        old_segments[1]["segment_id"],
        old_segments[2]["segment_id"],
    ]
    assert [item["line_index"] for item in segments] == [0, 1, 2, 3]
    assert read_files(project)[0]["next_segment_sequence"] == 5
    assert read_jsonl(project, stage_result_path(project, "translation"))[-1][
        "segment_id"
    ] == old_segments[2]["segment_id"]


def test_file_replacement_removes_changed_segments_but_keeps_history(
    tmp_path: Path,
) -> None:
    project = init_empty(tmp_path)
    original = tmp_path / "book.txt"
    replacement = tmp_path / "book-revised.txt"
    original.write_text("KEEP\nCHANGE\nREMOVE", encoding="utf-8")
    replacement.write_text("KEEP\nCHANGED", encoding="utf-8")
    add_project_files(project, [str(original)])
    old_segments = read_segments(project)
    metadata = read_json(project, project / "project.json")
    history_path = stage_result_path(project, "translation")
    for segment in old_segments:
        append_jsonl(
            project,
            history_path,
            record_header(
                "stage_result",
                str(metadata["project_id"]),
                stage="translation",
                segment_id=str(segment["segment_id"]),
                status="completed",
                text=f"译:{segment['source']}",
                validation_status="passed",
                validation_findings=[],
                stage_fingerprint="sha256:test",
                terms_revision=None,
                run_id="RUN-OLD",
                request_id=f"REQ-{segment['line_index']}",
            ),
        )

    plan = prepare_file_replacement(project, "F0001", replacement)
    assert plan.impact["preserved_segment_count"] == 1
    assert plan.impact["added_segment_count"] == 1
    assert plan.impact["removed_segment_count"] == 2
    apply_file_replacement(project, plan)

    active_ids = {str(item["segment_id"]) for item in read_segments(project)}
    assert active_ids == {str(old_segments[0]["segment_id"]), "F0001-S000004"}
    assert {str(item["segment_id"]) for item in read_jsonl(project, history_path)} == {
        str(item["segment_id"]) for item in old_segments
    }


def test_file_replacement_does_not_reuse_ids_across_replacements(
    tmp_path: Path,
) -> None:
    project = init_empty(tmp_path)
    original = tmp_path / "book.txt"
    first = tmp_path / "book-1.txt"
    second = tmp_path / "book-2.txt"
    original.write_text("A", encoding="utf-8")
    first.write_text("B", encoding="utf-8")
    second.write_text("C", encoding="utf-8")
    add_project_files(project, [str(original)])

    apply_file_replacement(
        project, prepare_file_replacement(project, "F0001", first)
    )
    first_id = str(read_segments(project)[0]["segment_id"])
    apply_file_replacement(
        project, prepare_file_replacement(project, "F0001", second)
    )

    assert first_id == "F0001-S000002"
    assert read_segments(project)[0]["segment_id"] == "F0001-S000003"


def test_file_replacement_uses_staged_input_after_external_change(
    tmp_path: Path,
) -> None:
    project = init_empty(tmp_path)
    original = tmp_path / "original.txt"
    replacement = tmp_path / "replacement.txt"
    original.write_text("one", encoding="utf-8")
    replacement.write_text("two", encoding="utf-8")
    add_project_files(project, [str(original)])

    plan = prepare_file_replacement(project, "F0001", replacement)
    assert plan.staged_input.read_text(encoding="utf-8") == "two"
    replacement.write_text("three", encoding="utf-8")

    try:
        apply_file_replacement(project, plan)
    finally:
        plan.cleanup()

    stored = project / "input" / str(read_files(project)[0]["stored_name"])
    assert stored.read_text(encoding="utf-8") == "two"
    assert not plan.temporary_root.exists()


def test_file_replacement_rejects_staged_input_changed_during_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = init_empty(tmp_path)
    original = tmp_path / "original.txt"
    replacement = tmp_path / "replacement.txt"
    original.write_text("one", encoding="utf-8")
    replacement.write_text("two", encoding="utf-8")
    add_project_files(project, [str(original)])
    imported = project_module._import_project_inputs
    roots: list[Path] = []
    original_mkdtemp = project_module.tempfile.mkdtemp

    def capture_root(*args: object, **kwargs: object) -> str:
        root = original_mkdtemp(*args, **kwargs)
        roots.append(Path(root))
        return root

    def mutate_during_parse(*args: object, **kwargs: object) -> object:
        inputs = args[0]
        assert isinstance(inputs, list)
        Path(str(inputs[0])).write_text("tampered", encoding="utf-8")
        return imported(*args, **kwargs)

    monkeypatch.setattr(project_module, "_import_project_inputs", mutate_during_parse)
    monkeypatch.setattr(project_module.tempfile, "mkdtemp", capture_root)
    with pytest.raises(UsageError, match="解析期间变化"):
        prepare_file_replacement(project, "F0001", replacement)
    assert roots and not roots[0].exists()


def test_file_replacement_rejects_staged_input_changed_before_apply(
    tmp_path: Path,
) -> None:
    project = init_empty(tmp_path)
    original = tmp_path / "original.txt"
    replacement = tmp_path / "replacement.txt"
    original.write_text("one", encoding="utf-8")
    replacement.write_text("two", encoding="utf-8")
    add_project_files(project, [str(original)])
    plan = prepare_file_replacement(project, "F0001", replacement)
    plan.staged_input.write_text("tampered", encoding="utf-8")

    try:
        with pytest.raises(UsageError, match="替换输入已变化"):
            apply_file_replacement(project, plan)
    finally:
        plan.cleanup()


def test_file_replacement_rejects_project_staging_digest_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = init_empty(tmp_path)
    original = tmp_path / "original.txt"
    replacement = tmp_path / "replacement.txt"
    original.write_text("one", encoding="utf-8")
    replacement.write_text("two", encoding="utf-8")
    add_project_files(project, [str(original)])
    plan = prepare_file_replacement(project, "F0001", replacement)
    copy2 = project_module.shutil.copy2

    def copy_and_tamper(source: str | Path, target: str | Path) -> object:
        result = copy2(source, target)
        if Path(target).name == "new-input":
            Path(target).write_text("tampered", encoding="utf-8")
        return result

    monkeypatch.setattr(project_module.shutil, "copy2", copy_and_tamper)
    try:
        with pytest.raises(UsageError, match="复制结果"):
            apply_file_replacement(project, plan)
    finally:
        plan.cleanup()

    stored = project / "input" / str(read_files(project)[0]["stored_name"])
    assert stored.read_text(encoding="utf-8") == "one"


def test_files_replace_cli_cleans_plan_after_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = init_empty(tmp_path)
    original = tmp_path / "original.txt"
    replacement = tmp_path / "replacement.txt"
    original.write_text("one", encoding="utf-8")
    replacement.write_text("two", encoding="utf-8")
    add_project_files(project, [str(original)])
    plans = []
    prepare = main_module.prepare_file_replacement

    def capture_plan(*args: object, **kwargs: object) -> object:
        plan = prepare(*args, **kwargs)
        plans.append(plan)
        return plan

    monkeypatch.setattr(main_module, "prepare_file_replacement", capture_plan)
    assert (
        run(
            [
                "files-replace",
                str(project),
                "F0001",
                str(replacement),
                "--dry-run",
            ]
        )
        == 0
    )
    assert len(plans) == 1
    assert not plans[0].temporary_root.exists()


def test_file_replacement_rejects_directory_input(tmp_path: Path) -> None:
    project = init_empty(tmp_path)
    original = tmp_path / "book.txt"
    original.write_text("A", encoding="utf-8")
    add_project_files(project, [str(original)])
    replacement_dir = tmp_path / "replacement"
    replacement_dir.mkdir()
    (replacement_dir / "one.txt").write_text("B", encoding="utf-8")

    with pytest.raises(UsageError, match="单个文件"):
        prepare_file_replacement(project, "F0001", replacement_dir)


def test_file_replacement_requires_publishing_pending_term_candidates(
    tmp_path: Path,
) -> None:
    project = init_empty(tmp_path)
    original = tmp_path / "book.txt"
    replacement = tmp_path / "book-revised.txt"
    original.write_text("A", encoding="utf-8")
    replacement.write_text("B", encoding="utf-8")
    add_project_files(project, [str(original)])
    metadata = read_json(project, project / "project.json")
    task_id = "TERM-TASK-REPLACE"
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
        project / "terminology" / "candidates.jsonl",
        record_header(
            "terminology_candidates",
            str(metadata["project_id"]),
            active_task_id=task_id,
            terms=[{"source": "A", "category": "name"}],
        ),
    )

    with pytest.raises(UsageError, match="未发布的术语候选"):
        prepare_file_replacement(project, "F0001", replacement)


def test_file_replacement_allows_completed_published_term_candidates(
    tmp_path: Path,
) -> None:
    project = init_empty(tmp_path)
    original = tmp_path / "book.txt"
    replacement = tmp_path / "book-revised.txt"
    original.write_text("A", encoding="utf-8")
    replacement.write_text("B", encoding="utf-8")
    add_project_files(project, [str(original)])
    metadata = read_json(project, project / "project.json")
    task_id = "TERM-TASK-PUBLISHED-REPLACE"
    write_json(
        project,
        project / "terminology" / "active_task.json",
        record_header(
            "terminology_task",
            str(metadata["project_id"]),
            record_id=task_id,
            active_task_id=task_id,
            status="completed",
            terms_revision=1,
        ),
    )
    append_jsonl(
        project,
        project / "terminology" / "candidates.jsonl",
        record_header(
            "terminology_candidates",
            str(metadata["project_id"]),
            active_task_id=task_id,
            terms=[{"source": "A", "category": "name"}],
        ),
    )

    plan = prepare_file_replacement(project, "F0001", replacement)

    assert plan.impact["added_segment_count"] == 1


def test_file_replacement_rejects_invalid_persisted_segment_sequence(
    tmp_path: Path,
) -> None:
    project = init_empty(tmp_path)
    original = tmp_path / "book.txt"
    replacement = tmp_path / "book-revised.txt"
    original.write_text("A", encoding="utf-8")
    replacement.write_text("B", encoding="utf-8")
    add_project_files(project, [str(original)])
    files = read_files(project)
    files[0]["next_segment_sequence"] = 0
    metadata = read_json(project, project / "project.json")
    replace_source(
        project,
        files,
        read_segments(project),
        metadata,
    )

    with pytest.raises(ProjectError, match="next_segment_sequence"):
        prepare_file_replacement(project, "F0001", replacement)


def test_file_replacement_initializes_missing_segment_sequence_from_existing_ids(
    tmp_path: Path,
) -> None:
    project = init_empty(tmp_path)
    original = tmp_path / "book.txt"
    replacement = tmp_path / "book-revised.txt"
    original.write_text("A", encoding="utf-8")
    replacement.write_text("B", encoding="utf-8")
    add_project_files(project, [str(original)])
    files = read_files(project)
    segments = read_segments(project)
    files[0].pop("next_segment_sequence")
    segments[0]["segment_id"] = "F0001-S000009"
    metadata = read_json(project, project / "project.json")
    replace_source(project, files, segments, metadata)

    apply_file_replacement(
        project,
        prepare_file_replacement(project, "F0001", replacement),
    )

    assert read_segments(project)[0]["segment_id"] == "F0001-S000010"
    assert read_files(project)[0]["next_segment_sequence"] == 11


def test_epub_file_replacement_reuses_part_progress_and_new_locators(
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.epub"
    replacement = tmp_path / "book-revised.epub"
    make_epub(
        source,
        xhtml=(
            b'<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            b"<p>A</p><p>ANCHOR</p></body></html>"
        ),
    )
    make_epub(
        replacement,
        xhtml=(
            b'<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            b"<p>A</p><p>EXTRA</p><p>ANCHOR</p></body></html>"
        ),
    )
    project, _ = init_project(
        [str(source)],
        name="book",
        document_adapter_id="epub",
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    old_segments = read_segments(project)
    metadata = read_json(project, project / "project.json")
    for segment in old_segments:
        append_jsonl(
            project,
            stage_result_path(project, "translation"),
            record_header(
                "stage_result",
                str(metadata["project_id"]),
                stage="translation",
                segment_id=str(segment["segment_id"]),
                status="completed",
                text=f"译:{segment['source']}",
                validation_status="passed",
                validation_findings=[],
                stage_fingerprint="sha256:test",
                terms_revision=None,
                run_id="RUN-OLD",
                request_id=f"REQ-{segment['line_index']}",
            ),
        )

    apply_file_replacement(
        project,
        prepare_file_replacement(project, "F0001", replacement),
    )

    segments = read_segments(project)
    assert [item["source"] for item in segments] == ["A", "EXTRA", "ANCHOR"]
    assert [item["segment_id"] for item in segments] == [
        old_segments[0]["segment_id"],
        "F0001-S000003",
        old_segments[1]["segment_id"],
    ]
    file_record = read_files(project)[0]
    state = read_json(project, project / str(file_record["document_adapter_state"]))
    assert len(state["state"]["locators"]) == 3
    exported = export_project(
        project,
        "translated",
        bilingual=False,
        allow_missing=True,
        output_format="original",
    )
    with zipfile.ZipFile(project / exported["written"][0]) as archive:
        chapter = archive.read("OEBPS/text/ch1.xhtml")
    assert b"\xe8\xaf\x91:A" in chapter
    assert b"EXTRA" in chapter


def test_epub_file_replacement_uses_existing_options_and_allows_overrides(
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.epub"
    replacement = tmp_path / "book-revised.epub"
    make_epub(source, xhtml=RUBY_XHTML)
    make_epub(replacement, xhtml=RUBY_XHTML)
    project, _ = init_project(
        [str(source)],
        name="book-options",
        document_adapter_id="epub",
        adapter_options={
            "epub": {
                "ruby_mode": "base_only",
                "inline_format_mode": "markers",
                "inline_format_policy": "strict",
            }
        },
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
    )
    assert project is not None

    preserved = prepare_file_replacement(project, "F0001", replacement)
    assert preserved.impact["previous_adapter_options"] == {
        "ruby_mode": "base_only",
        "inline_format_mode": "markers",
        "inline_format_policy": "strict",
    }
    assert preserved.impact["replacement_adapter_options"] == preserved.impact[
        "previous_adapter_options"
    ]
    assert preserved.impact["changed_adapter_options"] == []

    overridden = prepare_file_replacement(
        project,
        "F0001",
        replacement,
        adapter_options={"epub": {"ruby_mode": "short_xml"}},
    )
    assert overridden.impact["replacement_adapter_options"] == {
        "ruby_mode": "short_xml",
        "inline_format_mode": "markers",
        "inline_format_policy": "strict",
    }
    assert overridden.impact["changed_adapter_options"] == ["ruby_mode"]


def test_file_replacement_restores_source_when_publish_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = init_empty(tmp_path)
    original = tmp_path / "book.txt"
    replacement = tmp_path / "book-revised.txt"
    original.write_text("A", encoding="utf-8")
    replacement.write_text("B", encoding="utf-8")
    add_project_files(project, [str(original)])
    plan = prepare_file_replacement(project, "F0001", replacement)
    stored_path = project / "input" / str(read_files(project)[0]["stored_name"])
    original_replace = os.replace

    def fail_new_publish(source: str | Path, target: str | Path) -> None:
        if str(source).endswith("/new-input"):
            raise OSError("replacement publish failed")
        original_replace(source, target)

    monkeypatch.setattr("app.project.os.replace", fail_new_publish)
    with pytest.raises(OSError, match="replacement publish failed"):
        apply_file_replacement(project, plan)

    assert stored_path.read_text(encoding="utf-8") == "A"
    assert [item["source"] for item in read_segments(project)] == ["A"]


def test_file_replacement_restores_source_when_database_update_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = init_empty(tmp_path)
    original = tmp_path / "book.txt"
    replacement = tmp_path / "book-revised.txt"
    original.write_text("A", encoding="utf-8")
    replacement.write_text("B", encoding="utf-8")
    add_project_files(project, [str(original)])
    plan = prepare_file_replacement(project, "F0001", replacement)
    stored_path = project / "input" / str(read_files(project)[0]["stored_name"])

    def fail_database_update(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise StorageError("database update failed")

    monkeypatch.setattr("app.project.replace_source", fail_database_update)
    with pytest.raises(StorageError, match="database update failed"):
        apply_file_replacement(project, plan)

    assert stored_path.read_text(encoding="utf-8") == "A"
    assert [item["source"] for item in read_segments(project)] == ["A"]


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


def test_optimize_cli_reports_project_storage_sizes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = init_empty(tmp_path)

    assert run(["optimize", str(project)]) == 0

    summary = json.loads(capsys.readouterr().out)
    assert set(summary) == {"before_bytes", "after_bytes", "reclaimed_bytes"}
    assert summary["before_bytes"] >= summary["after_bytes"]
    assert summary["reclaimed_bytes"] == (
        summary["before_bytes"] - summary["after_bytes"]
    )


def rewrite_segment_payload(project: Path, segment: dict[str, object]) -> None:
    with sqlite3.connect(project / "project.sqlite") as connection:
        connection.execute(
            "UPDATE segments SET part_id = ? WHERE segment_id = ?",
            (segment.get("part_id"), segment["segment_id"]),
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


def test_files_replace_cli_parser_accepts_preview_and_confirmation_flags() -> None:
    parser = build_parser()
    preview = parser.parse_args(
        ["files-replace", "sample", "F0001", "revised.txt", "--dry-run"]
    )
    assert preview.command == "files-replace"
    assert preview.file_id == "F0001"
    assert preview.dry_run is True
    assert preview.yes is False

    confirmed = parser.parse_args(
        ["files-replace", "sample", "F0001", "revised.txt", "--yes"]
    )
    assert confirmed.yes is True
    assert confirmed.dry_run is False


def test_files_replace_cli_dry_run_and_yes_apply(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = init_empty(tmp_path)
    original = tmp_path / "original.txt"
    replacement = tmp_path / "replacement.txt"
    original.write_text("one\ntwo", encoding="utf-8")
    replacement.write_text("one\ninserted\ntwo", encoding="utf-8")
    add_project_files(project, [str(original)])

    assert run(["files-replace", str(project), "F0001", str(replacement), "--dry-run"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["preserved_segment_count"] == 2
    assert "preview_token" not in preview
    assert [item["source"] for item in read_segments(project)] == ["one", "two"]

    assert run(["files-replace", str(project), "F0001", str(replacement), "--yes"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["added_segment_count"] == 1
    assert "replaced_file_id" not in result
    assert "file_count" not in result
    assert "segment_count" not in result
    assert "preview_token" not in result
    assert [item["source"] for item in read_segments(project)] == [
        "one",
        "inserted",
        "two",
    ]


def test_files_replace_cli_requires_confirmation_in_non_tty(
    tmp_path: Path,
) -> None:
    project = init_empty(tmp_path)
    original = tmp_path / "original.txt"
    replacement = tmp_path / "replacement.txt"
    original.write_text("one", encoding="utf-8")
    replacement.write_text("two", encoding="utf-8")
    add_project_files(project, [str(original)])

    with pytest.raises(UsageError, match="--dry-run 或 --yes"):
        run(["files-replace", str(project), "F0001", str(replacement)])


def test_file_replacement_rejects_changed_stored_source_after_preview(
    tmp_path: Path,
) -> None:
    project = init_empty(tmp_path)
    original = tmp_path / "original.txt"
    replacement = tmp_path / "replacement.txt"
    original.write_text("one", encoding="utf-8")
    replacement.write_text("two", encoding="utf-8")
    add_project_files(project, [str(original)])
    plan = prepare_file_replacement(project, "F0001", replacement)
    stored = project / "input" / str(read_files(project)[0]["stored_name"])
    stored.write_text("changed outside preview", encoding="utf-8")

    with pytest.raises(UsageError, match="源文件已变化"):
        apply_file_replacement(project, plan)


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


def test_reorder_project_files_preserves_ids_history_and_adapter_state(
    tmp_path: Path,
) -> None:
    project = init_empty(tmp_path)
    chapter10 = tmp_path / "chapter10.txt"
    book2 = tmp_path / "book2.epub"
    chapter1 = tmp_path / "chapter1.txt"
    chapter10.write_text("ten", encoding="utf-8")
    make_epub(book2)
    chapter1.write_text("one", encoding="utf-8")
    add_project_files(project, [str(chapter10), str(book2), str(chapter1)])

    metadata_before = read_json(project, project / "project.json")
    files_before = {str(item["file_id"]): item for item in read_files(project)}
    segments_before = read_segments(project)
    segment_ids_before = [str(item["segment_id"]) for item in segments_before]
    epub_state_path = project / str(files_before["F0002"]["document_adapter_state"])
    epub_state_before = read_json(project, epub_state_path)
    history_path = stage_result_path(project, "translation")
    append_jsonl(
        project,
        history_path,
        record_header(
            "stage_result",
            str(metadata_before["project_id"]),
            stage="translation",
            segment_id=segment_ids_before[0],
            status="completed",
            text="十",
            validation_status="passed",
            validation_findings=[],
            stage_fingerprint="sha256:test",
            terms_revision=None,
            run_id="RUN-OLD",
            request_id="REQ-OLD",
        ),
    )
    history_before = read_jsonl(project, history_path)

    result = reorder_project_files(project, ["F0003", "F0001", "F0002"])

    assert result == {
        "reordered_file_ids": ["F0003", "F0001", "F0002"],
        "file_count": 3,
    }
    files_after = read_files(project)
    assert [item["file_id"] for item in files_after] == ["F0003", "F0001", "F0002"]
    assert [item["file_order"] for item in files_after] == [1, 2, 3]
    for item in files_after:
        previous = dict(files_before[str(item["file_id"])])
        previous["file_order"] = item["file_order"]
        assert item == previous
        assert (project / "input" / str(item["stored_name"])).is_file()
    segments_after = read_segments(project)
    assert list(dict.fromkeys(item["file_id"] for item in segments_after)) == [
        "F0003",
        "F0001",
        "F0002",
    ]
    assert {item["segment_id"] for item in segments_after} == set(segment_ids_before)
    selected = select_scope(
        segments_after,
        files_after,
        Scope(from_file="F0001"),
    )
    assert list(dict.fromkeys(item["file_id"] for item in selected)) == [
        "F0001",
        "F0002",
    ]
    assert read_json(project, epub_state_path) == epub_state_before
    assert read_jsonl(project, history_path) == history_before
    assert read_json(project, project / "project.json") == metadata_before

    exported = export_project(
        project,
        "translated",
        bilingual=False,
        allow_missing=True,
        output_format="txt",
    )
    assert [Path(path).name for path in exported["written"]] == [
        "chapter1.txt",
        "chapter10.txt",
        "book2.txt",
    ]


def test_reorder_project_files_rejects_invalid_order_and_running_run(
    tmp_path: Path,
) -> None:
    project = init_empty(tmp_path)
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")
    add_project_files(project, [str(first), str(second)])
    files_before = read_files(project)
    segments_before = read_segments(project)

    for file_ids in (
        ["F0001"],
        ["F0001", "F0001"],
        ["F0001", "F9999"],
    ):
        with pytest.raises(UsageError):
            reorder_project_files(project, file_ids)
        assert read_files(project) == files_before
        assert read_segments(project) == segments_before

    metadata = read_json(project, project / "project.json")
    write_json(
        project,
        project / "runs" / "RUN-ACTIVE" / "manifest.json",
        record_header(
            "run",
            str(metadata["project_id"]),
            record_id="RUN-ACTIVE",
            run_id="RUN-ACTIVE",
            stage="translation",
            status="running",
            started_at="2026-08-12T00:00:00Z",
        ),
    )
    with pytest.raises(UsageError, match="未完成 Run"):
        reorder_project_files(project, ["F0002", "F0001"])
    assert read_files(project) == files_before
    assert read_segments(project) == segments_before


def test_old_project_without_part_id_requires_rebuild(tmp_path: Path) -> None:
    project = init_empty(tmp_path)
    source = tmp_path / "old.txt"
    source.write_text("one", encoding="utf-8")
    add_project_files(project, [str(source)])
    segments = read_segments(project)
    segments[0]["part_id"] = ""
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
