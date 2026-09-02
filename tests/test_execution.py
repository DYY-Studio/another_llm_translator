from __future__ import annotations

import asyncio
import json
import math
import os
from collections import Counter
from pathlib import Path

import httpx
import pytest

from app.config import load_global_config
from app.diagnostics import Diagnostics
from app.errors import (
    ExternalError,
    FatalExternalError,
    RequestSizeError,
    StorageError,
)
from app.execution import (
    CJK_RE,
    ChunkPlan,
    LLMClient,
    PreviousContextIndex,
    Scope,
    SlidingWindowLimiter,
    build_chunk_plans,
    classify_stage,
    classify_stage_states,
    combine_usage,
    contiguous_groups,
    continue_run,
    create_run,
    dispatch_chunks,
    estimate_messages,
    estimate_messages_upper_bound,
    estimate_single_segment_preflight,
    estimate_tokens,
    finalize_run,
    full_prompt,
    iter_chunk_plans,
    localize_request_ids,
    materialize_chunk_stream,
    render_messages,
    save_debug_chunks,
    select_scope,
    stage_fingerprint,
)
from app.llm_adapter import load_json_adapter
from app.project import init_project
from app.sqlite_storage import (
    append_jsonl,
    latest_stage_states,
    read_json,
    read_jsonl,
    record_header,
    terminology_scan_state,
    write_json,
)
from app.stages import StageRunState, _execute_stage_run
from tests.test_foundation import make_app_root

ROOT = Path(__file__).parents[1]


def _finalize_project(tmp_path: Path) -> Path:
    project, _ = init_project(
        [],
        name="finalize",
        empty=True,
        app_root=make_app_root(tmp_path),
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    return project


def _run_manifest(project: Path, **fields: object) -> dict[str, object]:
    metadata = read_json(project, project / "project.json")
    return record_header(
        "run",
        str(metadata["project_id"]),
        record_id="RUN-TEST",
        run_id="RUN-TEST",
        stage="translation",
        status="running",
        started_at="2026-08-16T12:00:00+08:00",
        **fields,
    )


def config() -> dict:
    return load_global_config(ROOT)


def segments() -> list[dict]:
    return [
        {
            "segment_id": f"F0001-S{index + 1:06d}",
            "file_id": "F0001",
            "part_id": "document",
            "line_index": index,
            "source": source,
            "is_empty": source == "",
        }
        for index, source in enumerate(["one", "", "three", "four", "five"])
    ]


def test_stage_fingerprint_ignores_chunk_but_tracks_scheduling() -> None:
    first = config()
    prompt = full_prompt(
        "translation",
        (ROOT / "prompts" / "translation.zh-CN.middle.txt").read_text(encoding="utf-8"),
    )
    original = stage_fingerprint(first, "translation", prompt, terms_revision=1)
    first["chunking"]["target_chunk_input_tokens"] = 100
    assert stage_fingerprint(first, "translation", prompt, terms_revision=1) == original
    first["chunking"]["cross_boundary_batching"] = ["translation"]
    assert stage_fingerprint(first, "translation", prompt, terms_revision=1) == original
    first["execution"]["scheduling_mode"] = (
        "ordered_by_file"
        if first["execution"]["scheduling_mode"] == "parallel"
        else "parallel"
    )
    assert stage_fingerprint(first, "translation", prompt, terms_revision=1) != original


def test_stage_fingerprint_tracks_document_prompt_requirements() -> None:
    current = config()
    baseline = stage_fingerprint(current, "translation", {})
    current["_document_adapter_prompt_requirements"] = {
        "F0001": {"zh-CN": "保留 EPUB 标记", "en": "Keep EPUB markers"}
    }
    assert stage_fingerprint(current, "translation", {}) != baseline


def test_token_estimate_matches_the_previous_character_accounting() -> None:
    samples = [
        "",
        "English words and punctuation!",
        "简体中文、繁體中文、日本語、한국어",
        "line\n\twith\u2003unicode whitespace",
        '\\"quoted\\" \\ slash \\u0000 control',
        "😀" * 17,
    ]
    for text in samples:
        cjk_count = len(CJK_RE.findall(text))
        non_cjk = CJK_RE.sub("", text)
        non_space = sum(not char.isspace() for char in non_cjk)
        whitespace = sum(char.isspace() for char in non_cjk)
        expected = max(
            1,
            math.ceil(cjk_count * 1.1 + non_space / 4 + whitespace / 8),
        ) if text else 0
        assert estimate_tokens(text) == expected


def test_message_upper_bound_is_conservative() -> None:
    messages = render_messages(
        "系统提示：保留 JSONL。",
        {
            "segments": [
                {"id": "1", "source": '引号\\换行\n and symbols 😀'},
                {"id": "2", "source": "plain"},
            ]
        },
    )
    for factor in (0.5, 1.0, 1.05, 2.0):
        assert estimate_messages(messages, factor) <= estimate_messages_upper_bound(
            messages, factor
        )


def test_single_segment_preflight_falls_back_when_upper_bound_is_uncertain() -> None:
    segment = {
        "segment_id": "F0001-S000001",
        "source": "ordinary latin text " * 40,
    }

    def payload_builder(items: list[dict]) -> dict:
        return {
            "segments": [
                {"id": item["segment_id"], "source": item["source"]}
                for item in items
            ]
        }

    current = config()
    prompt = "prompt"
    messages = render_messages(prompt, payload_builder([segment]))
    exact = estimate_messages(messages, current["execution"]["token_safety_factor"])
    upper = estimate_messages_upper_bound(
        messages,
        current["execution"]["token_safety_factor"],
    )
    assert upper > exact
    current["llm"]["context_window_tokens"] = (
        exact + current["llm"]["context_safety_margin_tokens"]
    )
    current["execution"]["input_tokens_per_minute"] = 0
    assert estimate_single_segment_preflight(
        segment,
        config=current,
        prompt=prompt,
        payload_builder=payload_builder,
    ) is False

    current["llm"]["context_window_tokens"] -= 1
    with pytest.raises(RequestSizeError, match="模型硬限制"):
        estimate_single_segment_preflight(
            segment,
            config=current,
            prompt=prompt,
            payload_builder=payload_builder,
        )


def test_latest_stage_states_preserve_classification_semantics(tmp_path: Path) -> None:
    project = _finalize_project(tmp_path)
    selected = [
        {"segment_id": "F0001-S000001"},
        {"segment_id": "F0001-S000002"},
        {"segment_id": "F0001-S000003"},
        {"segment_id": "F0001-S000004"},
    ]

    def append_stage(segment_id: str, status: str, **fields: object) -> None:
        append_jsonl(
            project,
            project / "stages" / "translation.jsonl",
            record_header(
                "stage_result",
                "PROJECT",
                stage="translation",
                segment_id=segment_id,
                status=status,
                **fields,
            ),
        )

    append_stage("F0001-S000001", "completed", text="old", stage_fingerprint="fp-old")
    append_stage("F0001-S000001", "failed")
    append_stage("F0001-S000002", "completed", text="reset", stage_fingerprint="fp-reset")
    append_stage("F0001-S000002", "reset")
    append_stage("F0001-S000003", "reset")
    append_stage("F0001-S000003", "completed", text="new", stage_fingerprint="fp-new")
    append_stage("F0001-S000004", "failed")

    states = latest_stage_states(
        project,
        "translation",
        [item["segment_id"] for item in selected],
    )
    classified = classify_stage_states(selected, states, force=False)
    assert [item["segment_id"] for item in classified.reusable] == [
        "F0001-S000001",
        "F0001-S000003",
    ]
    assert [item["segment_id"] for item in classified.work] == [
        "F0001-S000002",
        "F0001-S000004",
    ]
    assert classified.latest_completed["F0001-S000001"]["text"] == "old"
    assert classified.latest_completed["F0001-S000003"]["text"] == "new"
    assert classified.last_attempt_failed == ("F0001-S000001",)
    assert classified.fingerprints == frozenset({"fp-old", "fp-new"})


def test_terminology_scan_state_uses_completed_records_only(tmp_path: Path) -> None:
    project = _finalize_project(tmp_path)
    path = project / "terminology" / "scans.jsonl"

    def append_scan(segment_id: str, status: str, fingerprint: str | None) -> None:
        append_jsonl(
            project,
            path,
            record_header(
                "terminology_scan",
                "PROJECT",
                active_task_id="TASK-1",
                segment_id=segment_id,
                status=status,
                stage_fingerprint=fingerprint,
            ),
        )

    append_scan("F0001-S000001", "completed", "fp-1")
    append_scan("F0001-S000001", "failed", "fp-failed")
    append_scan("F0001-S000002", "failed", "fp-2")
    completed, fingerprints = terminology_scan_state(
        project,
        "TASK-1",
        ("F0001-S000001", "F0001-S000002"),
    )
    assert completed == {"F0001-S000001"}
    assert fingerprints == {"fp-1"}


@pytest.mark.parametrize("stage", ["proofreading", "polishing"])
def test_review_prompt_uses_conditional_fields(stage: str) -> None:
    prompt = full_prompt(stage, "Review carefully.")
    assert "accepted 仅含 type、id、status" in prompt
    assert "表示无条件保留 current_text" in prompt
    assert (
        '{"type":"segment","id":"1","status":"suggested",'
        '"suggested_text":"完整建议","reason":"原因"}'
        in prompt
    )
    assert "suggested 还须含非空完整 suggested_text" in prompt


def test_request_payload_uses_local_ids_without_mutating_source() -> None:
    items = [
        {"segment_id": "F0001-S000001", "source": "one"},
        {"segment_id": "F0001-S000002", "source": "two"},
    ]
    payload = {
        "reference_context": [{"source": "before"}],
        "segments": [
            {"id": item["segment_id"], "source": item["source"]}
            for item in items
        ],
    }

    localized, mapping = localize_request_ids(payload, items)

    assert [item["id"] for item in localized["segments"]] == ["1", "2"]
    assert mapping == {"1": "F0001-S000001", "2": "F0001-S000002"}
    assert payload["segments"][0]["id"] == "F0001-S000001"


def test_chunk_plans_are_iterated_lazily() -> None:
    source = segments()
    current = config()
    calls = 0

    def payload_builder(items: list[dict]) -> dict:
        nonlocal calls
        calls += 1
        return {
            "segments": [
                {"id": item["segment_id"], "source": item["source"]}
                for item in items
            ]
        }

    stream = iter_chunk_plans(
        [source[0], source[2], source[3]],
        all_segments=source,
        config=current,
        stage="translation",
        prompt=full_prompt("translation", "Translate."),
        payload_builder=payload_builder,
    )
    assert calls == 0
    first = next(iter(stream))
    assert first.segments[0]["segment_id"] == "F0001-S000001"
    assert calls > 0


def test_chunk_plans_keep_document_prompt_requirements_separate() -> None:
    current = config()
    current["chunking"]["cross_boundary_batching"] = ["translation"]
    source = [
        {
            "segment_id": "F0001-S000001",
            "file_id": "F0001",
            "part_id": "document",
            "line_index": 0,
            "source": "TXT",
            "is_empty": False,
        },
        {
            "segment_id": "F0002-S000001",
            "file_id": "F0002",
            "part_id": "document",
            "line_index": 0,
            "source": "EPUB",
            "is_empty": False,
        },
    ]
    requirements = {"F0001": None, "F0002": "EPUB markers"}
    plans = list(
        iter_chunk_plans(
            source,
            all_segments=source,
            config=current,
            stage="translation",
            prompt="base",
            payload_builder=lambda items: {
                "segments": [
                    {"id": item["segment_id"], "source": item["source"]}
                    for item in items
                ]
            },
            prompt_builder=lambda items: "base "
            + " ".join(
                value
                for value in (requirements[str(item["file_id"])] for item in items)
                if value
            ),
            partition_key=lambda item: requirements[str(item["file_id"])],
        )
    )
    assert [tuple(item["file_id"] for item in plan.segments) for plan in plans] == [
        ("F0001",),
        ("F0002",),
    ]


@pytest.mark.asyncio
async def test_parallel_dispatch_keeps_input_buffer_bounded() -> None:
    started: list[str] = []
    release = asyncio.Event()

    async def worker(chunk) -> str:
        started.append(chunk.file_id)
        await release.wait()
        return chunk.file_id

    chunks = (
        ChunkPlan(
            file_id="F0001",
            segments=(),
            payload={},
            estimated_input_tokens=1,
        )
        for _ in range(4)
    )
    task = asyncio.create_task(
        dispatch_chunks(chunks, worker, mode="parallel", max_parallel=2)
    )
    for _ in range(20):
        await asyncio.sleep(0)
        if len(started) == 2:
            break
    assert len(started) == 2
    release.set()
    assert len(await task) == 4


def test_scope_and_result_selection_preserve_old_success() -> None:
    source = segments()
    files = [{"file_id": "F0001", "file_order": 1}]
    selected = select_scope(source, files, Scope())
    history = [
        {
            "segment_id": "F0001-S000001",
            "status": "completed",
            "stage_fingerprint": "old",
        },
        {
            "segment_id": "F0001-S000001",
            "status": "failed",
            "stage_fingerprint": "new",
        },
    ]
    result = classify_stage(selected, history, force=False)
    assert [item["segment_id"] for item in result.reusable] == ["F0001-S000001"]
    assert result.last_attempt_failed == ("F0001-S000001",)
    assert "F0001-S000001" not in {
        item["segment_id"] for item in result.work
    }
    forced = classify_stage(selected, history, force=True)
    assert len(forced.work) == 4


def test_chunk_builder_crosses_empty_gaps_and_materializes_run_ids() -> None:
    current = config()
    prompt = full_prompt("translation", "Translate faithfully.")
    work = [segments()[0], segments()[2], segments()[3]]

    def payload_builder(items: list[dict]) -> dict:
        return {
            "segments": [
                {"id": item["segment_id"], "source": item["source"]} for item in items
            ]
        }

    plans = build_chunk_plans(
        work,
        all_segments=segments(),
        config=current,
        stage="translation",
        prompt=prompt,
        payload_builder=payload_builder,
    )
    assert [[item["line_index"] for item in plan.segments] for plan in plans] == [
        [0, 2, 3],
    ]
    chunks = list(materialize_chunk_stream("RUN-X", "translation", plans))
    assert all(chunk.chunk_id and "RUN-X" in chunk.chunk_id for chunk in chunks)


def test_materialize_chunk_stream_namespaces_continuation_chunks() -> None:
    plan = ChunkPlan(
        file_id="F0001",
        segments=tuple(segments()[:1]),
        payload={},
        estimated_input_tokens=1,
    )

    fresh = list(materialize_chunk_stream("RUN-X", "translation", [plan]))
    resumed = list(
        materialize_chunk_stream(
            "RUN-X",
            "translation",
            [plan],
            continuation_index=3,
        )
    )

    assert fresh[0].chunk_id == "CHK-RUN-X-TR-F0001-C00001"
    assert resumed[0].chunk_id == "CHK-RUN-X-TR-R0003-F0001-C00001"


def test_resume_debug_chunks_do_not_reuse_sqlite_record_ids(tmp_path: Path) -> None:
    project = _finalize_project(tmp_path)
    current = config()
    metadata = read_json(project, project / "project.json")
    run_id, run_dir = create_run(
        project,
        config=current,
        stage="translation",
        fingerprint="fingerprint",
        prompt="prompt",
        selected_count=1,
        requested_count=1,
        reused_count=0,
        details={"scope": {"all_nonempty": True}},
    )
    plan = ChunkPlan(
        file_id="F0001",
        segments=tuple(segments()[:1]),
        payload={},
        estimated_input_tokens=1,
    )
    fresh = next(materialize_chunk_stream(run_id, "translation", [plan]))
    save_debug_chunks(
        project,
        run_dir,
        str(metadata["project_id"]),
        run_id,
        "translation",
        [fresh],
    )

    resumed_id, resumed_dir, continuation_index = continue_run(
        project,
        run_id,
        config=current,
        stage="translation",
        fingerprint="fingerprint",
        prompt="prompt",
        scope=Scope(),
        selected_count=1,
        requested_count=1,
        reused_count=0,
    )
    assert resumed_id == run_id
    assert continuation_index == 1
    resumed = next(
        materialize_chunk_stream(
            resumed_id,
            "translation",
            [plan],
            continuation_index=continuation_index,
        )
    )
    save_debug_chunks(
        project,
        resumed_dir,
        str(metadata["project_id"]),
        resumed_id,
        "translation",
        [resumed],
    )

    records = read_jsonl(project, run_dir / "chunks.jsonl")
    assert [record["record_id"] for record in records] == [
        "CHK-" + run_id + "-TR-F0001-C00001",
        "CHK-" + run_id + "-TR-R0001-F0001-C00001",
    ]


@pytest.mark.asyncio
async def test_stage_storage_error_finalizes_run_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _finalize_project(tmp_path)
    current = config()
    current["debug"]["enabled"] = True
    metadata = read_json(project, project / "project.json")
    run_id, run_dir = create_run(
        project,
        config=current,
        stage="translation",
        fingerprint="fingerprint",
        prompt="prompt",
        selected_count=1,
        requested_count=1,
        reused_count=0,
        details={"scope": {"all_nonempty": True}},
    )
    state = StageRunState(
        project=project,
        stage="translation",
        config=current,
        metadata=metadata,
        segments=segments(),
        prompt="prompt",
        fingerprint="fingerprint",
        resume_run_id=None,
        warnings=[],
        run_id=run_id,
        run_dir=run_dir,
    )
    requests = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    def fail_debug(*_: object) -> None:
        raise StorageError("duplicate debug chunk")

    monkeypatch.setattr("app.stage_runtime.save_debug_chunks", fail_debug)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def noop_one(_: object) -> None:
        return None

    async def noop_zero() -> None:
        return None

    async def never_process(*_: object) -> None:
        raise AssertionError("storage failure must happen before dispatch")

    os.environ["LLM_API_KEY"] = "test"
    try:
        with pytest.raises(StorageError, match="duplicate debug chunk"):
            await _execute_stage_run(
                state,
                request_segments=[segments()[0]],
                part_original={},
                original_parts={},
                preflight_failed=[],
                limiter=SlidingWindowLimiter(0, 0),
                payload_builder=lambda _: {},
                prompt_builder=lambda _: "prompt",
                prompt_partition_key=lambda _: None,
                process_once=never_process,
                record_preflight_failure=noop_one,
                record_context_failure=noop_one,
                before_finalize=noop_zero,
                completed_count=lambda: 0,
                failed_count=lambda: 0,
                exception_completed=lambda: 0,
                exception_failed=lambda: 0,
                failure_counts=Counter(),
                http_client=client,
            )
    finally:
        os.environ.pop("LLM_API_KEY", None)
        await client.aclose()

    manifest = read_json(project, run_dir / "manifest.json")
    assert manifest["status"] == "failed"
    assert manifest["completed_segment_count"] == 0
    assert manifest["failed_segment_count"] == 0
    assert any("duplicate debug chunk" in warning for warning in manifest["warnings"])
    assert requests == 0


@pytest.mark.asyncio
async def test_stage_execution_resolves_keys_before_dispatch_when_no_chunks(
    tmp_path: Path,
) -> None:
    project = _finalize_project(tmp_path)
    current = config()
    metadata = read_json(project, project / "project.json")
    _run_id, run_dir = create_run(
        project,
        config=current,
        stage="translation",
        fingerprint="fingerprint",
        prompt="prompt",
        selected_count=0,
        requested_count=0,
        reused_count=0,
        details={"scope": {"all_nonempty": True}},
    )
    state = StageRunState(
        project=project,
        stage="translation",
        config=current,
        metadata=metadata,
        segments=segments(),
        prompt="prompt",
        fingerprint="fingerprint",
        resume_run_id=None,
        warnings=[],
        run_id=_run_id,
        run_dir=run_dir,
    )

    async def noop_one(_: object) -> None:
        return None

    async def noop_zero() -> None:
        return None

    os.environ["LLM_API_KEY"] = "test"
    try:
        await _execute_stage_run(
            state,
            request_segments=[],
            part_original={},
            original_parts={},
            preflight_failed=[],
            limiter=SlidingWindowLimiter(0, 0),
            payload_builder=lambda _: {},
            prompt_builder=lambda _: "prompt",
            prompt_partition_key=lambda _: None,
            process_once=lambda *_: noop_zero(),
            record_preflight_failure=noop_one,
            record_context_failure=noop_one,
            before_finalize=noop_zero,
            completed_count=lambda: 0,
            failed_count=lambda: 0,
            exception_completed=lambda: 0,
            exception_failed=lambda: 0,
            failure_counts=Counter(),
        )
    finally:
        os.environ.pop("LLM_API_KEY", None)

    manifest = read_json(project, run_dir / "manifest.json")
    assert manifest["key_audits"][0]["key_count"] == 1


def test_chunk_builder_only_crosses_gaps_made_entirely_of_empty_segments() -> None:
    source = [
        {
            "segment_id": f"F0001-S{index + 1:06d}",
            "file_id": "F0001",
            "part_id": "document",
            "line_index": index,
            "source": value,
            "is_empty": value == "" or value.isspace(),
        }
        for index, value in enumerate(["one", "\u3000", " \t", "four", "five"])
    ]
    source.append(
        {
            "segment_id": "F0002-S000001",
            "file_id": "F0002",
            "part_id": "document",
            "line_index": 0,
            "source": "other",
            "is_empty": False,
        }
    )
    work = [source[0], source[3], source[5]]
    plans = build_chunk_plans(
        work,
        all_segments=source,
        config=config(),
        stage="translation",
        prompt=full_prompt("translation", "Translate."),
        payload_builder=lambda items: {
            "segments": [
                {"id": item["segment_id"], "source": item["source"]}
                for item in items
            ]
        },
    )
    assert [[item["segment_id"] for item in plan.segments] for plan in plans] == [
        ["F0001-S000001", "F0001-S000004"],
        ["F0002-S000001"],
    ]

    plans = build_chunk_plans(
        [source[0], source[4]],
        all_segments=source,
        config=config(),
        stage="translation",
        prompt=full_prompt("translation", "Translate."),
        payload_builder=lambda items: {
            "segments": [
                {"id": item["segment_id"], "source": item["source"]}
                for item in items
            ]
        },
    )
    assert [len(plan.segments) for plan in plans] == [1, 1]


def test_chunk_and_context_stop_at_document_part_boundary() -> None:
    source = [
        {
            "segment_id": "F0001-S000001",
            "file_id": "F0001",
            "part_id": "OEBPS/text/ch1.xhtml",
            "line_index": 0,
            "source": "第一章",
            "is_empty": False,
        },
        {
            "segment_id": "F0001-S000002",
            "file_id": "F0001",
            "part_id": "OEBPS/text/ch1.xhtml",
            "line_index": 1,
            "source": "",
            "is_empty": True,
        },
        {
            "segment_id": "F0001-S000003",
            "file_id": "F0001",
            "part_id": "OEBPS/text/ch2.xhtml",
            "line_index": 2,
            "source": "第二章",
            "is_empty": False,
        },
        {
            "segment_id": "F0001-S000004",
            "file_id": "F0001",
            "part_id": "OEBPS/text/ch2.xhtml",
            "line_index": 3,
            "source": "第二章续",
            "is_empty": False,
        },
    ]
    plans = build_chunk_plans(
        [source[0], source[2], source[3]],
        all_segments=source,
        config=config(),
        stage="translation",
        prompt=full_prompt("translation", "Translate."),
        payload_builder=lambda items: {
            "segments": [
                {"id": item["segment_id"], "source": item["source"]}
                for item in items
            ]
        },
    )

    assert [[item["segment_id"] for item in plan.segments] for plan in plans] == [
        ["F0001-S000001"],
        ["F0001-S000003", "F0001-S000004"],
    ]
    context_index = PreviousContextIndex(source)
    assert context_index.previous(source[2], 3) == []
    assert context_index.previous(source[3], 3) == [{"source": "第二章"}]


def test_chunk_builder_can_cross_file_and_part_boundaries_when_enabled() -> None:
    source = [
        {
            "segment_id": "F0001-S000001",
            "file_id": "F0001",
            "part_id": "OEBPS/text/ch1.xhtml",
            "line_index": 0,
            "source": "第一章",
            "is_empty": False,
        },
        {
            "segment_id": "F0001-S000002",
            "file_id": "F0001",
            "part_id": "OEBPS/text/ch1.xhtml",
            "line_index": 1,
            "source": "",
            "is_empty": True,
        },
        {
            "segment_id": "F0001-S000003",
            "file_id": "F0001",
            "part_id": "OEBPS/text/ch2.xhtml",
            "line_index": 2,
            "source": "第二章",
            "is_empty": False,
        },
        {
            "segment_id": "F0002-S000001",
            "file_id": "F0002",
            "part_id": "document",
            "line_index": 0,
            "source": "另一个文件",
            "is_empty": False,
        },
    ]
    current = config()
    current["chunking"]["cross_boundary_batching"] = ["translation"]
    plans = build_chunk_plans(
        [source[0], source[2], source[3]],
        all_segments=source,
        config=current,
        stage="translation",
        prompt=full_prompt("translation", "Translate."),
        payload_builder=lambda items: {
            "segments": [
                {"id": item["segment_id"], "source": item["source"]}
                for item in items
            ]
        },
    )
    assert [
        [item["segment_id"] for item in plan.segments] for plan in plans
    ] == [["F0001-S000001", "F0001-S000003", "F0002-S000001"]]

    source[1]["source"] = "未选中的非空段"
    source[1]["is_empty"] = False
    plans = build_chunk_plans(
        [source[0], source[2]],
        all_segments=source,
        config=current,
        stage="translation",
        prompt=full_prompt("translation", "Translate."),
        payload_builder=lambda items: {
            "segments": [
                {"id": item["segment_id"], "source": item["source"]}
                for item in items
            ]
        },
    )
    assert [len(plan.segments) for plan in plans] == [1, 1]


def test_chunk_and_repair_grouping_follow_source_file_order_not_file_id() -> None:
    source = [
        {
            "segment_id": "F0002-S000001",
            "file_id": "F0002",
            "part_id": "document",
            "line_index": 0,
            "source": "second file in ID order",
            "is_empty": False,
        },
        {
            "segment_id": "F0001-S000001",
            "file_id": "F0001",
            "part_id": "document",
            "line_index": 0,
            "source": "first file in ID order",
            "is_empty": False,
        },
    ]
    payload_builder = lambda items: {
        "segments": [
            {"id": item["segment_id"], "source": item["source"]}
            for item in items
        ]
    }

    plans = build_chunk_plans(
        reversed(source),
        all_segments=source,
        config=config(),
        stage="translation",
        prompt=full_prompt("translation", "Translate."),
        payload_builder=payload_builder,
    )
    assert [plan.file_id for plan in plans] == ["F0002", "F0001"]

    cross_boundary_config = config()
    cross_boundary_config["chunking"]["cross_boundary_batching"] = ["translation"]
    plans = build_chunk_plans(
        reversed(source),
        all_segments=source,
        config=cross_boundary_config,
        stage="translation",
        prompt=full_prompt("translation", "Translate."),
        payload_builder=payload_builder,
    )
    assert [item["file_id"] for item in plans[0].segments] == ["F0002", "F0001"]
    repair_groups = contiguous_groups(
        reversed(source),
        all_segments=source,
        cross_boundary=True,
    )
    assert [item["file_id"] for item in repair_groups[0]] == ["F0002", "F0001"]


@pytest.mark.asyncio
async def test_ordered_dispatch_tracks_all_files_in_cross_boundary_chunk() -> None:
    started: list[str] = []
    release = asyncio.Event()

    def chunk(*file_ids: str) -> ChunkPlan:
        return ChunkPlan(
            file_id=file_ids[0],
            segments=tuple(
                {
                    "segment_id": f"{file_id}-S1",
                    "file_id": file_id,
                }
                for file_id in file_ids
            ),
            payload={},
            estimated_input_tokens=1,
        )

    async def worker(current: ChunkPlan) -> str:
        started.append(current.segments[0]["segment_id"])
        await release.wait()
        return current.segments[0]["segment_id"]

    chunks = iter((chunk("F0001", "F0002"), chunk("F0002", "F0003"), chunk("F0004")))
    task = asyncio.create_task(
        dispatch_chunks(chunks, worker, mode="ordered_by_file", max_parallel=2)
    )
    for _ in range(20):
        await asyncio.sleep(0)
        if len(started) == 2:
            break
    assert started == ["F0001-S1", "F0004-S1"]
    release.set()
    assert await task == ["F0001-S1", "F0002-S1", "F0004-S1"]


def test_chunk_builder_packs_alternating_empty_lines_near_soft_target() -> None:
    source: list[dict] = []
    for index in range(80):
        for value in (f"source text number {index:03d}", ""):
            line_index = len(source)
            source.append(
                {
                    "segment_id": f"F0001-S{line_index + 1:06d}",
                    "file_id": "F0001",
                    "part_id": "document",
                    "line_index": line_index,
                    "source": value,
                    "is_empty": value == "",
                }
            )
    current = config()
    current["chunking"]["target_chunk_input_tokens"] = 600
    work = [item for item in source if not item["is_empty"]]
    plans = build_chunk_plans(
        work,
        all_segments=source,
        config=current,
        stage="translation",
        prompt=full_prompt("translation", "Translate."),
        payload_builder=lambda items: {
            "segments": [
                {"id": item["segment_id"], "source": item["source"]}
                for item in items
            ]
        },
    )
    assert len(plans) <= 10
    assert any(plan.estimated_input_tokens >= 480 for plan in plans)
    assert all(plan.estimated_input_tokens <= 600 for plan in plans)
    assert plans[-1].estimated_input_tokens <= 600


def test_single_segment_may_exceed_soft_target_but_not_input_limit() -> None:
    current = config()
    current["chunking"]["target_chunk_input_tokens"] = 50
    source = [segments()[0]]
    plans = build_chunk_plans(
        source,
        all_segments=source,
        config=current,
        stage="translation",
        prompt=full_prompt("translation", "Translate."),
        payload_builder=lambda items: {
            "segments": [
                {"id": item["segment_id"], "source": item["source"]}
                for item in items
            ]
        },
    )
    assert len(plans) == 1
    assert plans[0].estimated_input_tokens > 50


def test_chunk_builder_splits_without_duplicating_segments() -> None:
    current = config()
    current["chunking"]["target_chunk_input_tokens"] = 75
    prompt = full_prompt("translation", "Translate.")
    work = [segments()[2], segments()[3], segments()[4]]

    def payload_builder(items: list[dict]) -> dict:
        return {
            "segments": [
                {
                    "id": item["segment_id"],
                    "source": str(item["source"]) * 30,
                }
                for item in items
            ]
        }

    plans = build_chunk_plans(
        work,
        all_segments=segments(),
        config=current,
        stage="translation",
        prompt=prompt,
        payload_builder=payload_builder,
    )
    ids = [
        item["segment_id"]
        for plan in plans
        for item in plan.segments
    ]
    assert ids == [item["segment_id"] for item in work]


def test_chunk_builder_ignores_disabled_itpm() -> None:
    current = config()
    current["execution"]["input_tokens_per_minute"] = 0
    prompt = full_prompt("translation", "Translate.")
    plans = build_chunk_plans(
        [segments()[0]],
        all_segments=segments(),
        config=current,
        stage="translation",
        prompt=prompt,
        payload_builder=lambda items: {
            "segments": [
                {"id": item["segment_id"], "source": item["source"]}
                for item in items
            ]
        },
    )
    assert len(plans) == 1
    assert plans[0].estimated_input_tokens > 0


def test_context_is_same_file_and_optional_target() -> None:
    source = segments()
    source.append(
        {
            "segment_id": "F0002-S000001",
            "file_id": "F0002",
            "part_id": "document",
            "line_index": 0,
            "source": "other",
            "is_empty": False,
        }
    )
    context = PreviousContextIndex(source).previous(
        source[4],
        2,
        target_resolver=lambda segment_id: f"translated:{segment_id}",
    )
    assert [item["source"] for item in context] == ["three", "four"]
    assert all("translation" in item for item in context)


def test_previous_context_index_matches_sparse_and_probe_segments() -> None:
    source = [
        {
            "segment_id": "S3",
            "file_id": "F1",
            "part_id": "P1",
            "line_index": 3,
            "source": "three",
            "model_source": "model-three",
            "is_empty": False,
        },
        {
            "segment_id": "S1",
            "file_id": "F1",
            "part_id": "P1",
            "line_index": 1,
            "source": "one",
            "model_source": "model-one",
            "is_empty": False,
        },
        {
            "segment_id": "S2",
            "file_id": "F1",
            "part_id": "P1",
            "line_index": 2,
            "source": "",
            "model_source": "",
            "is_empty": True,
        },
        {
            "segment_id": "S4",
            "file_id": "F2",
            "part_id": "P1",
            "line_index": 4,
            "source": "other file",
            "model_source": "other model",
            "is_empty": False,
        },
    ]
    index = PreviousContextIndex(source)
    probe = {**source[0], "segment_id": "S3-PROBE", "source": "probe"}
    resolver = lambda segment_id: f"translated:{segment_id}"
    assert index.previous(
        probe,
        2,
        target_resolver=resolver,
        source_key="model_source",
    ) == [
        {"source": "model-one", "translation": "translated:S1"},
    ]


@pytest.mark.asyncio
async def test_llm_client_retries_429_and_saves_debug(tmp_path: Path) -> None:
    current = config()
    current["debug"]["enabled"] = True
    current["retry"]["base_delay_seconds"] = 0
    current["retry"]["jitter_seconds"] = 0
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="slow")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"type":"end"}'}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    limiter = SlidingWindowLimiter(100, 100000)
    os.environ["LLM_API_KEY"] = "test"
    try:
        async with LLMClient(
            current,
            limiter,
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            response, request_id = await llm.chat(
                messages=render_messages("prompt", {"segments": []}),
                temperature=0.2,
                estimated_input_tokens=10,
            )
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()
    assert response.content == '{"type":"end"}'
    assert response.reasoning_content is None
    assert calls == 2
    assert len(list((tmp_path / "payloads").glob(f"{request_id}-A*.request.json"))) == 2
    assert (tmp_path / "attempts.jsonl").is_file()


@pytest.mark.asyncio
async def test_llm_client_changes_key_after_auth_failure_without_retry_budget(
    tmp_path: Path,
) -> None:
    current = load_global_config(ROOT)
    current["retry"]["http_max_attempts"] = 1
    current["execution"]["max_parallel"] = 1
    current["execution"]["max_parallel_per_key"] = 1
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorization = request.headers["Authorization"]
        calls.append(authorization)
        if authorization == "Bearer bad-key":
            return httpx.Response(401, text="invalid key")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"type":"end"}'}}],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "bad-key\ngood-key"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            response, _ = await llm.chat(
                messages=render_messages("prompt", {"segments": []}),
                temperature=0.2,
                estimated_input_tokens=10,
            )
            second_response, _ = await llm.chat(
                messages=render_messages("prompt", {"segments": []}),
                temperature=0.2,
                estimated_input_tokens=10,
            )
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()
    assert response.content == '{"type":"end"}'
    assert second_response.content == '{"type":"end"}'
    assert calls == ["Bearer bad-key", "Bearer good-key", "Bearer good-key"]


@pytest.mark.asyncio
async def test_llm_client_rotates_keys_on_429_without_consuming_round(
    tmp_path: Path,
) -> None:
    current = load_global_config(ROOT)
    current["retry"]["http_max_attempts"] = 1
    current["retry"]["base_delay_seconds"] = 0
    current["retry"]["jitter_seconds"] = 0
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["Authorization"])
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="slow")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"type":"end"}'}}],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "first-key\nsecond-key"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            response, _ = await llm.chat(
                messages=render_messages("prompt", {"segments": []}),
                temperature=0.2,
                estimated_input_tokens=10,
            )
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()
    assert response.content == '{"type":"end"}'
    assert calls == ["Bearer first-key", "Bearer second-key"]
    audit = llm.key_audit_summary(execution_index=0)
    assert audit["key_count"] == 2
    assert audit["keys"] == [
        {
            "key_index": 1,
            "request_count": 1,
            "attempt_count": 1,
            "authentication_error_count": 0,
            "rate_limit_count": 1,
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "available": False,
                "partial": False,
            },
        },
        {
            "key_index": 2,
            "request_count": 1,
            "attempt_count": 1,
            "authentication_error_count": 0,
            "rate_limit_count": 0,
            "usage": {
                "input_tokens": 3,
                "output_tokens": 2,
                "total_tokens": 5,
                "available": True,
                "partial": False,
            },
        },
    ]


@pytest.mark.asyncio
async def test_llm_client_bounds_all_key_429_by_retry_rounds(tmp_path: Path) -> None:
    current = load_global_config(ROOT)
    current["retry"]["http_max_attempts"] = 2
    current["retry"]["base_delay_seconds"] = 0
    current["retry"]["jitter_seconds"] = 0
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["Authorization"])
        return httpx.Response(429, headers={"Retry-After": "0"}, text="slow")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "first-key\nsecond-key"
    try:
        with pytest.raises(ExternalError, match="限流"):
            async with LLMClient(
                current,
                SlidingWindowLimiter(0, 0),
                run_dir=tmp_path,
                project_id="PRJ",
                run_id="RUN",
                stage="translation",
                client=client,
            ) as llm:
                await llm.chat(
                    messages=render_messages("prompt", {"segments": []}),
                    temperature=0.2,
                    estimated_input_tokens=10,
                )
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()
    assert calls == [
        "Bearer first-key",
        "Bearer second-key",
        "Bearer first-key",
        "Bearer second-key",
    ]


@pytest.mark.asyncio
async def test_llm_client_does_not_switch_key_for_configuration_error(
    tmp_path: Path,
) -> None:
    current = load_global_config(ROOT)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["Authorization"])
        return httpx.Response(400, text="invalid request")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "first-key\nsecond-key"
    try:
        with pytest.raises(FatalExternalError, match="配置错误"):
            async with LLMClient(
                current,
                SlidingWindowLimiter(0, 0),
                run_dir=tmp_path,
                project_id="PRJ",
                run_id="RUN",
                stage="translation",
                client=client,
            ) as llm:
                await llm.chat(
                    messages=render_messages("prompt", {"segments": []}),
                    temperature=0.2,
                    estimated_input_tokens=10,
                )
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()
    assert calls == ["Bearer first-key"]


@pytest.mark.asyncio
async def test_llm_client_warns_and_uses_backoff_for_invalid_retry_after(
    tmp_path: Path,
) -> None:
    current = load_global_config(ROOT)
    current["retry"]["http_max_attempts"] = 1
    current["retry"]["base_delay_seconds"] = 0
    current["retry"]["jitter_seconds"] = 0
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["Authorization"])
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "later"}, text="slow")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"type":"end"}'}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "first-key\nsecond-key"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            response, _ = await llm.chat(
                messages=render_messages("prompt", {"segments": []}),
                temperature=0.2,
                estimated_input_tokens=10,
            )
            warnings = list(llm.warnings)
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()
    assert response.content == '{"type":"end"}'
    assert calls == ["Bearer first-key", "Bearer second-key"]
    assert any("Retry-After 无效" in warning for warning in warnings)


@pytest.mark.asyncio
async def test_llm_client_extracts_embedded_reasoning(tmp_path: Path) -> None:
    current = config()
    current["debug"]["enabled"] = True

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                "\ufeff \r\n<thought>reasoning</thought>\r\n"
                                '{"type":"end"}'
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "test"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            response, _ = await llm.chat(
                messages=render_messages("prompt", {"segments": []}),
                temperature=0.2,
                estimated_input_tokens=10,
            )
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()

    assert response.content == '{"type":"end"}'
    assert response.reasoning_content == "reasoning"
    saved_response = next((tmp_path / "payloads").glob("*.response.json"))
    assert "<thought>reasoning</thought>" in saved_response.read_text("utf-8")
    assert not list(tmp_path.rglob("*reasoning*"))


@pytest.mark.asyncio
async def test_llm_client_rejects_structured_and_embedded_reasoning_together(
    tmp_path: Path,
) -> None:
    current = config()
    definition = dict(current["_llm_adapter"].definition)
    definition["response_reasoning_content_pointers"] = [
        "/choices/0/message/reasoning_content"
    ]
    adapter_file = tmp_path / "adapter.json"
    adapter_file.write_text(json.dumps(definition), encoding="utf-8")
    current["_llm_adapter"] = load_json_adapter(adapter_file)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                "<think>embedded</think>\n"
                                '{"type":"end"}'
                            ),
                            "reasoning_content": "structured",
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "test"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            with pytest.raises(ExternalError, match="同时包含结构化"):
                await llm.chat(
                    messages=render_messages("prompt", {"segments": []}),
                    temperature=0.2,
                    estimated_input_tokens=10,
                )
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()


@pytest.mark.asyncio
async def test_llm_client_sends_preset_extra_body(tmp_path: Path) -> None:
    current = config()
    current["_llm_extra_body"] = {
        "provider": {
            "order": ["anthropic", "google"],
            "allow_fallbacks": False,
        }
    }
    sent_payload: dict | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent_payload
        sent_payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"type":"end"}'}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "test"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            await llm.chat(
                messages=render_messages("prompt", {"segments": []}),
                temperature=0.2,
                estimated_input_tokens=10,
            )
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()

    assert sent_payload is not None
    assert sent_payload["provider"] == current["_llm_extra_body"]["provider"]


@pytest.mark.asyncio
async def test_llm_client_stops_on_auth_and_normal_mode_has_no_payloads(
    tmp_path: Path,
) -> None:
    current = config()
    current["debug"]["enabled"] = False
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, text="unauthorized")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "test"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(100, 100000),
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            with pytest.raises(FatalExternalError, match="鉴权失败"):
                await llm.chat(
                    messages=render_messages("prompt", {"segments": []}),
                    temperature=0.2,
                    estimated_input_tokens=10,
                )
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()
    assert calls == 1
    assert not (tmp_path / "payloads").exists()
    assert not (tmp_path / "attempts.jsonl").exists()


@pytest.mark.asyncio
async def test_llm_client_clamps_max_tokens_to_remaining_context(
    tmp_path: Path,
) -> None:
    current = config()
    current["llm"]["context_window_tokens"] = 1000
    current["llm"]["context_safety_margin_tokens"] = 100
    current["llm"]["max_output_tokens"] = 5000
    current["debug"]["enabled"] = False
    sent_payload: dict | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent_payload
        sent_payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"type":"end"}'}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "test"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            await llm.chat(
                messages=render_messages("prompt", {"segments": []}),
                temperature=0.2,
                estimated_input_tokens=250,
            )
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()
    assert sent_payload is not None
    assert sent_payload["max_tokens"] == 650
    assert len(llm.warnings) == 1
    assert "5000" in llm.warnings[0]
    assert "650" in llm.warnings[0]


@pytest.mark.asyncio
async def test_rate_limits_can_be_disabled_independently() -> None:
    now = [0.0]
    waits: list[float] = []

    async def sleeper(delay: float) -> None:
        waits.append(delay)
        now[0] += delay

    no_limits = SlidingWindowLimiter(
        0,
        0,
        clock=lambda: now[0],
        sleeper=sleeper,
    )
    await no_limits.acquire(1000000)
    await no_limits.acquire(1000000)
    assert not no_limits.records
    assert waits == []

    itpm_only = SlidingWindowLimiter(
        0,
        5,
        clock=lambda: now[0],
        sleeper=sleeper,
    )
    await itpm_only.acquire(3)
    await itpm_only.acquire(3)
    assert waits == [60.0]

    waits.clear()
    rpm_only = SlidingWindowLimiter(
        1,
        0,
        clock=lambda: now[0],
        sleeper=sleeper,
    )
    await rpm_only.acquire(1000000)
    await rpm_only.acquire(1000000)
    assert waits == [60.0]


@pytest.mark.asyncio
async def test_concurrent_requests_reserve_one_shared_rate_window() -> None:
    now = [0.0]
    waits: list[float] = []

    async def sleeper(delay: float) -> None:
        waits.append(delay)
        now[0] += delay

    limiter = SlidingWindowLimiter(
        1,
        0,
        clock=lambda: now[0],
        sleeper=sleeper,
    )
    await asyncio.gather(limiter.acquire(10), limiter.acquire(10))
    assert waits == [60.0]
    assert len(limiter.records) == 1


@pytest.mark.asyncio
async def test_rpm_paces_concurrent_admissions_evenly() -> None:
    now = [0.0]
    admissions: list[float] = []

    async def sleeper(delay: float) -> None:
        now[0] += delay

    limiter = SlidingWindowLimiter(
        15,
        0,
        clock=lambda: now[0],
        sleeper=sleeper,
    )

    async def acquire() -> None:
        await limiter.acquire(10)
        admissions.append(now[0])

    await asyncio.gather(*(acquire() for _ in range(15)))
    assert admissions == [float(index * 4) for index in range(15)]


@pytest.mark.asyncio
async def test_rpm_and_itpm_use_the_later_admission_time() -> None:
    now = [0.0]
    waits: list[float] = []

    async def sleeper(delay: float) -> None:
        waits.append(delay)
        now[0] += delay

    limiter = SlidingWindowLimiter(
        60,
        5,
        clock=lambda: now[0],
        sleeper=sleeper,
    )
    await limiter.acquire(3)
    await limiter.acquire(3)
    assert waits == [60.0]


def test_token_safety_factor_below_one_scales_estimate() -> None:
    messages = render_messages("prompt", {"segments": [{"source": "one"}]})
    assert estimate_messages(messages, 0.5) < estimate_messages(messages, 1.0)


@pytest.mark.asyncio
async def test_llm_client_accumulates_usage_across_requests(tmp_path: Path) -> None:
    current = config()
    responses = [
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
    ]
    calls = 0
    observed: list[dict[str, object] | None] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        usage = responses[min(calls - 1, len(responses) - 1)]
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"type":"end"}'}}],
                "usage": usage,
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "test"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
            on_usage=observed.append,
        ) as llm:
            for _ in range(3):
                await llm.chat(
                    messages=render_messages("prompt", {"segments": []}),
                    temperature=0.2,
                    estimated_input_tokens=10,
                )
            assert llm.usage_summary() == {
                "input_tokens": 14,
                "output_tokens": 11,
                "total_tokens": 25,
                "available": True,
                "partial": False,
            }
            assert observed[-1] == llm.usage_summary()
            assert len(observed) == 3
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()


@pytest.mark.asyncio
async def test_llm_client_marks_usage_unavailable_when_omitted(
    tmp_path: Path,
) -> None:
    current = config()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"type":"end"}'}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "test"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            await llm.chat(
                messages=render_messages("prompt", {"segments": []}),
                temperature=0.2,
                estimated_input_tokens=10,
            )
            summary = llm.usage_summary()
            assert summary is not None
            assert summary["available"] is False
            assert summary["partial"] is False
            assert summary["input_tokens"] == 0
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()


@pytest.mark.parametrize(
    "second_usage",
    [
        None,
        {"prompt_tokens": "invalid", "completion_tokens": 3, "total_tokens": 5},
    ],
)
@pytest.mark.asyncio
async def test_llm_client_preserves_observed_usage_as_partial(
    tmp_path: Path,
    second_usage: object,
) -> None:
    current = config()
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload: dict[str, object] = {
            "choices": [{"message": {"content": '{"type":"end"}'}}],
        }
        usage = (
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
            if calls == 1
            else second_usage
        )
        if usage is not None:
            payload["usage"] = usage
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "test"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            for _ in range(2):
                await llm.chat(
                    messages=render_messages("prompt", {"segments": []}),
                    temperature=0.2,
                    estimated_input_tokens=10,
                )
            assert llm.usage_summary() == {
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "available": False,
                "partial": True,
            }
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()


@pytest.mark.asyncio
async def test_llm_client_marks_missing_usage_on_failed_attempt_as_partial(
    tmp_path: Path,
) -> None:
    current = config()
    current["retry"]["http_max_attempts"] = 2
    current["retry"]["base_delay_seconds"] = 0
    current["retry"]["jitter_seconds"] = 0
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500, text="temporary")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"type":"end"}'}}],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "test"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            await llm.chat(
                messages=render_messages("prompt", {"segments": []}),
                temperature=0.2,
                estimated_input_tokens=10,
            )
            assert llm.usage_summary() == {
                "input_tokens": 3,
                "output_tokens": 2,
                "total_tokens": 5,
                "available": False,
                "partial": True,
            }
    finally:
        os.environ.pop("LLM_API_KEY", None)
        await client.aclose()


@pytest.mark.asyncio
async def test_diagnostics_records_retry_round_at_send_time(tmp_path: Path) -> None:
    current = config()
    current["retry"]["http_max_attempts"] = 2
    current["retry"]["base_delay_seconds"] = 0
    current["retry"]["jitter_seconds"] = 0
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500, text="temporary")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"type":"end"}'}}]},
        )

    diagnostics = Diagnostics(tmp_path / "logs" / "app.log")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "test"
    try:
        with diagnostics.activate("sample", "translation"):
            async with LLMClient(
                current,
                SlidingWindowLimiter(0, 0),
                run_dir=tmp_path,
                project_id="PRJ",
                run_id="RUN",
                stage="translation",
                client=client,
            ) as llm:
                await llm.chat(
                    messages=render_messages("prompt", {"segments": []}),
                    temperature=0.2,
                    estimated_input_tokens=10,
                )
        item = diagnostics.snapshot()["requests"]["items"][0]
        detail = diagnostics.request_detail(item["request_id"])
        assert [attempt["retry_round"] for attempt in detail["attempts"]] == [1, 2]
    finally:
        os.environ.pop("LLM_API_KEY", None)
        await client.aclose()


@pytest.mark.asyncio
async def test_llm_client_without_usage_mapping_has_no_summary(
    tmp_path: Path,
) -> None:
    current = config()
    definition = dict(current["_llm_adapter"].definition)
    definition.pop("usage")
    adapter_file = tmp_path / "adapter.json"
    adapter_file.write_text(json.dumps(definition), encoding="utf-8")
    current["_llm_adapter"] = load_json_adapter(adapter_file)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"type":"end"}'}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "test"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            await llm.chat(
                messages=render_messages("prompt", {"segments": []}),
                temperature=0.2,
                estimated_input_tokens=10,
            )
            assert llm.usage_summary() is None
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()


def test_finalize_run_records_usage_in_manifest(tmp_path: Path) -> None:
    project = _finalize_project(tmp_path)
    run_dir = project / "runs" / "RUN-TEST"
    run_dir.mkdir(parents=True)

    def write_manifest() -> None:
        write_json(
            project,
            run_dir / "manifest.json",
            _run_manifest(project),
        )

    usage = {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "available": True,
    }
    write_manifest()
    finalize_run(project, run_dir, status="completed", completed=2, failed=0, usage=usage)
    manifest = read_json(project, run_dir / "manifest.json")
    assert manifest["usage"] == usage
    assert manifest["status"] == "completed"
    write_manifest()
    finalize_run(project, run_dir, status="failed", completed=0, failed=1)
    manifest = read_json(project, run_dir / "manifest.json")
    assert "usage" not in manifest


def test_finalize_run_appends_key_audit(tmp_path: Path) -> None:
    project = _finalize_project(tmp_path)
    current = config()
    run_id, run_dir = create_run(
        project,
        config=current,
        stage="translation",
        fingerprint="fingerprint",
        prompt="prompt",
        selected_count=0,
        requested_count=0,
        reused_count=0,
    )
    audit = {
        "credential": {"kind": "environment", "name": "LLM_API_KEY"},
        "execution_index": 0,
        "key_count": 1,
        "keys": [],
    }
    finalize_run(
        project,
        run_dir,
        status="completed",
        completed=0,
        failed=0,
        key_audit=audit,
    )
    manifest = read_json(project, run_dir / "manifest.json")
    assert manifest["key_audits"] == [audit]
    assert manifest["status"] == "completed"


def test_combine_usage_accumulates_observed_lower_bounds() -> None:
    exact = {
        "input_tokens": 10,
        "output_tokens": 4,
        "total_tokens": 14,
        "available": True,
    }
    assert combine_usage(
        exact,
        {
            "input_tokens": 3,
            "output_tokens": 2,
            "total_tokens": 5,
            "available": False,
            "partial": True,
        },
    ) == {
        "input_tokens": 13,
        "output_tokens": 6,
        "total_tokens": 19,
        "available": False,
        "partial": True,
    }
    assert combine_usage(exact, None) == {
        "input_tokens": 10,
        "output_tokens": 4,
        "total_tokens": 14,
        "available": False,
        "partial": True,
    }
    assert combine_usage(
        {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2, "available": False},
        exact,
    ) == {
        "input_tokens": 10,
        "output_tokens": 4,
        "total_tokens": 14,
        "available": False,
        "partial": True,
    }
    assert combine_usage(None, None) == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "available": False,
        "partial": False,
    }


def test_finalize_run_accumulates_exact_usage_across_continuations(
    tmp_path: Path,
) -> None:
    project = _finalize_project(tmp_path)
    run_dir = project / "runs" / "RUN-TEST"
    run_dir.mkdir(parents=True)
    write_json(
        project,
        run_dir / "manifest.json",
        _run_manifest(
            project,
            usage_invocation_count=1,
            usage={
                "input_tokens": 10,
                "output_tokens": 4,
                "total_tokens": 14,
                "available": True,
            },
        ),
    )

    combined = finalize_run(
        project,
        run_dir,
        status="completed",
        completed=2,
        failed=0,
        usage={
            "input_tokens": 7,
            "output_tokens": 3,
            "total_tokens": 10,
            "available": True,
        },
    )

    assert combined == {
        "input_tokens": 17,
        "output_tokens": 7,
        "total_tokens": 24,
        "available": True,
        "partial": False,
    }
    manifest = read_json(project, run_dir / "manifest.json")
    assert manifest["usage"] == combined
    assert manifest["usage_invocation_count"] == 2


@pytest.mark.parametrize(
    "current",
    [
        None,
        {
            "input_tokens": 7,
            "output_tokens": 3,
            "total_tokens": 10,
            "available": False,
        },
    ],
)
def test_finalize_run_preserves_lower_bound_for_incomplete_continuation_usage(
    tmp_path: Path, current: dict[str, object] | None
) -> None:
    project = _finalize_project(tmp_path)
    run_dir = project / "runs" / "RUN-TEST"
    run_dir.mkdir(parents=True)
    write_json(
        project,
        run_dir / "manifest.json",
        _run_manifest(
            project,
            continuations=[{"started_at": "now"}],
            usage={
                "input_tokens": 10,
                "output_tokens": 4,
                "total_tokens": 14,
                "available": True,
            },
        ),
    )

    usage = finalize_run(
        project,
        run_dir,
        status="completed",
        completed=2,
        failed=0,
        usage=current,
    )

    assert usage == {
        "input_tokens": 10,
        "output_tokens": 4,
        "total_tokens": 14,
        "available": False,
        "partial": True,
    }


def _use_adapter(config: dict, name: str) -> None:
    config["_llm_adapter"] = load_json_adapter(
        ROOT / "llm_adapters" / f"{name}.json"
    )
    config["_llm_adapter_hash"] = config["_llm_adapter"].digest


@pytest.mark.asyncio
async def test_llm_client_sends_anthropic_format_request(tmp_path: Path) -> None:
    current = config()
    _use_adapter(current, "anthropic")
    sent: dict | None = None
    sent_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent, sent_headers
        sent = json.loads(request.content)
        sent_headers = dict(request.headers)
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": '{"type":"end"}'}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "test"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            response, _ = await llm.chat(
                messages=render_messages("prompt", {"segments": []}),
                temperature=0.2,
                estimated_input_tokens=10,
            )
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()

    assert response.content == '{"type":"end"}'
    assert response.reasoning_content is None
    assert sent is not None
    assert sent_headers["x-api-key"] == "test"
    assert sent_headers["anthropic-version"] == "2023-06-01"
    assert sent["system"] == "prompt"
    assert sent["messages"] == [
        {"role": "user", "content": '{"segments":[]}'}
    ]
    assert sent["max_tokens"] == current["llm"]["max_output_tokens"]
    assert sent["stream"] is False


@pytest.mark.asyncio
async def test_llm_client_sends_gemini_format_request(tmp_path: Path) -> None:
    current = config()
    _use_adapter(current, "google-gemini")
    current["llm"]["model"] = "gemini-2.5-flash"
    current["llm"]["base_url"] = "https://example.com/v1beta"
    current["llm"]["endpoint"] = "/models/${model}:generateContent"
    sent: dict | None = None
    sent_url = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent, sent_url
        sent_url = str(request.url)
        sent = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {"text": "thought", "thought": True},
                                {"text": '{"type":"end"}'},
                            ],
                        },
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "test"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            response, _ = await llm.chat(
                messages=render_messages("prompt", {"segments": []}),
                temperature=0.2,
                estimated_input_tokens=10,
            )
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()

    assert response.content == '{"type":"end"}'
    assert response.reasoning_content is None
    assert sent_url == (
        "https://example.com/v1beta/models/gemini-2.5-flash:generateContent"
    )
    assert sent is not None
    assert sent["system_instruction"] == {"parts": [{"text": "prompt"}]}
    assert sent["contents"] == [
        {"role": "user", "parts": [{"text": '{"segments":[]}'}]}
    ]
    assert sent["generationConfig"] == {
        "temperature": 0.2,
        "maxOutputTokens": current["llm"]["max_output_tokens"],
    }
    assert "api_key" not in sent_url


@pytest.mark.asyncio
async def test_llm_client_sends_openai_responses_format_request(
    tmp_path: Path,
) -> None:
    current = config()
    _use_adapter(current, "openai-responses")
    current["llm"]["endpoint"] = "/responses"
    sent: dict | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "reasoning",
                        "id": "rs_123",
                        "summary": [],
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"type":"end"}',
                                "annotations": [],
                            }
                        ],
                    },
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    os.environ["LLM_API_KEY"] = "test"
    try:
        async with LLMClient(
            current,
            SlidingWindowLimiter(0, 0),
            run_dir=tmp_path,
            project_id="PRJ",
            run_id="RUN",
            stage="translation",
            client=client,
        ) as llm:
            response, _ = await llm.chat(
                messages=render_messages("prompt", {"segments": []}),
                temperature=0.2,
                estimated_input_tokens=10,
            )
    finally:
        del os.environ["LLM_API_KEY"]
        await client.aclose()

    assert response.content == '{"type":"end"}'
    assert sent is not None
    assert sent["input"] == render_messages("prompt", {"segments": []})
    assert sent["store"] is False
    assert sent["stream"] is False
