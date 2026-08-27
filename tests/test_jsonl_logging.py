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
from app.sqlite_storage import read_json, read_jsonl
from app.stages import (
    _parse_review_items,
    _parse_translation_items,
    _validate_term_items,
    run_terminology,
    run_translation,
)
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
    content = f'说明\n```{label}\n{{"type":"end"}}\n```\n说明'
    document = parse_jsonl_document(content, record_type="segment")
    assert document.complete is True
    assert document.records == ()


def test_jsonl_extraction_accepts_leading_thought_blocks() -> None:
    template = "\ufeff \r\n<think>推理过程</think>\r\n{answer}"
    answer = llm_jsonl([{"type": "segment", "id": "S1", "translation": "译文"}])
    document = parse_jsonl_document(
        template.replace("{answer}", answer),
        record_type="segment",
    )
    assert document.complete is True
    assert document.records[0]["translation"] == "译文"


def test_jsonl_extraction_uses_answer_fence_after_thought_block() -> None:
    content = (
        '<think>```jsonl\n{"type":"end"}\n```</think>\n'
        "```jsonl\n"
        '{"type":"segment","id":"S1","translation":"最终译文"}\n'
        '{"type":"end"}\n'
        "```"
    )
    document = parse_jsonl_document(content, record_type="segment")
    assert document.complete is True
    assert document.records[0]["translation"] == "最终译文"


@pytest.mark.parametrize(
    "content",
    [
        '<think>未闭合\n```jsonl\n{"type":"end"}\n```',
        '<think>first</think><thought>second</thought>\n{"type":"end"}',
        '<think>outer <thought>inner</thought></think>\n{"type":"end"}',
        '<think>mismatched </thought></think>\n{"type":"end"}',
        '说明<think>推理</think>\n{"type":"end"}',
        '```jsonl\n{"type":"end"}\n```\n<think>后置推理</think>',
    ],
)
def test_jsonl_extraction_rejects_malformed_or_nonleading_thought_blocks(
    content: str,
) -> None:
    document = parse_jsonl_document(content, record_type="segment")
    assert document.complete is False
    assert document.errors


def test_jsonl_extraction_preserves_thought_tags_inside_translation() -> None:
    translation = (
        "保留 <think>文本</think>、<thinking>文本</thinking>、"
        "<thought>文本</thought> 和 <analysis>文本</analysis>"
    )
    valid, unresolved, errors, complete = _parse_translation_items(
        llm_jsonl([{"type": "segment", "id": "S1", "translation": translation}]),
        ["S1"],
    )
    assert valid == {"S1": translation}
    assert unresolved == []
    assert errors == []
    assert complete is True


@pytest.mark.parametrize(
    "content",
    [
        '{"segments":[]}',
        '[]\n{"type":"end"}',
        '{"type":"unknown"}\n{"type":"end"}',
        '{"type":"end","extra":true}',
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


@pytest.mark.parametrize(
    "extra_fields",
    [
        {},
        {"suggested_text": None, "reason": None},
        {"suggested_text": "回显当前文本", "reason": "无需修改"},
        {"suggested_text": 123, "reason": {"detail": "ignored"}},
        {"suggested_text": [], "reason": ["ignored"]},
    ],
)
def test_review_parser_ignores_accepted_optional_fields(
    extra_fields: dict[str, object],
) -> None:
    record = {
        "type": "segment",
        "id": "S1",
        "status": "accepted",
        **extra_fields,
    }
    valid, unresolved, errors, complete = _parse_review_items(
        llm_jsonl([record]),
        ["S1"],
    )
    assert valid == {
        "S1": {
            "review_status": "accepted",
            "suggested_text": None,
            "reason": None,
        }
    }
    assert unresolved == []
    assert errors == []
    assert complete is True


@pytest.mark.parametrize(
    "fields",
    [
        {"reason": None},
        {"suggested_text": "", "reason": None},
        {"suggested_text": 123, "reason": None},
        {"suggested_text": "建议", "reason": {"detail": "invalid"}},
    ],
)
def test_review_parser_keeps_suggested_fields_strict(
    fields: dict[str, object],
) -> None:
    record = {
        "type": "segment",
        "id": "S1",
        "status": "suggested",
        **fields,
    }
    valid, unresolved, errors, complete = _parse_review_items(
        llm_jsonl([record]),
        ["S1"],
    )
    assert valid == {}
    assert unresolved == ["S1"]
    assert errors
    assert complete is False


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
                    "category": "女性人名",
                    "aliases": [],
                },
                {"type": "term", "source": "", "category": "人物", "description": "x"},
                {
                    "type": "term",
                    "source": "Bob",
                    "category": "男性人名",
                    "description": 1,
                },
            ]
        )
    )
    assert [item["source"] for item in terms] == ["Alice"]
    assert terms[0]["description"] is None
    assert errors
    assert any("description 类型错误" in error for error in errors)
    assert complete is False


def test_terminology_rejects_malformed_end_without_discarding_valid_candidates() -> (
    None
):
    content = (
        '{"type":"term","source":"Alice","category":"人物",'
        '"description":"人物","aliases":[]}\n'
        '{"type":"type":"end"}'
    )
    terms, errors, complete = _validate_term_items(content)
    assert [item["source"] for item in terms] == ["Alice"]
    assert complete is False
    assert any("第 2 行" in error for error in errors)
    assert any("最终 end" in error for error in errors)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "template",
    [
        "<think>推理过程</think>\n{answer}",
        "<thinking>推理过程</thinking>\n{answer}",
        "<thought>推理过程</thought>\n{answer}",
        "<analysis>推理过程</analysis>\n{answer}",
    ],
)
async def test_translation_accepts_embedded_thought_content_without_retry(
    tmp_path: Path,
    template: str,
) -> None:
    project = await create_project(tmp_path, "one")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        segment_id = payload["segments"][0]["id"]
        answer = llm_jsonl(
            [
                {
                    "type": "segment",
                    "id": segment_id,
                    "translation": "译文",
                }
            ]
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": template.replace("{answer}", answer)}}
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = await run_translation(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    assert summary["completed"] == 1
    assert calls == 1


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
            saved = read_jsonl(project, project / "stages" / "translation.jsonl")
            assert any(item.get("segment_id") == "F0001-S000001" for item in saved)
            assert [item["id"] for item in payload["segments"]] == ["1"]
            content = llm_jsonl(
                [
                    {
                        "type": "segment",
                        "id": "1",
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
async def test_translation_format_retry_regroups_around_valid_nonempty_segment(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "one\ntwo\nthree")
    calls: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        ids = [item["id"] for item in payload["segments"]]
        calls.append(ids)
        returned = [ids[1]] if len(ids) == 3 else ids
        content = llm_jsonl(
            [
                {
                    "type": "segment",
                    "id": segment_id,
                    "translation": f"translated:{segment_id}",
                }
                for segment_id in returned
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
    assert summary["completed"] == 3
    assert calls == [
        ["1", "2", "3"],
        ["1"],
        ["1"],
    ]


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
            correction = payload["format_correction"]
            assert "第 1 行" in correction
            assert "未知 type" in correction
            assert "缺少最终 end 记录" in correction
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
                    "category": "女性人名",
                    "aliases": [],
                }
            )
        else:
            assert read_jsonl(project, project / "terminology" / "candidates.jsonl")
            scans = read_jsonl(project, project / "terminology" / "scans.jsonl")
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
    terms = read_json(project, project / "terminology" / "terms.json")["terms"]
    assert terms[0]["description"] == ""


@pytest.mark.asyncio
async def test_malformed_end_keeps_candidates_and_marks_scan_failed(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "Alice")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        content = (
            '{"type":"term","source":"Alice","category":"人物",'
            '"description":"人物","aliases":[]}\n'
            '{"type":"type":"end"}'
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = await run_terminology(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]

    candidates = read_jsonl(project, project / "terminology" / "candidates.jsonl")
    scans = read_jsonl(project, project / "terminology" / "scans.jsonl")
    assert calls == 3
    assert summary["published"] is False
    assert summary["failed"] == 1
    assert summary["failure_counts"] == {"format_error": 1}
    assert candidates and candidates[0]["terms"][0]["source"] == "Alice"
    assert scans[-1]["status"] == "failed"
    assert scans[-1]["error_class"] == "format_error"
    assert not (project / "terminology" / "terms.json").exists()
    manifest = read_json(
        project, project / "runs" / summary["run_id"] / "manifest.json"
    )
    assert manifest["failure_counts"] == {"format_error": 1}


@pytest.mark.asyncio
async def test_terminology_format_retry_carries_parse_error_details(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "Alice")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        if calls == 1:
            content = '{"type":"type":"end"}'
        else:
            correction = payload["format_correction"]
            assert "第 1 行" in correction
            assert "不是合法 JSON 对象" in correction
            content = llm_jsonl(
                [
                    {
                        "type": "term",
                        "source": "Alice",
                        "category": "人物",
                        "description": "人物",
                        "aliases": [],
                    }
                ]
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = await run_terminology(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    assert summary["failed"] == 0
    assert calls == 2
    assert read_json(project, project / "terminology" / "terms.json") is not None


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
