from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx

from app.execution import Scope
from app.plugins import get_document_adapter, get_document_adapter_for_extension
from app.project import init_project, load_segments, load_source_files
from app.project_export import export_project
from app.stage_translation import run_translation
from tests.helpers import llm_jsonl
from tests.test_foundation import make_app_root


def test_srt_entrypoint_runs_host_import_translate_and_exports(
    tmp_path: Path,
) -> None:
    adapter = get_document_adapter_for_extension(".SRT")
    assert adapter.adapter_id == "srt"
    assert get_document_adapter("srt").version == "1"

    source = tmp_path / "episode.srt"
    source.write_text(
        "3\n00:00:01,000 --> 00:00:02,000\n第一行\n第二行\n\n"
        "7\n00:00:03,000 --> 00:00:04,000\n第二 cue\n",
        encoding="utf-8",
    )
    project, summary = init_project(
        [str(source)],
        name="srt-demo",
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
        document_adapter_id=None,
    )
    assert project is not None
    assert summary["document_adapter"] == "srt"
    assert load_source_files(project)[0]["document_adapter_id"] == "srt"
    assert [item["source"] for item in load_segments(project)] == [
        "第一行\n第二行",
        "第二 cue",
    ]

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
        result = asyncio.run(run_translation(project, Scope(), http_client=client))
    finally:
        os.environ.pop("LLM_API_KEY", None)
        asyncio.run(client.aclose())
    assert result["completed"] == 2

    translated = export_project(
        project, "translated", bilingual=False, allow_missing=False
    )
    assert translated["files"] == 1
    assert (project / "output" / "translated" / "episode.srt").read_text(
        encoding="utf-8-sig"
    ) == (
        "3\n00:00:01,000 --> 00:00:02,000\n译文:第一行\n第二行\n\n"
        "7\n00:00:03,000 --> 00:00:04,000\n译文:第二 cue\n"
    )

    bilingual = export_project(
        project, "translated", bilingual=True, allow_missing=False
    )
    assert bilingual["files"] == 1
    assert (project / "output" / "bilingual" / "translated" / "episode.srt").read_text(
        encoding="utf-8-sig"
    ) == (
        "3\n00:00:01,000 --> 00:00:02,000\n第一行\n第二行\n"
        "译文:第一行\n第二行\n\n"
        "7\n00:00:03,000 --> 00:00:04,000\n第二 cue\n译文:第二 cue\n"
    )

    txt = export_project(
        project,
        "translated",
        bilingual=False,
        allow_missing=False,
        output_format="txt",
    )
    assert txt["files"] == 1
    assert (project / "output" / "translated" / "episode.txt").read_text(
        encoding="utf-8-sig"
    ) == "译文:第一行\n第二行\n译文:第二 cue"
