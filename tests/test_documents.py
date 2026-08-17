from __future__ import annotations

import json
import re
import sqlite3
import stat
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest

from app.config import dump_config, load_config
from app.errors import ConfigError, IncompleteError, ProjectError, UsageError
from app.documents import DocumentChoiceOption, DocumentExportJob, ImportedFile
from app.documents import publish_document_exports
from app.execution import stage_result_path
from app.plugins import (
    PLUGIN_PROTOCOL_VERSION,
    PluginDescriptor,
    get_document_adapter_for_extension,
    get_document_adapter,
    load_plugins,
    normalize_model_text,
    validate_document_import_options,
)
from app.project import _normalize_imported_file, init_project
from app.stages import _project_context, export_project
from app.sqlite_storage import (
    append_jsonl,
    read_files,
    read_json,
    read_segments,
    record_header,
    write_json,
)
from tests.test_foundation import make_app_root


def make_epub(
    path: Path,
    *,
    xhtml: bytes | None = None,
    xhtmls: tuple[bytes, ...] | None = None,
    opf_version: str = "3.0",
    opf_languages: tuple[str, ...] = (),
    opf_titles: tuple[str, ...] = ("Demo",),
    opf_identifier: str | None = "source-id",
    opf_modified: tuple[str, ...] = (),
) -> None:
    default_chapter = xhtml or (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Book</title>'
        b'<link rel="stylesheet" href="../style.css"/></head><body>'
        b'<h1>Chapter <em>One</em></h1><p>Hello world.</p>'
        b'<img src="../cover.png" alt="cover"/></body></html>'
    )
    chapters = xhtmls or (default_chapter,)
    container = (
        b'<?xml version="1.0"?>'
        b'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        b'<rootfiles><rootfile full-path="OEBPS/content.opf" '
        b'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    manifest = b"".join(
        f'<item id="c{index}" href="text/ch{index}.xhtml" '
        'media-type="application/xhtml+xml"/>'.encode()
        for index in range(1, len(chapters) + 1)
    )
    spine = b"".join(
        f'<itemref idref="c{index}"/>'.encode()
        for index in range(1, len(chapters) + 1)
    )
    titles = b"".join(
        f"<dc:title>{value}</dc:title>".encode()
        for value in opf_titles
    )
    languages = b"".join(
        f'<dc:language>{value}</dc:language>'.encode()
        for value in opf_languages
    )
    identifier = (
        f'<dc:identifier id="source-id">{opf_identifier}</dc:identifier>'.encode()
        if opf_identifier is not None
        else b""
    )
    if opf_version == "2.0":
        modified = b"".join(
            f'<dc:date opf:event="modification">{value}</dc:date>'.encode()
            for value in opf_modified
        )
    else:
        modified = b"".join(
            f'<meta property="dcterms:modified">{value}</meta>'.encode()
            for value in opf_modified
        )
    opf = (
        b'<?xml version="1.0" encoding="utf-8"?>'
        + f'<package xmlns="http://www.idpf.org/2007/opf" version="{opf_version}"'.encode()
        + (b' unique-identifier="source-id">' if opf_identifier is not None else b">")
        + b'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">'
        + identifier
        + titles
        + languages
        + modified
        + b"</metadata>"
        + b'<manifest>' + manifest
        + b'<item id="css" href="style.css" media-type="text/css"/>'
        + b'<item id="cover" href="cover.png" media-type="image/png"/></manifest>'
        + b'<spine>' + spine + b'</spine></package>'
    )
    with zipfile.ZipFile(
        path, "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        archive.writestr(
            "mimetype",
            b"application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
        for index, chapter in enumerate(chapters, start=1):
            archive.writestr(f"OEBPS/text/ch{index}.xhtml", chapter)
        archive.writestr("OEBPS/style.css", b"body { color: #222; }")
        archive.writestr("OEBPS/cover.png", b"\x89PNG\r\nfixture")


def init_epub(tmp_path: Path) -> Path:
    source = tmp_path / "book.epub"
    make_epub(source)
    project, _ = init_project(
        [str(source)],
        name="book",
        document_adapter_id="epub",
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    return project


def add_translations(project: Path) -> list[dict[str, object]]:
    metadata = read_json(project, project / "project.json")
    segments = read_segments(project)
    for segment in segments:
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
                stage_fingerprint="sha256:test",
                terms_revision=0,
                run_id="RUN-TEST",
                request_id="REQ-TEST",
            ),
        )
    return segments


def test_epub_round_trip_preserves_resources_and_exports_both_modes(
    tmp_path: Path,
) -> None:
    project = init_epub(tmp_path)
    file_record = read_files(project)[0]
    assert file_record["document_adapter_id"] == "epub"
    assert file_record["document_adapter_state"]
    segments = add_translations(project)
    assert [item["source"] for item in segments] == [
        "Chapter One",
        "Hello world.",
    ]

    translated = export_project(
        project, "translated", bilingual=False, allow_missing=False
    )
    bilingual = export_project(
        project, "translated", bilingual=True, allow_missing=False
    )
    assert translated["files"] == bilingual["files"] == 1
    translated_path = project / translated["written"][0]
    bilingual_path = project / bilingual["written"][0]
    with zipfile.ZipFile(translated_path) as archive:
        assert archive.read("OEBPS/style.css") == b"body { color: #222; }"
        assert archive.read("OEBPS/cover.png") == b"\x89PNG\r\nfixture"
        assert b"\xe8\xaf\x91\xef\xbc\x9aHello world." in archive.read(
            "OEBPS/text/ch1.xhtml"
        )
    with zipfile.ZipFile(bilingual_path) as archive:
        chapter = archive.read("OEBPS/text/ch1.xhtml")
        assert b"Hello world.\n" in chapter
        assert b"white-space: pre-line" in chapter


def test_epub_export_rewrites_language_metadata_and_xhtml_attributes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "language.epub"
    make_epub(
        source,
        opf_languages=("ja", "JA", "en"),
        xhtml=(
            b'<html xmlns="http://www.w3.org/1999/xhtml" lang="ja" '
            b'xml:lang="ja"><body><p>Hello world.</p></body></html>'
        ),
    )
    project, _ = init_project(
        [str(source)],
        name="language",
        document_adapter_id="epub",
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    config_path = project / "config.toml"
    config = load_config(config_path)
    config["project"]["target_language_tag"] = "zh-Hant"
    config_path.write_text(dump_config(config), encoding="utf-8")
    add_translations(project)

    translated = export_project(
        project, "translated", bilingual=False, allow_missing=False
    )
    bilingual = export_project(
        project, "translated", bilingual=True, allow_missing=False
    )

    with zipfile.ZipFile(project / translated["written"][0]) as archive:
        opf = ElementTree.fromstring(archive.read("OEBPS/content.opf"))
        languages = [
            element.text
            for element in opf.iter()
            if element.tag.rsplit("}", 1)[-1] == "language"
        ]
        assert languages == ["zh-Hant"]
        titles = [
            element.text
            for element in opf.iter()
            if element.tag.rsplit("}", 1)[-1] == "title"
        ]
        assert titles == ["Demo（简体中文）"]
        identifier_id = opf.get("unique-identifier")
        assert identifier_id and identifier_id != "source-id"
        identifiers = {
            element.get("id"): element.text
            for element in opf.iter()
            if element.tag.rsplit("}", 1)[-1] == "identifier"
        }
        assert identifiers["source-id"] == "source-id"
        assert identifiers[identifier_id].startswith("urn:uuid:")
        modified = [
            element.text
            for element in opf.iter()
            if element.tag.rsplit("}", 1)[-1] == "meta"
            and element.get("property") == "dcterms:modified"
        ]
        assert len(modified) == 1
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", modified[0] or ""
        )
        root = ElementTree.fromstring(
            archive.read("OEBPS/text/ch1.xhtml")
        )
        assert root.get("lang") == "zh-Hant"
        assert root.get("{http://www.w3.org/XML/1998/namespace}lang") == "zh-Hant"

    with zipfile.ZipFile(project / bilingual["written"][0]) as archive:
        opf = ElementTree.fromstring(archive.read("OEBPS/content.opf"))
        languages = [
            element.text
            for element in opf.iter()
            if element.tag.rsplit("}", 1)[-1] == "language"
        ]
        assert languages == ["zh-Hant", "ja", "en"]
        titles = [
            element.text
            for element in opf.iter()
            if element.tag.rsplit("}", 1)[-1] == "title"
        ]
        assert titles == ["Demo（简体中文·双语）"]
        bilingual_identifier_id = opf.get("unique-identifier")
        assert bilingual_identifier_id
        bilingual_identifiers = {
            element.get("id"): element.text
            for element in opf.iter()
            if element.tag.rsplit("}", 1)[-1] == "identifier"
        }
        assert bilingual_identifiers[bilingual_identifier_id] != identifiers[identifier_id]
        root = ElementTree.fromstring(
            archive.read("OEBPS/text/ch1.xhtml")
        )
        assert root.get("lang") == "zh-Hant"


def test_epub_publication_identity_is_stable_and_language_changes_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = init_epub(tmp_path)
    add_translations(project)
    timestamps = iter(
        ("2026-08-10T01:02:03Z", "2026-08-10T01:02:04Z", "2026-08-10T01:02:05Z")
    )
    monkeypatch.setattr(
        "app.epub_adapter._utc_modified_timestamp", lambda: next(timestamps)
    )

    first = export_project(
        project, "translated", bilingual=False, allow_missing=False
    )
    first_path = project / first["written"][0]
    with zipfile.ZipFile(first_path) as archive:
        first_opf = ElementTree.fromstring(archive.read("OEBPS/content.opf"))
    first_id = first_opf.get("unique-identifier")
    first_identifier = next(
        element.text
        for element in first_opf.iter()
        if element.tag.rsplit("}", 1)[-1] == "identifier"
        and element.get("id") == first_id
    )
    first_modified = next(
        element.text
        for element in first_opf.iter()
        if element.tag.rsplit("}", 1)[-1] == "meta"
        and element.get("property") == "dcterms:modified"
    )

    second = export_project(
        project, "translated", bilingual=False, allow_missing=False
    )
    with zipfile.ZipFile(project / second["written"][0]) as archive:
        second_opf = ElementTree.fromstring(archive.read("OEBPS/content.opf"))
    assert second_opf.get("unique-identifier") == first_id
    assert next(
        element.text
        for element in second_opf.iter()
        if element.tag.rsplit("}", 1)[-1] == "identifier"
        and element.get("id") == first_id
    ) == first_identifier
    assert (
        next(
            element.text
            for element in second_opf.iter()
            if element.tag.rsplit("}", 1)[-1] == "meta"
            and element.get("property") == "dcterms:modified"
        )
        != first_modified
    )

    config_path = project / "config.toml"
    config = load_config(config_path)
    config["project"]["target_language_tag"] = "ja"
    config_path.write_text(dump_config(config), encoding="utf-8")
    third = export_project(
        project, "translated", bilingual=False, allow_missing=False
    )
    with zipfile.ZipFile(project / third["written"][0]) as archive:
        third_opf = ElementTree.fromstring(archive.read("OEBPS/content.opf"))
    third_id = third_opf.get("unique-identifier")
    assert third_id == first_id
    third_identifier = next(
        element.text
        for element in third_opf.iter()
        if element.tag.rsplit("}", 1)[-1] == "identifier"
        and element.get("id") == third_id
    )
    assert third_identifier != first_identifier


def test_epub2_export_updates_modification_date_and_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "legacy.epub"
    make_epub(
        source,
        opf_version="2.0",
        opf_modified=("2000-01-01", "2001-01-01"),
    )
    project, _ = init_project(
        [str(source)],
        name="legacy",
        document_adapter_id="epub",
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    add_translations(project)
    monkeypatch.setattr(
        "app.epub_adapter._utc_modified_timestamp",
        lambda: "2026-08-10T01:02:03Z",
    )
    result = export_project(
        project, "translated", bilingual=False, allow_missing=False
    )
    with zipfile.ZipFile(project / result["written"][0]) as archive:
        opf = ElementTree.fromstring(archive.read("OEBPS/content.opf"))
    dates = [
        element.text
        for element in opf.iter()
        if element.tag.rsplit("}", 1)[-1] == "date"
        and element.get("{http://www.idpf.org/2007/opf}event") == "modification"
    ]
    assert dates == ["2026-08-10T01:02:03Z"]
    assert [
        element.text
        for element in opf.iter()
        if element.tag.rsplit("}", 1)[-1] == "title"
    ] == ["Demo（简体中文）"]


def test_epub_export_titles_escape_display_language_and_preserve_secondary_title(
    tmp_path: Path,
) -> None:
    source = tmp_path / "titles.epub"
    make_epub(source, opf_titles=("Demo", "副标题"))
    project, _ = init_project(
        [str(source)],
        name="titles",
        document_adapter_id="epub",
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    config_path = project / "config.toml"
    config = load_config(config_path)
    config["project"]["target_language"] = "中 <文>&"
    config_path.write_text(dump_config(config), encoding="utf-8")
    add_translations(project)
    result = export_project(
        project, "translated", bilingual=False, allow_missing=False
    )
    with zipfile.ZipFile(project / result["written"][0]) as archive:
        opf = ElementTree.fromstring(archive.read("OEBPS/content.opf"))
    assert [
        element.text
        for element in opf.iter()
        if element.tag.rsplit("}", 1)[-1] == "title"
    ] == ["Demo（中 <文>&）", "副标题"]


def test_epub_export_rejects_missing_primary_title_without_output(
    tmp_path: Path,
) -> None:
    source = tmp_path / "untitled.epub"
    make_epub(source, opf_titles=("",))
    project, _ = init_project(
        [str(source)],
        name="untitled",
        document_adapter_id="epub",
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    add_translations(project)
    with pytest.raises(IncompleteError, match="主标题"):
        export_project(project, "translated", bilingual=False, allow_missing=False)
    assert not list((project / "output").rglob("*.epub"))


def test_epub_export_rejects_empty_target_language_without_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = init_epub(tmp_path)
    add_translations(project)
    config_path = project / "config.toml"
    config = load_config(config_path)
    config["project"]["target_language"] = ""
    monkeypatch.setattr(
        "app.stages.load_project_config",
        lambda *_args, **_kwargs: config,
    )

    with pytest.raises(IncompleteError, match="target_language"):
        export_project(project, "translated", bilingual=False, allow_missing=False)
    assert not list((project / "output").rglob("*.epub"))


def test_epub_export_requires_language_tag_without_partial_output(
    tmp_path: Path,
) -> None:
    project = init_epub(tmp_path)
    config_path = project / "config.toml"
    config = load_config(config_path)
    config["project"]["target_language_tag"] = ""
    config_path.write_text(dump_config(config), encoding="utf-8")
    add_translations(project)

    with pytest.raises(IncompleteError, match="target_language_tag"):
        export_project(project, "translated", bilingual=False, allow_missing=False)
    assert not list((project / "output").rglob("*.epub"))


def test_epub_parts_follow_xhtml_files_without_splitting_the_file(
    tmp_path: Path,
) -> None:
    source = tmp_path / "chapters.epub"
    chapters = tuple(
        (
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            f"<p>{value} one</p><p>{value} two</p>"
            "</body></html>"
        ).encode()
        for value in ("第一章", "第二章")
    )
    make_epub(source, xhtmls=chapters)

    project, _ = init_project(
        [str(source)],
        name="chapters",
        document_adapter_id="epub",
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    files = read_files(project)
    segments = read_segments(project)

    assert len(files) == 1
    assert [item["source"] for item in segments] == [
        "第一章 one",
        "第一章 two",
        "第二章 one",
        "第二章 two",
    ]
    assert [item["part_id"] for item in segments] == [
        "OEBPS/text/ch1.xhtml",
        "OEBPS/text/ch1.xhtml",
        "OEBPS/text/ch2.xhtml",
        "OEBPS/text/ch2.xhtml",
    ]
    assert files[0]["document_adapter_version"] == "0.3"


@pytest.mark.parametrize("parts", [(), ("only-one",), ("", "two")])
def test_imported_file_rejects_invalid_segment_parts(parts: tuple[str, ...]) -> None:
    item = ImportedFile(
        source_path=Path("book.txt"),
        original_name="book.txt",
        segments=("one", "two"),
        encoding_detected="utf-8",
        encoding_used="utf-8",
        encoding_confidence=1.0,
        segment_part_ids=parts,
    )

    with pytest.raises(UsageError, match="segment_part_ids"):
        _normalize_imported_file(item)


def test_epub_inline_text_forms_one_segment_and_preserves_tag_skeleton(
    tmp_path: Path,
) -> None:
    source = tmp_path / "inline.epub"
    make_epub(
        source,
        xhtml=(
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            '<p>「こぉら！　何を嗅ぎまわっておるか'
            '<span class="tcy">!!</span>」</p>'
            '<p>A<span class="outer"><em> B</em></span>'
            '<strong> C</strong> D</p>'
            '</body></html>'
        ).encode(),
    )
    project, _ = init_project(
        [str(source)],
        name="inline",
        document_adapter_id="epub",
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
    )
    assert project is not None

    segments = add_translations(project)
    assert [item["source"] for item in segments] == [
        "「こぉら！　何を嗅ぎまわっておるか!!」",
        "A B C D",
    ]
    translated = export_project(
        project, "translated", bilingual=False, allow_missing=False
    )
    bilingual = export_project(
        project, "translated", bilingual=True, allow_missing=False
    )

    with zipfile.ZipFile(project / translated["written"][0]) as archive:
        root = ElementTree.fromstring(archive.read("OEBPS/text/ch1.xhtml"))
        paragraphs = [
            element
            for element in root.iter()
            if element.tag.rsplit("}", 1)[-1] == "p"
        ]
        assert paragraphs[0].text == "译：「こぉら！　何を嗅ぎまわっておるか!!」"
        assert paragraphs[0][0].get("class") == "tcy"
        assert paragraphs[0][0].text is None
        assert paragraphs[0][0].tail is None
        assert paragraphs[1].text == "译：A B C D"
        assert paragraphs[1][0].get("class") == "outer"
        assert paragraphs[1][0][0].text is None
        assert paragraphs[1][0][0].tail is None

    with zipfile.ZipFile(project / bilingual["written"][0]) as archive:
        root = ElementTree.fromstring(archive.read("OEBPS/text/ch1.xhtml"))
        paragraphs = [
            element
            for element in root.iter()
            if element.tag.rsplit("}", 1)[-1] == "p"
        ]
        assert paragraphs[0].text == "「こぉら！　何を嗅ぎまわっておるか"
        assert paragraphs[0][0].text == "!!"
        assert paragraphs[0][0].tail == (
            "」\n译：「こぉら！　何を嗅ぎまわっておるか!!」"
        )


def test_epub_markers_persist_model_source_and_strip_valid_output(
    tmp_path: Path,
) -> None:
    source = tmp_path / "markers.epub"
    make_epub(
        source,
        xhtml=(
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            '<p>A <em class="em"><strong>B</strong></em> C</p>'
            '</body></html>'
        ).encode(),
    )
    project, _ = init_project(
        [str(source)],
        name="markers",
        document_adapter_id="epub",
        adapter_options={
            "epub": {
                "inline_format_mode": "markers",
                "inline_format_policy": "strict",
            }
        },
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    segments = read_segments(project)
    assert segments[0]["source"] == "A B C"
    assert segments[0]["model_source"] == (
        "A <em1><strong2>B</strong2></em1> C"
    )
    loaded = __import__("app.project", fromlist=["load_segments"]).load_segments(project)
    assert loaded[0]["_format_markers"]
    context_config, _, _, _ = _project_context(project, stage="translation")
    assert "<em1>" in context_config[
        "_document_adapter_prompt_requirements"
    ]["F0001"]["en"]
    adapter = get_document_adapter("epub")
    assert adapter.normalize_model_output(
        segment=loaded[0],
        text="A <em1><strong2>B</strong2></em1> C",
        stage="translation",
    ) == "A B C"


def test_epub_model_prompt_requirements_follow_format_state() -> None:
    adapter = get_document_adapter("epub")
    assert adapter.model_prompt_requirements(
        stage="translation",
        language="zh-CN",
        opaque_state={"inline_format_mode": "plain"},
    ) is None
    strict = adapter.model_prompt_requirements(
        stage="translation",
        language="zh-CN",
        opaque_state={
            "inline_format_mode": "markers",
            "inline_format_policy": "strict",
        },
    )
    assert strict is not None
    assert "必须保留所有已有标记" in strict
    tiered = adapter.model_prompt_requirements(
        stage="proofreading",
        language="en",
        opaque_state={
            "inline_format_mode": "markers",
            "inline_format_policy": "tiered",
        },
    )
    assert tiered is not None
    assert "presentation markers may be omitted" in tiered
    assert adapter.model_prompt_requirements(
        stage="terminology",
        language="en",
        opaque_state={
            "inline_format_mode": "markers",
            "inline_format_policy": "strict",
        },
    ) is None


def test_epub_markers_reject_unknown_or_broken_output(tmp_path: Path) -> None:
    source = tmp_path / "marker-errors.epub"
    make_epub(
        source,
        xhtml=(
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            '<p><strong>Text</strong></p></body></html>'
        ).encode(),
    )
    project, _ = init_project(
        [str(source)],
        name="marker-errors",
        document_adapter_id="epub",
        adapter_options={
            "epub": {
                "inline_format_mode": "markers",
                "inline_format_policy": "strict",
            }
        },
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    segment = __import__("app.project", fromlist=["load_segments"]).load_segments(project)[0]
    adapter = get_document_adapter("epub")
    for value in ("<strong1>Text", "<unknown1>Text</unknown1>", "<strong1>Text</em1>"):
        with pytest.raises(IncompleteError):
            adapter.normalize_model_output(
                segment=segment, text=value, stage="translation"
            )


def test_epub_inline_runs_respect_blocks_and_line_breaks(tmp_path: Path) -> None:
    source = tmp_path / "boundaries.epub"
    make_epub(
        source,
        xhtml=(
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            '<p>A<span>B</span></p><p>C<br/>D</p>'
            '</body></html>'
        ).encode(),
    )

    imported = get_document_adapter("epub").import_sources(
        [str(source)], recursive=False, config={}, options={"ruby_mode": "aozora"}
    )

    assert list(imported.files[0].segments) == ["AB", "C", "D"]


def test_epub_ruby_stays_in_one_inline_text_run(tmp_path: Path) -> None:
    source = tmp_path / "ruby-inline.epub"
    make_epub(
        source,
        xhtml=(
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            '<p>前<span class="keep">中<ruby>漢<rt>かん</rt></ruby>後</span>'
            '<em>間</em><ruby>字<rt>じ</rt></ruby> 末</p>'
            '<p>外<custom>未知</custom>后</p><p>行<br/>后</p>'
            '</body></html>'
        ).encode(),
    )

    imported = get_document_adapter("epub").import_sources(
        [str(source)], recursive=False, config={}, options={"ruby_mode": "aozora"}
    )

    assert list(imported.files[0].segments) == [
        "前中｜漢《かん》後間｜字《じ》 末",
        "外",
        "未知",
        "后",
        "行",
        "后",
    ]
    first_slot = imported.files[0].opaque_state["locators"][0]["slot"]
    assert first_slot["kind"] == "composite"
    assert [slot["kind"] for slot in first_slot["slots"]] == [
        "text",
        "text",
        "ruby",
        "text",
        "ruby",
    ]


def test_epub_mixed_ruby_export_removes_all_ruby_and_preserves_inline_attrs(
    tmp_path: Path,
) -> None:
    source = tmp_path / "ruby-mixed.epub"
    make_epub(
        source,
        xhtml=(
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            '<p class="line">前<span class="keep">中</span>'
            '<ruby>漢<rt>かん</rt></ruby>後<strong data-mark="1">尾</strong>'
            '<ruby>字<rt>じ</rt></ruby>終</p>'
            '</body></html>'
        ).encode(),
    )
    project, _ = init_project(
        [str(source)],
        name="ruby-mixed",
        document_adapter_id="epub",
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    segments = read_segments(project)
    assert [item["source"] for item in segments] == [
        "前中｜漢《かん》後尾｜字《じ》終"
    ]
    metadata = read_json(project, project / "project.json")
    append_jsonl(
        project,
        stage_result_path(project, "translation"),
        record_header(
            "stage_result",
            str(metadata["project_id"]),
            stage="translation",
            segment_id=segments[0]["segment_id"],
            status="completed",
            text="整句译文",
            validation_status="passed",
            validation_findings=[],
            stage_fingerprint="sha256:test",
            terms_revision=0,
            run_id="RUN-RUBY-MIXED",
            request_id="REQ-RUBY-MIXED",
        ),
    )

    translated = export_project(
        project, "translated", bilingual=False, allow_missing=False
    )
    bilingual = export_project(
        project, "translated", bilingual=True, allow_missing=False
    )

    with zipfile.ZipFile(project / translated["written"][0]) as archive:
        root = ElementTree.fromstring(archive.read("OEBPS/text/ch1.xhtml"))
        paragraph = next(
            element
            for element in root.iter()
            if element.tag.rsplit("}", 1)[-1] == "p"
        )
        assert paragraph.text == "整句译文"
        assert [child.tag.rsplit("}", 1)[-1] for child in paragraph] == [
            "span",
            "strong",
        ]
        assert paragraph[0].get("class") == "keep"
        assert paragraph[1].get("data-mark") == "1"
        assert paragraph[0].text is None
        assert paragraph[1].text is None
        assert "ruby" not in {
            element.tag.rsplit("}", 1)[-1] for element in root.iter()
        }
        assert "整句译文" == "".join(root.itertext())

    with zipfile.ZipFile(project / bilingual["written"][0]) as archive:
        root = ElementTree.fromstring(archive.read("OEBPS/text/ch1.xhtml"))
        paragraph = next(
            element
            for element in root.iter()
            if element.tag.rsplit("}", 1)[-1] == "p"
        )
        assert paragraph.text == "前"
        assert paragraph[0].text == "中"
        assert paragraph[1].tag.rsplit("}", 1)[-1] == "ruby"
        assert paragraph[1].tail == "後"
        assert paragraph[-1].tail == "終\n整句译文"


@pytest.mark.parametrize("field", ["path", "tail"])
def test_epub_mixed_ruby_locator_corruption_fails_explicitly(
    tmp_path: Path, field: str
) -> None:
    source = tmp_path / "ruby-corrupt.epub"
    make_epub(
        source,
        xhtml=(
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            '<p>前<ruby>漢<rt>かん</rt></ruby>後</p>'
            '</body></html>'
        ).encode(),
    )
    project, _ = init_project(
        [str(source)],
        name=f"ruby-corrupt-{field}",
        document_adapter_id="epub",
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    add_translations(project)
    file_record = read_files(project)[0]
    state_path = project / str(file_record["document_adapter_state"])
    state = read_json(project, state_path)
    ruby_slot = state["state"]["locators"][0]["slot"]["slots"][1]
    ruby_slot[field] = [99] if field == "path" else "不一致"
    write_json(project, state_path, state)

    with pytest.raises(IncompleteError, match="结构|tail|原文不一致"):
        export_project(project, "translated", bilingual=False, allow_missing=False)


def test_epub_composite_locator_corruption_fails_explicitly(
    tmp_path: Path,
) -> None:
    source = tmp_path / "corrupt-composite.epub"
    make_epub(
        source,
        xhtml=(
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            '<p>A<span>B</span>C</p></body></html>'
        ).encode(),
    )
    project, _ = init_project(
        [str(source)],
        name="corrupt-composite",
        document_adapter_id="epub",
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    add_translations(project)
    file_record = read_files(project)[0]
    state_path = project / str(file_record["document_adapter_state"])
    state = read_json(project, state_path)
    state["state"]["locators"][0]["slot"]["slots"][0]["path"] = [99]
    write_json(project, state_path, state)

    with pytest.raises(IncompleteError, match="结构"):
        export_project(project, "translated", bilingual=False, allow_missing=False)


RUBY_XHTML = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Ruby</title>'
    b'</head><body><p>'
    b'\xe5\xbd\xbc\xe3\x81\xaf<ruby>\xe6\xbc\xa2<rt>\xe3\x81\x8b\xe3\x82\x93</rt>'
    b'\xe5\xad\x97<rt>\xe3\x81\x98</rt></ruby>\xe3\x82\x92\xe8\xaa\xad\xe3\x82\x80\xe3\x80\x82'
    b'</p><p><ruby><rb><em>\xe7\x89\xb9\xe5\x88\xa5</em></rb><rp>\xef\xbc\x88</rp>'
    b'<rt>\xe3\x82\xb9\xe3\x83\x9a\xe3\x82\xb7\xe3\x83\xa3\xe3\x83\xab</rt><rp>\xef\xbc\x89</rp>'
    b'<rtc><rt>\xe3\x81\xa8\xe3\x81\x8f\xe3\x81\xb9\xe3\x81\xa4</rt></rtc></ruby>'
    b'\xe3\x81\xa0\xe3\x80\x82</p></body></html>'
)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (
            "aozora",
            ["彼は｜漢字《かんじ》を読む。", "｜特別《スペシャル／とくべつ》だ。"],
        ),
        ("base_only", ["彼は漢字を読む。", "特別だ。"]),
        ("parenthetical", ["彼は漢字（かんじ）を読む。", "特別（スペシャル／とくべつ）だ。"]),
    ],
)
def test_epub_ruby_modes_form_semantic_segments(
    tmp_path: Path, mode: str, expected: list[str]
) -> None:
    source = tmp_path / f"ruby-{mode}.epub"
    make_epub(source, xhtml=RUBY_XHTML)

    imported = get_document_adapter("epub").import_sources(
        [str(source)], recursive=False, config={}, options={"ruby_mode": mode}
    )

    assert list(imported.files[0].segments) == expected
    assert imported.files[0].opaque_state is not None
    assert imported.files[0].opaque_state["ruby_mode"] == mode


def test_epub_ruby_export_removes_stale_readings_but_bilingual_keeps_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "ruby.epub"
    make_epub(source, xhtml=RUBY_XHTML)
    project, _ = init_project(
        [str(source)],
        name="ruby",
        document_adapter_id="epub",
        adapter_options={"epub": {"ruby_mode": "aozora"}},
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    metadata = read_json(project, project / "project.json")
    segments = read_segments(project)
    targets = ["他は汉字を読む。", "特别だ。"]
    for segment, target in zip(segments, targets, strict=True):
        append_jsonl(
            project,
            stage_result_path(project, "translation"),
            record_header(
                "stage_result",
                str(metadata["project_id"]),
                stage="translation",
                segment_id=segment["segment_id"],
                status="completed",
                text=target,
                validation_status="passed",
                validation_findings=[],
                stage_fingerprint="sha256:test",
                terms_revision=0,
                run_id="RUN-RUBY",
                request_id="REQ-RUBY",
            ),
        )

    translated = export_project(
        project, "translated", bilingual=False, allow_missing=False
    )
    bilingual = export_project(
        project, "translated", bilingual=True, allow_missing=False
    )

    with zipfile.ZipFile(project / translated["written"][0]) as archive:
        root = ElementTree.fromstring(archive.read("OEBPS/text/ch1.xhtml"))
        names = [element.tag.rsplit("}", 1)[-1] for element in root.iter()]
        assert "ruby" not in names
        assert "rt" not in names
        assert "".join(root.itertext()) == "Ruby他は汉字を読む。特别だ。"
    with zipfile.ZipFile(project / bilingual["written"][0]) as archive:
        root = ElementTree.fromstring(archive.read("OEBPS/text/ch1.xhtml"))
        names = [element.tag.rsplit("}", 1)[-1] for element in root.iter()]
        assert names.count("ruby") == 2
        assert names.count("rt") == 4
        text = "".join(root.itertext())
        assert "彼は漢かん字じを読む。\n他は汉字を読む。" in text
        assert "だ。\n特别だ。" in text


def test_epub_aozora_output_restores_only_explicit_ruby(tmp_path: Path) -> None:
    source = tmp_path / "ruby-aozora-output.epub"
    make_epub(source, xhtml=RUBY_XHTML)
    project, _ = init_project(
        [str(source)],
        name="ruby-aozora-output",
        document_adapter_id="epub",
        adapter_options={"epub": {"ruby_mode": "aozora"}},
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    metadata = read_json(project, project / "project.json")
    segments = read_segments(project)
    targets = [
        "他｜漢字《hànzì》と｜特別《tèbié》。",
        "特別だ。",
    ]
    for segment, target in zip(segments, targets, strict=True):
        append_jsonl(
            project,
            stage_result_path(project, "translation"),
            record_header(
                "stage_result",
                str(metadata["project_id"]),
                stage="translation",
                segment_id=segment["segment_id"],
                status="completed",
                text=target,
                validation_status="passed",
                validation_findings=[],
                stage_fingerprint="sha256:test",
                terms_revision=0,
                run_id="RUN-RUBY-AOZORA",
                request_id="REQ-RUBY-AOZORA",
            ),
        )

    translated = export_project(
        project, "translated", bilingual=False, allow_missing=False
    )
    bilingual = export_project(
        project, "translated", bilingual=True, allow_missing=False
    )

    with zipfile.ZipFile(project / translated["written"][0]) as archive:
        root = ElementTree.fromstring(archive.read("OEBPS/text/ch1.xhtml"))
        rubies = [
            element
            for element in root.iter()
            if element.tag.rsplit("}", 1)[-1] == "ruby"
        ]
        assert len(rubies) == 2
        assert ["".join(ruby.itertext()) for ruby in rubies] == [
            "漢字hànzì",
            "特別tèbié",
        ]
        assert "｜" not in "".join(root.itertext())
        assert "《" not in "".join(root.itertext())

    with zipfile.ZipFile(project / bilingual["written"][0]) as archive:
        root = ElementTree.fromstring(archive.read("OEBPS/text/ch1.xhtml"))
        rubies = [
            element
            for element in root.iter()
            if element.tag.rsplit("}", 1)[-1] == "ruby"
        ]
        assert len(rubies) == 4
        text = "".join(root.itertext())
        assert "彼は漢かん字じを読む。\n他漢字hànzìと特別tèbié。" in text
        assert "｜" not in text
        assert "《" not in text


def test_epub_aozora_output_can_add_ruby_to_plain_segment(tmp_path: Path) -> None:
    source = tmp_path / "plain-aozora.epub"
    make_epub(
        source,
        xhtml=(
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            "<p>猫です。</p></body></html>"
        ).encode(),
    )
    project, _ = init_project(
        [str(source)],
        name="plain-aozora",
        document_adapter_id="epub",
        adapter_options={"epub": {"ruby_mode": "aozora"}},
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    metadata = read_json(project, project / "project.json")
    segment = read_segments(project)[0]
    append_jsonl(
        project,
        stage_result_path(project, "translation"),
        record_header(
            "stage_result",
            str(metadata["project_id"]),
            stage="translation",
            segment_id=segment["segment_id"],
            status="completed",
            text="｜猫《neko》です。",
            validation_status="passed",
            validation_findings=[],
            stage_fingerprint="sha256:test",
            terms_revision=0,
            run_id="RUN-PLAIN-AOZORA",
            request_id="REQ-PLAIN-AOZORA",
        ),
    )
    translated = export_project(
        project, "translated", bilingual=False, allow_missing=False
    )
    with zipfile.ZipFile(project / translated["written"][0]) as archive:
        root = ElementTree.fromstring(archive.read("OEBPS/text/ch1.xhtml"))
        rubies = [
            item
            for item in root.iter()
            if item.tag.rsplit("}", 1)[-1] == "ruby"
        ]
        assert len(rubies) == 1
        assert "".join(rubies[0].itertext()) == "猫neko"
        assert "｜" not in "".join(root.itertext())


@pytest.mark.parametrize(
    "target",
    [
        "｜《reading》",
        "｜base《》",
        "｜outer｜inner《reading》《outer》",
        "｜base《reading",
        "<ruby>base</ruby>",
    ],
)
def test_epub_aozora_invalid_markers_remain_plain_text(
    tmp_path: Path, target: str
) -> None:
    source = tmp_path / "ruby-invalid-output.epub"
    make_epub(
        source,
        xhtml=(
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            "<p>原文<ruby>漢<rt>かん</rt></ruby>末</p>"
            "</body></html>"
        ).encode(),
    )
    project, _ = init_project(
        [str(source)],
        name="ruby-invalid-output",
        document_adapter_id="epub",
        adapter_options={"epub": {"ruby_mode": "aozora"}},
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    metadata = read_json(project, project / "project.json")
    segment = read_segments(project)[0]
    append_jsonl(
        project,
        stage_result_path(project, "translation"),
        record_header(
            "stage_result",
            str(metadata["project_id"]),
            stage="translation",
            segment_id=segment["segment_id"],
            status="completed",
            text=target,
            validation_status="passed",
            validation_findings=[],
            stage_fingerprint="sha256:test",
            terms_revision=0,
            run_id="RUN-RUBY-INVALID",
            request_id="REQ-RUBY-INVALID",
        ),
    )
    translated = export_project(
        project, "translated", bilingual=False, allow_missing=False
    )
    with zipfile.ZipFile(project / translated["written"][0]) as archive:
        root = ElementTree.fromstring(archive.read("OEBPS/text/ch1.xhtml"))
        assert not any(
            element.tag.rsplit("}", 1)[-1] == "ruby"
            for element in root.iter()
        )
        assert "".join(root.itertext()) == target


@pytest.mark.parametrize(
    "ruby",
    [
        "<ruby>外<ruby>内<rt>うち</rt></ruby><rt>そと</rt></ruby>",
        "<ruby>漢<rt></rt></ruby>",
        "<ruby>漢<rtc><span>かん</span></rtc></ruby>",
    ],
)
def test_epub_rejects_ambiguous_ruby_with_xhtml_location(
    tmp_path: Path, ruby: str
) -> None:
    source = tmp_path / "bad-ruby.epub"
    make_epub(
        source,
        xhtml=(
            '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>'
            f"{ruby}</p></body></html>"
        ).encode(),
    )

    with pytest.raises(ProjectError, match=r"ch1\.xhtml.*ruby path"):
        get_document_adapter("epub").import_sources(
            [str(source)],
            recursive=False,
            config={},
            options={"ruby_mode": "aozora"},
        )


def test_epub_missing_or_corrupt_state_fails_without_txt_fallback(
    tmp_path: Path,
) -> None:
    project = init_epub(tmp_path)
    file_record = read_files(project)[0]
    state_path = project / str(file_record["document_adapter_state"])
    write_json(project, state_path, {"schema_version": 1, "state": []})
    with pytest.raises(IncompleteError, match="状态损坏"):
        export_project(
            project, "translated", bilingual=False, allow_missing=True
        )
    with pytest.raises(UsageError, match="未安装 Document Adapter"):
        get_document_adapter("missing")


def test_epub_adapter_version_mismatch_fails_explicitly(
    tmp_path: Path,
) -> None:
    project = init_epub(tmp_path)
    file_record = read_files(project)[0]
    file_record["document_adapter_version"] = "future"
    with sqlite3.connect(project / "project.sqlite") as connection:
        connection.execute(
            "UPDATE files SET payload_json = ? WHERE file_id = ?",
            (json.dumps(file_record, ensure_ascii=False), file_record["file_id"]),
        )
    with pytest.raises(IncompleteError, match="版本不兼容"):
        export_project(
            project, "translated", bilingual=False, allow_missing=True
        )


def test_epub_rejects_zip_path_traversal(tmp_path: Path) -> None:
    source = tmp_path / "bad.epub"
    make_epub(source)
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr("../escape", b"x")
    with pytest.raises(ProjectError, match="不安全 ZIP 路径"):
        get_document_adapter("epub").import_sources(
            [str(source)], recursive=False, config={}, options={"ruby_mode": "aozora"}
        )


def test_epub_rejects_xml_entities(tmp_path: Path) -> None:
    source = tmp_path / "bad.epub"
    make_epub(
        source,
        xhtml=(
            b'<!DOCTYPE html [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
            b'<html><body>&x;</body></html>'
        ),
    )
    with pytest.raises(ProjectError, match="实体声明"):
        get_document_adapter("epub").import_sources(
            [str(source)], recursive=False, config={}, options={"ruby_mode": "aozora"}
        )


def test_epub3_accepts_bare_doctype_and_ignores_comment_text(
    tmp_path: Path,
) -> None:
    source = tmp_path / "epub3-doctype.epub"
    make_epub(
        source,
        xhtml=(
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE html><!-- <!ENTITY fake SYSTEM "file:///secret"> -->'
            b'<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            + "<p>合法正文</p></body></html>".encode()
        ),
    )

    imported = get_document_adapter("epub").import_sources(
        [str(source)], recursive=False, config={}, options={"ruby_mode": "aozora"}
    )

    assert list(imported.files[0].segments) == ["合法正文"]


def test_epub2_accepts_xhtml11_public_with_custom_system_identifier(
    tmp_path: Path,
) -> None:
    source = tmp_path / "epub2-doctype.epub"
    make_epub(
        source,
        opf_version="2.0",
        xhtml=(
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
            b'"https://example.invalid/local.xhtml11.dtd">'
            b'<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            + "<p>EPUB 2 正文</p></body></html>".encode()
        ),
    )

    imported = get_document_adapter("epub").import_sources(
        [str(source)], recursive=False, config={}, options={"ruby_mode": "aozora"}
    )

    assert list(imported.files[0].segments) == ["EPUB 2 正文"]


@pytest.mark.parametrize(
    ("opf_version", "doctype"),
    [
        (
            "3.0",
            '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://x">',
        ),
        (
            "2.0",
            '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN" "http://x">',
        ),
        ("2.0", '<!DOCTYPE html SYSTEM "http://x">'),
    ],
)
def test_epub_rejects_incompatible_doctype(
    tmp_path: Path, opf_version: str, doctype: str
) -> None:
    source = tmp_path / "bad-doctype.epub"
    make_epub(
        source,
        opf_version=opf_version,
        xhtml=(
            f'{doctype}<html xmlns="http://www.w3.org/1999/xhtml">'
            "<body><p>正文</p></body></html>"
        ).encode(),
    )

    with pytest.raises(ProjectError, match="DOCTYPE|PUBLIC|外部 DTD"):
        get_document_adapter("epub").import_sources(
            [str(source)], recursive=False, config={}, options={"ruby_mode": "aozora"}
        )


def test_epub_rejects_missing_or_unknown_opf_version(tmp_path: Path) -> None:
    for version in ("", "4.0"):
        source = tmp_path / f"opf-{version or 'missing'}.epub"
        make_epub(source, opf_version=version)
        with pytest.raises(ProjectError, match="OPF 版本"):
            get_document_adapter("epub").import_sources(
                [str(source)],
                recursive=False,
                config={},
                options={"ruby_mode": "aozora"},
            )


def test_epub_rejects_zip_symlink(tmp_path: Path) -> None:
    source = tmp_path / "bad.epub"
    make_epub(source)
    info = zipfile.ZipInfo("OEBPS/link")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr(info, "../secret")
    with pytest.raises(ProjectError, match="符号链接"):
        get_document_adapter("epub").import_sources(
            [str(source)], recursive=False, config={}, options={"ruby_mode": "aozora"}
        )


def test_epub_rejects_abnormal_compression_ratio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "bad.epub"
    make_epub(source)
    with zipfile.ZipFile(
        source, "a", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        archive.writestr("OEBPS/repeated.bin", b"x" * 10_000)
    monkeypatch.setattr("app.epub_adapter.MAX_EPUB_COMPRESSION_RATIO", 2)
    with pytest.raises(ProjectError, match="压缩比异常"):
        get_document_adapter("epub").import_sources(
            [str(source)], recursive=False, config={}, options={"ruby_mode": "aozora"}
        )


class FailingExportAdapter:
    adapter_id = "failing"
    version = "1"
    capabilities = frozenset({"translated_export"})
    extensions = frozenset()
    import_options = ()

    def export_sources(self, *, staging_dir: Path, **_: object) -> list[Path]:
        (staging_dir / "partial.txt").write_text("partial", encoding="utf-8")
        raise ProjectError("fixture failure")


class IncompleteExportAdapter:
    adapter_id = "incomplete"
    version = "1"
    capabilities = frozenset({"translated_export"})
    extensions = frozenset()
    import_options = ()

    def export_sources(self, *, staging_dir: Path, **_: object) -> list[Path]:
        (staging_dir / "ready.txt").write_text("ready", encoding="utf-8")
        return [Path("ready.txt"), Path("missing.txt")]


def test_document_export_failure_publishes_nothing(tmp_path: Path) -> None:
    project = tmp_path / "project"
    directory = project / "output" / "translated"
    with pytest.raises(ProjectError, match="fixture failure"):
        publish_document_exports(
            [DocumentExportJob(
                adapter=FailingExportAdapter(),  # type: ignore[arg-type]
                file={}, segments=[], opaque_state=None,
            )],
            project=project,
            directory=directory,
            output_text={},
            bilingual=False,
            output_encoding="utf-8",
            target_language="简体中文",
            target_language_tag="zh-Hans",
        )
    assert not directory.exists()


def test_document_export_validates_all_files_before_publish(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    directory = project / "output" / "translated"
    with pytest.raises(ProjectError, match="未生成声明的输出"):
        publish_document_exports(
            [DocumentExportJob(
                adapter=IncompleteExportAdapter(),  # type: ignore[arg-type]
                file={}, segments=[], opaque_state=None,
            )],
            project=project,
            directory=directory,
            output_text={},
            bilingual=False,
            output_encoding="utf-8",
            target_language="简体中文",
            target_language_tag="zh-Hans",
        )
    assert not directory.exists()


class FakeEntryPoint:
    name = "fixture"

    def __init__(self, descriptor: PluginDescriptor) -> None:
        self.descriptor = descriptor

    def load(self) -> object:
        return self.descriptor


@pytest.mark.parametrize(
    "incompatible_version", [5, 6, 7, 9, PLUGIN_PROTOCOL_VERSION + 1]
)
def test_plugin_host_rejects_protocol_and_duplicate_adapter(
    monkeypatch: pytest.MonkeyPatch, incompatible_version: int
) -> None:
    txt_adapter = get_document_adapter("txt")
    incompatible = PluginDescriptor(
        plugin_id="future",
        version="1",
        protocol_version=incompatible_version,
    )
    monkeypatch.setattr(
        "app.plugins.entry_points",
        lambda **_: [FakeEntryPoint(incompatible)],
    )
    with pytest.raises(ConfigError, match="协议版本不兼容"):
        load_plugins()

    duplicate = PluginDescriptor(
        plugin_id="duplicate",
        version="1",
        protocol_version=PLUGIN_PROTOCOL_VERSION,
        document_adapters=(txt_adapter,),
    )
    monkeypatch.setattr(
        "app.plugins.entry_points",
        lambda **_: [FakeEntryPoint(duplicate)],
    )
    with pytest.raises(ConfigError, match="Adapter ID 重复"):
        load_plugins()


def test_document_adapter_extensions_are_unique_and_resolve_case_insensitively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert get_document_adapter_for_extension(".TXT").adapter_id == "txt"
    assert get_document_adapter_for_extension(".text").adapter_id == "txt"
    assert get_document_adapter_for_extension(".EPUB").adapter_id == "epub"
    with pytest.raises(UsageError, match="没有 Document Adapter"):
        get_document_adapter_for_extension(".pdf")

    class DuplicateExtensionAdapter:
        adapter_id = "duplicate-extension"
        version = "1"
        capabilities = frozenset({"import"})
        extensions = frozenset({".TXT".casefold()})
        import_options = ()

    duplicate = PluginDescriptor(
        plugin_id="duplicate-extension",
        version="1",
        protocol_version=PLUGIN_PROTOCOL_VERSION,
        document_adapters=(DuplicateExtensionAdapter(),),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        "app.plugins.entry_points",
        lambda **_: [FakeEntryPoint(duplicate)],
    )
    with pytest.raises(ConfigError, match="扩展名重复"):
        load_plugins()


def test_document_adapter_choice_options_apply_defaults_and_validate_values() -> None:
    epub = get_document_adapter("epub")
    assert validate_document_import_options(epub, None) == {
        "ruby_mode": "aozora",
        "inline_format_mode": "plain",
    }
    assert validate_document_import_options(
        epub, {"ruby_mode": "base_only"}
    ) == {"ruby_mode": "base_only", "inline_format_mode": "plain"}
    with pytest.raises(UsageError, match="未知导入选项"):
        validate_document_import_options(epub, {"unknown": "value"})
    with pytest.raises(UsageError, match="取值无效"):
        validate_document_import_options(epub, {"ruby_mode": "invalid"})


def test_plugin_host_rejects_invalid_choice_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InvalidOptionAdapter:
        adapter_id = "invalid-option"
        version = "1"
        capabilities = frozenset({"import"})
        extensions = frozenset({".invalid"})
        import_options = (
            DocumentChoiceOption(
                option_id="mode",
                label="Mode",
                default="missing",
                choices=(("one", "One"), ("two", "Two")),
            ),
        )

    descriptor = PluginDescriptor(
        plugin_id="invalid-option",
        version="1",
        protocol_version=PLUGIN_PROTOCOL_VERSION,
        document_adapters=(InvalidOptionAdapter(),),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        "app.plugins.entry_points",
        lambda **_: [FakeEntryPoint(descriptor)],
    )
    with pytest.raises(ConfigError, match="导入选项声明无效"):
        load_plugins()


def test_normalize_model_text_rejects_unknown_file_reference() -> None:
    with pytest.raises(ProjectError, match="未知文件"):
        normalize_model_text(
            files=[],
            segment={"file_id": "missing"},
            text="模型输出",
            stage="translation",
        )
