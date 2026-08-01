from __future__ import annotations

import posixpath
import stat
import zipfile
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote
from xml.etree import ElementTree

from .documents import DocumentChoiceOption, DocumentImport, ImportedFile
from .errors import IncompleteError, ProjectError, UsageError


MAX_EPUB_ENTRIES = 10_000
MAX_EPUB_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_EPUB_COMPRESSION_RATIO = 200
_SKIPPED_TEXT_ELEMENTS = {"head", "script", "style", "title"}


class EPUBDocumentAdapter:
    adapter_id = "epub"
    version = "0.2"
    capabilities = frozenset({"import", "translated_export", "bilingual_export"})
    extensions = frozenset({".epub"})
    import_options = (
        DocumentChoiceOption(
            option_id="ruby_mode",
            label="Ruby 表示",
            default="aozora",
            choices=(
                ("aozora", "青空格式｜原文《Ruby》"),
                ("base_only", "仅基础文字"),
                ("parenthetical", "原文（Ruby）"),
            ),
        ),
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
            xhtml_paths = _spine_paths(archive, entries, opf_path)
            segments: list[str] = []
            locators: list[dict[str, Any]] = []
            for xhtml_path in xhtml_paths:
                root = _parse_xml(archive.read(xhtml_path), xhtml_path)
                body = next(
                    (
                        element
                        for element in root.iter()
                        if _local_name(element.tag) == "body"
                    ),
                    None,
                )
                if body is None:
                    continue
                for locator, source in _text_slots(
                    body, ruby_mode=ruby_mode, location=xhtml_path
                ):
                    segments.append(source)
                    locators.append(
                        {"path": xhtml_path, "slot": locator}
                    )
        if not segments:
            raise ProjectError("EPUB spine 中没有可翻译文本")
        return DocumentImport(
            files=(
                ImportedFile(
                    source_path=path,
                    original_name=path.name,
                    segments=tuple(segments),
                    encoding_detected="xhtml",
                    encoding_used="utf-8",
                    encoding_confidence=1.0,
                    opaque_state={
                        "opf_path": opf_path,
                        "spine_paths": xhtml_paths,
                        "locators": locators,
                        "ruby_mode": ruby_mode,
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
        opaque_state: dict[str, Any] | None,
    ) -> list[Path]:
        del output_encoding
        if opaque_state is None:
            raise IncompleteError("EPUB 文件缺少 Document Adapter 状态")
        state = deepcopy(opaque_state)
        locators = state.get("locators")
        if not isinstance(locators, list) or len(locators) != len(segments):
            raise IncompleteError("EPUB Segment 定位状态与项目不一致")
        stored_name = str(file["stored_name"])
        source_path = project / "input" / stored_name
        ordered_segments = sorted(
            segments, key=lambda value: int(value["line_index"])
        )
        by_xhtml: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
        for segment, locator in zip(ordered_segments, locators, strict=True):
            if not isinstance(locator, dict) or not isinstance(
                locator.get("path"), str
            ):
                raise IncompleteError("EPUB Segment 定位状态损坏")
            if not isinstance(locator.get("slot"), dict):
                raise IncompleteError("EPUB Segment 定位 slot 损坏")
            by_xhtml.setdefault(locator["path"], []).append((segment, locator))
        changed: dict[str, bytes] = {}
        with zipfile.ZipFile(source_path) as archive:
            entries = _validated_entries(archive)
            for xhtml_path, items in by_xhtml.items():
                if xhtml_path not in entries:
                    raise IncompleteError(
                        f"EPUB 状态引用了缺失资源：{xhtml_path}"
                    )
                root = _parse_xml(archive.read(xhtml_path), xhtml_path)
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
                if bilingual:
                    style = body.get("style", "")
                    addition = "white-space: pre-line"
                    body.set(
                        "style",
                        f"{style.rstrip(';')}; {addition}".lstrip("; "),
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
                regular = [
                    item
                    for item in resolved
                    if item[1]["slot"].get("kind") != "ruby"
                ]
                rubies = [
                    item
                    for item in resolved
                    if item[1]["slot"].get("kind") == "ruby"
                ]
                for segment, locator, target in regular:
                    source = str(segment["source"])
                    value = f"{source}\n{target}" if bilingual else target
                    _set_text_slot(body, locator.get("slot"), value)
                ruby_items = rubies if bilingual else sorted(
                    rubies,
                    key=lambda item: tuple(item[1]["slot"].get("path", [])),
                    reverse=True,
                )
                for _, locator, target in ruby_items:
                    _set_ruby_slot(
                        body,
                        locator.get("slot"),
                        target,
                        bilingual=bilingual,
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


def _parse_xml(payload: bytes, location: str) -> ElementTree.Element:
    lowered = payload.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ProjectError(f"EPUB XML 不允许 DTD 或实体声明：{location}")
    try:
        return ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise ProjectError(f"EPUB XML 无效：{location}: {exc}") from exc


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


def _spine_paths(
    archive: zipfile.ZipFile,
    entries: dict[str, zipfile.ZipInfo],
    opf_path: str,
) -> list[str]:
    root = _parse_xml(archive.read(opf_path), opf_path)
    manifest: dict[str, tuple[str, str]] = {}
    spine: list[str] = []
    for element in root.iter():
        name = _local_name(element.tag)
        if name == "item" and element.get("id") and element.get("href"):
            manifest[str(element.get("id"))] = (
                str(element.get("href")),
                str(element.get("media-type", "")),
            )
        elif name == "itemref" and element.get("idref"):
            spine.append(str(element.get("idref")))
    base = posixpath.dirname(opf_path)
    paths = []
    for item_id in spine:
        item = manifest.get(item_id)
        if item is None:
            raise ProjectError(f"EPUB spine 引用了未知 manifest ID：{item_id}")
        href, media_type = item
        if media_type != "application/xhtml+xml":
            continue
        resolved = _safe_archive_path(base, unquote(href.split("#", 1)[0]))
        if resolved not in entries:
            raise ProjectError(f"EPUB spine 资源不存在：{resolved}")
        paths.append(resolved)
    if not paths:
        raise ProjectError("EPUB spine 没有 XHTML 内容")
    return paths


def _safe_archive_path(base: str, value: str) -> str:
    if not value or value.startswith("/") or "\\" in value:
        raise ProjectError(f"EPUB 资源路径无效：{value}")
    resolved = posixpath.normpath(posixpath.join(base, value))
    if resolved == ".." or resolved.startswith("../"):
        raise ProjectError(f"EPUB 资源路径越界：{value}")
    return resolved


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _text_slots(
    root: ElementTree.Element,
    *,
    ruby_mode: str,
    location: str,
) -> list[tuple[dict[str, Any], str]]:
    values: list[tuple[dict[str, Any], str]] = []

    def visit(element: ElementTree.Element, path: list[int]) -> None:
        if _local_name(element.tag) in _SKIPPED_TEXT_ELEMENTS:
            return
        if element.text and not element.text.isspace():
            values.append(({"path": path, "kind": "text"}, element.text))
        for index, child in enumerate(list(element)):
            child_path = [*path, index]
            if _local_name(child.tag) == "ruby":
                ruby = _render_ruby(
                    child,
                    ruby_mode=ruby_mode,
                    location=location,
                    path=child_path,
                )
                tail = child.tail or ""
                tail_in_source = bool(tail and not tail.isspace())
                values.append(
                    (
                        {
                            "path": child_path,
                            "kind": "ruby",
                            "tail": tail,
                            "tail_in_source": tail_in_source,
                        },
                        ruby + (tail if tail_in_source else ""),
                    )
                )
                continue
            visit(child, child_path)
            if child.tail and not child.tail.isspace():
                values.append(
                    (
                        {"path": child_path, "kind": "tail"},
                        child.tail,
                    )
                )

    visit(root, [])
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


def _set_text_slot(
    root: ElementTree.Element, raw: Any, value: str
) -> None:
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
    if raw["kind"] == "text":
        element.text = value
    else:
        element.tail = value


def _set_ruby_slot(
    root: ElementTree.Element,
    raw: Any,
    target: str,
    *,
    bilingual: bool,
) -> None:
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
        raise IncompleteError("EPUB XHTML 结构与 Ruby 导入状态不一致") from exc
    if _local_name(ruby.tag) != "ruby":
        raise IncompleteError("EPUB Ruby 定位未指向 ruby 元素")
    if bilingual:
        ruby.tail = f"{ruby.tail or ''}\n{target}"
        return
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
    index = path[-1]
    if index == 0:
        parent.text = (parent.text or "") + value
    else:
        previous = list(parent)[index - 1]
        previous.tail = (previous.tail or "") + value
    parent.remove(ruby)
