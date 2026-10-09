from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.config import dump_config, load_config, load_run_config
from app.execution import Scope
from app.errors import FatalExternalError
from app.sqlite_storage import read_json, read_jsonl, record_header, write_json
from app.stage_translation import run_translation
from app.user_config import write_user
from tests.helpers import llm_jsonl
from tests.test_decision import preset
from tests.test_terminology_translation import create_project


@pytest.mark.asyncio
@pytest.mark.parametrize("choice,confidence,repairs,status", [
    ("required", 0.9, 1, "passed"),
    ("ordinary", 0.9, 0, "passed"),
    ("acceptable", 0.9, 0, "passed"),
    ("required", 0.5, 0, "warning"),
    ("uncertain", 0.9, 0, "warning"),
    ("still_missing", 0.9, 1, "warning"),
    ("http_error", 0, 0, "failed"),
    ("context_error", 0, 0, "failed"),
])
async def test_decision_gates_actual_translation_repair(tmp_path: Path, monkeypatch, choice, confidence, repairs, status):
    project = await create_project(tmp_path, "Alice arrived.")
    metadata = read_json(project, project / "project.json")
    write_json(project, project / "terminology" / "terms.json", record_header(
        "terminology_library", metadata["project_id"], terms_revision=1,
        terms=[dict(record_id="TERM-A", source="Alice", normalized="alice", category="人名",
                    description="A character", preferred_translation="爱丽丝", aliases=[],
                    group_primary=None, conflicts={})]))
    value = preset()
    if choice == "context_error":
        value.update(context_window_tokens=513, context_safety_margin_tokens=512)
    write_user("decision_presets/local.json").write_text(json.dumps(value), encoding="utf-8")
    config = load_config(project / "config.toml")
    config["validation"]["translation"].update(validators=["preferred_term_usage"], decision_enabled=True, decision_preset="local")
    (project / "config.toml").write_text(dump_config(config), encoding="utf-8")
    monkeypatch.setenv("DECISION_TEST_KEY", "test")
    calls = []
    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        if str(request.url) == value["url"]:
            assert choice != "context_error", "Oversized Decision must not send HTTP"
            if choice == "http_error":
                return httpx.Response(401)
            selected = "required" if choice == "still_missing" else choice
            assert body["state"]["terms"]["term_0"]["description"] == "A character"
            return httpx.Response(200, json=dict(model="decision-test", usage=dict(input_tokens=10, output_tokens=0),
                answers={name: dict(type="choice", choice=selected, confidence=confidence,
                    probabilities={key: 0.9 if key == selected else 0.1 / 3 for key in ("required", "ordinary", "acceptable", "uncertain")}) for name in body["questions"]}))
        payload = json.loads(body["messages"][1]["content"])
        text = "爱丽丝到了。" if "validation_repair" in payload and choice != "still_missing" else "她到了。"
        return httpx.Response(200, json=dict(choices=[dict(message=dict(content=llm_jsonl([
            dict(type="segment", id=item["id"], translation=text) for item in payload["segments"]])))],
            usage=dict(prompt_tokens=20, completion_tokens=5, total_tokens=25)))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        if choice == "context_error":
            with pytest.raises(FatalExternalError, match="Decision.*Token"):
                await run_translation(project, Scope(), http_client=http)
            manifest = read_json(project, next((project / "runs").iterdir()) / "manifest.json")
            assert manifest["status"] == "failed"
            assert len(calls) == 1
            return
        if choice == "http_error":
            with pytest.raises(FatalExternalError, match="HTTP 401"):
                await run_translation(project, Scope(), http_client=http)
            manifest = read_json(project, next((project / "runs").iterdir()) / "manifest.json")
            assert manifest["status"] == "failed"
            assert manifest["decision_validation"][0]["error"] == "decision_error"
            return
        result = await run_translation(project, Scope(), http_client=http)
    assert result["completed"] == 1
    assert sum("messages" in body for body in calls) == 1 + repairs
    record = read_jsonl(project, project / "stages" / "translation.jsonl")[-1]
    assert record["validation_status"] == status
    if status == "warning" and choice != "still_missing":
        assert record["validation_findings"][0]["repairable"] is False
    run_dir = project / "runs" / result["run_id"]
    assert load_run_config(run_dir)["_decision_preset_definition"]["url"] == value["url"]
    diagnostic = read_json(project, run_dir / "manifest.json")["decision_validation"]
    assert diagnostic[0]["segment_id"] == record["segment_id"]
    assert result["usage"]["input_tokens"] == 20 * (1 + repairs) + 10 * len(diagnostic)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,count,expected", [(False, 2, []), (True, 0, []), (True, 1, ["前文二"]), (True, 2, ["前文一", "前文二"])])
async def test_translation_decision_context_toggle_and_count(tmp_path, monkeypatch, enabled, count, expected):
    from app.sqlite_storage import read_segment_sources
    project = await create_project(tmp_path, "前文一\n前文二\nAlice arrived.")
    metadata = read_json(project, project / "project.json")
    write_json(project, project / "terminology" / "terms.json", record_header(
        "terminology_library", metadata["project_id"], terms_revision=1,
        terms=[dict(record_id="TERM-A", source="Alice", normalized="alice", category="人名",
                    description="A character", preferred_translation="爱丽丝", aliases=[],
                    group_primary=None, conflicts={})]))
    value = preset()
    write_user("decision_presets/local.json").write_text(json.dumps(value), encoding="utf-8")
    config = load_config(project / "config.toml")
    config["validation"]["translation"].update(validators=["preferred_term_usage"], decision_enabled=True,
        decision_preset="local", decision_context_enabled=enabled, decision_previous_segments=count)
    (project / "config.toml").write_text(dump_config(config), encoding="utf-8")
    monkeypatch.setenv("DECISION_TEST_KEY", "test")
    calls = []
    def respond(request):
        body = json.loads(request.content)
        if str(request.url) == value["url"]:
            calls.append(body)
            assert body["state"].get("reference_context", []) == expected
            return httpx.Response(200, json=dict(model="decision-test", answers={name: dict(type="choice",
                choice="ordinary", confidence=0.9,
                probabilities={key: 1 if key == "ordinary" else 0 for key in body["questions"][name]["criteria"]})
                for name in body["questions"]}))
        payload = json.loads(body["messages"][1]["content"])
        return httpx.Response(200, json=dict(choices=[dict(message=dict(content=llm_jsonl([
            dict(type="segment", id=item["id"], translation="她到了。") for item in payload["segments"]])))],
            usage=dict(prompt_tokens=20, completion_tokens=5, total_tokens=25)))
    segment = read_segment_sources(project)[-1]
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await run_translation(project, Scope(only_segment=segment["segment_id"]), http_client=http)
    assert result["completed"] == 1
    assert len(calls) == 1
    from app.web_store import WebStore
    store = WebStore(project)
    manual_context = store._translation_validation_context(segment, "她到了。")
    assert list(manual_context.previous_source) == expected
