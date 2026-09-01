from __future__ import annotations

import json
import os
import re
import sqlite3
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from app.config import load_project_config
from app.errors import ConfigError, IncompleteError, UsageError
from app.execution import Scope
from app.main import run
from app.project import add_project_files
from app.stages import (
    export_project,
    inspect_full,
    run_all,
    run_apply,
    run_review,
    run_translation,
)
from app.sqlite_storage import read_jsonl
from tests.helpers import llm_jsonl, use_llm_preset
from tests.test_terminology_translation import create_project


def workflow_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    system = body["messages"][0]["content"]
    payload = json.loads(body["messages"][1]["content"])
    if 'type="term"' in system:
        records = []
    elif "完整 translation" in system:
        records = [
            {
                "type": "segment",
                "id": item["id"],
                "translation": f"译:{item['source']}",
            }
            for item in payload["segments"]
        ]
    else:
        records = [
            {
                "type": "segment",
                "id": item["id"],
                "status": "suggested",
                "suggested_text": (
                    f"润:{item['current_text']}"
                    if "改善译文表达" in system
                    else f"校:{item['current_text']}"
                ),
                "reason": "test",
            }
            for item in payload["segments"]
        ]
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
    )


@pytest.mark.asyncio
async def test_review_apply_and_bilingual_export(tmp_path: Path) -> None:
    project = await create_project(
        tmp_path, "one\n\u3000\n \t\ntwo", encoding="utf-8-sig"
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(workflow_handler))
    proof_progress: list[tuple[int, int, int]] = []
    try:
        await run_translation(project, Scope(), http_client=client)
        proof = await run_review(
            project,
            "proofreading",
            Scope(),
            http_client=client,
            on_progress=lambda completed, failed, total: proof_progress.append(
                (completed, failed, total)
            ),
        )
        applied = run_apply(
            project,
            "proofreading",
            Scope(),
            allow_outdated_base=False,
            confirmed_all=True,
        )
        polish = await run_review(
            project, "polishing", Scope(), http_client=client
        )
        polished = run_apply(
            project,
            "polishing",
            Scope(),
            allow_outdated_base=False,
            confirmed_all=True,
        )
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]

    assert proof_progress[0] == (0, 0, 2)
    assert proof_progress[-1] == (2, 0, 2)
    assert proof["completed"] == 2
    assert applied["completed"] == 2
    assert polish["completed"] == 2
    assert polished["completed"] == 2

    mono = export_project(
        project, "proofread", bilingual=False, allow_missing=False
    )
    bilingual = export_project(
        project, "polished", bilingual=True, allow_missing=False
    )
    assert mono["files"] == bilingual["files"] == 1
    mono_text = (
        project / "output" / "proofread" / "source.txt"
    ).read_text(encoding="utf-8-sig")
    bilingual_text = (
        project / "output" / "bilingual" / "polished" / "source.txt"
    ).read_text(encoding="utf-8-sig")
    assert mono_text.splitlines() == ["校:译:one", "", "", "校:译:two"]
    assert bilingual_text.splitlines() == [
        "one",
        "润:校:译:one",
        "",
        "",
        "two",
        "润:校:译:two",
    ]


@pytest.mark.asyncio
async def test_review_apply_and_export_restore_source_indentation(
    tmp_path: Path,
) -> None:
    source = " \t\u3000one"
    project = await create_project(tmp_path, source, encoding="utf-8-sig")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        payload = json.loads(body["messages"][1]["content"])
        if "完整 translation" in system:
            records = [
                {
                    "type": "segment",
                    "id": item["id"],
                    "translation": "\u00a0translated  \t",
                }
                for item in payload["segments"]
            ]
        else:
            records = [
                {
                    "type": "segment",
                    "id": item["id"],
                    "status": "suggested",
                    "suggested_text": "\t\u3000fixed  \t",
                    "reason": "test",
                }
                for item in payload["segments"]
            ]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await run_translation(project, Scope(), http_client=client)
        await run_review(project, "proofreading", Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]

    translation = read_jsonl(project, project / "stages" / "translation.jsonl")[-1]
    suggestion = read_jsonl(project, project / "stages" / "proofreading.jsonl")[-1]
    assert translation["text"] == " \t\u3000translated  \t"
    assert suggestion["suggested_text"] == " \t\u3000fixed  \t"

    run_apply(
        project,
        "proofreading",
        Scope(),
        allow_outdated_base=False,
        confirmed_all=True,
    )
    applied_path = project / "stages" / "proofreading_applied.jsonl"
    applied = read_jsonl(project, applied_path)
    assert applied[-1]["text"] == " \t\u3000fixed  \t"

    # Simulate a result written before local whitespace protection existed.
    applied[-1]["text"] = "\tlegacy  \t"
    connection = sqlite3.connect(project / "project.sqlite")
    try:
        with connection:
            connection.execute(
                "UPDATE stage_results SET payload_json = ? WHERE record_id = ?",
                (
                    json.dumps(applied[-1], ensure_ascii=False, separators=(",", ":")),
                    applied[-1]["record_id"],
                ),
            )
    finally:
        connection.close()
    export_project(project, "proofread", bilingual=False, allow_missing=False)
    export_project(project, "proofread", bilingual=True, allow_missing=False)
    assert (
        project / "output" / "proofread" / "source.txt"
    ).read_text(encoding="utf-8-sig") == " \t\u3000legacy  \t"
    assert (
        project / "output" / "bilingual" / "proofread" / "source.txt"
    ).read_text(encoding="utf-8-sig") == (
        f"{source}\n \t\u3000legacy  \t"
    )


@pytest.mark.asyncio
async def test_apply_rejects_outdated_base(tmp_path: Path) -> None:
    project = await create_project(tmp_path, "one")
    client = httpx.AsyncClient(transport=httpx.MockTransport(workflow_handler))
    try:
        await run_translation(project, Scope(), http_client=client)
        await run_review(project, "proofreading", Scope(), http_client=client)
        await run_translation(project, Scope(force=True), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    with pytest.raises(IncompleteError, match="旧上游"):
        run_apply(
            project,
            "proofreading",
            Scope(),
            allow_outdated_base=False,
            confirmed_all=True,
        )
    summary = run_apply(
        project,
        "proofreading",
        Scope(),
        allow_outdated_base=True,
        confirmed_all=True,
    )
    assert summary["completed"] == 1
    assert summary["warnings"]


@pytest.mark.asyncio
async def test_review_missing_upstream_creates_no_run(tmp_path: Path) -> None:
    project = await create_project(tmp_path, "one")
    before = list((project / "runs").iterdir())
    try:
        with pytest.raises(IncompleteError, match="缺少上游结果"):
            await run_review(project, "proofreading", Scope())
    finally:
        del os.environ["LLM_API_KEY"]
    assert list((project / "runs").iterdir()) == before


@pytest.mark.asyncio
async def test_run_all_generates_suggestions_without_apply(tmp_path: Path) -> None:
    project = await create_project(tmp_path, "one\ntwo")
    client = httpx.AsyncClient(transport=httpx.MockTransport(workflow_handler))
    try:
        summary = await run_all(project, Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    assert [step["stage"] for step in summary["steps"]] == [
        "terminology",
        "translation",
        "proofreading",
        "polishing",
    ]
    assert read_jsonl(project, project / "stages" / "proofreading.jsonl")
    assert read_jsonl(project, project / "stages" / "polishing.jsonl")
    assert not (project / "stages" / "proofreading_applied.jsonl").exists()
    assert not (project / "stages" / "polishing_applied.jsonl").exists()


@pytest.mark.asyncio
async def test_run_all_scans_terms_for_new_files_after_completed_task(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "one")
    client = httpx.AsyncClient(transport=httpx.MockTransport(workflow_handler))
    try:
        first = await run_all(project, Scope(), http_client=client)
        second = await run_all(project, Scope(), http_client=client)
        source = tmp_path / "second.txt"
        source.write_text("two", encoding="utf-8")
        add_project_files(project, [str(source)])
        third = await run_all(
            project,
            Scope(),
            http_client=client,
            reuse_mixed_fingerprints=True,
        )
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]

    assert [step["stage"] for step in first["steps"]] == [
        "terminology",
        "translation",
        "proofreading",
        "polishing",
    ]
    assert [step["stage"] for step in second["steps"]] == [
        "translation",
        "proofreading",
        "polishing",
    ]
    assert [step["stage"] for step in third["steps"]] == [
        "terminology",
        "translation",
        "proofreading",
        "polishing",
    ]


@pytest.mark.asyncio
async def test_run_all_dry_run_plans_without_writing(tmp_path: Path) -> None:
    project = await create_project(tmp_path, "one\ntwo")
    before = {
        path.relative_to(project): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
    }
    summary = await run_all(project, Scope(dry_run=True))
    after = {
        path.relative_to(project): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
    }
    assert [step["stage"] for step in summary["steps"]] == [
        "terminology",
        "translation",
        "proofreading",
        "polishing",
    ]
    assert before == after
    del os.environ["LLM_API_KEY"]


@pytest.mark.asyncio
async def test_run_all_shares_production_client_and_limiter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = await create_project(tmp_path, "one")
    clients: list[object] = []
    calls: list[tuple[str, object, object]] = []

    class DummyClient:
        def __init__(self, **_: object) -> None:
            clients.append(self)
            self.closed = False

        async def __aenter__(self) -> "DummyClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            self.closed = True

    async def fake_terminology(
        _project: Path,
        _scope: Scope,
        **kwargs: object,
    ) -> dict:
        assert kwargs["reuse_mixed_fingerprints"] is True
        calls.append(
            ("terminology", kwargs["http_client"], kwargs["limiter"])
        )
        return {"stage": "terminology", "failed": 0, "pending": 0}

    async def fake_translation(
        _project: Path,
        _scope: Scope,
        **kwargs: object,
    ) -> dict:
        assert kwargs["reuse_mixed_fingerprints"] is True
        calls.append(
            ("translation", kwargs["http_client"], kwargs["limiter"])
        )
        return {"stage": "translation", "failed": 0, "pending": 0}

    async def fake_review(
        _project: Path,
        stage: str,
        _scope: Scope,
        **kwargs: object,
    ) -> dict:
        assert kwargs["reuse_mixed_fingerprints"] is True
        calls.append((stage, kwargs["http_client"], kwargs["limiter"]))
        return {"stage": stage, "failed": 0, "pending": 0}

    monkeypatch.setattr("app.stages.httpx.AsyncClient", DummyClient)
    monkeypatch.setattr("app.stages.run_terminology", fake_terminology)
    monkeypatch.setattr("app.stages.run_translation", fake_translation)
    monkeypatch.setattr("app.stages.run_review", fake_review)
    try:
        await run_all(
            project,
            Scope(),
            reuse_mixed_fingerprints=True,
        )
    finally:
        del os.environ["LLM_API_KEY"]

    assert len(clients) == 1
    assert clients[0].closed
    assert {id(client) for _, client, _ in calls} == {id(clients[0])}
    assert len({id(limiter) for _, _, limiter in calls}) == 1


@pytest.mark.asyncio
async def test_run_all_aggregates_progress_and_latest_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = await create_project(tmp_path, "one")
    progress: list[tuple[int, int, int]] = []
    usage: list[dict[str, object] | None] = []

    def exact(value: int) -> dict[str, object]:
        return {
            "input_tokens": value,
            "output_tokens": value,
            "total_tokens": value * 2,
            "available": True,
            "partial": False,
        }

    async def fake_terminology(
        _project: Path,
        _scope: Scope,
        **kwargs: object,
    ) -> dict[str, object]:
        kwargs["on_progress"](0, 0, 1)
        kwargs["on_usage"](exact(1))
        kwargs["on_usage"](exact(3))
        kwargs["on_progress"](1, 0, 1)
        return {"stage": "terminology", "selected": 1, "requested": 1, "reused": 0,
                "completed": 1, "failed": 0, "pending": 0, "usage": exact(3)}

    async def fake_translation(
        _project: Path,
        _scope: Scope,
        **kwargs: object,
    ) -> dict[str, object]:
        kwargs["on_progress"](0, 0, 1)
        kwargs["on_usage"](exact(5))
        kwargs["on_progress"](1, 0, 1)
        return {"stage": "translation", "selected": 1, "requested": 1, "reused": 0,
                "completed": 1, "failed": 0, "pending": 0, "usage": exact(5)}

    async def fake_review(
        _project: Path,
        stage: str,
        _scope: Scope,
        **kwargs: object,
    ) -> dict[str, object]:
        value = 7 if stage == "proofreading" else 9
        kwargs["on_progress"](0, 0, 1)
        kwargs["on_usage"](exact(value))
        kwargs["on_progress"](1, 0, 1)
        return {"stage": stage, "selected": 1, "requested": 1, "reused": 0,
                "completed": 1, "failed": 0, "pending": 0, "usage": exact(value)}

    monkeypatch.setattr("app.stages.run_terminology", fake_terminology)
    monkeypatch.setattr("app.stages.run_translation", fake_translation)
    monkeypatch.setattr("app.stages.run_review", fake_review)
    client = httpx.AsyncClient()
    try:
        summary = await run_all(
            project,
            Scope(),
            http_client=client,
            on_progress=lambda completed, failed, total: progress.append(
                (completed, failed, total)
            ),
            on_usage=usage.append,
        )
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]

    assert progress[0] == (0, 0, 1)
    assert progress[-1] == (4, 0, 4)
    assert summary["selected"] == summary["requested"] == 4
    assert summary["completed"] == 4
    assert summary["failed"] == summary["pending"] == 0
    assert summary["usage"] == {
        "input_tokens": 24,
        "output_tokens": 24,
        "total_tokens": 48,
        "available": True,
        "partial": False,
    }
    assert usage[-1] == summary["usage"]


@pytest.mark.asyncio
async def test_run_all_separates_clients_and_limiters_by_preset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = await create_project(tmp_path, "one")
    default = load_project_config(project)
    alternate = deepcopy(default)
    alternate["_llm_preset_id"] = "alternate"
    alternate["_llm_preset_hash"] = "sha256:alternate"
    clients: list[object] = []
    calls: list[tuple[str, object, object]] = []

    class DummyClient:
        def __init__(self, **_: object) -> None:
            clients.append(self)

        async def __aenter__(self) -> "DummyClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    async def fake_stage(
        _project: Path, _scope: Scope, **kwargs: object
    ) -> dict[str, object]:
        stage = "translation"
        calls.append((stage, kwargs["http_client"], kwargs["limiter"]))
        return {"stage": stage, "failed": 0, "pending": 0}

    async def fake_review(
        _project: Path, stage: str, _scope: Scope, **kwargs: object
    ) -> dict[str, object]:
        calls.append((stage, kwargs["http_client"], kwargs["limiter"]))
        return {"stage": stage, "failed": 0, "pending": 0}

    monkeypatch.setattr(
        "app.stages.load_project_config",
        lambda _project, *, stage=None: (
            alternate if stage in {"proofreading", "polishing"} else default
        ),
    )
    monkeypatch.setattr("app.stages.httpx.AsyncClient", DummyClient)
    monkeypatch.setattr("app.stages.run_translation", fake_stage)
    monkeypatch.setattr("app.stages.run_review", fake_review)
    monkeypatch.setattr(
        "app.stages.load_terms", lambda _project: {"terms_revision": 1}
    )
    try:
        await run_all(project, Scope())
    finally:
        del os.environ["LLM_API_KEY"]

    assert len(clients) == 2
    assert len({id(client) for _, client, _ in calls}) == 2
    assert len({id(limiter) for _, _, limiter in calls}) == 2
    assert calls[1][1:] == calls[2][1:]


@pytest.mark.asyncio
async def test_run_all_rejects_inconsistent_shared_limits_without_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = await create_project(tmp_path, "one")
    default = load_project_config(project)
    alternate = deepcopy(default)

    def changed_config(
        _project: Path, *, stage: str | None = None
    ) -> dict[str, object]:
        config = alternate if stage == "proofreading" else default
        if stage in {"translation", "proofreading"}:
            config = deepcopy(config)
            config["_llm_preset_id"] = "shared"
            config["_llm_preset_hash"] = "same"
            config["execution"] = {
                **config["execution"],
                "requests_per_minute": 1 if stage == "translation" else 2,
            }
        return config

    monkeypatch.setattr("app.stages.load_project_config", changed_config)

    with pytest.raises(ConfigError, match="共享限流配置不一致"):
        await run_all(project, Scope(dry_run=True))


@pytest.mark.asyncio
async def test_review_format_retry_regroups_around_valid_nonempty_segment(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "one\ntwo\nthree")
    review_calls: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        payload = json.loads(body["messages"][1]["content"])
        ids = [item["id"] for item in payload["segments"]]
        if "完整 translation" in system:
            records = [
                {
                    "type": "segment",
                    "id": item["id"],
                    "translation": f"译:{item['source']}",
                }
                for item in payload["segments"]
            ]
        else:
            review_calls.append(ids)
            if "format_correction" in payload:
                correction = payload["format_correction"]
                assert "遵守固定字段" in correction
                assert "accepted 仅含 type、id、status" in system
                assert '"suggested_text":"完整建议"' in system
            returned = [ids[1]] if len(ids) == 3 else ids
            records = [
                {
                    "type": "segment",
                    "id": segment_id,
                    "status": "accepted",
                    "suggested_text": None,
                    "reason": None,
                }
                for segment_id in returned
            ]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await run_translation(project, Scope(), http_client=client)
        summary = await run_review(
            project, "proofreading", Scope(), http_client=client
        )
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    assert summary["completed"] == 3
    assert review_calls == [
        ["1", "2", "3"],
        ["1"],
        ["1"],
    ]


@pytest.mark.asyncio
async def test_review_format_retry_uses_abstract_guidance(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "one")
    review_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal review_calls
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        payload = json.loads(body["messages"][1]["content"])
        if "完整 translation" in system:
            records = [
                {
                    "type": "segment",
                    "id": item["id"],
                    "translation": f"译:{item['source']}",
                }
                for item in payload["segments"]
            ]
            content = llm_jsonl(records)
        else:
            review_calls += 1
            if review_calls == 1:
                content = json.dumps({"segments": [{"id": "1"}]})
            else:
                correction = payload["format_correction"]
                assert "当前待处理内容" in correction
                assert "JSONL 结构" in correction
                assert "固定字段" in correction
                assert "完整" in correction
                assert "上次" not in correction
                assert "第 1 行" not in correction
                assert "未知 type" not in correction
                assert "缺少最终 end 记录" not in correction
                records = [
                    {
                        "type": "segment",
                        "id": item["id"],
                        "status": "accepted",
                        "suggested_text": None,
                        "reason": None,
                    }
                    for item in payload["segments"]
                ]
                content = llm_jsonl(records)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await run_translation(project, Scope(), http_client=client)
        summary = await run_review(project, "proofreading", Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    assert summary["completed"] == 1
    assert review_calls == 2


@pytest.mark.asyncio
async def test_review_accepts_thought_wrapped_echo_without_format_retry(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "one")
    review_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal review_calls
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        payload = json.loads(body["messages"][1]["content"])
        if "完整 translation" in system:
            records = [
                {
                    "type": "segment",
                    "id": item["id"],
                    "translation": f"译:{item['source']}",
                }
                for item in payload["segments"]
            ]
            content = llm_jsonl(records)
        else:
            review_calls += 1
            assert "format_correction" not in payload
            records = [
                {
                    "type": "segment",
                    "id": item["id"],
                    "status": "accepted",
                    "suggested_text": item["current_text"],
                    "reason": {"detail": "ignored"},
                }
                for item in payload["segments"]
            ]
            content = f"<thought>无需修改</thought>\n{llm_jsonl(records)}"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await run_translation(project, Scope(), http_client=client)
        summary = await run_review(project, "proofreading", Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]

    assert summary["completed"] == 1
    assert summary["failed"] == 0
    assert review_calls == 1
    completed = [
        item
        for item in read_jsonl(project, project / "stages" / "proofreading.jsonl")
        if item["status"] == "completed"
    ]
    assert len(completed) == 1
    assert completed[0]["review_status"] == "accepted"
    assert completed[0]["suggested_text"] is None
    assert completed[0]["reason"] is None


@pytest.mark.asyncio
async def test_oversized_review_segment_is_combined_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = await create_project(tmp_path, "A" * 5000)
    client = httpx.AsyncClient(transport=httpx.MockTransport(workflow_handler))
    try:
        await run_translation(project, Scope(), http_client=client)
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
        summary = await run_review(
            project, "proofreading", Scope(), http_client=client
        )
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    assert summary["completed"] == 1
    completed = [
        item
        for item in read_jsonl(project, project / "stages" / "proofreading.jsonl")
        if item["status"] == "completed"
    ]
    assert len(completed) == 1
    assert completed[0]["segment_id"] == "F0001-S000001"
    assert len(completed[0]["suggested_text"]) > 5000


@pytest.mark.asyncio
async def test_applied_export_reports_translation_validation_warning(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "one")
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        .replace("validators = []", 'validators = ["japanese_kana"]')
        .replace('exhausted_mode = "fail"', 'exhausted_mode = "warning"')
        .replace("max_retry_attempts = 2", "max_retry_attempts = 0"),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        payload = json.loads(body["messages"][1]["content"])
        if "完整 translation" in system:
            records = [
                {
                    "type": "segment",
                    "id": item["id"],
                    "translation": "候选カ",
                }
                for item in payload["segments"]
            ]
        else:
            records = [
                {
                    "type": "segment",
                    "id": item["id"],
                    "status": "accepted",
                    "suggested_text": None,
                    "reason": None,
                }
                for item in payload["segments"]
            ]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await run_translation(project, Scope(), http_client=client)
        await run_review(project, "proofreading", Scope(), http_client=client)
    finally:
        await client.aclose()
        del os.environ["LLM_API_KEY"]
    run_apply(
        project,
        "proofreading",
        Scope(),
        allow_outdated_base=False,
        confirmed_all=True,
    )
    summary = export_project(
        project, "proofread", bilingual=False, allow_missing=False
    )
    assert summary["validation_warnings"] == 1
    assert inspect_full(project)["validation_warnings"] == 1


@pytest.mark.asyncio
async def test_apply_requires_all_as_usage_error(tmp_path: Path) -> None:
    project = await create_project(tmp_path, "one")
    try:
        with pytest.raises(UsageError, match="--all"):
            run(["apply", str(project), "--stage", "proofreading"])
    finally:
        del os.environ["LLM_API_KEY"]


@pytest.mark.asyncio
async def test_export_allow_missing_falls_back_and_reports(tmp_path: Path) -> None:
    project = await create_project(tmp_path, " \tone")
    summary = export_project(
        project, "translated", bilingual=False, allow_missing=True
    )
    assert summary["fallback_segments"] == ["F0001-S000001"]
    output = project / "output" / "translated" / "source.txt"
    assert output.read_text(encoding="utf-8-sig") == " \tone"
    del os.environ["LLM_API_KEY"]


@pytest.mark.asyncio
async def test_export_fails_when_output_encoding_cannot_represent_text(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "中文")
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'output_encoding = "utf-8-sig"', 'output_encoding = "ascii"'
        ),
        encoding="utf-8",
    )
    try:
        with pytest.raises(IncompleteError, match="无法表示"):
            export_project(
                project, "translated", bilingual=False, allow_missing=True
            )
    finally:
        del os.environ["LLM_API_KEY"]
