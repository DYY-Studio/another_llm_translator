"""LLM-backed Choice decisions using the shared LLM transport and prompt resources."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .errors import ConfigError, FatalExternalError

if TYPE_CHECKING:
    from .decision import DecisionAnswer, DecisionClient, DecisionQuestion


def resolve_llm_resources(preset: dict[str, Any], root: Path, *, project: Path | None = None) -> dict[str, Any]:
    from .execution import full_prompt
    from .llm_adapter import load_json_adapter
    from .llm_preset import load_llm_preset
    from .project import prompt_file
    from .prompt_resources import ProjectPrompts, global_prompt_path
    from .user_config import effective_path

    selected = load_llm_preset(effective_path(f"llm_presets/{preset['llm_preset']}.json", builtin_root=root))
    if selected.preset_id != preset["llm_preset"]:
        raise ConfigError("Decision 引用的 LLM Preset ID 与文件名不一致")
    adapter = load_json_adapter(effective_path(f"llm_adapters/{selected.adapter_id}.json", builtin_root=root))
    language = preset["prompt_language"]
    path = (ProjectPrompts(project, builtin_root=root).path("decision_llm", language) if project is not None
            else global_prompt_path("decision_llm", language, builtin_root=root))
    try:
        middle = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"无法读取 Decision LLM Prompt：{prompt_file('decision_llm', language)}") from exc
    result = {**preset, "_resources": {"preset": selected.definition, "adapter": adapter.definition, "prompt": full_prompt("decision_llm", middle, language)}}
    llm_config(result, retry=None, debug=False)
    return result


def llm_config(preset: dict[str, Any], *, retry: dict[str, Any] | None, debug: bool) -> dict[str, Any]:
    from .config import _resolve_llm_config
    from .llm_adapter import parse_json_adapter
    from .llm_preset import _parse_llm_preset

    resources = preset.get("_resources")
    if not isinstance(resources, dict) or set(resources) != {"preset", "adapter", "prompt"}:
        raise ConfigError("LLM Decision 缺少冻结资源")
    if not isinstance(resources["prompt"], str) or not resources["prompt"].strip():
        raise ConfigError("Decision LLM Prompt 不能为空")
    selected = _parse_llm_preset(resources["preset"])
    if selected.preset_id != preset["llm_preset"]:
        raise ConfigError("Decision 引用的 LLM Preset 与冻结资源不一致")
    config = {"llm": {}, "execution": {}, "retry": retry,
              "debug": {"enabled": debug, "inject_timeout_every": 0, "inject_429_every": 0,
                        "inject_500_every": 0, "inject_invalid_json_every": 0, "inject_missing_segment_every": 0}}
    return _resolve_llm_config(config, preset=selected, adapter=parse_json_adapter(resources["adapter"]))


def request_plan(preset: dict[str, Any], config: dict[str, Any], state: Any,
                 questions: list[DecisionQuestion]) -> tuple[dict[str, Any], int]:
    from .execution import estimate_tokens

    evidence = dict(state=state, questions={
        q.name: dict(type="choice", instructions=q.instructions, criteria=q.choices) for q in questions})
    messages = [dict(role="system", content=preset["_resources"]["prompt"]),
                dict(role="user", content=json.dumps(evidence, ensure_ascii=False))]
    _, payload = config["_llm_adapter"].build_request(
        api_key="***", model=preset["model"], messages=messages, temperature=0,
        max_output_tokens=config["llm"]["max_output_tokens"], stream=config["llm"]["stream"],
        send_temperature=config["llm"]["send_temperature"], reasoning_effort=config["llm"]["reasoning_effort"],
        extra_body=config["_llm_extra_body"], extra_headers=config["_llm_extra_headers"],
        session_id="RUN-DECISION", request_id="REQ-DECISION")
    estimate = math.ceil(estimate_tokens(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))) * preset["token_safety_factor"])
    return {"messages": messages}, estimate


async def request_decision(client: DecisionClient, body: dict[str, Any], estimated: int,
                           questions: list[DecisionQuestion], segment_id: str | None,
                           request_id: str, requested: int, used: int,
                           overview: dict[str, Any]) -> dict[str, DecisionAnswer]:
    from .diagnostics import current_diagnostics
    from .llm_client import LLMClient

    diagnostics = current_diagnostics()
    llm = LLMClient({**client.llm_config, "retry": dict(client.retry)}, client.pool, run_dir=client.debug_directory or Path(),
                    project_id=client.project_id, run_id=client.run_id, stage=client.stage or "decision",
                    client=client.http_client, decision_overview=overview)
    async with llm:
        for attempt in range(client.retry["http_max_attempts"]):
            started = time.monotonic()
            current_id = request_id if attempt == 0 else f"{request_id}-P{attempt + 1}"
            record = {"attempt": attempt + 1, "segment_id": segment_id,
                      "context_segments_requested": requested, "context_segments_used": used,
                      "questions": [q.name for q in questions],
                      "evidence_digest": hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}
            before = llm.usage_summary()
            llm.config["retry"] = {**client.retry, "http_max_attempts": client.retry["http_max_attempts"] - llm.send_count}
            try:
                response, current_id = await llm.chat(messages=body["messages"], temperature=0,
                    estimated_input_tokens=estimated, request_id=current_id)
                try:
                    data = {**json.loads(response.content), "model": client.preset["model"]}
                    answers = client._parse(data, questions)
                except (ValueError, TypeError, KeyError) as exc:
                    record["error"] = "response_parse_error"
                    if diagnostics is not None:
                        diagnostics.fail_request(current_id, "response_parse_error")
                    if llm.send_count >= client.retry["http_max_attempts"]:
                        raise FatalExternalError("Decision 响应无效，重试预算耗尽") from exc
                    if diagnostics is not None:
                        diagnostics.retried(current_id)
                else:
                    overview["answers"] = {k: asdict(v) for k, v in answers.items()}
                    record.update(model=data["model"], answers=overview["answers"])
                    if diagnostics is not None:
                        diagnostics.complete_request(current_id, content=response.content,
                            reasoning_content=response.reasoning_content, overview_answers=overview["answers"])
                    return answers
            except BaseException as exc:
                record["error"] = str(exc) or type(exc).__name__
                if diagnostics is not None:
                    diagnostics.fail_request(current_id, str(exc), interrupted=isinstance(exc, asyncio.CancelledError))
                raise
            finally:
                # Preserve usage from malformed responses too, without counting earlier attempts twice.
                after = llm.usage_summary()
                if after is not None:
                    record["usage"] = {**after, **{key: after[key] - (before[key] if before else 0)
                                                  for key in ("input_tokens", "output_tokens", "total_tokens")}}
                record["elapsed_seconds"] = time.monotonic() - started
                client.records.append(record)
            await asyncio.sleep(min(client.retry.get("max_delay_seconds", 60), client.retry["base_delay_seconds"] * 2 ** attempt))
    raise AssertionError("unreachable")
