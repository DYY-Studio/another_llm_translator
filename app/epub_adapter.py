from __future__ import annotations

import posixpath
import re
import stat
import zipfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote
from xml.parsers import expat
from xml.etree import ElementTree
from uuid import UUID, uuid5

import regex

from .documents import (
    DocumentChoiceOption,
    DocumentImport,
    EMPHASIS_RUBY_CHARACTERS,
    ImportedFile,
    aozora_to_model_ruby,
    compact_emphasis_aozora,
    escape_model_ruby_literal,
    parse_aozora_text,
)
from .errors import (
    ConfigError,
    ExportError,
    IncompleteError,
    ProjectError,
    StorageError,
    UsageError,
)
from .sqlite_storage import read_json


MAX_EPUB_ENTRIES = 10_000
MAX_EPUB_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_EPUB_COMPRESSION_RATIO = 200
_OPF_NAMESPACE = "http://www.idpf.org/2007/opf"
_DC_NAMESPACE = "http://purl.org/dc/elements/1.1/"
_XML_NAMESPACE = "http://www.w3.org/XML/1998/namespace"
_PUBLICATION_ID_NAMESPACE = UUID("2e22f6f0-4406-4f02-a9ee-0d4a8a3a4a82")
_OPF_EVENT_ATTRIBUTE = f"{{{_OPF_NAMESPACE}}}event"
_NCX_MEDIA_TYPE = "application/x-dtbncx+xml"
_NCX_PUBLIC_ID = "-//NISO//DTD ncx 2005-1//EN"
_NCX_SYSTEM_ID = "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd"
_SKIPPED_TEXT_ELEMENTS = {"head", "script", "style", "title"}
_INLINE_TEXT_ELEMENTS = {
    "a",
    "abbr",
    "b",
    "bdi",
    "bdo",
    "cite",
    "code",
    "data",
    "dfn",
    "em",
    "i",
    "kbd",
    "mark",
    "q",
    "s",
    "samp",
    "small",
    "span",
    "strong",
    "sub",
    "sup",
    "time",
    "u",
    "var",
}
_REQUIRED_INLINE_TEXT_ELEMENTS = {
    "a",
    "abbr",
    "bdi",
    "bdo",
    "cite",
    "code",
    "data",
    "dfn",
    "kbd",
    "q",
    "samp",
    "sub",
    "sup",
    "time",
    "var",
}


class EPUBDocumentAdapter:
    adapter_id = "epub"
    version = "0.5"
    readable_versions = frozenset({"0.3", "0.4", "0.5"})
    capabilities = frozenset({"import", "translated_export", "bilingual_export"})
    extensions = frozenset({".epub"})
    import_options = (
        DocumentChoiceOption(
            option_id="ruby_mode",
            label="Ruby 表示",
            default="aozora",
            choices=(
                ("aozora", "青空格式｜原文《Ruby》"),
                ("short_xml", "模型短 XML（用户显示青空格式）"),
                ("compact", "模型紧凑格式（用户显示青空格式）"),
                ("base_only", "仅基础文字"),
            ),
        ),
        DocumentChoiceOption(
            option_id="inline_format_mode",
            label="普通内联格式",
            default="plain",
            choices=(
                ("plain", "纯文本"),
                ("markers", "受控标记"),
            ),
        ),
    )
    run_options = (
        DocumentChoiceOption(
            option_id="inline_format_policy",
            label="内联格式保留策略",
            default="tiered",
            choices=(
                ("tiered", "分级保留"),
                ("strict", "全部保留"),
            ),
        ),
    )

    def model_prompt_requirements(
        self,
        *,
        stage: str,
        language: str,
        opaque_state: dict[str, Any] | None,
    ) -> str | None:
        if stage not in {
            "terminology",
            "translation",
            "proofreading",
            "polishing",
        }:
            return None
        if not isinstance(opaque_state, dict):
            raise ConfigError("EPUB Document Adapter 状态无效")
        ruby_mode = opaque_state.get("ruby_mode", "aozora")
        requirements: list[str] = []
        if ruby_mode == "short_xml":
            requirements.append(
                (
                    "source 中的 Ruby 使用 <r><b>base</b><y>reading</y></r>；"
                    "base 是必须翻译的正文。可删除整个 Ruby 标记并只返回已译 base；"
                    "保留时必须使用相同严格、闭合、非嵌套结构，reading 必须翻译或转写。"
                    "XML 特殊字符必须使用标准实体。"
                    if language == "zh-CN"
                    else
                    "Ruby in source uses <r><b>base</b><y>reading</y></r>; "
                    "base is source text and must be translated. You may drop the "
                    "whole Ruby structure and return only the translated base. If "
                    "kept, return the same strict, closed, non-nested structure and "
                    "translate or transliterate reading. Escape XML special "
                    "characters with standard entities."
                )
            )
        elif ruby_mode == "compact":
            requirements.append(
                (
                    "source 中的 Ruby 使用 ⟦R:base|Y:reading⟧；base 是必须翻译的正文。"
                    "可删除整个 Ruby 标记并只返回已译 base；保留时必须使用同一严格结构，"
                    "reading 必须翻译或转写。在 base/reading 中用 \\\\, \\|, \\⟦, \\⟧ "
                    "转义保留字符。"
                    if language == "zh-CN"
                    else
                    "Ruby in source uses ⟦R:base|Y:reading⟧; base is source "
                    "text and must be translated. You may drop the whole Ruby "
                    "structure and return only the translated base. If kept, use "
                    "the same strict structure and translate or transliterate "
                    "reading. Escape reserved characters in base/reading as \\\\, "
                    "\\|, \\⟦, and \\⟧."
                )
            )
        policy = opaque_state.get("inline_format_policy", "tiered")
        if policy not in {"tiered", "strict"}:
            raise ConfigError(f"EPUB 内联格式策略无效：{policy}")
        if opaque_state.get("inline_format_mode") != "markers":
            return " ".join(requirements) or None
        if language == "zh-CN":
            if policy == "strict":
                requirements.append(
                    "source 中的受控内联标记（如 <em1>）是 EPUB 格式的一部分；"
                    "必须保留所有已有标记，保持成对、原有顺序和正确嵌套。"
                    "不得新增标记、属性、未知 HTML 或改变标签层级。"
                )
            else:
                requirements.append(
                "source 中的受控内联标记（如 <em1>）用于重建 EPUB 格式；"
                "语义标记包括 a、abbr、bdi、bdo、cite、code、data、dfn、kbd、"
                "q、samp、sub、sup、time、var；保留这些已有标记并保持成对、"
                "原有顺序和正确嵌套。表现层标记 b、em、i、mark、s、small、"
                "span、strong、u 可以整体省略。不得新增标记、属性、未知 HTML "
                "或改变标签层级。"
                )
        elif language == "en":
            if policy == "strict":
                requirements.append(
                    "Controlled inline markers in source (such as <em1>) are "
                    "part of the EPUB format. Keep every existing marker with "
                    "its pairing, order, and correct nesting. Never add markers, "
                    "attributes, unknown HTML, or change the tag hierarchy."
                )
            else:
                requirements.append(
                "Controlled inline markers in source (such as <em1>) are used "
                "to rebuild EPUB formatting. Semantic markers are a, abbr, bdi, "
                "bdo, cite, code, data, dfn, kbd, q, samp, sub, sup, time, and "
                "var; keep existing ones with their pairing, order, and correct "
                "nesting. Presentation markers b, em, i, mark, s, small, span, "
                "strong, and u may be omitted as a whole. Never add markers, "
                "attributes, unknown HTML, or change the tag hierarchy."
                )
        else:
            raise ConfigError(f"不支持的 Prompt 语言：{language}")
        return " ".join(requirements)

    def normalize_model_output(
        self, *, segment: dict[str, Any], text: str, stage: str
    ) -> str:
        del stage
        ruby_mode = segment.get("_ruby_mode")
        marker_ids = {
            str(item["id"])
            for item in segment.get("_format_markers", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        if ruby_mode == "short_xml":
            text = _short_xml_to_aozora(text, marker_ids)
        elif ruby_mode == "compact":
            text = _compact_to_aozora(text)
        return _normalize_inline_markers(
            compact_emphasis_aozora(text), segment.get("_format_markers", [])
        )

    def import_sources(
        self,
        inputs: list[str],
        *,
        recursive: bool,
        config: dict[str, Any],
        options: dict[str, str],
    ) -> DocumentImport:
        del config
        ruby_mode = options["ruby_mode"]
        inline_format_mode = options.get("inline_format_mode", "plain")
        inline_format_policy = options.get("inline_format_policy", "tiered")
        if recursive:
            raise UsageError("EPUB Adapter 不支持目录递归发现")
        if len(inputs) != 1:
            raise UsageError("EPUB Adapter 每个项目只接受一个 EPUB 文件")
        path = Path(inputs[0])
        if path.is_symlink() or not path.is_file():
            raise UsageError(f"EPUB 输入不存在或是符号链接：{path}")
        if path.suffix.casefold() != ".epub":
            raise UsageError(f"EPUB Adapter 只接受 .epub 文件：{path}")
        with zipfile.ZipFile(path) as archive:
            entries = _validated_entries(archive)
            opf_path = _opf_path(archive, entries)
            document_paths, epub_version = _publication_paths(
                archive, entries, opf_path
            )
            segments: list[str] = []
            model_sources: list[str | None] = []
            segment_part_ids: list[str] = []
            locators: list[dict[str, Any]] = []
            for resource_type, resource_path in document_paths:
                if resource_type == "ncx":
                    root = _parse_ncx(archive.read(resource_path), resource_path)
                    text_root = root
                else:
                    root = _parse_xml(
                        archive.read(resource_path),
                        resource_path,
                        doctype_policy=f"epub{epub_version}_xhtml",
                    )
                    text_root = next(
                        (
                            element
                            for element in root.iter()
                            if _local_name(element.tag) == "body"
                        ),
                        None,
                    )
                    if text_root is None:
                        continue
                for locator, source, model_source in _text_slots(
                    text_root,
                    ruby_mode=ruby_mode,
                    inline_format_mode=inline_format_mode,
                    inline_format_policy=inline_format_policy,
                    location=resource_path,
                ):
                    segments.append(source)
                    model_sources.append(model_source)
                    segment_part_ids.append(resource_path)
                    locators.append(
                        {
                            "path": resource_path,
                            "resource_type": resource_type,
                            "slot": locator,
                        }
                    )
        if not segments:
            raise ProjectError("EPUB spine 中没有可翻译文本")
        return DocumentImport(
            files=(
                ImportedFile(
                    source_path=path,
                    original_name=path.name,
                    segments=tuple(segments),
                    model_sources=tuple(model_sources),
                    segment_part_ids=tuple(segment_part_ids),
                    encoding_detected="xhtml",
                    encoding_used="utf-8",
                    encoding_confidence=1.0,
                    opaque_state={
                        "opf_path": opf_path,
                        "locators": locators,
                        "ruby_mode": ruby_mode,
                        "inline_format_mode": inline_format_mode,
                        "inline_format_policy": inline_format_policy,
                    },
                ),
            ),
        )

    def export_sources(
        self,
        *,
        project: Path,
        staging_dir: Path,
        file: dict[str, Any],
        segments: list[dict[str, Any]],
        output_text: dict[str, str],
        bilingual: bool,
        output_encoding: str,
        target_language: str,
        target_language_tag: str,
        opaque_state: dict[str, Any] | None,
    ) -> list[Path]:
        del output_encoding
        if not target_language_tag:
            raise ExportError(
                "EPUB 导出需要 project.target_language_tag",
                reason="missing_target_language_tag",
                adapter_id="epub",
                setting="project.target_language_tag",
            )
        target_language = target_language.strip()
        if not target_language:
            raise ExportError(
                "EPUB 导出需要非空 project.target_language",
                reason="missing_target_language",
                adapter_id="epub",
                setting="project.target_language",
            )
        if opaque_state is None:
            raise ExportError(
                "EPUB 文件缺少 Document Adapter 状态",
                reason="adapter_state_invalid",
                adapter_id="epub",
                file_id=str(file.get("file_id", "")),
            )
        state = deepcopy(opaque_state)
        locators = state.get("locators")
        if not isinstance(locators, list) or len(locators) != len(segments):
            raise IncompleteError("EPUB Segment 定位状态与项目不一致")
        stored_name = str(file["stored_name"])
        source_path = project / "input" / stored_name
        ordered_segments = sorted(
            segments, key=lambda value: int(value["line_index"])
        )
        by_resource: dict[
            tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]
        ] = {}
        for segment, locator in zip(ordered_segments, locators, strict=True):
            if not isinstance(locator, dict) or not isinstance(
                locator.get("path"), str
            ):
                raise IncompleteError("EPUB Segment 定位状态损坏")
            if not isinstance(locator.get("slot"), dict):
                raise IncompleteError("EPUB Segment 定位 slot 损坏")
            resource_type = locator.get("resource_type")
            if resource_type is None:
                if str(file.get("document_adapter_version", "")) not in {
                    "0.3",
                    "0.4",
                }:
                    raise IncompleteError("EPUB Segment 定位资源类型缺失")
                resource_type = "xhtml"
            if resource_type not in {"xhtml", "ncx"}:
                raise IncompleteError("EPUB Segment 定位资源类型无效")
            by_resource.setdefault((resource_type, locator["path"]), []).append(
                (segment, locator)
            )
        changed: dict[str, bytes] = {}
        with zipfile.ZipFile(source_path) as archive:
            entries = _validated_entries(archive)
            opf_path = state.get("opf_path")
            if not isinstance(opf_path, str):
                raise IncompleteError("EPUB 状态缺少 OPF 路径")
            _, epub_version = _publication_paths(archive, entries, opf_path)
            opf_root = _parse_xml(archive.read(opf_path), opf_path)
            source_languages = _opf_languages(opf_root)
            _set_opf_languages(
                opf_root,
                target_language_tag,
                source_languages if bilingual else [],
            )
            _set_opf_publication_metadata(
                opf_root,
                project_id=_project_id(project),
                file_id=str(file.get("file_id", "")),
                target_language=target_language,
                target_language_tag=target_language_tag,
                bilingual=bilingual,
                epub_version=epub_version,
            )
            ElementTree.register_namespace("", _OPF_NAMESPACE)
            ElementTree.register_namespace("opf", _OPF_NAMESPACE)
            ElementTree.register_namespace("dc", _DC_NAMESPACE)
            changed[opf_path] = ElementTree.tostring(
                opf_root, encoding="utf-8", xml_declaration=True
            )
            for (resource_type, resource_path), items in by_resource.items():
                if resource_path not in entries:
                    raise IncompleteError(
                        f"EPUB 状态引用了缺失资源：{resource_path}"
                    )
                resolved: list[tuple[dict[str, Any], dict[str, Any], str]] = []
                for segment, locator in items:
                    segment_id = str(segment["segment_id"])
                    target = output_text.get(segment_id)
                    if target is None:
                        raise IncompleteError(
                            f"EPUB 导出缺少结果：{segment_id}"
                        )
                    resolved.append((segment, locator, target))
                if resource_type == "ncx":
                    root = _parse_ncx(archive.read(resource_path), resource_path)
                    _set_xml_language(root, target_language_tag)
                    for segment, locator, target in resolved:
                        _set_regular_slot(
                            root,
                            locator.get("slot"),
                            str(segment.get("_adapter_source", segment["source"])),
                            target,
                            bilingual=bilingual,
                        )
                    changed[resource_path] = ElementTree.tostring(
                        root, encoding="utf-8", xml_declaration=True
                    )
                    continue
                xhtml_path = resource_path
                root = _parse_xml(
                    archive.read(xhtml_path),
                    xhtml_path,
                    doctype_policy=f"epub{epub_version}_xhtml",
                )
                body = next(
                    (
                        element
                        for element in root.iter()
                        if _local_name(element.tag) == "body"
                    ),
                    None,
                )
                if body is None:
                    raise IncompleteError(
                        f"EPUB XHTML 缺少 body：{xhtml_path}"
                    )
                _set_xml_language(root, target_language_tag)
                if bilingual:
                    style = body.get("style", "")
                    addition = "white-space: pre-line"
                    body.set(
                        "style",
                        f"{style.rstrip(';')}; {addition}".lstrip("; "),
                    )
                plain_items: list[
                    tuple[dict[str, Any], dict[str, Any], str]
                ] = []
                ruby_items: list[
                    tuple[dict[str, Any], dict[str, Any], str]
                ] = []
                for item in resolved:
                    slot = item[1]["slot"]
                    if _slot_contains_ruby(slot):
                        ruby_items.append(item)
                    else:
                        plain_items.append(item)
                for segment, locator, target in plain_items:
                    _set_regular_slot(
                        body,
                        locator.get("slot"),
                        str(segment.get("_adapter_source", segment["source"])),
                        target,
                        bilingual=bilingual,
                        ruby_mode=str(state.get("ruby_mode", "")),
                    )
                if not bilingual:
                    ruby_items.sort(
                        key=lambda item: _first_ruby_path(item[1]["slot"]),
                        reverse=True,
                    )
                for segment, locator, target in ruby_items:
                    slot = locator.get("slot")
                    if isinstance(slot, dict) and slot.get("kind") == "ruby":
                        _set_ruby_slot(
                            body,
                            slot,
                            target,
                            bilingual=bilingual,
                            ruby_mode=str(state.get("ruby_mode", "")),
                        )
                    else:
                        _set_composite_slot(
                            body,
                            slot,
                            str(segment.get("_adapter_source", segment["source"])),
                            target,
                            bilingual=bilingual,
                            ruby_mode=str(state.get("ruby_mode", "")),
                        )
                changed[xhtml_path] = ElementTree.tostring(
                    root, encoding="utf-8", xml_declaration=True
                )
            stem = Path(str(file["original_name"])).stem
            suffix = "bilingual" if bilingual else "translated"
            relative = Path(f"{stem}.{suffix}.epub")
            destination = staging_dir / relative
            with zipfile.ZipFile(destination, "w") as output:
                for info in archive.infolist():
                    output.writestr(info, changed.get(info.filename, archive.read(info)))
        return [relative]


def _validated_entries(
    archive: zipfile.ZipFile,
) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > MAX_EPUB_ENTRIES:
        raise ProjectError("EPUB ZIP 条目数量超过安全限制")
    entries: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for info in infos:
        path = PurePosixPath(info.filename)
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in info.filename
            or not info.filename
        ):
            raise ProjectError(f"EPUB 包含不安全 ZIP 路径：{info.filename}")
        mode = info.external_attr >> 16
        if mode and stat.S_ISLNK(mode):
            raise ProjectError(f"EPUB ZIP 不允许符号链接：{info.filename}")
        if info.filename in entries:
            raise ProjectError(f"EPUB ZIP 包含重复路径：{info.filename}")
        total += info.file_size
        if total > MAX_EPUB_UNCOMPRESSED_BYTES:
            raise ProjectError("EPUB 解压后总大小超过安全限制")
        if (
            info.file_size > 0
            and info.compress_size > 0
            and info.file_size / info.compress_size
            > MAX_EPUB_COMPRESSION_RATIO
        ):
            raise ProjectError(
                f"EPUB ZIP 压缩比异常：{info.filename}"
            )
        entries[info.filename] = info
    mimetype = entries.get("mimetype")
    if mimetype is None or archive.read(mimetype) != b"application/epub+zip":
        raise ProjectError("EPUB 缺少有效 mimetype")
    return entries


def _parse_xml(
    payload: bytes,
    location: str,
    *,
    doctype_policy: str = "no_external",
) -> ElementTree.Element:
    _validate_xml_declarations(
        payload, location=location, doctype_policy=doctype_policy
    )
    try:
        return ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise ProjectError(f"EPUB XML 无效：{location}: {exc}") from exc


def _parse_ncx(payload: bytes, location: str) -> ElementTree.Element:
    root = _parse_xml(payload, location, doctype_policy="epub2_ncx")
    if _local_name(root.tag) != "ncx":
        raise ProjectError(f"EPUB NCX 根元素无效：{location}")
    return root


def _validate_xml_declarations(
    payload: bytes,
    *,
    location: str,
    doctype_policy: str,
) -> None:
    if doctype_policy not in {
        "no_external",
        "epub2_xhtml",
        "epub3_xhtml",
        "epub2_ncx",
    }:
        raise ProjectError(f"EPUB XML 校验策略无效：{doctype_policy}")
    parser = expat.ParserCreate()

    def reject_entity(*_: object) -> None:
        raise ProjectError(f"EPUB XML 不允许实体声明：{location}")

    def check_doctype(
        name: str,
        system_id: str | None,
        public_id: str | None,
        _has_internal_subset: int,
    ) -> None:
        if doctype_policy == "no_external":
            if system_id is not None or public_id is not None:
                raise ProjectError(
                    f"EPUB XML 不允许外部 DTD 标识：{location}"
                )
            return
        if doctype_policy == "epub2_ncx":
            if (
                name.casefold() != "ncx"
                or public_id != _NCX_PUBLIC_ID
                or system_id != _NCX_SYSTEM_ID
            ):
                raise ProjectError(f"EPUB NCX DOCTYPE 标识无效：{location}")
            return
        if name.casefold() != "html":
            raise ProjectError(f"EPUB XHTML DOCTYPE 名称无效：{location}")
        if doctype_policy == "epub3_xhtml":
            if system_id is not None or public_id is not None:
                raise ProjectError(
                    f"EPUB 3 XHTML 不允许外部 DTD 标识：{location}"
                )
            return
        if public_id != "-//W3C//DTD XHTML 1.1//EN":
            raise ProjectError(
                f"EPUB 2 XHTML PUBLIC 标识无效：{location}"
            )

    parser.StartDoctypeDeclHandler = check_doctype
    parser.EntityDeclHandler = reject_entity
    parser.UnparsedEntityDeclHandler = reject_entity
    parser.ExternalEntityRefHandler = reject_entity
    try:
        parser.Parse(payload, True)
    except ProjectError:
        raise
    except expat.ExpatError as exc:
        raise ProjectError(f"EPUB XML 无效：{location}: {exc}") from exc


def _opf_metadata(root: ElementTree.Element) -> ElementTree.Element:
    for child in list(root):
        if _local_name(child.tag) == "metadata":
            return child
    raise IncompleteError("EPUB OPF 缺少 metadata")


def _opf_languages(root: ElementTree.Element) -> list[str]:
    metadata = _opf_metadata(root)
    return [
        (child.text or "").strip()
        for child in list(metadata)
        if _namespace_uri(child.tag) == _DC_NAMESPACE
        and _local_name(child.tag) == "language"
        and (child.text or "").strip()
    ]


def _set_opf_languages(
    root: ElementTree.Element,
    target_language_tag: str,
    source_languages: list[str],
) -> None:
    metadata = _opf_metadata(root)
    language_nodes = [
        child
        for child in list(metadata)
        if _namespace_uri(child.tag) == _DC_NAMESPACE
        and _local_name(child.tag) == "language"
    ]
    first_index = (
        list(metadata).index(language_nodes[0])
        if language_nodes
        else len(list(metadata))
    )
    for child in language_nodes:
        metadata.remove(child)
    values: list[str] = []
    seen: set[str] = set()
    for value in [target_language_tag, *source_languages]:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            values.append(value)
    for offset, value in enumerate(values):
        element = ElementTree.Element(f"{{{_DC_NAMESPACE}}}language")
        element.text = value
        metadata.insert(first_index + offset, element)


def _set_xml_language(
    root: ElementTree.Element, target_language_tag: str
) -> None:
    root.set(f"{{{_XML_NAMESPACE}}}lang", target_language_tag)
    if _local_name(root.tag) == "html":
        root.set("lang", target_language_tag)


def _project_id(project: Path) -> str:
    try:
        metadata = read_json(project, project / "project.json")
    except (OSError, ProjectError, StorageError) as exc:
        raise IncompleteError(f"无法读取 EPUB 所属项目身份：{project}") from exc
    value = metadata.get("project_id")
    if not isinstance(value, str) or not value.strip():
        raise IncompleteError("项目缺少有效 project_id")
    return value.strip()


def _publication_identifier(
    *,
    project_id: str,
    file_id: str,
    target_language_tag: str,
    bilingual: bool,
) -> str:
    if not file_id:
        raise IncompleteError("EPUB 文件缺少 file_id")
    mode = "bilingual" if bilingual else "translated"
    seed = "\n".join(
        (project_id, file_id, target_language_tag.casefold(), mode)
    )
    return f"urn:uuid:{uuid5(_PUBLICATION_ID_NAMESPACE, seed)}"


def _new_xml_id(root: ElementTree.Element) -> str:
    used = {
        value
        for element in root.iter()
        if (value := element.get("id"))
    }
    base = "another-translator-publication-id"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _set_opf_publication_metadata(
    root: ElementTree.Element,
    *,
    project_id: str,
    file_id: str,
    target_language: str,
    target_language_tag: str,
    bilingual: bool,
    epub_version: str,
) -> None:
    metadata = _opf_metadata(root)
    title_nodes = [
        child
        for child in list(metadata)
        if _namespace_uri(child.tag) == _DC_NAMESPACE
        and _local_name(child.tag) == "title"
    ]
    if not title_nodes or not (title_nodes[0].text or "").strip():
        raise ExportError(
            "EPUB OPF 缺少有效主标题",
            reason="missing_primary_title",
            adapter_id="epub",
        )
    suffix = f"（{target_language}"
    if bilingual:
        suffix += "·双语"
    suffix += "）"
    title_nodes[0].text = f"{title_nodes[0].text.strip()}{suffix}"

    identifier_id = _new_xml_id(root)
    identifier = ElementTree.Element(f"{{{_DC_NAMESPACE}}}identifier")
    identifier.set("id", identifier_id)
    identifier.text = _publication_identifier(
        project_id=project_id,
        file_id=file_id,
        target_language_tag=target_language_tag,
        bilingual=bilingual,
    )
    identifier_indices = [
        index
        for index, child in enumerate(list(metadata))
        if _namespace_uri(child.tag) == _DC_NAMESPACE
        and _local_name(child.tag) == "identifier"
    ]
    insert_at = (max(identifier_indices) + 1) if identifier_indices else 0
    metadata.insert(insert_at, identifier)
    root.set("unique-identifier", identifier_id)

    _set_opf_modified(metadata, epub_version, _utc_modified_timestamp())


def _utc_modified_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _set_opf_modified(
    metadata: ElementTree.Element, epub_version: str, value: str
) -> None:
    if epub_version == "3":
        nodes = [
            child
            for child in list(metadata)
            if _local_name(child.tag) == "meta"
            and child.get("property") == "dcterms:modified"
        ]
        if nodes:
            nodes[0].text = value
            for duplicate in nodes[1:]:
                metadata.remove(duplicate)
            return
        element = ElementTree.Element(f"{{{_OPF_NAMESPACE}}}meta")
        element.set("property", "dcterms:modified")
        element.text = value
        metadata.append(element)
        return

    nodes = [
        child
        for child in list(metadata)
        if _namespace_uri(child.tag) == _DC_NAMESPACE
        and _local_name(child.tag) == "date"
        and child.get(_OPF_EVENT_ATTRIBUTE, "").casefold() == "modification"
    ]
    if nodes:
        nodes[0].text = value
        for duplicate in nodes[1:]:
            metadata.remove(duplicate)
        return
    element = ElementTree.Element(f"{{{_DC_NAMESPACE}}}date")
    element.set(_OPF_EVENT_ATTRIBUTE, "modification")
    element.text = value
    metadata.append(element)


def _opf_path(
    archive: zipfile.ZipFile, entries: dict[str, zipfile.ZipInfo]
) -> str:
    container_path = "META-INF/container.xml"
    if container_path not in entries:
        raise ProjectError("EPUB 缺少 META-INF/container.xml")
    root = _parse_xml(archive.read(container_path), container_path)
    for element in root.iter():
        if _local_name(element.tag) == "rootfile":
            value = element.get("full-path")
            if value:
                resolved = _safe_archive_path("", value)
                if resolved not in entries:
                    raise ProjectError(f"EPUB OPF 不存在：{resolved}")
                return resolved
    raise ProjectError("EPUB container.xml 缺少 rootfile")


def _publication_paths(
    archive: zipfile.ZipFile,
    entries: dict[str, zipfile.ZipInfo],
    opf_path: str,
) -> tuple[list[tuple[str, str]], str]:
    root = _parse_xml(archive.read(opf_path), opf_path)
    if _local_name(root.tag) != "package":
        raise ProjectError(f"EPUB OPF 根元素无效：{opf_path}")
    version = root.get("version")
    if version not in {"2.0", "3.0"}:
        raise ProjectError(f"EPUB OPF 版本不支持：{opf_path}")
    manifest: dict[str, tuple[str, str, str]] = {}
    spine: list[str] = []
    toc_id: str | None = None
    for element in root.iter():
        name = _local_name(element.tag)
        if name == "item" and element.get("id") and element.get("href"):
            manifest[str(element.get("id"))] = (
                str(element.get("href")),
                str(element.get("media-type", "")),
                str(element.get("properties", "")),
            )
        elif name == "itemref" and element.get("idref"):
            spine.append(str(element.get("idref")))
        elif name == "spine" and element.get("toc") is not None:
            toc_id = str(element.get("toc"))
    base = posixpath.dirname(opf_path)
    spine_paths: list[str] = []
    for item_id in spine:
        item = manifest.get(item_id)
        if item is None:
            raise ProjectError(f"EPUB spine 引用了未知 manifest ID：{item_id}")
        href, media_type, _ = item
        if media_type != "application/xhtml+xml":
            continue
        resolved = _safe_archive_path(base, unquote(href.split("#", 1)[0]))
        if resolved not in entries:
            raise ProjectError(f"EPUB spine 资源不存在：{resolved}")
        spine_paths.append(resolved)
    if not spine_paths:
        raise ProjectError("EPUB spine 没有 XHTML 内容")
    nav_paths: list[str] = []
    for href, media_type, properties in manifest.values():
        if "nav" not in properties.split():
            continue
        if media_type != "application/xhtml+xml":
            raise ProjectError("EPUB nav 资源必须是 XHTML")
        resolved = _safe_archive_path(base, unquote(href.split("#", 1)[0]))
        if resolved not in entries:
            raise ProjectError(f"EPUB nav 资源不存在：{resolved}")
        nav_paths.append(resolved)
    ncx_path: str | None = None
    if toc_id is not None:
        item = manifest.get(toc_id)
        if item is None:
            raise ProjectError(f"EPUB spine toc 引用了未知 manifest ID：{toc_id}")
        href, media_type, _ = item
        if media_type != _NCX_MEDIA_TYPE:
            raise ProjectError("EPUB spine toc 必须引用 NCX 资源")
        ncx_path = _safe_archive_path(base, unquote(href.split("#", 1)[0]))
        if ncx_path not in entries:
            raise ProjectError(f"EPUB NCX 资源不存在：{ncx_path}")
    spine_set = set(spine_paths)
    paths: list[tuple[str, str]] = [
        ("xhtml", path) for path in nav_paths if path not in spine_set
    ]
    if ncx_path is not None:
        paths.append(("ncx", ncx_path))
    paths.extend(("xhtml", path) for path in spine_paths)
    return paths, version[0]


def _safe_archive_path(base: str, value: str) -> str:
    if not value or value.startswith("/") or "\\" in value:
        raise ProjectError(f"EPUB 资源路径无效：{value}")
    resolved = posixpath.normpath(posixpath.join(base, value))
    if resolved == ".." or resolved.startswith("../"):
        raise ProjectError(f"EPUB 资源路径越界：{value}")
    return resolved


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _slot_contains_ruby(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    if raw.get("kind") == "ruby":
        return True
    if raw.get("kind") != "composite":
        return False
    slots = raw.get("slots")
    return isinstance(slots, list) and any(
        isinstance(slot, dict) and slot.get("kind") == "ruby"
        for slot in slots
    )


_INLINE_MARKER_RE = re.compile(r"</?([a-z][a-z0-9]*\d+)>")
_COMPACT_ESCAPES = frozenset("\\|⟦⟧")


def _xml_fragment_content(
    element: ElementTree.Element,
    marker_ids: set[str],
) -> str:
    parts: list[str] = [element.text or ""]
    for child in list(element):
        if child.attrib:
            raise IncompleteError("EPUB short_xml 输出不允许属性")
        name = _local_name(child.tag)
        if name == "r":
            children = list(child)
            if (
                child.text
                or len(children) != 2
                or _local_name(children[0].tag) != "b"
                or _local_name(children[1].tag) != "y"
                or children[0].attrib
                or children[1].attrib
                or list(children[0])
                or list(children[1])
                or children[0].tail
                or children[1].tail
                or not children[0].text
                or not children[1].text
                or any(
                    character in "\r\n"
                    for character in f"{children[0].text}{children[1].text}"
                )
            ):
                raise IncompleteError("EPUB short_xml Ruby 结构无效")
            parts.append(f"｜{children[0].text}《{children[1].text}》")
        elif name in marker_ids:
            parts.append(f"<{name}>")
            parts.append(_xml_fragment_content(child, marker_ids))
            parts.append(f"</{name}>")
        else:
            raise IncompleteError(f"EPUB short_xml 输出包含未知标记：{name}")
        parts.append(child.tail or "")
    return "".join(parts)


def _short_xml_to_aozora(text: str, marker_ids: set[str]) -> str:
    if "<" not in text and ">" not in text:
        return text
    if "<!" in text or "<?" in text:
        raise IncompleteError("EPUB short_xml 输出包含非法 XML 声明")
    try:
        root = ElementTree.fromstring(f"<root>{text}</root>")
    except ElementTree.ParseError as exc:
        raise IncompleteError("EPUB short_xml 输出不是合法 XML") from exc
    return _xml_fragment_content(root, marker_ids)


def _read_compact_value(
    text: str,
    cursor: int,
    terminator: str,
) -> tuple[str, int]:
    parts: list[str] = []
    while cursor < len(text):
        if text.startswith(terminator, cursor):
            return "".join(parts), cursor + len(terminator)
        character = text[cursor]
        if character == "\\":
            cursor += 1
            if cursor >= len(text) or text[cursor] not in _COMPACT_ESCAPES:
                raise IncompleteError("EPUB compact Ruby 包含无效转义")
            parts.append(text[cursor])
            cursor += 1
            continue
        if character in "\r\n":
            raise IncompleteError("EPUB compact Ruby 不能跨行")
        parts.append(character)
        cursor += 1
    raise IncompleteError("EPUB compact Ruby 缺少结束符")


def _read_compact_literal(text: str, cursor: int) -> tuple[str, int]:
    parts: list[str] = []
    while cursor < len(text):
        if text.startswith("⟦R:", cursor):
            break
        character = text[cursor]
        if character == "\\":
            cursor += 1
            if cursor >= len(text) or text[cursor] not in _COMPACT_ESCAPES:
                raise IncompleteError("EPUB compact Ruby 包含无效转义")
            parts.append(text[cursor])
            cursor += 1
            continue
        parts.append(character)
        cursor += 1
    return "".join(parts), cursor


def _compact_to_aozora(text: str) -> str:
    parts: list[str] = []
    cursor = 0
    while cursor < len(text):
        literal, marker = _read_compact_literal(text, cursor)
        parts.append(literal)
        if marker >= len(text):
            break
        base, after_base = _read_compact_value(text, marker + 3, "|Y:")
        reading, after_reading = _read_compact_value(text, after_base, "⟧")
        if not base or not reading:
            raise IncompleteError("EPUB compact Ruby 缺少 base 或 reading")
        parts.append(f"｜{base}《{reading}》")
        cursor = after_reading
    return "".join(parts)


def _normalize_inline_markers(text: str, formats: Any) -> str:
    if not isinstance(formats, list) or not formats:
        return text
    expected: dict[str, dict[str, Any]] = {}
    for item in formats:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise IncompleteError("EPUB 内联格式状态损坏")
        marker_id = str(item["id"])
        if marker_id in expected or not isinstance(item.get("tag"), str):
            raise IncompleteError("EPUB 内联格式标记重复或无效")
        expected[marker_id] = item
    stack: list[str] = []
    seen_open: set[str] = set()
    seen_close: set[str] = set()
    output: list[str] = []
    cursor = 0
    for match in _INLINE_MARKER_RE.finditer(text):
        literal = text[cursor : match.start()]
        if "<" in literal or ">" in literal:
            raise IncompleteError("EPUB 内联格式输出包含未知标记")
        output.append(literal)
        marker_id = match.group(1)
        if marker_id not in expected:
            raise IncompleteError("EPUB 内联格式输出包含未知标记")
        closing = text[match.start() : match.end()].startswith("</")
        if closing:
            if not stack or stack[-1] != marker_id or marker_id in seen_close:
                raise IncompleteError("EPUB 内联格式输出嵌套顺序无效")
            stack.pop()
            seen_close.add(marker_id)
        else:
            if marker_id in seen_open:
                raise IncompleteError("EPUB 内联格式输出标记重复")
            current_path = expected[marker_id].get("path")
            if isinstance(current_path, list):
                ancestors = [
                    other_id
                    for other_id, item in expected.items()
                    if other_id != marker_id
                    and isinstance(item.get("path"), list)
                    and len(item["path"]) < len(current_path)
                    and current_path[: len(item["path"])] == item["path"]
                    and other_id in seen_open
                    and other_id not in seen_close
                ]
                if ancestors and (not stack or stack[-1] != max(
                    ancestors, key=lambda value: len(expected[value]["path"])
                )):
                    raise IncompleteError("EPUB 内联格式输出父子关系无效")
            stack.append(marker_id)
            seen_open.add(marker_id)
        cursor = match.end()
    tail = text[cursor:]
    if "<" in tail or ">" in tail:
        raise IncompleteError("EPUB 内联格式输出包含未知标记")
    output.append(tail)
    if stack:
        raise IncompleteError("EPUB 内联格式输出缺少闭合标记")
    for marker_id, item in expected.items():
        required = item.get("required") is True
        if required and (marker_id not in seen_open or marker_id not in seen_close):
            raise IncompleteError("EPUB 必需内联格式标记缺失")
    return "".join(output)


def _first_ruby_path(raw: Any) -> tuple[int, ...]:
    if not isinstance(raw, dict):
        raise IncompleteError("EPUB Ruby 定位状态损坏")
    if raw.get("kind") == "ruby":
        path = raw.get("path")
    elif raw.get("kind") == "composite":
        slots = raw.get("slots")
        path = next(
            (
                slot.get("path")
                for slot in slots
                if isinstance(slot, dict) and slot.get("kind") == "ruby"
            ),
            None,
        ) if isinstance(slots, list) else None
    else:
        path = None
    if not isinstance(path, list) or not all(
        isinstance(index, int) and index >= 0 for index in path
    ):
        raise IncompleteError("EPUB Ruby 定位 path 损坏")
    return tuple(path)


def _text_slots(
    root: ElementTree.Element,
    *,
    ruby_mode: str,
    inline_format_mode: str = "plain",
    inline_format_policy: str = "tiered",
    location: str,
) -> list[tuple[dict[str, Any], str, str | None]]:
    values: list[tuple[dict[str, Any], str, str | None]] = []
    semantic_run: list[tuple[dict[str, Any], str]] = []
    format_elements: list[tuple[list[int], str]] = []

    def keep_format(name: str) -> bool:
        return inline_format_mode == "markers" and (
            inline_format_policy == "strict"
            or name in _REQUIRED_INLINE_TEXT_ELEMENTS
        )

    def is_descendant(path: list[int], ancestor: list[int]) -> bool:
        return len(path) >= len(ancestor) and path[: len(ancestor)] == ancestor

    def model_run() -> tuple[str, list[dict[str, Any]]]:
        has_ruby = any(
            isinstance(locator, dict) and locator.get("kind") == "ruby"
            for locator, _ in semantic_run
        )
        if inline_format_mode != "markers" and not (
            has_ruby and ruby_mode in {"short_xml", "compact"}
        ):
            return "", []
        entries: list[dict[str, Any]] = []
        for path, name in format_elements:
            indexes = [
                index
                for index, (locator, _) in enumerate(semantic_run)
                if isinstance(locator, dict)
                and isinstance(locator.get("path"), list)
                and is_descendant(
                    (
                        locator["path"]
                        if locator.get("kind") == "text"
                        else locator["path"][:-1]
                    ),
                    path,
                )
            ]
            if not indexes:
                continue
            entries.append(
                {
                    "path": path,
                    "tag": name,
                    "start": min(indexes),
                    "end": max(indexes),
                    "required": name in _REQUIRED_INLINE_TEXT_ELEMENTS,
                }
            )
        entries.sort(key=lambda item: (item["start"], len(item["path"]), item["tag"]))
        for index, item in enumerate(entries, start=1):
            item["id"] = f"{item['tag']}{index}"
        starts: dict[int, list[dict[str, Any]]] = {}
        ends: dict[int, list[dict[str, Any]]] = {}
        for item in entries:
            starts.setdefault(int(item["start"]), []).append(item)
            ends.setdefault(int(item["end"]), []).append(item)
        parts: list[str] = []
        for index, (locator, text) in enumerate(semantic_run):
            del locator
            for item in sorted(starts.get(index, []), key=lambda value: len(value["path"])):
                parts.append(f"<{item['id']}>")
            parts.append(text)
            for item in sorted(
                ends.get(index, []), key=lambda value: len(value["path"]), reverse=True
            ):
                parts.append(f"</{item['id']}>")
        rendered = "".join(parts)
        if ruby_mode not in {"aozora", "short_xml", "compact"}:
            return rendered, entries
        converted: list[str] = []
        cursor = 0
        for match in _INLINE_MARKER_RE.finditer(rendered):
            literal = compact_emphasis_aozora(rendered[cursor : match.start()])
            converted.append(
                aozora_to_model_ruby(literal, ruby_mode)
                if "｜" in literal
                else escape_model_ruby_literal(literal, ruby_mode)
            )
            converted.append(match.group())
            cursor = match.end()
        literal = compact_emphasis_aozora(rendered[cursor:])
        converted.append(
            aozora_to_model_ruby(literal, ruby_mode)
            if "｜" in literal
            else escape_model_ruby_literal(literal, ruby_mode)
        )
        return "".join(converted), entries

    def add_regular(locator: dict[str, Any], text: str | None) -> None:
        if not text:
            return
        if text.isspace() and not semantic_run:
            return
        semantic_run.append((locator, text))

    def display_run(formats: list[dict[str, Any]]) -> str:
        groups: list[tuple[tuple[str, ...], str]] = []
        for index, (_, text) in enumerate(semantic_run):
            signature = tuple(
                str(item["id"])
                for item in formats
                if int(item["start"]) <= index <= int(item["end"])
            )
            if groups and groups[-1][0] == signature:
                groups[-1] = (signature, groups[-1][1] + text)
            else:
                groups.append((signature, text))
        return "".join(
            compact_emphasis_aozora(text) for _, text in groups
        )

    def flush_run() -> None:
        if not semantic_run:
            return
        last_visible = max(
            (
                index
                for index, (_, text) in enumerate(semantic_run)
                if text and not text.isspace()
            ),
            default=-1,
        )
        if last_visible < 0:
            semantic_run.clear()
            return
        del semantic_run[last_visible + 1 :]
        for index, (locator, text) in enumerate(semantic_run):
            if not isinstance(locator, dict) or locator.get("kind") != "ruby":
                continue
            tail = locator.get("tail")
            rendered = locator.get("source")
            if not isinstance(tail, str) or not isinstance(rendered, str):
                raise ProjectError("EPUB Ruby 定位状态缺少源文")
            include_tail = bool(tail) and (
                not tail.isspace() or index < last_visible
            )
            locator["tail_in_source"] = include_tail
            semantic_run[index] = (
                locator,
                rendered + (tail if include_tail else ""),
            )
        if len(semantic_run) == 1:
            locator = semantic_run[0][0]
        else:
            locator = {
                "kind": "composite",
                "slots": [item[0] for item in semantic_run],
            }
        source = "".join(item[1] for item in semantic_run)
        model_source, formats = model_run()
        if ruby_mode in {"aozora", "short_xml", "compact"}:
            compacted_source = display_run(formats)
            if compacted_source != source:
                locator["adapter_source"] = source
            source = compacted_source
        if formats:
            locator["formats"] = formats
        values.append((locator, source, model_source or None))
        semantic_run.clear()
        format_elements.clear()

    def append_ruby(child: ElementTree.Element, child_path: list[int]) -> None:
        ruby = _render_ruby(
            child,
            ruby_mode=ruby_mode,
            location=location,
            path=child_path,
        )
        model_ruby = (
            aozora_to_model_ruby(ruby, ruby_mode)
            if ruby_mode in {"short_xml", "compact"}
            else None
        )
        tail = child.tail or ""
        locator = {
            "path": child_path,
            "kind": "ruby",
            "source": ruby,
            "tail": tail,
            "tail_in_source": False,
        }
        if model_ruby is not None:
            locator["model_source"] = model_ruby
        semantic_run.append(
            (
                locator,
                ruby + tail,
            )
        )

    def visit_inline(element: ElementTree.Element, path: list[int]) -> None:
        if _local_name(element.tag) in _SKIPPED_TEXT_ELEMENTS:
            return
        name = _local_name(element.tag)
        if keep_format(name):
            format_elements.append((path, name))
        add_regular({"path": path, "kind": "text"}, element.text)
        for index, child in enumerate(list(element)):
            child_path = [*path, index]
            name = _local_name(child.tag)
            if name == "ruby":
                append_ruby(child, child_path)
            elif name in _INLINE_TEXT_ELEMENTS:
                visit_inline(child, child_path)
            else:
                flush_run()
                visit(child, child_path)
            if name != "ruby":
                add_regular({"path": child_path, "kind": "tail"}, child.tail)

    def visit(element: ElementTree.Element, path: list[int]) -> None:
        if _local_name(element.tag) in _SKIPPED_TEXT_ELEMENTS:
            return
        add_regular({"path": path, "kind": "text"}, element.text)
        for index, child in enumerate(list(element)):
            child_path = [*path, index]
            name = _local_name(child.tag)
            if name == "ruby":
                append_ruby(child, child_path)
            elif name in _INLINE_TEXT_ELEMENTS:
                visit_inline(child, child_path)
            else:
                flush_run()
                visit(child, child_path)
            if name != "ruby":
                add_regular({"path": child_path, "kind": "tail"}, child.tail)
        flush_run()

    visit(root, [])
    flush_run()
    return values


def _plain_ruby_text(element: ElementTree.Element, location: str) -> str:
    parts: list[str] = []

    def collect(value: ElementTree.Element) -> None:
        if _local_name(value.tag) == "ruby":
            raise ProjectError(f"EPUB Ruby 不支持嵌套 ruby：{location}")
        if value.text:
            parts.append(value.text)
        for child in list(value):
            collect(child)
            if child.tail:
                parts.append(child.tail)

    collect(element)
    return "".join(parts).strip()


def _render_ruby(
    element: ElementTree.Element,
    *,
    ruby_mode: str,
    location: str,
    path: list[int],
) -> str:
    label = f"{location} ruby path {'/'.join(str(value) for value in path)}"
    base_parts: list[str] = []
    if element.text:
        base_parts.append(element.text)
    direct_readings: list[str] = []
    grouped_readings: list[str] = []
    for child in list(element):
        name = _local_name(child.tag)
        if name == "ruby":
            raise ProjectError(f"EPUB Ruby 不支持嵌套 ruby：{label}")
        if name == "rt":
            reading = _plain_ruby_text(child, label)
            if not reading:
                raise ProjectError(f"EPUB Ruby 包含空 rt：{label}")
            direct_readings.append(reading)
        elif name == "rtc":
            rtc_readings = [
                _plain_ruby_text(item, label)
                for item in list(child)
                if _local_name(item.tag) == "rt"
            ]
            if not rtc_readings or any(not value for value in rtc_readings):
                raise ProjectError(f"EPUB Ruby 的 rtc 缺少有效 rt：{label}")
            grouped_readings.append("".join(rtc_readings))
        elif name != "rp":
            base_parts.append(_plain_ruby_text(child, label))
        if child.tail:
            base_parts.append(child.tail)
    base = "".join(base_parts).strip()
    reading_groups = []
    if direct_readings:
        reading_groups.append("".join(direct_readings))
    reading_groups.extend(grouped_readings)
    if not base or not reading_groups:
        raise ProjectError(f"EPUB Ruby 缺少基础文字或读音：{label}")
    reading = "／".join(reading_groups)
    if ruby_mode == "base_only":
        return base
    if ruby_mode == "parenthetical":
        return f"{base}（{reading}）"
    return f"｜{base}《{reading}》"


def _resolve_text_slot(
    root: ElementTree.Element, raw: Any
) -> tuple[ElementTree.Element, str]:
    if not isinstance(raw, dict) or raw.get("kind") not in {"text", "tail"}:
        raise IncompleteError("EPUB Segment 定位 slot 损坏")
    path = raw.get("path")
    if not isinstance(path, list) or not all(
        isinstance(index, int) and index >= 0 for index in path
    ):
        raise IncompleteError("EPUB Segment 定位 path 损坏")
    element = root
    try:
        for index in path:
            element = list(element)[index]
    except IndexError as exc:
        raise IncompleteError("EPUB XHTML 结构与导入状态不一致") from exc
    return element, str(raw["kind"])


def _read_text_slot(root: ElementTree.Element, raw: Any) -> str:
    element, kind = _resolve_text_slot(root, raw)
    return element.text or "" if kind == "text" else element.tail or ""


def _set_text_slot(
    root: ElementTree.Element, raw: Any, value: str
) -> None:
    element, kind = _resolve_text_slot(root, raw)
    if kind == "text":
        element.text = value
    else:
        element.tail = value


def _set_regular_slot(
    root: ElementTree.Element,
    raw: Any,
    source: str,
    target: str,
    *,
    bilingual: bool,
    ruby_mode: str = "",
) -> None:
    if ruby_mode in {"aozora", "short_xml", "compact"}:
        fragments, found_ruby = parse_aozora_text(target)
        if found_ruby:
            _set_regular_aozora(
                root,
                raw,
                source,
                fragments,
                bilingual=bilingual,
            )
            return
    if not isinstance(raw, dict) or raw.get("kind") != "composite":
        value = f"{source}\n{target}" if bilingual else target
        _set_text_slot(root, raw, value)
        return
    slots = raw.get("slots")
    if not isinstance(slots, list) or len(slots) < 2:
        raise IncompleteError("EPUB 复合 Segment 定位损坏")
    if not all(
        isinstance(slot, dict)
        and slot.get("kind") in {"text", "tail"}
        for slot in slots
    ):
        raise IncompleteError("EPUB 复合 Segment 文本槽损坏")
    current = "".join(_read_text_slot(root, slot) for slot in slots)
    if current != source:
        raise IncompleteError("EPUB 复合 Segment 与原文不一致")
    if bilingual:
        last = slots[-1]
        _set_text_slot(root, last, f"{_read_text_slot(root, last)}\n{target}")
        return
    _set_text_slot(root, slots[0], target)
    for slot in slots[1:]:
        _set_text_slot(root, slot, "")


def _set_regular_aozora(
    root: ElementTree.Element,
    raw: Any,
    source: str,
    fragments: list[tuple[str, str, str | None]],
    *,
    bilingual: bool,
) -> None:
    if not isinstance(raw, dict):
        raise IncompleteError("EPUB Segment 定位 slot 损坏")
    if raw.get("kind") == "composite":
        slots = raw.get("slots")
        if not isinstance(slots, list) or len(slots) < 2 or not all(
            isinstance(slot, dict) and slot.get("kind") in {"text", "tail"}
            for slot in slots
        ):
            raise IncompleteError("EPUB 复合 Segment 文本槽损坏")
        members = [("text", _resolve_text_slot(root, slot)) for slot in slots]
    else:
        slots = [raw]
        members = [("text", _resolve_text_slot(root, raw))]
    current = _composite_source(slots, members)
    if current != source:
        raise IncompleteError("EPUB Segment 与原文不一致")
    slot = slots[-1] if bilingual else slots[0]
    parent, index, slot_kind, element = _text_slot_location(root, slot)
    if slot_kind == "text":
        index = len(list(parent))
    if not bilingual:
        for item in slots:
            _set_text_slot(root, item, "")
    inserted = _insert_fragments(
        parent,
        index,
        ([
            ("text", "\n", None),
            *fragments,
        ] if bilingual else fragments),
    )
    del element
    _append_fragment_suffix(parent, index, inserted, "")


def _resolve_ruby_slot(
    root: ElementTree.Element, raw: Any
) -> tuple[ElementTree.Element, ElementTree.Element]:
    if not isinstance(raw, dict) or raw.get("kind") != "ruby":
        raise IncompleteError("EPUB Ruby 定位 slot 损坏")
    path = raw.get("path")
    if (
        not isinstance(path, list)
        or not path
        or not all(isinstance(index, int) and index >= 0 for index in path)
    ):
        raise IncompleteError("EPUB Ruby 定位 path 损坏")
    parent = root
    try:
        for index in path[:-1]:
            parent = list(parent)[index]
        ruby = list(parent)[path[-1]]
    except IndexError as exc:
        raise IncompleteError("EPUB XHTML 结构与导入状态不一致") from exc
    if _local_name(ruby.tag) != "ruby":
        raise IncompleteError("EPUB Ruby 定位未指向 ruby 元素")
    return parent, ruby


def _replace_ruby_element(
    parent: ElementTree.Element,
    ruby: ElementTree.Element,
    raw: dict[str, Any],
    target: str,
) -> None:
    imported_tail = raw.get("tail")
    tail_in_source = raw.get("tail_in_source")
    if not isinstance(imported_tail, str) or not isinstance(tail_in_source, bool):
        raise IncompleteError("EPUB Ruby tail 状态损坏")
    current_tail = ruby.tail or ""
    if tail_in_source:
        if not current_tail.startswith(imported_tail):
            raise IncompleteError("EPUB Ruby tail 与导入状态不一致")
        suffix = current_tail[len(imported_tail):]
    else:
        suffix = current_tail
    value = target + suffix
    path = raw["path"]
    index = path[-1]
    if index == 0:
        parent.text = (parent.text or "") + value
    else:
        previous = list(parent)[index - 1]
        previous.tail = (previous.tail or "") + value


def _resolve_composite_members(
    root: ElementTree.Element, raw: Any
) -> tuple[list[dict[str, Any]], list[tuple[str, Any]]]:
    if not isinstance(raw, dict) or raw.get("kind") != "composite":
        raise IncompleteError("EPUB 复合 Segment 定位损坏")
    slots = raw.get("slots")
    if not isinstance(slots, list) or len(slots) < 2:
        raise IncompleteError("EPUB 复合 Segment 定位损坏")
    members: list[tuple[str, Any]] = []
    for slot in slots:
        if not isinstance(slot, dict):
            raise IncompleteError("EPUB 复合 Segment 槽损坏")
        kind = slot.get("kind")
        if kind in {"text", "tail"}:
            members.append(("text", _resolve_text_slot(root, slot)))
        elif kind == "ruby":
            members.append(("ruby", _resolve_ruby_slot(root, slot)))
        else:
            raise IncompleteError("EPUB 复合 Segment 槽类型无效")
    return slots, members


def _composite_source(
    slots: list[dict[str, Any]],
    members: list[tuple[str, Any]],
) -> str:
    parts: list[str] = []
    for slot, (kind, resolved) in zip(slots, members, strict=True):
        if kind == "text":
            element, slot_kind = resolved
            parts.append(
                element.text or ""
                if slot_kind == "text"
                else element.tail or ""
            )
            continue
        rendered = slot.get("source")
        tail = slot.get("tail")
        tail_in_source = slot.get("tail_in_source")
        if (
            not isinstance(rendered, str)
            or not isinstance(tail, str)
            or not isinstance(tail_in_source, bool)
        ):
            raise IncompleteError("EPUB 复合 Ruby 源文状态损坏")
        if tail_in_source and not (resolved[1].tail or "").startswith(tail):
            raise IncompleteError("EPUB Ruby tail 与导入状态不一致")
        parts.append(rendered + (tail if tail_in_source else ""))
    return "".join(parts)


def _namespace_uri(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def _qualified(tag: str, namespace: str) -> str:
    return f"{{{namespace}}}{tag}" if namespace else tag


def _new_aozora_ruby(
    parent: ElementTree.Element,
    base: str,
    reading: str,
) -> ElementTree.Element:
    namespace = _namespace_uri(parent.tag)
    ruby = ElementTree.Element(_qualified("ruby", namespace))
    ruby.text = base
    rt = ElementTree.SubElement(ruby, _qualified("rt", namespace))
    rt.text = reading
    return ruby


def _insert_fragments(
    parent: ElementTree.Element,
    index: int,
    fragments: list[tuple[str, str, str | None]],
) -> ElementTree.Element | None:
    previous = list(parent)[index - 1] if index else None
    insert_at = index
    last_inserted: ElementTree.Element | None = None
    for kind, text, reading in fragments:
        if kind == "text":
            if last_inserted is not None:
                last_inserted.tail = (last_inserted.tail or "") + text
            elif previous is not None:
                previous.tail = (previous.tail or "") + text
            else:
                parent.text = (parent.text or "") + text
            continue
        if reading is None:
            raise IncompleteError("EPUB Aozora Ruby 片段损坏")
        bases = (
            regex.findall(r"\X", text)
            if reading in EMPHASIS_RUBY_CHARACTERS
            else [text]
        )
        for base in bases:
            if base.isspace():
                if last_inserted is not None:
                    last_inserted.tail = (last_inserted.tail or "") + base
                elif previous is not None:
                    previous.tail = (previous.tail or "") + base
                else:
                    parent.text = (parent.text or "") + base
                continue
            ruby = _new_aozora_ruby(parent, base, reading)
            parent.insert(insert_at, ruby)
            insert_at += 1
            last_inserted = ruby
    return last_inserted


def _append_fragment_suffix(
    parent: ElementTree.Element,
    index: int,
    last_inserted: ElementTree.Element | None,
    suffix: str,
) -> None:
    if not suffix:
        return
    if last_inserted is not None:
        last_inserted.tail = (last_inserted.tail or "") + suffix
    elif index:
        previous = list(parent)[index - 1]
        previous.tail = (previous.tail or "") + suffix
    else:
        parent.text = (parent.text or "") + suffix


def _text_slot_location(
    root: ElementTree.Element,
    raw: dict[str, Any],
) -> tuple[ElementTree.Element, int, str, ElementTree.Element]:
    path = raw.get("path")
    if not isinstance(path, list) or not all(
        isinstance(index, int) and index >= 0 for index in path
    ):
        raise IncompleteError("EPUB Segment 定位 path 损坏")
    element, kind = _resolve_text_slot(root, raw)
    parent = root
    for index in path[:-1]:
        try:
            parent = list(parent)[index]
        except IndexError as exc:
            raise IncompleteError("EPUB XHTML 结构与导入状态不一致") from exc
    index = path[-1]
    if kind == "text":
        return element, 0, kind, element
    return parent, index + 1, kind, element


def _remove_ruby_members(
    members: list[tuple[str, Any]],
) -> None:
    rubies = [resolved for kind, resolved in members if kind == "ruby"]
    for parent, ruby in sorted(
        rubies,
        key=lambda item: (id(item[0]), list(item[0]).index(item[1])),
        reverse=True,
    ):
        parent.remove(ruby)


def _aozora_suffix(
    slots: list[dict[str, Any]],
    members: list[tuple[str, Any]],
) -> str:
    for slot, (kind, resolved) in reversed(list(zip(slots, members, strict=True))):
        if kind != "ruby":
            continue
        if slot.get("tail_in_source") is not False:
            return ""
        tail = slot.get("tail")
        if not isinstance(tail, str):
            raise IncompleteError("EPUB Ruby tail 状态损坏")
        return (resolved[1].tail or "") if resolved[1].tail is not None else tail
    return ""


def _set_composite_aozora(
    root: ElementTree.Element,
    slots: list[dict[str, Any]],
    members: list[tuple[str, Any]],
    target: str,
    *,
    bilingual: bool,
) -> None:
    fragments, found_ruby = parse_aozora_text(target)
    if not found_ruby:
        raise IncompleteError("EPUB Aozora Ruby 解析状态无标记")
    if bilingual:
        last_kind, last_resolved = members[-1]
        suffix = ""
        if last_kind == "ruby":
            _, ruby = last_resolved
            slot = slots[-1]
            tail = ruby.tail or ""
            if slot.get("tail_in_source") is False:
                suffix = tail
                tail = ""
            ruby.tail = tail
            parent = last_resolved[0]
            index = list(parent).index(ruby) + 1
        else:
            slot = slots[-1]
            parent, index, slot_kind, element = _text_slot_location(root, slot)
            if slot_kind == "text":
                index = len(parent)
        inserted = _insert_fragments(
            parent,
            index,
            [("text", "\n", None), *fragments],
        )
        _append_fragment_suffix(parent, index, inserted, suffix)
        return

    first_kind, first_resolved = members[0]
    anchor: ElementTree.Element | None = None
    if first_kind == "ruby":
        parent, ruby = first_resolved
        ruby_index = list(parent).index(ruby)
        anchor = list(parent)[ruby_index - 1] if ruby_index else None
        insertion_parent = parent
        insertion_index = ruby_index
    else:
        slot = slots[0]
        insertion_parent, insertion_index, slot_kind, element = _text_slot_location(
            root, slot
        )
        if slot_kind == "tail":
            anchor = element
    for slot, (kind, _) in zip(slots, members, strict=True):
        if kind == "text":
            _set_text_slot(root, slot, "")
    suffix = _aozora_suffix(slots, members)
    _remove_ruby_members(members)
    if anchor is not None:
        insertion_index = list(insertion_parent).index(anchor) + 1
    else:
        insertion_index = 0 if first_kind == "text" and slots[0].get("kind") == "text" else insertion_index
    inserted = _insert_fragments(insertion_parent, insertion_index, fragments)
    _append_fragment_suffix(insertion_parent, insertion_index, inserted, suffix)


def _set_composite_slot(
    root: ElementTree.Element,
    raw: Any,
    source: str,
    target: str,
    *,
    bilingual: bool,
    ruby_mode: str = "",
) -> None:
    slots, members = _resolve_composite_members(root, raw)
    if not any(kind == "ruby" for kind, _ in members):
        _set_regular_slot(root, raw, source, target, bilingual=bilingual)
        return
    if _composite_source(slots, members) != source:
        raise IncompleteError("EPUB 复合 Segment 与原文不一致")
    if ruby_mode in {"aozora", "short_xml", "compact"}:
        _, found_ruby = parse_aozora_text(target)
        if found_ruby:
            _set_composite_aozora(
                root,
                slots,
                members,
                target,
                bilingual=bilingual,
            )
            return
    if bilingual:
        last_kind, last_resolved = members[-1]
        if last_kind == "ruby":
            _, ruby = last_resolved
            ruby.tail = f"{ruby.tail or ''}\n{target}"
        else:
            element, slot_kind = last_resolved
            current = (
                element.text or ""
                if slot_kind == "text"
                else element.tail or ""
            )
            if slot_kind == "text":
                element.text = f"{current}\n{target}"
            else:
                element.tail = f"{current}\n{target}"
        return

    first_kind = members[0][0]
    if first_kind == "text":
        first_element, first_slot_kind = members[0][1]
        if first_slot_kind == "text":
            first_element.text = target
        else:
            first_element.tail = target
    for index, (kind, resolved) in enumerate(members):
        if kind != "text" or (index == 0 and first_kind == "text"):
            continue
        element, slot_kind = resolved
        if slot_kind == "text":
            element.text = ""
        else:
            element.tail = ""

    ruby_members = [
        (index, slots[index], members[index][1])
        for index, (kind, _) in enumerate(members)
        if kind == "ruby"
    ]
    ordered_rubies = sorted(
        ruby_members,
        key=lambda item: tuple(item[1].get("path", [])),
        reverse=True,
    )
    for index, slot, (parent, ruby) in ordered_rubies:
        _replace_ruby_element(parent, ruby, slot, target if index == 0 else "")
    for _, _, (parent, ruby) in ordered_rubies:
        parent.remove(ruby)


def _set_ruby_slot(
    root: ElementTree.Element,
    raw: Any,
    target: str,
    *,
    bilingual: bool,
    ruby_mode: str = "",
) -> None:
    if not isinstance(raw, dict):
        raise IncompleteError("EPUB Ruby 定位 slot 损坏")
    parent, ruby = _resolve_ruby_slot(root, raw)
    if ruby_mode in {"aozora", "short_xml", "compact"}:
        fragments, found_ruby = parse_aozora_text(target)
        if found_ruby:
            ruby_tail = ruby.tail or ""
            tail_in_source = raw.get("tail_in_source")
            if not isinstance(tail_in_source, bool):
                raise IncompleteError("EPUB Ruby tail 状态损坏")
            suffix = ruby_tail if not tail_in_source else ""
            if bilingual:
                ruby.tail = ruby_tail if tail_in_source else ""
                inserted = _insert_fragments(
                    parent,
                    list(parent).index(ruby) + 1,
                    [("text", "\n", None), *fragments],
                )
                _append_fragment_suffix(
                    parent, list(parent).index(ruby) + 1, inserted, suffix
                )
                return
            index = list(parent).index(ruby)
            previous = list(parent)[index - 1] if index else None
            parent.remove(ruby)
            insertion_index = (
                list(parent).index(previous) + 1
                if previous is not None
                else index
            )
            inserted = _insert_fragments(parent, insertion_index, fragments)
            _append_fragment_suffix(parent, insertion_index, inserted, suffix)
            return
    if bilingual:
        ruby.tail = f"{ruby.tail or ''}\n{target}"
        return
    _replace_ruby_element(parent, ruby, raw, target)
    parent.remove(ruby)
