from __future__ import annotations

import json
import stat
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest

from app.errors import ConfigError, IncompleteError, ProjectError, UsageError
from app.documents import DocumentExportJob, publish_document_exports
from app.documents import DocumentChoiceOption
from app.execution import stage_result_path
from app.plugins import (
    PLUGIN_PROTOCOL_VERSION,
    PluginDescriptor,
    get_document_adapter_for_extension,
    get_document_adapter,
    load_plugins,
    validate_document_import_options,
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
            ["彼は", "｜漢字《かんじ》を読む。", "｜特別《スペシャル／とくべつ》だ。"],
        ),
        ("base_only", ["彼は", "漢字を読む。", "特別だ。"]),
        ("parenthetical", ["彼は", "漢字（かんじ）を読む。", "特別（スペシャル／とくべつ）だ。"]),
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
    metadata = read_json(project / "project.json")
    segments = read_jsonl(project / "source" / "segments.jsonl")
    targets = ["他は", "汉字を読む。", "特别だ。"]
    for segment, target in zip(segments, targets, strict=True):
        append_jsonl(
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
        assert "彼は\n他は" in text
        assert "を読む。\n汉字を読む。" in text
        assert "だ。\n特别だ。" in text


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
    with pytest.raises(ProjectError, match="DTD 或实体"):
        get_document_adapter("epub").import_sources(
            [str(source)], recursive=False, config={}, options={"ruby_mode": "aozora"}
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
        "ruby_mode": "aozora"
    }
    assert validate_document_import_options(
        epub, {"ruby_mode": "base_only"}
    ) == {"ruby_mode": "base_only"}
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
