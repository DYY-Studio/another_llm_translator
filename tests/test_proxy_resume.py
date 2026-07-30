from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import load_config
from app.errors import ConfigError, UsageError
from app.execution import (
    LLMClient,
    Scope,
    SlidingWindowLimiter,
    choose_running_run,
    create_run,
)
from app.main import build_parser
from app.stages import run_review, run_terminology, run_translation
from app.storage import (
    append_jsonl,
    atomic_write_json,
    read_json,
    record_header,
)
from tests.helpers import llm_jsonl
from tests.test_terminology_translation import create_project


ROOT = Path(__file__).parents[1]


def _replace_config(project: Path, old: str, new: str) -> None:
    path = project / "config.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(old, new),
        encoding="utf-8",
    )


def _new_running_run(
    project: Path,
    stage: str,
    *,
    active_task_id: str | None = None,
) -> str:
    details: dict[str, Any] = {
        "scope": {
            "all_nonempty": True,
            "from_file": None,
            "only_file": None,
            "only_segment": None,
            "force": True,
        }
    }
    if active_task_id is not None:
        details["active_task_id"] = active_task_id
    run_id, _ = create_run(
        project,
        stage=stage,
        fingerprint="old-fingerprint",
        prompt="old prompt",
        selected_count=3,
        requested_count=3,
        reused_count=0,
        details=details,
    )
    return run_id


def test_resume_flags_only_exist_on_standalone_llm_commands() -> None:
    parser = build_parser()
    for command in ("terminology", "translate", "proofread", "polish"):
        parsed = parser.parse_args([command, "demo", "--resume-run"])
        assert parsed.resume_run is True
    with pytest.raises(SystemExit):
        parser.parse_args(["run-all", "demo", "--resume-run"])


def test_proxy_config_accepts_empty_http_and_https(tmp_path: Path) -> None:
    source = (ROOT / "config" / "config.toml").read_text(encoding="utf-8")
    for value in ("", "http://127.0.0.1:7890", "https://proxy.example:8443"):
        path = tmp_path / f"{len(value)}.toml"
        path.write_text(
            source.replace('proxy_url = ""', f'proxy_url = "{value}"'),
            encoding="utf-8",
        )
        assert load_config(path)["llm"]["proxy_url"] == value


@pytest.mark.parametrize(
    "value",
    ["socks5://127.0.0.1:1080", "ftp://proxy.example", "http://"],
)
def test_proxy_config_rejects_unsupported_or_incomplete_url(
    tmp_path: Path, value: str
) -> None:
    source = (ROOT / "config" / "config.toml").read_text(encoding="utf-8")
    path = tmp_path / "config.toml"
    path.write_text(
        source.replace('proxy_url = ""', f'proxy_url = "{value}"'),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="proxy_url"):
        load_config(path)


@pytest.mark.asyncio
async def test_llm_client_passes_proxy_and_preserves_httpx_environment_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    class DummyClient:
        def __init__(self, **kwargs: Any) -> None:
            calls.append(kwargs)

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(httpx, "AsyncClient", DummyClient)
    current = load_config(ROOT / "config" / "config.toml")
    current["llm"]["proxy_url"] = "http://127.0.0.1:7890"
    async with LLMClient(
        current,
        SlidingWindowLimiter(0, 0),
        run_dir=tmp_path,
        project_id="P",
        run_id="R",
        stage="translation",
    ):
        pass
    assert calls[0]["proxy"] == "http://127.0.0.1:7890"
    assert "trust_env" not in calls[0]

    current["llm"]["proxy_url"] = ""
    async with LLMClient(
        current,
        SlidingWindowLimiter(0, 0),
        run_dir=tmp_path,
        project_id="P",
        run_id="R",
        stage="translation",
    ):
        pass
    assert calls[1]["proxy"] is None
    assert "trust_env" not in calls[1]


@pytest.mark.asyncio
async def test_injected_http_client_does_not_create_another_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = load_config(ROOT / "config" / "config.toml")
    injected = object()

    def fail(**_: Any) -> None:
        raise AssertionError("unexpected client construction")

    monkeypatch.setattr(httpx, "AsyncClient", fail)
    async with LLMClient(
        current,
        SlidingWindowLimiter(0, 0),
        run_dir=tmp_path,
        project_id="P",
        run_id="R",
        stage="translation",
        client=injected,  # type: ignore[arg-type]
    ) as llm:
        assert llm.client is injected


@pytest.mark.asyncio
async def test_running_run_choice_decline_supersede_and_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = await create_project(tmp_path, "one")
    try:
        older = _new_running_run(project, "translation")
        newer = _new_running_run(project, "translation")
        older_path = project / "runs" / older / "manifest.json"
        newer_path = project / "runs" / newer / "manifest.json"
        older_manifest = read_json(older_path)
        newer_manifest = read_json(newer_path)
        older_manifest["started_at"] = "2026-01-01T00:00:00+00:00"
        newer_manifest["started_at"] = "2026-01-02T00:00:00+00:00"
        atomic_write_json(older_path, older_manifest)
        atomic_write_json(newer_path, newer_manifest)

        with pytest.raises(UsageError, match="--resume-run"):
            choose_running_run(
                project,
                "translation",
                action=None,
                dry_run=False,
                interactive=False,
            )
        assert read_json(older_path)["superseded_by_run_id"] == newer
        before = newer_path.read_bytes()
        chosen, warnings = choose_running_run(
            project,
            "translation",
            action="resume",
            dry_run=True,
            interactive=False,
        )
        assert chosen == newer
        assert warnings and newer_path.read_bytes() == before

        monkeypatch.setattr("builtins.input", lambda: "new")
        chosen, _ = choose_running_run(
            project,
            "translation",
            action=None,
            dry_run=False,
            interactive=True,
        )
        assert chosen is None
        declined = read_json(newer_path)
        assert declined["status"] == "interrupted"
        assert declined["resume_declined"] is True
        assert choose_running_run(
            project,
            "translation",
            action=None,
            dry_run=False,
            interactive=False,
        ) == (None, [])
    finally:
        os.environ.pop("LLM_API_KEY", None)


@pytest.mark.asyncio
async def test_translation_resume_uses_old_scope_current_settings_and_same_run(
    tmp_path: Path,
) -> None:
    project = await create_project(tmp_path, "one\ntwo\nthree")
    metadata = read_json(project / "project.json")
    run_id = _new_running_run(project, "translation")
    append_jsonl(
        project / "stages" / "translation.jsonl",
        record_header(
            "stage_result",
            str(metadata["project_id"]),
            stage="translation",
            segment_id="F0001-S000001",
            status="completed",
            text="旧译文",
            validation_status="passed",
            findings=[],
            terms_revision=None,
            stage_fingerprint="old-fingerprint",
            run_id=run_id,
            request_id="REQ-OLD",
        ),
    )
    _replace_config(project, 'model = "example-model"', 'model = "current-model"')
    prompt_path = project / "prompts" / "translation.middle.txt"
    prompt_path.write_text(
        prompt_path.read_text(encoding="utf-8") + "\nCURRENT PROMPT",
        encoding="utf-8",
    )
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "current-model"
        assert "CURRENT PROMPT" in body["messages"][0]["content"]
        payload = json.loads(body["messages"][1]["content"])
        requested.extend(item["id"] for item in payload["segments"])
        records = [
            {
                "type": "segment",
                "id": item["id"],
                "translation": f"译:{item['source']}",
            }
            for item in payload["segments"]
        ]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = await run_translation(
            project,
            Scope(only_segment="F0001-S000001", force=True),
            http_client=client,
            resume_run_id=run_id,
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)
    assert summary["run_id"] == run_id
    assert summary["selected"] == 3
    assert requested == ["F0001-S000002", "F0001-S000003"]
    continuation = project / "runs" / run_id / "continuations" / "0001"
    assert "current-model" in (continuation / "config.toml").read_text("utf-8")
    assert "CURRENT PROMPT" in (continuation / "prompt.txt").read_text("utf-8")
    manifest = read_json(project / "runs" / run_id / "manifest.json")
    assert manifest["status"] == "completed"
    assert manifest["completed_segment_count"] == 3
    assert manifest["continuations"][0]["requested_segment_count"] == 2


@pytest.mark.asyncio
async def test_terminology_resume_keeps_active_task(tmp_path: Path) -> None:
    project = await create_project(tmp_path, "Alice\nBob")
    metadata = read_json(project / "project.json")
    task_id = "TERM-TASK-KEEP"
    atomic_write_json(
        project / "terminology" / "active_task.json",
        record_header(
            "terminology_task",
            str(metadata["project_id"]),
            record_id=task_id,
            active_task_id=task_id,
            status="active",
            initial_stage_fingerprint="old",
        ),
    )
    append_jsonl(
        project / "terminology" / "scans.jsonl",
        record_header(
            "terminology_scan",
            str(metadata["project_id"]),
            stage="terminology",
            segment_id="F0001-S000001",
            status="completed",
            active_task_id=task_id,
            stage_fingerprint="old",
            run_id="OLD",
            request_id="OLD",
        ),
    )
    run_id = _new_running_run(
        project, "terminology", active_task_id=task_id
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        seen.extend(item["source"] for item in payload["source_segments"])
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl([])}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        summary = await run_terminology(
            project,
            Scope(force=True),
            http_client=client,
            resume_run_id=run_id,
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)
    assert summary["run_id"] == run_id
    assert summary["active_task_id"] == task_id
    assert seen == ["Bob"]


@pytest.mark.parametrize("stage", ["proofreading", "polishing"])
@pytest.mark.asyncio
async def test_review_resume_reuses_completed_segments(
    tmp_path: Path, stage: str
) -> None:
    project = await create_project(tmp_path, "one\ntwo")
    metadata = read_json(project / "project.json")
    for index in (1, 2):
        append_jsonl(
            project / "stages" / "translation.jsonl",
            record_header(
                "stage_result",
                str(metadata["project_id"]),
                stage="translation",
                segment_id=f"F0001-S{index:06d}",
                status="completed",
                text=f"译文{index}",
                validation_status="passed",
                findings=[],
                terms_revision=None,
                stage_fingerprint="translation",
                run_id="BASE",
                request_id="BASE",
            ),
        )
    run_id = _new_running_run(project, stage)
    append_jsonl(
        project / "stages" / f"{stage}.jsonl",
        record_header(
            "stage_result",
            str(metadata["project_id"]),
            stage=stage,
            segment_id="F0001-S000001",
            status="completed",
            review_status="accepted",
            suggested_text=None,
            reason=None,
            base_result_id="BASE",
            terms_revision=None,
            stage_fingerprint="old",
            run_id=run_id,
            request_id="OLD",
        ),
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        seen.extend(item["id"] for item in payload["segments"])
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
        summary = await run_review(
            project,
            stage,
            Scope(force=True),
            http_client=client,
            resume_run_id=run_id,
        )
    finally:
        await client.aclose()
        os.environ.pop("LLM_API_KEY", None)
    assert summary["run_id"] == run_id
    assert seen == ["F0001-S000002"]
