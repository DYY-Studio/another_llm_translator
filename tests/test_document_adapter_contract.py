from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.errors import ConfigError, IncompleteError, UsageError
from app.execution import Scope, create_run, stage_fingerprint, stage_result_path
from app.plugin_api import (
    DocumentChoiceOption,
    DocumentImport,
    ImportedFile,
    PLUGIN_PROTOCOL_VERSION,
    PluginDescriptor,
)
from app.main import run
from app.plugins import (
    document_adapter_replacement_options,
)
from app.project import (
    _import_project_inputs,
    file_run_options,
    init_project,
    load_segments,
    load_source_files,
    update_file_run_options,
)
from app.project_export import export_project
from app.sqlite_storage import append_jsonl, read_json, record_header, write_json
from app.stage_runtime import _project_context
from app.stage_translation import run_translation
from app.web import create_app
from app.web_store import WebStore
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

    def __init__(self) -> None:
        self.export_languages: list[str] = []
        self.export_language_tags: list[str] = []

    def model_prompt_requirements(
        self,
        *,
        stage: str,
        language: str,
        opaque_state: dict[str, object] | None,
        run_options: dict[str, str],
    ) -> str | None:
        del stage, language, opaque_state, run_options
        return None

    def render_model_source(
        self,
        *,
        segment: dict[str, object],
        opaque_state: dict[str, object] | None,
        run_options: dict[str, str],
    ) -> str:
        del opaque_state
        source = str(segment["source"])
        if run_options["line_ending"] == "crlf" and source.strip():
            return f"<k{int(segment['line_index']) + 1}>{source}</k{int(segment['line_index']) + 1}>"
        return source

    def segment_format_count(
        self,
        *,
        segment: dict[str, object],
        opaque_state: dict[str, object] | None,
    ) -> int:
        del segment, opaque_state
        return 0

    def replacement_options(
        self, *, opaque_state: dict[str, object] | None
    ) -> dict[str, str]:
        if not isinstance(opaque_state, dict):
            raise IncompleteError("Record 文件缺少 Document Adapter 状态")
        return {
            "source_style": str(opaque_state.get("source_style", "plain")),
        }

    def normalize_model_output(
        self, *, segment: dict[str, object], text: str, stage: str,
        opaque_state: dict[str, object] | None, run_options: dict[str, str]
    ) -> str:
        del segment, stage, opaque_state, run_options
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
                        "line_ending": "lf",
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
        target_language: str,
        target_language_tag: str,
        opaque_state: dict[str, object] | None,
    ) -> list[Path]:
        del project
        self.export_languages.append(target_language)
        self.export_language_tags.append(target_language_tag)
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


class StatelessRunOptionsAdapter(RecordDocumentAdapter):
    adapter_id = "stateless-record"
    extensions = frozenset({".srec"})
    import_options = ()
    run_options = (
        DocumentChoiceOption(
            option_id="mode",
            label="模式",
            default="normal",
            choices=(("normal", "普通"), ("strict", "严格")),
        ),
    )

    def __init__(self) -> None:
        super().__init__()
        self.export_states: list[dict[str, object] | None] = []

    def import_sources(
        self,
        inputs: list[str],
        *,
        recursive: bool,
        config: dict[str, object],
        options: dict[str, str],
    ) -> DocumentImport:
        del recursive, config, options
        path = Path(inputs[0])
        segments = tuple(path.read_text(encoding="utf-8").splitlines())
        return DocumentImport(
            files=(
                ImportedFile(
                    source_path=path,
                    original_name=path.name,
                    segments=segments,
                    segment_part_ids=tuple("document" for _ in segments),
                    encoding_detected="plain",
                    encoding_used="utf-8",
                    encoding_confidence=1.0,
                    opaque_state=None,
                ),
            ),
        )

    def replacement_options(
        self, *, opaque_state: dict[str, object] | None
    ) -> dict[str, str]:
        del opaque_state
        return {}

    def render_model_source(
        self,
        *,
        segment: dict[str, object],
        opaque_state: dict[str, object] | None,
        run_options: dict[str, str],
    ) -> str:
        del opaque_state, run_options
        return str(segment["source"])

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
        target_language: str,
        target_language_tag: str,
        opaque_state: dict[str, object] | None,
    ) -> list[Path]:
        del project, bilingual, output_encoding, target_language, target_language_tag
        self.export_states.append(opaque_state)
        relative = Path(str(file["original_name"]))
        destination = staging_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            "\n".join(output_text[str(item["segment_id"])] for item in segments),
            encoding="utf-8",
        )
        return [relative]


class OpaqueLocatorRecordAdapter(StatelessRunOptionsAdapter):
    adapter_id = "opaque-locator-record"
    extensions = frozenset({".olr"})

    def import_sources(
        self,
        inputs: list[str],
        *,
        recursive: bool,
        config: dict[str, object],
        options: dict[str, str],
    ) -> DocumentImport:
        imported = super().import_sources(
            inputs,
            recursive=recursive,
            config=config,
            options=options,
        )
        item = imported.files[0]
        return DocumentImport(
            files=(
                replace(
                    item,
                    opaque_state={"locators": {"private": "state"}},
                ),
            ),
        )


class ReplacementStateProbeAdapter(StatelessRunOptionsAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.replacement_states: list[dict[str, object] | None] = []

    def replacement_options(
        self, *, opaque_state: dict[str, object] | None
    ) -> dict[str, str]:
        self.replacement_states.append(opaque_state)
        return {}


def test_contract_stateless_run_options_persist_and_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_plugin(monkeypatch, StatelessRunOptionsAdapter())
    source = tmp_path / "book.srec"
    source.write_text("line one\nline two", encoding="utf-8")
    project, _ = init_project(
        [str(source)],
        name="stateless",
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
        document_adapter_id="stateless-record",
    )
    assert project is not None

    file_record = load_source_files(project)[0]
    assert file_record["document_adapter_state"] == (
        "source/adapters/stateless-record/F0001.json"
    )
    state = read_json(project, project / str(file_record["document_adapter_state"]))
    assert state["state"] is None
    assert state["run_options"] == {"mode": "normal"}
    assert file_run_options(project, "F0001") == {"mode": "normal"}

    before = load_segments(project)
    assert update_file_run_options(project, "F0001", {"mode": "strict"}) == {
        "mode": "strict"
    }
    assert load_segments(project) == before
    state = read_json(project, project / str(file_record["document_adapter_state"]))
    assert state["state"] is None
    assert state["run_options"] == {"mode": "strict"}


def test_contract_stateless_run_options_are_frozen_in_run_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_plugin(monkeypatch, StatelessRunOptionsAdapter())
    source = tmp_path / "book.srec"
    source.write_text("line one", encoding="utf-8")
    project, _ = init_project(
        [str(source)],
        name="stateless-run",
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
        document_adapter_id="stateless-record",
        adapter_options={"stateless-record": {"mode": "strict"}},
    )
    assert project is not None
    config, metadata, _, segments = _project_context(project, stage="translation")
    run_id, run_dir = create_run(
        project,
        config=config,
        stage="translation",
        fingerprint="test-fingerprint",
        prompt=None,
        selected_count=1,
        requested_count=1,
        reused_count=0,
    )
    assert run_id
    assert metadata["project_id"]
    assert segments[0]["_adapter_run_options"] == {"mode": "strict"}
    manifest = read_json(project, run_dir / "manifest.json")
    assert manifest["document_adapter_options"] == {
        "F0001": {"mode": "strict"}
    }


def test_contract_stateless_state_null_reaches_export_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = StatelessRunOptionsAdapter()
    register_plugin(monkeypatch, adapter)
    source = tmp_path / "book.srec"
    source.write_text("line one", encoding="utf-8")
    project, _ = init_project(
        [str(source)],
        name="stateless-export",
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
        document_adapter_id="stateless-record",
    )
    assert project is not None
    metadata = read_json(project, project / "project.json")
    segment = load_segments(project)[0]
    append_jsonl(
        project,
        stage_result_path(project, "translation"),
        record_header(
            "stage_result",
            str(metadata["project_id"]),
            stage="translation",
            segment_id=segment["segment_id"],
            status="completed",
            text="translated",
            validation_status="passed",
            validation_findings=[],
            stage_fingerprint="sha256:test",
            terms_revision=0,
            run_id="RUN-TEST",
            request_id="REQ-TEST",
        ),
    )

    result = export_project(
        project, "translated", bilingual=False, allow_missing=False
    )

    assert (project / result["written"][0]).read_text(encoding="utf-8") == "translated"
    assert adapter.export_states == [None]


def test_web_replacement_options_passes_only_opaque_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = ReplacementStateProbeAdapter()
    register_plugin(monkeypatch, adapter)
    source = tmp_path / "book.srec"
    source.write_text("line one", encoding="utf-8")
    app_root = make_app_root(tmp_path)
    project, _ = init_project(
        [str(source)],
        name="stateless-web-replacement",
        app_root=app_root,
        projects_root=tmp_path / "projects",
        document_adapter_id="stateless-record",
    )
    assert project is not None

    client = TestClient(
        create_app(
            projects_root=project.parent,
            app_root=app_root,
        )
    )
    response = client.get(
        "/api/v1/projects/stateless-web-replacement/files/F0001/"
        "replacement-options"
    )

    assert response.status_code == 200
    assert adapter.replacement_states == [None]


def test_replacement_import_accepts_replacement_choices_only_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class LegacySourceStyleAdapter(RecordDocumentAdapter):
        adapter_id = "legacy-record"
        extensions = frozenset({".lrec"})
        import_options = (
            DocumentChoiceOption(
                option_id="source_style",
                label="来源样式",
                default="plain",
                choices=(("plain", "纯文本"), ("marked", "受控标记")),
                replacement_choices=(("legacy", "旧样式"),),
            ),
        )

    register_plugin(monkeypatch, LegacySourceStyleAdapter())
    source = tmp_path / "book.lrec"
    write_record(source, "line one")
    options = {"legacy-record": {"source_style": "legacy"}}

    with pytest.raises(UsageError, match="取值无效"):
        _import_project_inputs(
            [str(source)],
            recursive=False,
            config={},
            document_adapter_id="legacy-record",
            adapter_options=options,
        )
    imported, _ = _import_project_inputs(
        [str(source)],
        recursive=False,
        config={},
        document_adapter_id="legacy-record",
        adapter_options=options,
        allow_replacement_choices=True,
    )
    assert imported[0][1].opaque_state == {"name": None, "line_ending": "lf"}


@pytest.mark.parametrize(
    "run_options",
    [
        {},
        {"mode": "normal", "unknown": "x"},
        {"mode": "invalid"},
    ],
)
def test_contract_persisted_run_options_must_be_complete_and_valid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_options: dict[str, str],
) -> None:
    register_plugin(monkeypatch, StatelessRunOptionsAdapter())
    source = tmp_path / "book.srec"
    source.write_text("line one", encoding="utf-8")
    project, _ = init_project(
        [str(source)],
        name="invalid-state",
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
        document_adapter_id="stateless-record",
    )
    assert project is not None
    file_record = load_source_files(project)[0]
    state_path = project / str(file_record["document_adapter_state"])
    state = read_json(project, state_path)
    state["run_options"] = run_options
    write_json(project, state_path, state)

    with pytest.raises(ConfigError, match="run_options"):
        file_run_options(project, "F0001")
    with pytest.raises(ConfigError, match="run_options"):
        _project_context(
            project,
            stage="translation",
            frozen_run_options={"F0001": run_options},
        )


def test_contract_render_model_source_must_return_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class InvalidRenderer(StatelessRunOptionsAdapter):
        def render_model_source(
            self,
            *,
            segment: dict[str, object],
            opaque_state: dict[str, object] | None,
            run_options: dict[str, str],
        ) -> str:
            del segment, opaque_state, run_options
            return None  # type: ignore[return-value]

    register_plugin(monkeypatch, InvalidRenderer())
    source = tmp_path / "book.srec"
    source.write_text("line one", encoding="utf-8")
    project, _ = init_project(
        [str(source)],
        name="invalid-renderer",
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
        document_adapter_id="stateless-record",
    )
    assert project is not None

    with pytest.raises(ConfigError, match="render_model_source"):
        _project_context(project, stage="translation")


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


def test_contract_requires_runtime_model_renderer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Document adapters must provide the v12 runtime render boundary."""
    class MissingRenderer(RecordDocumentAdapter):
        render_model_source = None

    register_plugin(monkeypatch, MissingRenderer())

    from app.plugins import load_plugins

    with pytest.raises(ConfigError, match="render_model_source"):
        load_plugins()


def test_contract_requires_segment_format_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingFormatCounter(RecordDocumentAdapter):
        segment_format_count = None

    register_plugin(monkeypatch, MissingFormatCounter())

    from app.plugins import load_plugins

    with pytest.raises(ConfigError, match="segment_format_count"):
        load_plugins()


def test_contract_web_store_does_not_interpret_adapter_locator_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_plugin(monkeypatch, OpaqueLocatorRecordAdapter())
    source = tmp_path / "book.olr"
    source.write_text("line one", encoding="utf-8")
    project, _ = init_project(
        [str(source)],
        name="opaque-locators",
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
        document_adapter_id="opaque-locator-record",
    )
    assert project is not None

    assert WebStore(project).overview()["segments"][0]["format_count"] == 0


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


def test_contract_rejects_invalid_model_prompt_requirements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = RecordDocumentAdapter()
    adapter.model_prompt_requirements = lambda **_: []  # type: ignore[method-assign]
    register_plugin(monkeypatch, adapter)
    source = tmp_path / "book.rec"
    write_record(source, "line one")
    project, _ = init_project(
        [str(source)],
        name="invalid-prompt",
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
        document_adapter_id="record",
    )
    assert project is not None
    with pytest.raises(ConfigError, match="模型 Prompt 要求"):
        _project_context(project, stage="translation")


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
    assert state["state"] == {"name": None, "line_ending": "lf"}
    assert state["run_options"] == {"line_ending": "crlf"}


def test_contract_rejects_unknown_or_invalid_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_plugin(monkeypatch, RecordDocumentAdapter())
    app_root = make_app_root(tmp_path)
    source = tmp_path / "book.rec"
    write_record(source, "line one")
    with pytest.raises(UsageError, match="未知选项"):
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
    adapter = RecordDocumentAdapter()
    register_plugin(monkeypatch, adapter)
    app_root = make_app_root(tmp_path)
    source = tmp_path / "book.rec"
    write_record(source, "# name: demo\nline one\n\n---\nline three")
    project, _ = init_project(
        [str(source)],
        name="demo",
        app_root=app_root,
        projects_root=tmp_path / "projects",
        document_adapter_id="record",
        adapter_options={"record": {"source_style": "marked", "line_ending": "crlf"}},
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
    assert adapter.export_languages == ["简体中文", "简体中文"]
    assert adapter.export_language_tags == ["zh-Hans", "zh-Hans"]
    paired = project / "output" / "bilingual" / "translated" / "book.rec"
    assert paired.read_text(encoding="utf-8-sig") == (
        "# name: demo\nline one\n译文:line one\n\nline three\n译文:line three"
    )


def test_contract_run_options_do_not_change_adapter_export_state(
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
        "\ufeff# name: demo\n译文:line one\n译文:line two".encode("utf-8")
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

    with pytest.raises(ConfigError, match="run_options"):
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


def test_contract_cli_adapter_option_reaches_import_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_plugin(monkeypatch, RecordDocumentAdapter())
    source = tmp_path / "book.rec"
    write_record(source, "line one")
    parent = tmp_path / "projects-root"
    parent.mkdir()

    code = run(
        [
            "init",
            str(source),
            "--name",
            "cli-demo",
            "--document-adapter",
            "record",
            "--adapter-option",
            "record.source_style=marked",
            "--parent-dir",
            str(parent),
        ]
    )

    assert code == 0
    project = parent / "cli-demo"
    assert load_segments(project)[0]["model_source"] == "<k1>line one</k1>"


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (
            {"source_style": "plain", "extra": "x"},
            "不完整",
        ),
        ({"source_style": "invalid"}, "取值无效"),
    ],
)
def test_document_adapter_replacement_options_require_exact_valid_values(
    values: dict[str, str], message: str
) -> None:
    class FixtureAdapter(RecordDocumentAdapter):
        def replacement_options(
            self, *, opaque_state: dict[str, object] | None
        ) -> dict[str, str]:
            del opaque_state
            return values

    with pytest.raises(ConfigError, match=message):
        document_adapter_replacement_options(
            FixtureAdapter(), opaque_state={"source_style": "plain"}
        )
