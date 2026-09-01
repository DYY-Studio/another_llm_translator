from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

import httpx
import pytest

from app.errors import FatalExternalError, RequestSizeError
from app.execution import Scope, latest_completed_by_segment, load_stage_history
from app.project import add_project_files, init_project
from app.sqlite_storage import read_json, read_jsonl, record_header, write_json
from app.stages import (
    TermNormalization,
    _restore_leading_whitespace,
    _TermMatchCache,
    load_terms,
    match_term_validation,
    match_terms,
    run_terminology,
    run_translation,
)
from tests.helpers import llm_jsonl, use_llm_preset
from tests.test_foundation import make_app_root


async def create_project(
    tmp_path: Path, text: str, *, encoding: str = "utf-8"
) -> Path:
    app_root = make_app_root(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text(text, encoding=encoding)
    project, _ = init_project(
        [str(source)],
        name="demo",
        app_root=app_root,
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    os.environ["LLM_API_KEY"] = "test"
    return project


def terminology_response(source: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "content": llm_jsonl(
                            [
                                {
                                    "type": "term",
                                    "source": source,
                                    "category": "人物",
                                    "description": "人物",
                                    "preferred_translation": f"译-{source}",
                                    "aliases": [],
                                }
                            ]
                        )
                    }
                }
            ]
        },
    )


@pytest.mark.parametrize(
    ("source", "model_text", "expected"),
    [
        ("  source", "translation  \t", "  translation  \t"),
        ("\tsource", "  translation", "\ttranslation"),
        ("\u3000source", "\ttranslation", "\u3000translation"),
        (" \t\u3000\u00a0source", "\u3000translation", " \t\u3000\u00a0translation"),
        ("source", "\ttranslation", "translation"),
    ],
)
def test_restore_leading_unicode_whitespace(
    source: str,
    model_text: str,
    expected: str,
) -> None:
    assert _restore_leading_whitespace(source, model_text) == expected


@pytest.mark.asyncio
async def test_terminology_publishes_and_translation_uses_terms(
    tmp_path: Path,
) -> None:
    project = await create_project(
        tmp_path,
        "Alice entered.\n\u3000\n \t\nAlice waved.",
        encoding="utf-8-sig",
    )
    seen_translation_payload: dict | None = None
    seen_terminology_payload: dict | None = None
    term_progress: list[tuple[int, int, int]] = []
    translation_progress: list[tuple[int, int, int]] = []
    live_usage: list[dict[str, object] | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_terminology_payload, seen_translation_payload
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        payload = json.loads(body["messages"][1]["content"])
        if 'type="term"' in system:
            seen_terminology_payload = payload
            records = [
                {
                    "type": "term",
                    "source": "Alice",
                    "category": "女性人名",
                    "description": "人物",
                    "preferred_translation": "爱丽丝",
                    "aliases": [],
                }
            ]
        else:
            seen_translation_payload = payload
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
            json={
                "choices": [{"message": {"content": llm_jsonl(records)}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        term_summary = await run_terminology(
            project,
            Scope(),
            http_client=client,
            on_progress=lambda completed, failed, total: term_progress.append(
                (completed, failed, total)
            ),
            on_usage=live_usage.append,
        )
        translation_summary = await run_translation(
            project,
            Scope(),
            http_client=client,
            on_progress=lambda completed, failed, total: translation_progress.append(
                (completed, failed, total)
            ),
            on_usage=live_usage.append,
        )
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]

    assert term_summary["published"] is True
    assert term_summary["terms_revision"] == 1
    assert seen_terminology_payload == {
        "target_language": "简体中文",
        "reference_context": [],
        "source_segments": ["Alice entered.", "Alice waved."],
    }
    assert load_terms(project)["terms"][0]["preferred_translation"] == "爱丽丝"
    assert translation_summary["completed"] == 2
    assert term_progress[0] == (0, 0, 2)
    assert term_progress[-1] == (2, 0, 2)
    assert translation_progress[0] == (0, 0, 2)
    assert translation_progress[-1] == (2, 0, 2)
    assert live_usage[-1]["available"] is True
    assert seen_translation_payload is not None
    assert seen_translation_payload["terms"][0]["source"] == "Alice"
    expected_usage = {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "available": True,
        "partial": False,
    }
    assert term_summary["usage"] == expected_usage
    assert translation_summary["usage"] == expected_usage
    for run_id in (term_summary["run_id"], translation_summary["run_id"]):
        manifest = json.loads(
            (project / "runs" / run_id / "manifest.json").read_text("utf-8")
        )
        assert manifest["usage"] == expected_usage


@pytest.mark.asyncio
async def test_case_insensitive_false_keeps_case_distinct_terms(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "Alice\nalice")
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "case_insensitive = true",
            "case_insensitive = false",
        ),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        payload = json.loads(body["messages"][1]["content"])
        records = []
        for source in payload["source_segments"]:
            records.append(
                {
                    "type": "term",
                    "source": source,
                    "category": "名称",
                    "description": f"说明-{source}",
                    "preferred_translation": f"译-{source}",
                    "aliases": [],
                }
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = await run_terminology(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    assert summary["published"] is True
    library = load_terms(project)
    assert [item["source"] for item in library["terms"]] == ["Alice", "alice"]
    spec = TermNormalization("NFKC", False)
    assert [item["source"] for item in match_terms("Alice walked", library, 10, spec)] == [
        "Alice"
    ]
    assert [item["source"] for item in match_terms("alice walked", library, 10, spec)] == [
        "alice"
    ]
    casefold_spec = TermNormalization("NFKC", True)
    assert {
        item["source"] for item in match_terms("ALICE", library, 10, casefold_spec)
    } == {"Alice", "alice"}


@pytest.mark.parametrize(
    ("normalization", "expected_sources"),
    [
        ('"NFKC"', ["ABC"]),
        ('""', ["ABC", "\uff21\uff22\uff23"]),
    ],
)
@pytest.mark.asyncio
async def test_unicode_normalization_setting_controls_scan_dedup(
    tmp_path: Path,
    normalization: str,
    expected_sources: list[str],
) -> None:
    project = await create_project(tmp_path, "\uff21\uff22\uff23\nABC")
    config_path = project / "config.toml"
    config_path.write_text(
        re.sub(
            r'(?m)^unicode_normalization\s*=.*$',
            f"unicode_normalization = {normalization}",
            config_path.read_text(encoding="utf-8"),
        ),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        payload = json.loads(body["messages"][1]["content"])
        records = [
            {
                "type": "term",
                "source": item,
                "category": "名称",
                "description": f"说明-{item}",
                "preferred_translation": f"译-{item}",
                "aliases": [],
            }
            for item in payload["source_segments"]
        ]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = await run_terminology(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    assert summary["published"] is True
    assert [
        item["source"] for item in load_terms(project)["terms"]
    ] == expected_sources


@pytest.mark.asyncio
async def test_completed_terminology_command_does_not_republish(tmp_path: Path) -> None:
    project = await create_project(tmp_path, "Alice")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        records = [
            {
                "type": "term",
                "source": "Alice",
                "category": "人物",
                "description": "人物",
                "preferred_translation": "爱丽丝",
                "aliases": [],
            }
        ]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        first = await run_terminology(project, Scope(), http_client=client)
        second = await run_terminology(
            project,
            Scope(),
            http_client=client,
            reuse_mixed_fingerprints=True,
        )
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    assert first["terms_revision"] == 1
    assert second["published"] is False
    assert second["terms_revision"] == 1
    assert calls == 1


@pytest.mark.asyncio
async def test_completed_terminology_task_publishes_new_file_results(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "Alice")
    source = tmp_path / "second.txt"
    source.write_text("Bob", encoding="utf-8")
    calls: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        sources = list(payload["source_segments"])
        calls.append(sources)
        records = [
            {
                "type": "term",
                "source": source,
                "category": "人物",
                "description": "人物",
                "preferred_translation": f"译-{source}",
                "aliases": [],
            }
            for source in sources
        ]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        first = await run_terminology(project, Scope(), http_client=client)
        add_project_files(project, [str(source)])
        second = await run_terminology(
            project,
            Scope(),
            http_client=client,
            reuse_mixed_fingerprints=True,
        )
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]

    assert first["published"] is True
    assert second["published"] is True
    assert second["terms_revision"] == 2
    assert calls == [["Alice"], ["Bob"]]
    library = load_terms(project)
    assert library is not None
    assert {item["source"] for item in library["terms"]} == {"Alice", "Bob"}
    assert library["published_run_id"] == second["run_id"]
    active = read_json(project, project / "terminology" / "active_task.json")
    assert active["status"] == "completed"


@pytest.mark.asyncio
async def test_incremental_terminology_failure_keeps_task_active_for_retry(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "Alice")
    source = tmp_path / "second.txt"
    source.write_text("Bob", encoding="utf-8")
    config_path = project / "config.toml"
    config_path.write_text(
        re.sub(
            r'(?m)^http_max_attempts =.*$',
            "http_max_attempts = 1",
            re.sub(
                r'(?m)^base_delay_seconds =.*$',
                "base_delay_seconds = 0",
                re.sub(
                    r'(?m)^max_delay_seconds =.*$',
                    "max_delay_seconds = 0",
                    re.sub(
                        r'(?m)^jitter_seconds =.*$',
                        "jitter_seconds = 0",
                        config_path.read_text(encoding="utf-8"),
                    ),
                ),
            ),
        ),
        encoding="utf-8",
    )
    fail_incremental = False

    def handler(request: httpx.Request) -> httpx.Response:
        if fail_incremental:
            return httpx.Response(500, text="temporary failure")
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        records = [
            {
                "type": "term",
                "source": item,
                "category": "人物",
                "description": "人物",
                "preferred_translation": f"译-{item}",
                "aliases": [],
            }
            for item in payload["source_segments"]
        ]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await run_terminology(project, Scope(), http_client=client)
        add_project_files(project, [str(source)])
        fail_incremental = True
        failed = await run_terminology(
            project,
            Scope(),
            http_client=client,
            reuse_mixed_fingerprints=True,
        )
        failed_active_status = read_json(
            project, project / "terminology" / "active_task.json"
        )["status"]
        fail_incremental = False
        retried = await run_terminology(
            project,
            Scope(),
            http_client=client,
            reuse_mixed_fingerprints=True,
        )
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]

    assert failed["failed"] == 1
    assert failed["published"] is False
    assert failed["terms_revision"] == 1
    assert failed_active_status == "active"
    assert retried["failed"] == 0
    assert retried["published"] is True
    assert load_terms(project)["terms_revision"] == 2


@pytest.mark.asyncio
async def test_terminology_cancel_records_completed_scans_and_retry_reuses_them(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "Alice")
    second = tmp_path / "second.txt"
    second.write_text("Bob", encoding="utf-8")
    add_project_files(project, [str(second)])
    second_started = asyncio.Event()
    release_second = asyncio.Event()

    async def interrupted_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        source = payload["source_segments"][0]
        if source == "Bob":
            second_started.set()
            await release_second.wait()
        return terminology_response(source)

    interrupted_client = httpx.AsyncClient(
        transport=httpx.MockTransport(interrupted_handler)
    )
    task = asyncio.create_task(
        run_terminology(project, Scope(), http_client=interrupted_client)
    )
    try:
        await asyncio.wait_for(second_started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await interrupted_client.aclose()

    manifest = max(
        (
            read_json(project, path)
            for path in (project / "runs").glob("*/manifest.json")
        ),
        key=lambda item: str(item["started_at"]),
    )
    assert manifest["status"] == "interrupted"
    assert manifest["completed_segment_count"] == 1
    assert manifest["failed_segment_count"] == 0

    retried_sources: list[str] = []

    def retry_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        source = payload["source_segments"][0]
        retried_sources.append(source)
        return terminology_response(source)

    retry_client = httpx.AsyncClient(transport=httpx.MockTransport(retry_handler))
    try:
        retried = await run_terminology(project, Scope(), http_client=retry_client)
    finally:
        await retry_client.aclose()
        del os.environ["LLM_API_KEY"]

    assert retried_sources == ["Bob"]
    assert retried["published"] is True


@pytest.mark.asyncio
async def test_terminology_fatal_error_records_completed_and_remaining_counts(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "Alice")
    second = tmp_path / "second.txt"
    second.write_text("Bob", encoding="utf-8")
    add_project_files(project, [str(second)])

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        source = payload["source_segments"][0]
        if source == "Bob":
            return httpx.Response(401, text="unauthorized")
        return terminology_response(source)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(FatalExternalError, match="鉴权失败"):
            await run_terminology(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]

    manifest = max(
        (
            read_json(project, path)
            for path in (project / "runs").glob("*/manifest.json")
        ),
        key=lambda item: str(item["started_at"]),
    )
    assert manifest["status"] == "failed"
    assert manifest["completed_segment_count"] == 1
    assert manifest["failed_segment_count"] == 1


@pytest.mark.asyncio
async def test_incremental_terminology_scope_publishes_after_all_new_files(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "Alice")
    first_new = tmp_path / "second.txt"
    second_new = tmp_path / "third.txt"
    first_new.write_text("Bob", encoding="utf-8")
    second_new.write_text("Carol", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        records = [
            {
                "type": "term",
                "source": item,
                "category": "人物",
                "description": "人物",
                "preferred_translation": f"译-{item}",
                "aliases": [],
            }
            for item in payload["source_segments"]
        ]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await run_terminology(project, Scope(), http_client=client)
        add_project_files(project, [str(first_new), str(second_new)])
        first_scope = await run_terminology(
            project,
            Scope(only_file="F0002"),
            http_client=client,
            reuse_mixed_fingerprints=True,
        )
        first_scope_active_status = read_json(
            project, project / "terminology" / "active_task.json"
        )["status"]
        second_scope = await run_terminology(
            project,
            Scope(only_file="F0003"),
            http_client=client,
            reuse_mixed_fingerprints=True,
        )
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]

    assert first_scope["published"] is False
    assert first_scope["pending"] == 1
    assert first_scope_active_status == "active"
    assert second_scope["published"] is True
    assert second_scope["terms_revision"] == 2
    assert {item["source"] for item in load_terms(project)["terms"]} == {
        "Alice",
        "Bob",
        "Carol",
    }


@pytest.mark.asyncio
async def test_forced_terminology_scan_is_always_project_wide(tmp_path: Path) -> None:
    project = await create_project(tmp_path, "Alice\nBob")
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert all(isinstance(item, str) for item in payload["source_segments"])
        assert all(isinstance(item, str) for item in payload["reference_context"])
        requested.extend(payload["source_segments"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": llm_jsonl([])}}
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = await run_terminology(
            project,
            Scope(only_segment="F0001-S000001", force=True),
            http_client=client,
        )
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    assert set(requested) == {"Alice", "Bob"}
    assert summary["published"] is True


@pytest.mark.asyncio
async def test_terminology_context_and_sources_never_expose_segment_ids(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "before\ncurrent")

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert payload["reference_context"] == ["before"]
        assert payload["source_segments"] == ["current"]
        assert "id" not in json.dumps(payload)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl([])}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = await run_terminology(
            project,
            Scope(only_segment="F0001-S000002"),
            http_client=client,
        )
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    assert summary["completed"] == 1


@pytest.mark.asyncio
async def test_parallel_translation_uses_compact_source_only_context(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "before\ncurrent")
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'scheduling_mode = "ordered_by_file"',
            'scheduling_mode = "parallel"',
        ),
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        seen.update(payload)
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

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await run_translation(
            project,
            Scope(only_segment="F0001-S000002"),
            http_client=client,
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)
    assert seen["reference_context"] == ["before"]
    assert seen["segments"] == [{"id": "1", "source": "current"}]


def test_term_matching_prefers_main_name_over_alias() -> None:
    library = {
        "terms": [
            {
                "source": "Alice",
                "aliases": [],
                "preferred_translation": "爱丽丝",
            },
            {
                "source": "Other",
                "aliases": ["Alice Wonderland"],
                "preferred_translation": "其他",
            },
        ]
    }
    matched = match_terms(
        "Alice Wonderland arrived.", library, 10, TermNormalization("NFKC", True)
    )
    assert [item["source"] for item in matched] == ["Alice", "Other"]


@pytest.mark.parametrize(
    ("source", "term", "matched"),
    [
        ("｜漢字《かんじ》", "漢字", True),
        ("｜漢字《かんじ》", "かんじ", True),
        ("｜漢《かん》｜字《じ》", "漢字", True),
        ("｜漢《かん》｜字《じ》", "かんじ", True),
        ("｜漢《・》｜字《・》", "漢字", True),
        ("漢｜字《じ》", "漢字", True),
        ("｜漢《かん》A｜字《じ》", "かんじ", False),
        ("｜漢字《かんじ》", "漢字かんじ", False),
        ("｜漢《かん》｜字《", "かん", True),
        ("｜漢《かん", "漢字", False),
        ("｜漢《》字", "漢字", False),
        ("｜漢《かん｜字《じ》》", "漢字", False),
        ("｜漢<em>字《かんじ》", "漢字", False),
        ("｜漢\n字《かんじ》", "漢字", False),
    ],
)
def test_term_matching_uses_separate_aozora_ruby_base_and_reading_views(
    source: str,
    term: str,
    matched: bool,
) -> None:
    library = {
        "terms": [
            {
                "source": term,
                "aliases": [],
                "preferred_translation": "译名",
            }
        ]
    }

    result = match_terms(
        source, library, 10, TermNormalization("NFKC", False)
    )

    assert bool(result) is matched


def test_term_matching_reads_names_and_aliases_from_ruby_reading() -> None:
    library = {
        "terms": [
            {
                "source": "Alice",
                "aliases": ["Aly"],
                "preferred_translation": "爱丽丝",
            }
        ]
    }
    spec = TermNormalization("NFKC", True)

    assert [item["source"] for item in match_terms(
        "｜猫《Alice》出现", library, 10, spec
    )] == ["Alice"]
    matched = match_terms("｜猫《Aly》出现", library, 10, spec)
    assert [item["source"] for item in matched] == ["Alice"]
    assert matched[0]["aliases"] == ["Aly"]


def test_term_matching_injects_only_aliases_hit_by_the_source() -> None:
    library = {
        "terms": [
            {
                "source": "Alice",
                "aliases": ["Aly", "Zelda"],
                "preferred_translation": "爱丽丝",
            }
        ]
    }
    spec = TermNormalization("NFKC", True)

    alias_only = match_terms("Aly arrived", library, 10, spec)
    assert alias_only[0]["aliases"] == ["Aly"]

    main_only = match_terms("Alice arrived", library, 10, spec)
    assert main_only[0]["aliases"] == []

    both = match_terms("Alice met Aly", library, 10, spec)
    assert both[0]["aliases"] == ["Aly"]


def test_term_matching_group_injects_primary_without_unmatched_aliases() -> None:
    library = {
        "terms": [
            {
                "source": "Alice",
                "aliases": ["PrimeAlias"],
                "preferred_translation": "爱丽丝",
                "group_primary": None,
            },
            {
                "source": "Alicia",
                "aliases": ["Ally", "MemberAlias"],
                "preferred_translation": "艾丽西亚",
                "group_primary": "alice",
            },
        ]
    }
    matched = match_terms(
        "Ally arrived", library, 10, TermNormalization("NFKC", True)
    )

    assert [item["source"] for item in matched] == ["Alice", "Alicia"]
    assert matched[0]["aliases"] == []
    assert matched[1]["aliases"] == ["Ally"]
    assert matched[1]["primary_source"] == "Alice"


def test_term_validation_matches_only_actual_preferred_term_hits() -> None:
    library = {
        "terms": [
            {
                "source": "Alice",
                "aliases": ["PrimeAlias"],
                "preferred_translation": "爱丽丝",
                "group_primary": None,
            },
            {
                "source": "Alicia",
                "aliases": ["Ally"],
                "preferred_translation": "艾丽西亚",
                "group_primary": "alice",
            },
        ]
    }
    matches = match_term_validation(
        "Ally arrived", library, 10, TermNormalization("NFKC", True)
    )
    assert [
        (item.source, item.matched_text, item.match_type, item.preferred_translation)
        for item in matches
    ] == [("Alicia", "Ally", "alias", "艾丽西亚")]


@pytest.mark.asyncio
async def test_advisory_term_usage_repairs_once_even_when_hard_repairs_are_zero(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "Alice arrived.")
    project_id = str(read_json(project, project / "project.json")["project_id"])
    write_json(
        project,
        project / "terminology" / "terms.json",
        record_header(
            "terminology_library",
            project_id,
            record_id="TERMS-USAGE",
            terms_revision=1,
            terms=[
                {
                    "record_id": "TERM-ALICE",
                    "source": "Alice",
                    "normalized": "alice",
                    "category": "人名",
                    "description": None,
                    "preferred_translation": "爱丽丝",
                    "aliases": [],
                    "group_primary": None,
                    "conflicts": {},
                }
            ],
        ),
    )
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        .replace("validators = []", 'validators = ["preferred_term_usage"]')
        .replace("max_retry_attempts = 2", "max_retry_attempts = 0"),
        encoding="utf-8",
    )
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        calls.append(payload)
        repaired = "validation_repair" in payload
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": llm_jsonl(
                                [
                                    {
                                        "type": "segment",
                                        "id": item["id"],
                                        "translation": (
                                            "译文：爱丽丝"
                                            if repaired
                                            else "译文"
                                        ),
                                    }
                                    for item in payload["segments"]
                                ]
                            )
                        }
                    }
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
    assert summary["failed"] == 0
    assert len(calls) == 2
    assert calls[0]["terms"] == calls[1]["terms"]
    assert calls[1]["segments"][0]["validation_matches"][0] == {
        "validator": "preferred_term_usage",
        "match_type": "preferred_term_missing",
        "severity": "advisory",
        "start": None,
        "end": None,
        "term_source": "Alice",
        "matched_source": "Alice",
        "expected_translation": "爱丽丝",
    }
    record = read_jsonl(project, project / "stages" / "translation.jsonl")[-1]
    assert record["validation_status"] == "passed"
    assert record["validation_findings"] == []


@pytest.mark.asyncio
async def test_advisory_term_usage_accepts_warning_after_one_repair(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "Alice arrived.")
    project_id = str(read_json(project, project / "project.json")["project_id"])
    write_json(
        project,
        project / "terminology" / "terms.json",
        record_header(
            "terminology_library",
            project_id,
            record_id="TERMS-USAGE-WARNING",
            terms_revision=1,
            terms=[
                {
                    "record_id": "TERM-ALICE-WARNING",
                    "source": "Alice",
                    "normalized": "alice",
                    "category": "人名",
                    "description": None,
                    "preferred_translation": "爱丽丝",
                    "aliases": [],
                    "group_primary": None,
                    "conflicts": {},
                }
            ],
        ),
    )
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "validators = []", 'validators = ["preferred_term_usage"]'
        ),
        encoding="utf-8",
    )
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": llm_jsonl(
                                [
                                    {
                                        "type": "segment",
                                        "id": item["id"],
                                        "translation": "译文",
                                    }
                                    for item in payload["segments"]
                                ]
                            )
                        }
                    }
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
    assert summary["failed"] == 0
    assert calls == 2
    record = read_jsonl(project, project / "stages" / "translation.jsonl")[-1]
    assert record["validation_status"] == "warning"
    assert record["validation_findings"][0]["severity"] == "advisory"


def test_term_matching_deduplicates_term_hit_across_base_and_reading() -> None:
    library = {
        "terms": [
            {
                "source": "Alice",
                "aliases": [],
                "preferred_translation": "爱丽丝",
            }
        ]
    }
    matched = match_terms(
        "｜Alice《Alice》出现", library, 10, TermNormalization("NFKC", True)
    )
    assert [item["source"] for item in matched] == ["Alice"]


def test_term_matching_applies_normalization_to_ruby_reading() -> None:
    library = {
        "terms": [
            {
                "source": "Alice",
                "aliases": [],
                "preferred_translation": "爱丽丝",
            }
        ]
    }
    matched = match_terms(
        "｜猫《ＡＬＩＣＥ》出现", library, 10, TermNormalization("NFKC", True)
    )
    assert [item["source"] for item in matched] == ["Alice"]


@pytest.mark.asyncio
async def test_translation_injects_term_across_aozora_ruby_without_rewriting_source(
    tmp_path: Path,
) -> None:
    source = "｜漢《かん》｜字《じ》を読む。"
    project = await create_project(tmp_path, source)
    project_id = str(read_json(project, project / "project.json")["project_id"])
    write_json(
        project,
        project / "terminology" / "terms.json",
        record_header(
            "terminology_library",
            project_id,
            record_id="TERMS-AOZORA",
            terms_revision=1,
            terms=[
                {
                    "record_id": "TERM-AOZORA",
                    "source": "漢字",
                    "normalized": "漢字",
                    "category": "普通名词",
                    "description": None,
                    "preferred_translation": "汉字",
                    "aliases": [],
                    "group_primary": None,
                    "conflicts": {},
                }
            ],
        ),
    )
    seen_payload: dict | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_payload
        seen_payload = json.loads(
            json.loads(request.content)["messages"][1]["content"]
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": llm_jsonl(
                                [
                                    {
                                        "type": "segment",
                                        "id": "1",
                                        "translation": "阅读汉字。",
                                    }
                                ]
                            )
                        }
                    }
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
    assert seen_payload is not None
    assert seen_payload["segments"] == [{"id": "1", "source": source}]
    assert [item["source"] for item in seen_payload["terms"]] == ["漢字"]


@pytest.mark.asyncio
async def test_translation_injects_term_found_only_in_aozora_ruby_reading(
    tmp_path: Path,
) -> None:
    source = "｜猫《Aoki》出现。"
    project = await create_project(tmp_path, source)
    project_id = str(read_json(project, project / "project.json")["project_id"])
    write_json(
        project,
        project / "terminology" / "terms.json",
        record_header(
            "terminology_library",
            project_id,
            record_id="TERMS-AOZORA-READING",
            terms_revision=1,
            terms=[
                {
                    "record_id": "TERM-AOZORA-READING",
                    "source": "Aoki",
                    "normalized": "aoki",
                    "category": "人名",
                    "description": None,
                    "preferred_translation": "青木",
                    "aliases": [],
                    "group_primary": None,
                    "conflicts": {},
                }
            ],
        ),
    )
    seen_payload: dict | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_payload
        seen_payload = json.loads(
            json.loads(request.content)["messages"][1]["content"]
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": llm_jsonl(
                                [
                                    {
                                        "type": "segment",
                                        "id": "1",
                                        "translation": "猫出现。",
                                    }
                                ]
                            )
                        }
                    }
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
    assert seen_payload is not None
    assert seen_payload["segments"] == [{"id": "1", "source": source}]
    assert [item["source"] for item in seen_payload["terms"]] == ["Aoki"]


def test_term_match_cache_is_keyed_by_segment_and_source() -> None:
    library = {
        "terms": [
            {
                "source": "Alice",
                "aliases": ["A"],
                "preferred_translation": "爱丽丝",
            }
        ]
    }
    cache = _TermMatchCache(library, TermNormalization("NFKC", True), 10)
    assert cache.matcher is not None
    calls = 0
    original_match = cache.matcher.match

    def counted_match(source: str, limit: int) -> list[dict]:
        nonlocal calls
        calls += 1
        return original_match(source, limit)

    cache.matcher.match = counted_match  # type: ignore[method-assign]
    items = [
        {"segment_id": "S1", "source": "Alice"},
        {"segment_id": "S1", "source": "Alice"},
        {"segment_id": "S2", "source": "Alice"},
    ]
    assert [item["source"] for item in cache.for_items(items)] == ["Alice"]
    assert calls == 2


def test_term_match_cache_unions_aliases_across_segments() -> None:
    library = {
        "terms": [
            {
                "source": "Alice",
                "aliases": ["Aly", "Zelda"],
                "preferred_translation": "爱丽丝",
            }
        ]
    }
    cache = _TermMatchCache(library, TermNormalization("NFKC", True), 10)

    matched = cache.for_items(
        [
            {"segment_id": "S1", "source": "Aly arrived"},
            {"segment_id": "S2", "source": "Zelda arrived"},
        ]
    )

    assert [item["source"] for item in matched] == ["Alice"]
    assert matched[0]["aliases"] == ["Aly", "Zelda"]


@pytest.mark.asyncio
async def test_translation_truncated_response_saves_prefix_and_retries_only_missing(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, " one\n\ttwo")
    calls: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        ids = [item["id"] for item in payload["segments"]]
        calls.append(ids)
        if len(calls) == 1:
            returned = ids[:1]
            content = "\n".join(
                json.dumps(
                    {
                        "type": "segment",
                        "id": segment_id,
                        "translation": f"ok:{segment_id}",
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                for segment_id in returned
            )
            saved = read_jsonl(project, project / "stages" / "translation.jsonl")
            assert not [item for item in saved if item["status"] == "completed"]
        else:
            returned = ids
            content = llm_jsonl(
                [
                    {
                        "type": "segment",
                        "id": segment_id,
                        "translation": f"ok:{segment_id}",
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
    assert summary["completed"] == 2
    assert calls == [
        ["1", "2"],
        ["1"],
    ]
    completed = latest_completed_by_segment(
        load_stage_history(project, "translation")
    )
    assert set(completed) == {"F0001-S000001", "F0001-S000002"}
    assert completed["F0001-S000001"]["text"] == " ok:1"
    assert completed["F0001-S000002"]["text"] == "\tok:1"


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["missing", "duplicate", "unknown"])
async def test_complete_id_mismatch_retries_original_translation_batch(
    tmp_path: Path,
    mismatch: str,
) -> None:
    project = await create_project(tmp_path, "one\ntwo\nthree")
    calls: list[list[str]] = []
    persisted_completed: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        ids = [item["id"] for item in payload["segments"]]
        calls.append(ids)
        if len(calls) == 1:
            if mismatch == "missing":
                returned = ids[:-1]
            elif mismatch == "duplicate":
                returned = [ids[0], ids[0], *ids[1:]]
            else:
                returned = [*ids, "unknown"]
            saved = read_jsonl(project, project / "stages" / "translation.jsonl")
            persisted_completed.append(
                sum(item["status"] == "completed" for item in saved)
            )
        else:
            returned = ids
        records = [
            {
                "type": "segment",
                "id": segment_id,
                "translation": f"ok:{segment_id}",
            }
            for segment_id in returned
        ]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = await run_translation(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]

    assert summary["completed"] == 3
    assert calls == [["1", "2", "3"], ["1", "2", "3"]]
    assert persisted_completed == [0]


@pytest.mark.asyncio
async def test_complete_missing_id_retries_full_132_segment_batch(
    tmp_path: Path,
) -> None:
    project = await create_project(
        tmp_path, "\n".join(f"line-{index}" for index in range(132))
    )
    calls: list[list[str]] = []
    persisted_completed: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        ids = [item["id"] for item in payload["segments"]]
        calls.append(ids)
        if len(calls) == 1:
            returned = ids[:-1]
            saved = read_jsonl(project, project / "stages" / "translation.jsonl")
            persisted_completed.append(
                sum(item["status"] == "completed" for item in saved)
            )
        else:
            returned = ids
        records = [
            {
                "type": "segment",
                "id": segment_id,
                "translation": f"ok:{segment_id}",
            }
            for segment_id in returned
        ]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = await run_translation(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]

    expected_ids = [str(index) for index in range(1, 133)]
    assert summary["completed"] == 132
    assert calls == [expected_ids, expected_ids]
    assert persisted_completed == [0]


@pytest.mark.asyncio
async def test_complete_id_mismatch_exhaustion_fails_entire_translation_batch(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "one\ntwo\nthree")
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "format_max_attempts = 2", "format_max_attempts = 0"
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        ids = [item["id"] for item in payload["segments"]]
        calls.append(ids)
        records = [
            {
                "type": "segment",
                "id": segment_id,
                "translation": f"ok:{segment_id}",
            }
            for segment_id in ids[:-1]
        ]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = await run_translation(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]

    assert summary["completed"] == 0
    assert summary["failed"] == 3
    assert calls == [["1", "2", "3"]]
    records = read_jsonl(project, project / "stages" / "translation.jsonl")
    assert [record["error_class"] for record in records] == [
        "format_error",
        "format_error",
        "format_error",
    ]


@pytest.mark.asyncio
async def test_partial_response_context_split_does_not_retry_completed_segment(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "one\ntwo")
    calls: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        ids = [item["id"] for item in payload["segments"]]
        calls.append(ids)
        if len(calls) == 1:
            returned = ids[:1]
            content = "\n".join(
                json.dumps(
                    {
                        "type": "segment",
                        "id": segment_id,
                        "translation": f"ok:{segment_id}",
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                for segment_id in returned
            )
        elif len(calls) == 2:
            return httpx.Response(
                400,
                text="context_length_exceeded: maximum context tokens",
            )
        else:
            returned = ids
            content = llm_jsonl(
                [
                    {
                        "type": "segment",
                        "id": segment_id,
                        "translation": f"ok:{segment_id}",
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
    assert summary["completed"] == 2
    assert calls[:2] == [
        ["1", "2"],
        ["1"],
    ]
    assert calls[2:]
    assert all(all(segment_id == "1" for segment_id in request) for request in calls[2:])
    records = read_jsonl(project, project / "stages" / "translation.jsonl")
    assert [
        record["segment_id"]
        for record in records
        if record["status"] == "completed"
    ] == ["F0001-S000001", "F0001-S000002"]


@pytest.mark.asyncio
async def test_dynamic_itpm_failure_finalizes_translation_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = await create_project(tmp_path, "one\ntwo")
    from app import stages

    original_estimate = stages._request_estimate
    estimate_calls = 0

    def fail_second_estimate(
        messages: list[dict[str, str]],
        config: dict,
        request_id: str,
    ) -> int:
        nonlocal estimate_calls
        estimate_calls += 1
        if estimate_calls == 2:
            raise RequestSizeError(
                "单请求预测 Token 超过 ITPM",
                reason="itpm",
            )
        return original_estimate(messages, config, request_id)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        first = payload["segments"][0]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": llm_jsonl(
                                [
                                    {
                                        "type": "segment",
                                        "id": first["id"],
                                        "translation": "ok",
                                    }
                                ]
                            )
                        }
                    }
                ]
            },
        )

    monkeypatch.setattr(stages, "_request_estimate", fail_second_estimate)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(RequestSizeError, match="ITPM"):
            await run_translation(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    manifests = [
        read_json(project, path)
        for path in (project / "runs").glob("*/manifest.json")
    ]
    assert len(manifests) == 1
    assert manifests[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_translation_reports_dynamic_output_budget_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = await create_project(tmp_path, "one")
    use_llm_preset(
        tmp_path,
        context_window_tokens=1000,
        context_safety_margin_tokens=100,
        max_output_tokens=5000,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        records = [
            {"type": "segment", "id": item["id"], "translation": "译文"}
            for item in payload["segments"]
        ]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = await run_translation(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    assert any("自动收窄" in warning for warning in summary["warnings"])
    run_dir = project / "runs" / summary["run_id"]
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert any("自动收窄" in warning for warning in manifest["warnings"])


@pytest.mark.asyncio
async def test_kana_validation_repairs_contiguous_failures(tmp_path: Path) -> None:
    project = await create_project(tmp_path, "first\nsecond\n\nthird")
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "validators = []", 'validators = ["japanese_kana"]'
        ),
        encoding="utf-8",
    )
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        calls.append(payload)
        repairing = "validation_repair" in payload
        records = [
            {
                "type": "segment",
                "id": item["id"],
                "translation": (
                    f"修复:{item['source']}"
                    if repairing
                    else f"候选カ:{item['source']}"
                ),
            }
            for item in payload["segments"]
        ]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = await run_translation(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    assert summary["completed"] == 3
    repair_payloads = [payload for payload in calls if "validation_repair" in payload]
    assert [
        [item["id"] for item in payload["segments"]] for payload in repair_payloads
    ] == [
        ["1", "2", "3"],
    ]
    records = read_jsonl(project, project / "stages" / "translation.jsonl")
    assert all(record["validation_status"] == "passed" for record in records)


@pytest.mark.asyncio
async def test_oversized_segment_is_split_and_saved_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = await create_project(
        tmp_path,
        " \t\u3000" + "A" * 5000,
        encoding="utf-8-sig",
    )
    config_path = project / "config.toml"
    text = config_path.read_text(encoding="utf-8")
    use_llm_preset(
        tmp_path,
        context_window_tokens=1200,
        max_output_tokens=300,
        context_safety_margin_tokens=100,
    )
    for key, value in (("target_chunk_input_tokens", "700"),):
        text = re.sub(rf"(?m)^{key}\s*=.*$", f"{key} = {value}", text)
    config_path.write_text(text, encoding="utf-8")
    requested_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        requested_ids.extend(item["id"] for item in payload["segments"])
        records = [
            {
                "type": "segment",
                "id": item["id"],
                "translation": item["source"].lower(),
            }
            for item in payload["segments"]
        ]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = await run_translation(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    assert summary["completed"] == 1
    assert len(requested_ids) > 1
    assert all(segment_id.isdigit() for segment_id in requested_ids)
    records = read_jsonl(project, project / "stages" / "translation.jsonl")
    completed = [record for record in records if record["status"] == "completed"]
    assert len(completed) == 1
    assert completed[0]["segment_id"] == "F0001-S000001"
    assert completed[0]["text"] == " \t\u3000" + "a" * 5000


@pytest.mark.asyncio
async def test_model_context_error_triggers_runtime_segment_split(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "ABCDEFGH")
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "enabled = false",
            "enabled = true",
        ),
        encoding="utf-8",
    )
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        source = payload["segments"][0]["source"]
        requested.append(source)
        if len(source) > 3:
            return httpx.Response(
                400,
                text="context_length_exceeded: maximum context tokens",
            )
        records = [
            {
                "type": "segment",
                "id": payload["segments"][0]["id"],
                "translation": source.lower(),
            }
        ]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = await run_translation(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    assert summary["completed"] == 1
    assert any(len(source) > 3 for source in requested)
    assert sum(len(source) <= 3 for source in requested) == 4
    completed = latest_completed_by_segment(
        load_stage_history(project, "translation")
    )
    assert completed["F0001-S000001"]["text"] == "abcdefgh"
    run_dir = next((project / "runs").iterdir())
    attempts = [
        json.loads(line)
        for line in (run_dir / "attempts.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(item.get("parent_request_id") for item in attempts)


@pytest.mark.asyncio
async def test_validation_repair_context_error_splits_without_part_results(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "ABCDEFGH")
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "validators = []", 'validators = ["japanese_kana"]'
        ),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        item = payload["segments"][0]
        if "validation_repair" in payload and len(item["source"]) > 2:
            return httpx.Response(
                400,
                text="context_length_exceeded: maximum context tokens",
            )
        translation = (
            "好" * len(item["source"])
            if "validation_repair" in payload
            else "カ" * len(item["source"])
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": llm_jsonl(
                                [
                                    {
                                        "type": "segment",
                                        "id": item["id"],
                                        "translation": translation,
                                    }
                                ]
                            )
                        }
                    }
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
    records = read_jsonl(project, project / "stages" / "translation.jsonl")
    completed = [item for item in records if item["status"] == "completed"]
    assert len(completed) == 1
    assert completed[0]["segment_id"] == "F0001-S000001"
    assert completed[0]["text"] == "好" * 8
