from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx
import pytest

from app.execution import Scope, parse_jsonl_document
from app.logging_utils import attach_project_log, configure_cli_logging
from app.main import run
from app.stages import (
    _parse_review_items,
    _parse_translation_items,
    _validate_term_items,
    run_terminology,
    run_translation,
)
from app.storage import read_jsonl
from tests.helpers import llm_jsonl
from tests.test_terminology_translation import create_project


def test_jsonl_extraction_accepts_bom_crlf_blanks_and_supported_fence() -> None:
    content = (
        "\ufeff模型说明\r\n```jsonl\r\n\r\n"
        '{"type":"segment","id":"S1","translation":"译文"}\r\n'
        '{"type":"end"}\r\n```\r\n更多说明'
    )
    document = parse_jsonl_document(content, record_type="segment")
    assert document.complete is True
    assert document.records[0]["id"] == "S1"


@pytest.mark.parametrize("label", ["jsonl", "ndjson", "json", ""])
def test_jsonl_extraction_accepts_supported_markdown_labels(label: str) -> None:
    content = f"说明\n```{label}\n{{\"type\":\"end\"}}\n```\n说明"
    document = parse_jsonl_document(content, record_type="segment")
    assert document.complete is True
    assert document.records == ()


@pytest.mark.parametrize(
    "content",
    [
        '{"segments":[]}',
        "[]\n{\"type\":\"end\"}",
        '{"type":"unknown"}\n{"type":"end"}',
        '{"type":"end"}\n{"type":"end"}',
        '{"type":"end"}\n{"type":"segment","id":"S1","translation":"x"}',
        '{"type":"segment","id":"S1","translation":"x"}',
    ],
)
def test_strict_jsonl_rejects_old_or_incomplete_protocol(content: str) -> None:
    document = parse_jsonl_document(content, record_type="segment")
    assert document.complete is False
    assert document.errors


def test_stage_jsonl_validators_reject_duplicates_and_keep_other_valid_rows() -> None:
    translation = llm_jsonl(
        [
            {"type": "segment", "id": "S1", "translation": "first"},
            {"type": "segment", "id": "S1", "translation": "duplicate"},
            {"type": "segment", "id": "S2", "translation": "second"},
        ]
    )
    valid, unresolved, errors, complete = _parse_translation_items(
        translation, ["S1", "S2"]
    )
    assert valid == {"S2": "second"}
    assert unresolved == ["S1"]
    assert errors
    assert complete is False

    review = llm_jsonl(
        [
            {
                "type": "segment",
                "id": "S1",
                "status": "suggested",
                "suggested_text": "new",
                "reason": "reason",
            }
        ]
    )
    review_valid, review_unresolved, review_errors, review_complete = (
        _parse_review_items(review, ["S1"])
    )
    assert review_valid["S1"]["suggested_text"] == "new"
    assert review_unresolved == []
    assert review_errors == []
    assert review_complete is True


def test_terminology_jsonl_allows_empty_response_and_validates_fields() -> None:
    terms, errors, complete = _validate_term_items('{"type":"end"}')
    assert terms == []
    assert errors == []
    assert complete is True

    terms, errors, complete = _validate_term_items(
        llm_jsonl(
            [
                {
                    "type": "term",
                    "source": "Alice",
                    "category": "人物",
                    "description": "人物",
                    "aliases": [],
                },
                {"type": "term", "source": "", "category": "人物", "description": "x"},
            ]
        )
    )
    assert [item["source"] for item in terms] == ["Alice"]
    assert errors
    assert complete is False


@pytest.mark.asyncio
async def test_partial_truncated_translation_is_saved_before_format_retry(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "one\ntwo")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        if calls == 1:
            first_id = payload["segments"][0]["id"]
            content = json.dumps(
                {"type": "segment", "id": first_id, "translation": "first"}
            )
        else:
            saved = read_jsonl(project / "stages" / "translation.jsonl")
            assert any(item.get("segment_id") == "F0001-S000001" for item in saved)
            assert [item["id"] for item in payload["segments"]] == [
                "F0001-S000002"
            ]
            content = llm_jsonl(
                [
                    {
                        "type": "segment",
                        "id": "F0001-S000002",
                        "translation": "second",
                    }
                ]
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = await run_translation(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    assert summary["completed"] == 2
    assert calls == 2


@pytest.mark.asyncio
async def test_old_top_level_json_enters_format_correction(tmp_path: Path) -> None:
    project = await create_project(tmp_path, "one")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        segment_id = payload["segments"][0]["id"]
        if calls == 1:
            content = json.dumps(
                {"segments": [{"id": segment_id, "translation": "old"}]}
            )
        else:
            assert "format_correction" in payload
            content = llm_jsonl(
                [
                    {
                        "type": "segment",
                        "id": segment_id,
                        "translation": "new",
                    }
                ]
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = await run_translation(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    assert summary["completed"] == 1
    assert calls == 2


@pytest.mark.asyncio
async def test_incomplete_terms_save_candidates_without_advancing_scan(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "Alice")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            content = json.dumps(
                {
                    "type": "term",
                    "source": "Alice",
                    "category": "人物",
                    "description": "人物",
                    "aliases": [],
                }
            )
        else:
            assert read_jsonl(project / "terminology" / "candidates.jsonl")
            scans = read_jsonl(project / "terminology" / "scans.jsonl")
            assert not any(item["status"] == "completed" for item in scans)
            content = '{"type":"end"}'
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = await run_terminology(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    assert summary["published"] is True
    assert calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("debug_enabled", [False, True])
async def test_llm_runtime_logs_are_live_and_do_not_expose_content_or_secrets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    debug_enabled: bool,
) -> None:
    source_marker = "PRIVATE_SOURCE_MARKER"
    secret_marker = "PRIVATE_API_KEY_MARKER"
    project = await create_project(tmp_path, source_marker)
    os.environ["LLM_API_KEY"] = secret_marker
    config_path = project / "config.toml"
    text = config_path.read_text(encoding="utf-8")
    text = re.sub(
        r"(?ms)(^\[debug\]\s*.*?^enabled\s*=\s*)\w+",
        rf"\g<1>{str(debug_enabled).lower()}",
        text,
    )
    config_path.write_text(text, encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        content = llm_jsonl(
            [
                {
                    "type": "segment",
                    "id": payload["segments"][0]["id"],
                    "translation": "PRIVATE_TRANSLATION_MARKER",
                }
            ]
        )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        )

    configure_cli_logging()
    attach_project_log(project)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = await run_translation(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    captured = capsys.readouterr()
    assert summary["completed"] == 1
    assert "run start run=" in captured.err
    assert "request start request=" in captured.err
    assert "request complete request=" in captured.err
    assert "segment complete segment=" in captured.err
    assert captured.out == ""
    file_log = (project / "logs" / "app.log").read_text(encoding="utf-8")
    for log_text in (captured.err, file_log):
        assert source_marker not in log_text
        assert secret_marker not in log_text
        assert "PRIVATE_TRANSLATION_MARKER" not in log_text
        assert "Authorization" not in log_text
        assert "Cookie" not in log_text


@pytest.mark.asyncio
@pytest.mark.parametrize("debug_enabled", [False, True])
async def test_cli_logs_to_stderr_and_project_file_while_stdout_is_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    debug_enabled: bool,
) -> None:
    project = await create_project(tmp_path, "one")
    config_path = project / "config.toml"
    text = config_path.read_text(encoding="utf-8")
    text = re.sub(
        r"(?ms)(^\[debug\]\s*.*?^enabled\s*=\s*)\w+",
        rf"\g<1>{str(debug_enabled).lower()}",
        text,
    )
    config_path.write_text(text, encoding="utf-8")
    try:
        exit_code = run(["inspect", str(project), "--dry-run"])
    finally:
        del os.environ["LLM_API_KEY"]
    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out)["name"] == "demo"
    assert "command start command=inspect" in captured.err
    assert "command complete command=inspect" in captured.err
    file_log = (project / "logs" / "app.log").read_text(encoding="utf-8")
    assert "command complete command=inspect" in file_log
