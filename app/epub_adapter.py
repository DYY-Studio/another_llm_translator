from __future__ import annotations

import posixpath
import stat
import zipfile
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote
from xml.parsers import expat
from xml.etree import ElementTree

from .documents import DocumentChoiceOption, DocumentImport, ImportedFile
from .errors import IncompleteError, ProjectError, UsageError


MAX_EPUB_ENTRIES = 10_000
MAX_EPUB_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_EPUB_COMPRESSION_RATIO = 200
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


class EPUBDocumentAdapter:
    adapter_id = "epub"
    version = "0.3"
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
            xhtml_paths, epub_version = _spine_paths(
                archive, entries, opf_path
            )
            segments: list[str] = []
            segment_part_ids: list[str] = []
            locators: list[dict[str, Any]] = []
            for xhtml_path in xhtml_paths:
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
                    continue
                for locator, source in _text_slots(
                    body, ruby_mode=ruby_mode, location=xhtml_path
                ):
                    segments.append(source)
                    segment_part_ids.append(xhtml_path)
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
                    segment_part_ids=tuple(segment_part_ids),
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
            opf_path = state.get("opf_path")
            if not isinstance(opf_path, str):
                raise IncompleteError("EPUB 状态缺少 OPF 路径")
            _, epub_version = _spine_paths(archive, entries, opf_path)
            for xhtml_path, items in by_xhtml.items():
                if xhtml_path not in entries:
                    raise IncompleteError(
                        f"EPUB 状态引用了缺失资源：{xhtml_path}"
                    )
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
                        str(segment["source"]),
                        target,
                        bilingual=bilingual,
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
                            str(segment["source"]),
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
) -> tuple[list[str], str]:
    root = _parse_xml(archive.read(opf_path), opf_path)
    if _local_name(root.tag) != "package":
        raise ProjectError(f"EPUB OPF 根元素无效：{opf_path}")
    version = root.get("version")
    if version not in {"2.0", "3.0"}:
        raise ProjectError(f"EPUB OPF 版本不支持：{opf_path}")
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
    location: str,
) -> list[tuple[dict[str, Any], str]]:
    values: list[tuple[dict[str, Any], str]] = []
    semantic_run: list[tuple[dict[str, Any], str]] = []

    def add_regular(locator: dict[str, Any], text: str | None) -> None:
        if not text:
            return
        if text.isspace() and not semantic_run:
            return
        semantic_run.append((locator, text))

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
        values.append((locator, "".join(item[1] for item in semantic_run)))
        semantic_run.clear()

    def append_ruby(child: ElementTree.Element, child_path: list[int]) -> None:
        ruby = _render_ruby(
            child,
            ruby_mode=ruby_mode,
            location=location,
            path=child_path,
        )
        tail = child.tail or ""
        semantic_run.append(
            (
                {
                    "path": child_path,
                    "kind": "ruby",
                    "source": ruby,
                    "tail": tail,
                    "tail_in_source": False,
                },
                ruby + tail,
            )
        )

    def visit_inline(element: ElementTree.Element, path: list[int]) -> None:
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
) -> None:
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


_AOZORA_DELIMITERS = frozenset("｜《》\r\n<>")


def _parse_aozora_text(
    value: str,
) -> tuple[list[tuple[str, str, str | None]], bool]:
    """Parse only strict, non-nested Aozora ruby expressions."""
    fragments: list[tuple[str, str, str | None]] = []
    plain_start = 0
    cursor = 0
    found_ruby = False
    while cursor < len(value):
        marker = value.find("｜", cursor)
        if marker < 0:
            break
        opening = value.find("《", marker + 1)
        closing = value.find("》", opening + 1) if opening >= 0 else -1
        if opening < 0 or closing < 0:
            break
        base = value[marker + 1 : opening]
        reading = value[opening + 1 : closing]
        candidate = f"{base}{reading}"
        if (
            not base
            or not reading
            or any(character in _AOZORA_DELIMITERS for character in candidate)
        ):
            cursor = closing + 1
            continue
        if marker > plain_start:
            fragments.append(("text", value[plain_start:marker], None))
        fragments.append(("ruby", base, reading))
        found_ruby = True
        cursor = closing + 1
        plain_start = cursor
    if plain_start < len(value):
        fragments.append(("text", value[plain_start:], None))
    if not fragments:
        fragments.append(("text", value, None))
    return fragments, found_ruby


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
        ruby = _new_aozora_ruby(parent, text, reading)
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
    if not isinstance(path, list) or not path or not all(
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
    fragments, found_ruby = _parse_aozora_text(target)
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
    if ruby_mode == "aozora":
        _, found_ruby = _parse_aozora_text(target)
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
    if ruby_mode == "aozora":
        fragments, found_ruby = _parse_aozora_text(target)
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
