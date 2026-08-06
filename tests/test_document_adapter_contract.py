from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

import httpx
import pytest

from app.documents import DocumentChoiceOption, DocumentImport, ImportedFile
from app.errors import IncompleteError, UsageError
from app.execution import Scope, stage_fingerprint
from app.plugins import PLUGIN_PROTOCOL_VERSION, PluginDescriptor
from app.project import init_project, load_segments, load_source_files
from app.sqlite_storage import read_json, write_json
from app.stages import _project_context, export_project, run_translation
from tests.helpers import llm_jsonl
from tests.test_documents import FakeEntryPoint
from tests.test_foundation import make_app_root

_RECORD_MARKER_RE = re.compile(r"</?k\d+>")


class RecordDocumentAdapter:
    adapter_id = "record"
    version = "1"
    capabilities = frozenset({"import", "translated_export", "bilingual_export"})
    extensions = frozenset({".rec"})
    import_options = (
        DocumentChoiceOption(
            option_id="source_style",
            label="来源样式",
            default="plain",
            choices=(("plain", "纯文本"), ("marked", "受控标记")),
        ),
    )
    run_options = (
        DocumentChoiceOption(
            option_id="line_ending",
            label="行尾",
            default="lf",
            choices=(("lf", "LF"), ("crlf", "CRLF")),
        ),
    )

    def normalize_model_output(
        self, *, segment: dict[str, object], text: str, stage: str
    ) -> str:
        del segment, stage
        parts: list[str] = []
        stack: list[str] = []
        cursor = 0
        for match in _RECORD_MARKER_RE.finditer(text):
            literal = text[cursor : match.start()]
            if "<" in literal or ">" in literal:
                raise IncompleteError("Record 内联标记输出包含未知字符")
            parts.append(literal)
            marker = match.group()
            if marker.startswith("</"):
                if not stack:
                    raise IncompleteError("Record 内联标记输出顺序无效")
                stack.pop()
            else:
                stack.append(marker)
            cursor = match.end()
        tail = text[cursor:]
        if "<" in tail or ">" in tail:
            raise IncompleteError("Record 内联标记输出包含未知字符")
        parts.append(tail)
        if stack:
            raise IncompleteError("Record 内联标记输出缺少闭合标记")
        return "".join(parts)

    def import_sources(
        self,
        inputs: list[str],
        *,
        recursive: bool,
        config: dict[str, object],
        options: dict[str, str],
    ) -> DocumentImport:
        del config
        if recursive:
            raise UsageError("Record Adapter 不支持目录递归发现")
        if len(inputs) != 1:
            raise UsageError("Record Adapter 每个项目只接受一个文件")
        path = Path(inputs[0])
        if path.is_symlink() or not path.is_file():
            raise UsageError(f"Record 输入不存在或是符号链接：{path}")
        if path.suffix.casefold() not in self.extensions:
            raise UsageError(f"Record Adapter 只接受 {sorted(self.extensions)} 文件：{path}")
        source_style = options["source_style"]
        line_ending = options.get("line_ending", "lf")
        header: dict[str, str] = {}
        segments: list[str] = []
        parts: list[str] = []
        model_sources: list[str | None] = []
        current_part = "a"
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                key, _, value = line[2:].partition(":")
                if key.strip() and value.strip():
                    header[key.strip()] = value.strip()
                continue
            if line == "---":
                current_part = "b"
                continue
            segments.append(line)
            parts.append(current_part)
            model_sources.append(
                f"<k{len(segments)}>{line}</k{len(segments)}>"
                if source_style == "marked" and line.strip()
                else None
            )
        if not segments:
            raise UsageError(f"Record 文件没有内容行：{path}")
        return DocumentImport(
            files=(
                ImportedFile(
                    source_path=path,
                    original_name=path.name,
                    segments=tuple(segments),
                    model_sources=tuple(model_sources),
                    segment_part_ids=tuple(parts),
                    encoding_detected="plain",
                    encoding_used="utf-8",
                    encoding_confidence=1.0,
                    opaque_state={
                        "name": header.get("name"),
                        "line_ending": line_ending,
                    },
                ),
            ),
        )

    def export_sources(
        self,
        *,
        project: Path,
        staging_dir: Path,
        file: dict[str, object],
        segments: list[dict[str, object]],
        output_text: dict[str, str],
        bilingual: bool,
        output_encoding: str,
        opaque_state: dict[str, object] | None,
    ) -> list[Path]:
        del project
        if not isinstance(opaque_state, dict):
            raise IncompleteError("Record 文件缺少 Document Adapter 状态")
        line_ending = opaque_state.get("line_ending")
        if line_ending not in {"lf", "crlf"}:
            raise IncompleteError("Record 状态缺少有效 line_ending")
        name = opaque_state.get("name")
        if name is not None and not isinstance(name, str):
            raise IncompleteError("Record 状态 name 无效")
        lines: list[str] = []
        if name:
            lines.append(f"# name: {name}")
        for segment in sorted(
            segments, key=lambda item: int(item["line_index"])
        ):
            if segment["is_empty"]:
                lines.append("")
            elif bilingual:
                lines.append(str(segment["source"]))
                lines.append(output_text[str(segment["segment_id"])])
            else:
                lines.append(output_text[str(segment["segment_id"])])
        payload = "\n".join(lines)
        if line_ending == "crlf":
            payload = payload.replace("\n", "\r\n")
        relative = Path(str(file["original_name"]))
        destination = staging_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            destination.write_bytes(
                payload.encode(output_encoding, errors="strict")
            )
        except UnicodeEncodeError as exc:
            raise IncompleteError(
                f"输出编码 {output_encoding} 无法表示 {relative}: {exc}"
            ) from exc
        return [relative]


class ImportOnlyRecordAdapter(RecordDocumentAdapter):
    adapter_id = "frozen"
    version = "1"
    capabilities = frozenset({"import"})
    extensions = frozenset({".frz"})
    import_options = ()
    run_options = ()

    def import_sources(
        self,
        inputs: list[str],
        *,
        recursive: bool,
        config: dict[str, object],
        options: dict[str, str],
    ) -> DocumentImport:
        del options
        return super().import_sources(
            inputs, recursive=recursive, config=config, options={"source_style": "plain"}
        )


def register_plugin(
    monkeypatch: pytest.MonkeyPatch, *adapters: object
) -> PluginDescriptor:
    descriptor = PluginDescriptor(
        plugin_id="fixture-record",
        version="1",
        protocol_version=PLUGIN_PROTOCOL_VERSION,
        document_adapters=tuple(adapters),
    )
    monkeypatch.setattr(
        "app.plugins.entry_points",
        lambda **_: [FakeEntryPoint(descriptor)],
    )
    return descriptor


def write_record(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def test_contract_import_by_extension_stores_file_and_segments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_plugin(monkeypatch, RecordDocumentAdapter())
    app_root = make_app_root(tmp_path)
    source = tmp_path / "book.rec"
    write_record(source, "# name: demo\nline one\n\n---\nline three")

    project, summary = init_project(
        [str(source)],
        name="demo",
        app_root=app_root,
        projects_root=tmp_path / "projects",
        document_adapter_id=None,
    )

    assert project is not None
    assert summary["document_adapter"] == "record"
    assert summary["file_count"] == 1
    assert summary["segment_count"] == 3
    files = load_source_files(project)
    assert files[0]["document_adapter_id"] == "record"
    assert files[0]["document_adapter_version"] == "1"
    assert files[0]["document_adapter_state"] == (
        "source/adapters/record/F0001.json"
    )
    segments = load_segments(project)
    assert [item["part_id"] for item in segments] == ["a", "a", "b"]
    assert "model_source" not in segments[0]
    state = read_json(project, project / "source/adapters/record/F0001.json")
    assert state["adapter_id"] == "record"
    assert state["adapter_version"] == "1"
    assert state["file_id"] == "F0001"
    assert state["state"] == {"name": "demo", "line_ending": "lf"}


def test_contract_import_by_id_applies_options_and_model_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_plugin(monkeypatch, RecordDocumentAdapter())
    app_root = make_app_root(tmp_path)
    source = tmp_path / "book.rec"
    write_record(source, "line one\n\nline three")

    project, _ = init_project(
        [str(source)],
        name="demo",
        app_root=app_root,
        projects_root=tmp_path / "projects",
        document_adapter_id="record",
        adapter_options={
            "record": {"source_style": "marked", "line_ending": "crlf"}
        },
    )

    assert project is not None
    segments = load_segments(project)
    assert segments[0]["model_source"] == "<k1>line one</k1>"
    assert "model_source" not in segments[1]
    assert segments[2]["model_source"] == "<k3>line three</k3>"
    state = read_json(project, project / "source/adapters/record/F0001.json")
    assert state["state"] == {"name": None, "line_ending": "crlf"}


def test_contract_rejects_unknown_or_invalid_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_plugin(monkeypatch, RecordDocumentAdapter())
    app_root = make_app_root(tmp_path)
    source = tmp_path / "book.rec"
    write_record(source, "line one")
    with pytest.raises(UsageError, match="未知导入选项"):
        init_project(
            [str(source)],
            name="demo",
            app_root=app_root,
            projects_root=tmp_path / "projects",
            document_adapter_id="record",
            adapter_options={"record": {"nope": "x"}},
        )
    with pytest.raises(UsageError, match="取值无效"):
        init_project(
            [str(source)],
            name="demo",
            app_root=app_root,
            projects_root=tmp_path / "projects",
            document_adapter_id="record",
            adapter_options={"record": {"source_style": "bogus"}},
        )


def test_contract_missing_adapter_fails_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_plugin(monkeypatch, RecordDocumentAdapter())
    app_root = make_app_root(tmp_path)
    source = tmp_path / "book.rec"
    write_record(source, "line one")
    with pytest.raises(UsageError, match="未安装 Document Adapter"):
        init_project(
            [str(source)],
            name="demo",
            app_root=app_root,
            projects_root=tmp_path / "projects",
            document_adapter_id="missing",
        )


def test_contract_translation_uses_model_source_and_normalizes_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_plugin(monkeypatch, RecordDocumentAdapter())
    app_root = make_app_root(tmp_path)
    source = tmp_path / "book.rec"
    write_record(source, "# name: demo\nline one\n\n---\nline three")
    project, _ = init_project(
        [str(source)],
        name="demo",
        app_root=app_root,
        projects_root=tmp_path / "projects",
        document_adapter_id="record",
        adapter_options={"record": {"source_style": "marked"}},
    )
    assert project is not None
    seen_sources: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        payload = json.loads(body["messages"][1]["content"])
        records = []
        for index, item in enumerate(payload["segments"]):
            seen_sources.append(str(item["source"]))
            cleaned = _RECORD_MARKER_RE.sub("", str(item["source"]))
            records.append(
                {
                    "type": "segment",
                    "id": item["id"],
                    "translation": f"<k{index + 1}>译文:{cleaned}</k{index + 1}>",
                }
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
        )

    os.environ["LLM_API_KEY"] = "test"
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = asyncio.run(run_translation(project, Scope(), http_client=client))
    finally:
        os.environ.pop("LLM_API_KEY", None)
        asyncio.run(client.aclose())

    assert summary["completed"] == 2
    assert seen_sources == ["<k1>line one</k1>", "<k3>line three</k3>"]

    result = export_project(
        project, "translated", bilingual=False, allow_missing=False
    )
    assert result["files"] == 1
    written = project / "output" / "translated" / "book.rec"
    assert written.read_text(encoding="utf-8-sig") == (
        "# name: demo\n译文:line one\n\n译文:line three"
    )

    bilingual = export_project(
        project, "translated", bilingual=True, allow_missing=False
    )
    assert bilingual["files"] == 1
    paired = project / "output" / "bilingual" / "translated" / "book.rec"
    assert paired.read_text(encoding="utf-8-sig") == (
        "# name: demo\nline one\n译文:line one\n\nline three\n译文:line three"
    )


def test_contract_run_options_baked_at_import_drive_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_plugin(monkeypatch, RecordDocumentAdapter())
    app_root = make_app_root(tmp_path)
    source = tmp_path / "book.rec"
    write_record(source, "# name: demo\nline one\nline two")
    project, _ = init_project(
        [str(source)],
        name="demo",
        app_root=app_root,
        projects_root=tmp_path / "projects",
        document_adapter_id="record",
        adapter_options={"record": {"line_ending": "crlf"}},
    )
    assert project is not None

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        payload = json.loads(body["messages"][1]["content"])
        records = [
            {
                "type": "segment",
                "id": item["id"],
                "translation": f"译文:{item['source']}",
            }
            for item in payload["segments"]
        ]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
        )

    os.environ["LLM_API_KEY"] = "test"
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = asyncio.run(run_translation(project, Scope(), http_client=client))
    finally:
        os.environ.pop("LLM_API_KEY", None)
        asyncio.run(client.aclose())
    assert summary["completed"] == 2

    export_project(project, "translated", bilingual=False, allow_missing=False)
    written = project / "output" / "translated" / "book.rec"
    assert written.read_bytes() == (
        "\ufeff# name: demo\r\n译文:line one\r\n译文:line two".encode("utf-8")
    )


def test_contract_version_mismatch_blocks_export_without_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = register_plugin(monkeypatch, RecordDocumentAdapter())
    app_root = make_app_root(tmp_path)
    source = tmp_path / "book.rec"
    write_record(source, "line one")
    project, _ = init_project(
        [str(source)],
        name="demo",
        app_root=app_root,
        projects_root=tmp_path / "projects",
        document_adapter_id="record",
    )
    assert project is not None
    descriptor.document_adapters[0].version = "2"

    with pytest.raises(IncompleteError, match="版本不兼容"):
        export_project(
            project, "translated", bilingual=False, allow_missing=True
        )
    assert not (project / "output" / "translated").exists()


def test_contract_corrupt_state_blocks_export_without_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_plugin(monkeypatch, RecordDocumentAdapter())
    app_root = make_app_root(tmp_path)
    source = tmp_path / "book.rec"
    write_record(source, "line one")
    project, _ = init_project(
        [str(source)],
        name="demo",
        app_root=app_root,
        projects_root=tmp_path / "projects",
        document_adapter_id="record",
    )
    assert project is not None
    write_json(
        project,
        project / "source/adapters/record/F0001.json",
        {
            "schema_version": 1,
            "adapter_id": "record",
            "adapter_version": "1",
            "file_id": "F0001",
            "state": {"bad": True},
        },
    )

    with pytest.raises(IncompleteError, match="line_ending"):
        export_project(
            project, "translated", bilingual=False, allow_missing=True
        )
    assert not (project / "output" / "translated").exists()


def test_contract_missing_export_capability_blocks_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_plugin(monkeypatch, ImportOnlyRecordAdapter())
    app_root = make_app_root(tmp_path)
    source = tmp_path / "book.frz"
    write_record(source, "line one")
    project, _ = init_project(
        [str(source)],
        name="demo",
        app_root=app_root,
        projects_root=tmp_path / "projects",
        document_adapter_id=None,
    )
    assert project is not None
    assert load_source_files(project)[0]["document_adapter_id"] == "frozen"

    with pytest.raises(IncompleteError, match="不支持此导出模式"):
        export_project(
            project, "translated", bilingual=False, allow_missing=True
        )
    assert not (project / "output" / "translated").exists()


def test_contract_fingerprint_tracks_adapter_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_plugin(monkeypatch, RecordDocumentAdapter())
    app_root = make_app_root(tmp_path)
    source = tmp_path / "book.rec"
    write_record(source, "line one")
    project, _ = init_project(
        [str(source)],
        name="demo",
        app_root=app_root,
        projects_root=tmp_path / "projects",
        document_adapter_id="record",
    )
    assert project is not None
    config, _, _, _ = _project_context(project)
    assert config["_document_adapters"] == {
        "F0001": {"adapter_id": "record", "version": "1"}
    }
    base = stage_fingerprint(config, "translation", "prompt")
    config["_document_adapters"]["F0001"]["version"] = "2"
    assert stage_fingerprint(config, "translation", "prompt") != base
