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
    assert load_run_config(run_dir)["_decision_preset_definitions"]["local"]["url"] == value["url"]
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


@pytest.mark.asyncio
async def test_decision_receives_matching_long_term_without_questioning_it():
    from app.decision import DecisionAnswer
    from app.translation_validation import TranslationTermMatch, TranslationValidationContext
    from plugins.term_validation.plugin import PreferredTermUsageValidator

    class Decision:
        async def choose(self, state, questions, **kwargs):
            assert len(questions) == 1
            assert state['terms']['term_0']['source'] == 'ニア・リストン'
            assert [term['source'] for term in state['matched_terms']] == [
                'ニア・リストン', 'ニア・リストンの職業訪問']
            assert state['matched_terms'][1]['preferred_translation'] == '妮娅·利斯顿的职业探访'
            return {'term_0': DecisionAnswer('required', {'required': 1, 'ordinary': 0,
                                                        'acceptable': 0, 'uncertain': 0}, 0.99)}

    findings = await PreferredTermUsageValidator().validate(TranslationValidationContext(
        'ニア・リストンの職業訪問', '妮娅·利斯顿的职业探访',
        terms=(TranslationTermMatch('ニア・リストン', 'ニア・リストン', 'source', '妮娅·里斯顿'),
               TranslationTermMatch('ニア・リストンの職業訪問', 'ニア・リストンの職業訪問',
                                    'source', '妮娅·利斯顿的职业探访')),
        decision=Decision()))
    assert len(findings) == 1
    assert findings[0].expected_translation == '妮娅·里斯顿'
    assert findings[0].repairable


def test_decision_presets_inherit_and_override_independently(tmp_path, monkeypatch):
    from app.config import _resolve_decision_config
    from tests.test_foundation import make_app_root
    root = make_app_root(tmp_path)
    common = preset()
    override = {**common, "preset_id": "other", "model": "other-model"}
    write_user("decision_presets/local.json").write_text(json.dumps(common), encoding="utf-8")
    write_user("decision_presets/other.json").write_text(json.dumps(override), encoding="utf-8")
    config = load_config(root / "config" / "config.toml")
    config["decision"] = {"preset": "local"}
    options = config["validation"]["translation"]
    options.update(validators=["preferred_term_usage", "segment_alignment"], decision_enabled=True, decision_preset="")
    options["alignment"] = {"decision_preset": "other", "confidence_threshold": 0.8, "tail_segments": 3}
    _resolve_decision_config(config, root)
    assert config["_decision_validator_presets"] == {"preferred_term_usage": "local", "segment_alignment": "other"}
    assert set(config["_decision_preset_definitions"]) == {"local", "other"}
    options["alignment"]["decision_preset"] = ""
    _resolve_decision_config(config, root)
    assert set(config["_decision_preset_definitions"]) == {"local"}


@pytest.mark.asyncio
@pytest.mark.parametrize("choice,confidence,expected_status,repairs", [
    ("aligned", 0.9, "passed", 0), ("misaligned", 0.9, "passed", 1),
    ("uncertain", 0.9, "warning", 0), ("aligned", 0.5, "warning", 0),
])
async def test_alignment_checks_tail_and_gates_whole_translation(tmp_path, monkeypatch, choice, confidence, expected_status, repairs):
    project = await create_project(tmp_path, "One.\nTwo.\nThree.\nFour.")
    value = preset()
    write_user("decision_presets/local.json").write_text(json.dumps(value), encoding="utf-8")
    config = load_config(project / "config.toml")
    config["decision"]["preset"] = "local"
    config["validation"]["translation"]["validators"] = ["segment_alignment"]
    (project / "config.toml").write_text(dump_config(config), encoding="utf-8")
    monkeypatch.setenv("DECISION_TEST_KEY", "test")
    requests, decisions = [], []
    def respond(request):
        body = json.loads(request.content)
        if str(request.url) == value["url"]:
            decisions.append(body)
            assert [item["source"] for item in body["state"]["segments"]] == ["Two.", "Three.", "Four."]
            selected = "aligned" if len(decisions) > 1 else choice
            return httpx.Response(200, json={"model": "decision-test", "answers": {name: {"type": "choice", "choice": selected,
                "confidence": confidence, "probabilities": {key: int(key == selected) for key in question["criteria"]}}
                for name, question in body["questions"].items()}})
        payload = json.loads(body["messages"][1]["content"])
        requests.append(payload)
        return httpx.Response(200, json={"choices": [{"message": {"content": llm_jsonl([
            {"type": "segment", "id": item["id"], "translation": "译文" + item["id"]} for item in payload["segments"]])}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await run_translation(project, Scope(), http_client=http)
    assert result["completed"] == 4
    assert len(requests) == 1 + repairs
    assert len(decisions) == 1 + repairs
    records = read_jsonl(project, project / "stages" / "translation.jsonl")
    assert {record["validation_status"] for record in records} == {expected_status}
    if repairs:
        assert len(requests[-1]["segments"]) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("gate,term_requests,status", [
    ("mechanical", 0, "failed"), ("uncertain", 0, "warning"),
    ("refused", 0, "warning"), ("aligned", 4, "passed"), ("misaligned", 4, "passed"),
])
async def test_alignment_phase_prevents_invalid_terminology_requests(tmp_path, monkeypatch, gate, term_requests, status):
    project = await create_project(tmp_path, "Alice one.\nAlice two.\nAlice three.\nAlice four.")
    metadata = read_json(project, project / "project.json")
    write_json(project, project / "terminology" / "terms.json", record_header(
        "terminology_library", metadata["project_id"], terms_revision=1,
        terms=[dict(record_id="TERM-A", source="Alice", normalized="alice", category="人名",
                    description="Character", preferred_translation="爱丽丝", aliases=[], group_primary=None, conflicts={})]))
    common = preset()
    other = {**common, "preset_id": "other", "url": common["url"] + "/other"}
    for value in (common, other):
        write_user(f"decision_presets/{value['preset_id']}.json").write_text(json.dumps(value), encoding="utf-8")
    config = load_config(project / "config.toml")
    config["decision"]["preset"] = "local"
    options = config["validation"]["translation"]
    options.update(validators=["japanese_kana", "segment_alignment", "preferred_term_usage"], decision_enabled=True,
                   decision_preset="other", max_retry_attempts=0 if gate == "mechanical" else 2)
    (project / "config.toml").write_text(dump_config(config), encoding="utf-8")
    monkeypatch.setenv("DECISION_TEST_KEY", "test")
    alignment_calls, terminology_calls = [], []
    def respond(request):
        body = json.loads(request.content)
        if "questions" in body:
            assert not read_jsonl(project, project / "stages" / "translation.jsonl"), "Candidates must not be saved before whole-batch validation"
            if "segments" in body["state"]:
                alignment_calls.append(body)
                assert str(request.url) == common["url"]
                selected = "aligned" if len(alignment_calls) > 1 else gate
            else:
                terminology_calls.append(body)
                assert str(request.url) == other["url"]
                selected = "ordinary"
            answers = {name: ({"type": "refusal"} if selected == "refused" else {
                "type": "choice", "choice": selected, "confidence": 0.99,
                "probabilities": {key: int(key == selected) for key in question["criteria"]}})
                for name, question in body["questions"].items()}
            return httpx.Response(200, json={"model": "test", "answers": answers})
        payload = json.loads(body["messages"][1]["content"])
        records = [{"type": "segment", "id": item["id"], "translation": "あ" if gate == "mechanical" and index == 0 else "她来了。"}
                   for index, item in enumerate(payload["segments"])]
        return httpx.Response(200, json={"choices": [{"message": {"content": llm_jsonl(records)}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await run_translation(project, Scope(), http_client=http)
    assert len(terminology_calls) == term_requests
    assert len(alignment_calls) == (0 if gate == "mechanical" else 2 if gate == "misaligned" else 1)
    records = read_jsonl(project, project / "stages" / "translation.jsonl")
    assert len(records) == 4
    assert {record.get("validation_status", record["status"]) for record in records} == {status}
    frozen = load_run_config(project / "runs" / result["run_id"])
    assert set(frozen["_decision_preset_definitions"]) == {"local", "other"}


@pytest.mark.asyncio
@pytest.mark.parametrize("exhausted", [False, True])
async def test_partial_response_is_buffered_before_alignment(tmp_path, monkeypatch, exhausted):
    project = await create_project(tmp_path, "One.\nTwo.")
    value = preset()
    write_user("decision_presets/local.json").write_text(json.dumps(value), encoding="utf-8")
    config = load_config(project / "config.toml")
    config["decision"]["preset"] = "local"
    config["validation"]["translation"]["validators"] = ["segment_alignment"]
    if exhausted:
        config["retry"]["format_max_attempts"] = 0
    (project / "config.toml").write_text(dump_config(config), encoding="utf-8")
    monkeypatch.setenv("DECISION_TEST_KEY", "test")
    translations, decisions = [], []
    def respond(request):
        body = json.loads(request.content)
        assert not read_jsonl(project, project / "stages" / "translation.jsonl")
        if "questions" in body:
            decisions.append(body)
            assert [item["source"] for item in body["state"]["segments"]] == ["One.", "Two."]
            return httpx.Response(200, json={"model": "test", "answers": {name: {
                "type": "choice", "choice": "aligned", "confidence": 0.99,
                "probabilities": {key: int(key == "aligned") for key in question["criteria"]}}
                for name, question in body["questions"].items()}})
        payload = json.loads(body["messages"][1]["content"])
        translations.append(payload)
        records = [{"type": "segment", "id": item["id"], "translation": "译文"} for item in payload["segments"]]
        content = json.dumps(records[0]) if len(translations) == 1 else llm_jsonl(records)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await run_translation(project, Scope(), http_client=http)
    assert result["completed"] == (0 if exhausted else 2)
    assert len(decisions) == (0 if exhausted else 1)
    assert {record["status"] for record in read_jsonl(project, project / "stages" / "translation.jsonl")} == ({"failed"} if exhausted else {"completed"})


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["split", "joint", "joint_runtime_split"])
async def test_alignment_uses_complete_segments_in_split_and_joint_requests(tmp_path, monkeypatch, mode):
    from tests.helpers import use_llm_preset
    from app.stage_terminology import run_terminology
    source = "A" * 5000 if mode == "split" else "A" * 20 if mode == "joint_runtime_split" else "One.\nTwo."
    project = await create_project(tmp_path, source)
    value = {**preset(), "context_window_tokens": 16000}
    write_user("decision_presets/local.json").write_text(json.dumps(value), encoding="utf-8")
    config = load_config(project / "config.toml")
    config["decision"]["preset"] = "local"
    config["validation"]["translation"]["validators"] = ["segment_alignment"]
    (project / "config.toml").write_text(dump_config(config), encoding="utf-8")
    if mode == "split":
        use_llm_preset(tmp_path, context_window_tokens=1200, max_output_tokens=300,
                       context_safety_margin_tokens=100, target_chunk_input_tokens=700)
    monkeypatch.setenv("DECISION_TEST_KEY", "test")
    decision_calls, llm_calls = [], []
    def respond(request):
        body = json.loads(request.content)
        if "questions" in body:
            decision_calls.append(body)
            assert [item["source"] for item in body["state"]["segments"]] == source.splitlines()
            return httpx.Response(200, json={"model": "test", "answers": {name: {
                "type": "choice", "choice": "aligned", "confidence": 0.99,
                "probabilities": {key: int(key == "aligned") for key in question["criteria"]}}
                for name, question in body["questions"].items()}})
        payload = json.loads(body["messages"][1]["content"])
        llm_calls.append(payload)
        if mode == "joint_runtime_split" and any(len(item["source"]) > 10 for item in payload["segments"]):
            return httpx.Response(400, text="context_length_exceeded: maximum context tokens")
        records = [{"type": "no_terms"}] if mode.startswith("joint") else []
        for item in payload["segments"]:
            records.append({"type": "segment", "id": item["id"], "translation": item["source"].lower()})
        return httpx.Response(200, json={"choices": [{"message": {"content": llm_jsonl(records)}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        if mode.startswith("joint"):
            await run_terminology(project, Scope(), http_client=http, include_draft_translation=True)
        else:
            await run_translation(project, Scope(), http_client=http)
    assert len(decision_calls) == 1
    assert (len(llm_calls) == 1) if mode == "joint" else (len(llm_calls) > 1)
    assert len(read_jsonl(project, project / "stages" / "translation.jsonl")) == len(source.splitlines())


@pytest.mark.asyncio
async def test_final_terminology_phase_repairs_only_affected_segments(tmp_path):
    project = await create_project(tmp_path, "One.\nAlice.")
    metadata = read_json(project, project / "project.json")
    write_json(project, project / "terminology" / "terms.json", record_header(
        "terminology_library", metadata["project_id"], terms_revision=1,
        terms=[dict(record_id="TERM-A", source="Alice", normalized="alice", category="人名",
                    description=None, preferred_translation="爱丽丝", aliases=[], group_primary=None, conflicts={})]))
    config = load_config(project / "config.toml")
    config["validation"]["translation"]["validators"] = ["preferred_term_usage"]
    (project / "config.toml").write_text(dump_config(config), encoding="utf-8")
    repairs = []
    def respond(request):
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        if "validation_repair" in payload:
            repairs.append(payload)
        return httpx.Response(200, json={"choices": [{"message": {"content": llm_jsonl([
            {"type": "segment", "id": item["id"], "translation": "她。"} for item in payload["segments"]])}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        await run_translation(project, Scope(), http_client=http)
    assert len(repairs) == 1
    assert [item["source"] for item in repairs[0]["segments"]] == ["Alice."]
    records = read_jsonl(project, project / "stages" / "translation.jsonl")
    assert records[0]["validation_status"] == "passed"
    assert records[1]["validation_status"] == "warning"


@pytest.mark.asyncio
@pytest.mark.parametrize("exhausted_mode", ["fail", "warning"])
async def test_alignment_repair_split_does_not_accept_unchecked_partial_candidate(tmp_path, monkeypatch, exhausted_mode):
    project = await create_project(tmp_path, "A" * 20)
    value = preset()
    write_user("decision_presets/local.json").write_text(json.dumps(value), encoding="utf-8")
    config = load_config(project / "config.toml")
    config["decision"]["preset"] = "local"
    config["validation"]["translation"].update(validators=["segment_alignment"], max_retry_attempts=1, exhausted_mode=exhausted_mode)
    config["retry"]["format_max_attempts"] = 0
    (project / "config.toml").write_text(dump_config(config), encoding="utf-8")
    monkeypatch.setenv("DECISION_TEST_KEY", "test")
    decisions, repair_parts = [], []
    def respond(request):
        body = json.loads(request.content)
        if "questions" in body:
            decisions.append(body)
            assert len(body["state"]["segments"][0]["source"]) == 20
            return httpx.Response(200, json={"model": "test", "answers": {name: {
                "type": "choice", "choice": "misaligned", "confidence": 0.99,
                "probabilities": {key: int(key == "misaligned") for key in question["criteria"]}}
                for name, question in body["questions"].items()}})
        payload = json.loads(body["messages"][1]["content"])
        if "validation_repair" in payload:
            if len(payload["segments"][0]["source"]) > 10:
                return httpx.Response(400, text="context_length_exceeded: maximum context tokens")
            repair_parts.append(payload)
            if len(repair_parts) == 1:
                return httpx.Response(200, json={"choices": [{"message": {"content": '{"type":"end"}'}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": llm_jsonl([
            {"type": "segment", "id": item["id"], "translation": "错" * len(item["source"])} for item in payload["segments"]])}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await run_translation(project, Scope(), http_client=http)
    records = read_jsonl(project, project / "stages" / "translation.jsonl")
    assert len(records) == 1
    if exhausted_mode == "fail":
        assert result["failed"] == 1
        assert records[0]["status"] == "failed"
    else:
        assert len(decisions) == 2
        assert result["completed"] == 1
        assert records[0]["validation_status"] == "warning"
        assert records[0]["validation_findings"][0]["match_type"] == "segment_misaligned"
