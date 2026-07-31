from __future__ import annotations

import json
import stat
import zipfile
from pathlib import Path

import pytest

from app.errors import ConfigError, IncompleteError, ProjectError, UsageError
from app.documents import DocumentExportJob, publish_document_exports
from app.execution import stage_result_path
from app.plugins import (
    PLUGIN_PROTOCOL_VERSION,
    PluginDescriptor,
    get_document_adapter,
    load_plugins,
)
from app.project import init_project
from app.stages import export_project
from app.storage import append_jsonl, read_json, read_jsonl, record_header
from tests.test_foundation import make_app_root


def make_epub(path: Path, *, xhtml: bytes | None = None) -> None:
    chapter = xhtml or (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Book</title>'
        b'<link rel="stylesheet" href="../style.css"/></head><body>'
        b'<h1>Chapter <em>One</em></h1><p>Hello world.</p>'
        b'<img src="../cover.png" alt="cover"/></body></html>'
    )
    container = (
        b'<?xml version="1.0"?>'
        b'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        b'<rootfiles><rootfile full-path="OEBPS/content.opf" '
        b'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    opf = (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        b'<metadata><dc:title xmlns:dc="http://purl.org/dc/elements/1.1/">Demo</dc:title></metadata>'
        b'<manifest><item id="c1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>'
        b'<item id="css" href="style.css" media-type="text/css"/>'
        b'<item id="cover" href="cover.png" media-type="image/png"/></manifest>'
        b'<spine><itemref idref="c1"/></spine></package>'
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
        archive.writestr("OEBPS/text/ch1.xhtml", chapter)
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
    metadata = read_json(project / "project.json")
    segments = read_jsonl(project / "source" / "segments.jsonl")
    for segment in segments:
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
    file_record = read_jsonl(project / "source" / "files.jsonl")[0]
    assert file_record["document_adapter_id"] == "epub"
    assert file_record["document_adapter_state"]
    segments = add_translations(project)
    assert [item["source"] for item in segments] == [
        "Chapter ",
        "One",
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


def test_epub_missing_or_corrupt_state_fails_without_txt_fallback(
    tmp_path: Path,
) -> None:
    project = init_epub(tmp_path)
    file_record = read_jsonl(project / "source" / "files.jsonl")[0]
    state_path = project / str(file_record["document_adapter_state"])
    state_path.write_text('{"schema_version":1,"state":[]}\n', encoding="utf-8")
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
    files_path = project / "source" / "files.jsonl"
    file_record = read_jsonl(files_path)[0]
    file_record["document_adapter_version"] = "future"
    files_path.write_text(
        json.dumps(file_record, ensure_ascii=False) + "\n", encoding="utf-8"
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
            [str(source)], recursive=False, config={}
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
    with pytest.raises(ProjectError, match="DTD 或实体"):
        get_document_adapter("epub").import_sources(
            [str(source)], recursive=False, config={}
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
            [str(source)], recursive=False, config={}
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
            [str(source)], recursive=False, config={}
        )


class FailingExportAdapter:
    adapter_id = "failing"
    version = "1"
    capabilities = frozenset({"translated_export"})

    def export_sources(self, *, staging_dir: Path, **_: object) -> list[Path]:
        (staging_dir / "partial.txt").write_text("partial", encoding="utf-8")
        raise ProjectError("fixture failure")


class IncompleteExportAdapter:
    adapter_id = "incomplete"
    version = "1"
    capabilities = frozenset({"translated_export"})

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
        )
    assert not directory.exists()


class FakeEntryPoint:
    name = "fixture"

    def __init__(self, descriptor: PluginDescriptor) -> None:
        self.descriptor = descriptor

    def load(self) -> object:
        return self.descriptor


def test_plugin_host_rejects_protocol_and_duplicate_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    txt_adapter = get_document_adapter("txt")
    incompatible = PluginDescriptor(
        plugin_id="future",
        version="1",
        protocol_version=PLUGIN_PROTOCOL_VERSION + 1,
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
