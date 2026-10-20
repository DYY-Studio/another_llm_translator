"""Host-managed HTTP Choice decisions."""
from __future__ import annotations
from .async_utils import run_blocking

import asyncio
import hashlib
import json
import math
import re
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from .diagnostics import current_diagnostics
from .credentials import resolve_api_keys
from .errors import ConfigError, ExternalError, FatalExternalError
from .llm_keys import KeyPool
from .logging_utils import get_logger
from .sqlite_storage import atomic_write_json, append_jsonl_file, record_header


@dataclass(frozen=True)
class DecisionQuestion:
    name: str
    instructions: str
    choices: dict[str, str]


@dataclass(frozen=True)
class DecisionAnswer:
    choice: str | None
    probabilities: dict[str, float]
    confidence: float
    refused: bool = False


class DecisionService(Protocol):
    async def choose(self, state: Any, questions: list[DecisionQuestion], *,
                     segment_id: str | None = None,
                     reference_context: list[str] | None = None) -> dict[str, DecisionAnswer]: ...


def decision_preset_path(root: Path, preset_id: str) -> Path:
    if not isinstance(preset_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", preset_id):
        raise ConfigError("Decision Preset ID 格式无效")
    return root / "decision_presets" / f"{preset_id}.json"


def validate_decision_preset(value: dict[str, Any], *, editable: bool = False) -> dict[str, Any]:
    if editable and value.get("protocol") == "":
        decision_preset_path(Path(), value["preset_id"])
        return value
    if value.get("protocol") == "llm":
        from .llm_preset import preset_path
        from .project import PROMPT_LANGUAGES
        if set(value) != {"preset_id", "protocol", "llm_preset", "prompt_language"}:
            raise ConfigError("LLM Decision Preset 字段不完整或包含未知字段")
        decision_preset_path(Path(), value["preset_id"])
        if not isinstance(value["llm_preset"], str):
            raise ConfigError("LLM Preset ID 格式无效")
        if not (editable and value["llm_preset"] == ""):
            preset_path(Path(), value["llm_preset"])
        if value["prompt_language"] not in PROMPT_LANGUAGES:
            raise ConfigError("Prompt 语言必须是 zh-CN 或 en")
        return value
    required = {"preset_id", "protocol", "url", "model", "credential", "proxy_url",
                "request_timeout_seconds", "requests_per_minute", "max_parallel",
                "context_window_tokens", "context_safety_margin_tokens", "token_safety_factor"}
    if set(value) != required:
        raise ConfigError("Decision Preset 字段不完整或包含未知字段")
    decision_preset_path(Path(), value["preset_id"])
    if not isinstance(value["protocol"], str) or value["protocol"] not in {"typesafe", "openai-decisions"}:
        raise ConfigError("不支持的 Decision 协议")
    url = value["url"]
    if not isinstance(url, str):
        raise ConfigError("Decision URL 必须是完整 HTTP URL")
    try:
        parsed = urlsplit(url)
        parsed.port
    except ValueError as exc:
        raise ConfigError("Decision URL 无效") from exc
    if not (editable and url == "") and (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not parsed.path or parsed.path == "/"):
        raise ConfigError("Decision URL 必须含最终 Path，不能含凭据、查询参数或 fragment")
    if not isinstance(value["model"], str) or (not editable and not value["model"].strip()):
        raise ConfigError("Decision 模型不能为空")
    proxy_url = value["proxy_url"]
    if not isinstance(proxy_url, str):
        raise ConfigError("Decision proxy_url 必须是字符串")
    if proxy_url:
        try:
            proxy = urlsplit(proxy_url)
            proxy.port
        except ValueError as exc:
            raise ConfigError("Decision 代理 URL 无效") from exc
        if proxy.scheme not in {"http", "https"} or not proxy.hostname or proxy.username or proxy.password:
            raise ConfigError("Decision 代理必须是无凭据的 HTTP/HTTPS URL")
    credential = value["credential"]
    if (not isinstance(credential, dict) or set(credential) != {"kind", "name"}
            or not isinstance(credential["kind"], str) or credential["kind"] not in ({"", "environment", "keychain"} if editable else {"environment", "keychain"})
            or not isinstance(credential["name"], str) or (not editable and not credential["name"].strip())):
        raise ConfigError("Decision 凭据引用无效")
    for key, minimum in (("requests_per_minute", 0), ("max_parallel", 1),
                         ("context_window_tokens", 1), ("context_safety_margin_tokens", 0)):
        if type(value[key]) is not int or value[key] < minimum:
            raise ConfigError(f"Decision {key} 无效")
    if value["context_safety_margin_tokens"] >= value["context_window_tokens"]:
        raise ConfigError("Decision 安全余量必须小于上下文窗口")
    factor = value["token_safety_factor"]
    if type(factor) not in {int, float} or not math.isfinite(factor) or factor <= 0:
        raise ConfigError("Decision token_safety_factor 必须是有限正数")
    timeout = value["request_timeout_seconds"]
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout <= 0:
        raise ConfigError("Decision 超时必须是有限正数")
    return value


def normalize_decision_preset(value: Any, *, preset_id: str, warnings: list[str] | None = None) -> dict[str, Any]:
    from .config import report_unknown
    if not isinstance(value, dict):
        raise ConfigError("Decision Preset 必须是对象")
    protocol = value.get("protocol", "")
    if not isinstance(protocol, str) or protocol not in {"", "llm", "typesafe", "openai-decisions"}:
        raise ConfigError("不支持的 Decision 协议")
    defaults: dict[str, Any] = {"preset_id": preset_id, "protocol": protocol}
    if protocol == "llm":
        defaults.update(llm_preset="", prompt_language="zh-CN")
    elif protocol:
        defaults.update(url="", model="", credential={"kind": "", "name": ""}, proxy_url="",
                        request_timeout_seconds=120, requests_per_minute=30, max_parallel=4,
                        context_window_tokens=16384, context_safety_margin_tokens=512, token_safety_factor=1.25)
    report_unknown("Decision Preset", set(value) - set(defaults), warnings)
    result = {key: value.get(key, default) for key, default in defaults.items()}
    if "credential" in result:
        credential = result["credential"]
        if not isinstance(credential, dict):
            raise ConfigError("Decision 凭据引用无效")
        report_unknown("Decision Preset.credential", set(credential) - {"kind", "name"}, warnings)
        result["credential"] = {key: credential.get(key, "") for key in ("kind", "name")}
    return validate_decision_preset(result, editable=True)


def missing_decision_fields(value: dict[str, Any]) -> list[str]:
    if not value["protocol"]:
        return ["protocol"]
    if value["protocol"] == "llm":
        return ["llm_preset"] if not value["llm_preset"] else []
    return [key for key in ("url", "model") if not value[key].strip()] + [
        f"credential.{key}" for key in ("kind", "name") if not value["credential"][key].strip()]


def load_decision_preset(path: Path, *, snapshot: bool = False, warnings: list[str] | None = None) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"无法读取 Decision Preset：{path.name}") from exc
    if not isinstance(value, dict):
        raise ConfigError("Decision Preset 必须是对象")
    if snapshot and value.get("protocol") == "llm":
        validate_decision_preset({k: v for k, v in value.items() if k != "_resources"})
        from .llm_preset import _parse_llm_preset
        from .llm_adapter import parse_json_adapter
        from .config import _resolve_llm_config
        resources = value.get("_resources")
        if not isinstance(resources, dict) or set(resources) != {"preset", "adapter", "prompt"}:
            raise ConfigError("LLM Decision 缺少冻结资源")
        if not isinstance(resources["prompt"], str) or not resources["prompt"].strip():
            raise ConfigError("Decision LLM Prompt 不能为空")
        selected = _parse_llm_preset(resources["preset"])
        if selected.preset_id != value["llm_preset"]:
            raise ConfigError("Decision 引用的 LLM Preset 与冻结资源不一致")
        _resolve_llm_config({"llm": {}, "execution": {}}, preset=selected, adapter=parse_json_adapter(resources["adapter"]))
        return value
    return validate_decision_preset(value) if snapshot else normalize_decision_preset(value, preset_id=path.stem, warnings=warnings)


class DecisionClient:
    def __init__(self, preset: dict[str, Any], *, http_client: httpx.AsyncClient | None = None,
                 retry: dict[str, Any] | None = None, debug_directory: Path | None = None,
                 project_id: str = "", run_id: str = "", stage: str = "",
                 context_reserve_tokens: int | None = None) -> None:
        validate_decision_preset({key: child for key, child in preset.items() if key != "_resources"})
        self.debug_directory = debug_directory
        self.project_id, self.run_id, self.stage = project_id, run_id, stage
        self.retry = {"http_max_attempts": 1, "base_delay_seconds": 0, "max_delay_seconds": 60,
                               "jitter_seconds": 0, "empty_unknown_mode": "retry", "empty_unknown_max_attempts": 0,
                               "empty_truncated_mode": "retry", "empty_truncated_max_attempts": 0, **(retry or {})}
        self.preset = dict(preset) if preset["protocol"] == "llm" else preset
        self.llm_config = None
        if preset["protocol"] == "llm":
            from .decision_llm import llm_config
            if type(context_reserve_tokens) is not int or context_reserve_tokens < 0:
                raise ConfigError("LLM Decision 需要显式的项目通用上下文预留")
            self.llm_config = llm_config(preset, retry=self.retry, debug=debug_directory is not None,
                                         context_reserve_tokens=context_reserve_tokens)
            definition = self.llm_config["_llm_preset_definition"]
            self.preset.update({key: definition[key] for key in (
                "model", "context_window_tokens", "token_safety_factor")})
            self.preset["context_safety_margin_tokens"] = context_reserve_tokens
        self.http_client = http_client
        self.records: list[dict[str, Any]] = []
        limits = self.llm_config["execution"] if self.llm_config is not None else preset
        self.pool = KeyPool(limits["requests_per_minute"], limits.get("input_tokens_per_minute", 0),
                            limits["max_parallel"], limits.get("max_parallel_per_key", limits["max_parallel"]))

    def request_plan(self, state: Any, questions: list[DecisionQuestion]) -> tuple[dict[str, Any], int]:
        """Build the actual wire payload and its input-token estimate."""
        from .execution import estimate_tokens
        if self.llm_config is not None:
            from .decision_llm import request_plan
            return request_plan(self.preset, self.llm_config, state, questions)
        if self.preset["protocol"] == "typesafe":
            body = dict(model=self.preset["model"], state=state,
                        questions={q.name: dict(type="choice", instructions=q.instructions, criteria=q.choices) for q in questions})
        else:
            body = dict(model=self.preset["model"], input=json.dumps(state, ensure_ascii=False),
                        questions=[dict(type="choice", name=q.name, instructions=q.instructions,
                                        choices=[dict(value=k, description=v) for k, v in q.choices.items()]) for q in questions])
        return body, math.ceil(estimate_tokens(json.dumps(body, ensure_ascii=False, separators=(",", ":")))
                               * self.preset["token_safety_factor"])

    async def choose(self, state: Any, questions: list[DecisionQuestion], *, segment_id: str | None = None,
                     reference_context: list[str] | None = None) -> dict[str, DecisionAnswer]:
        if not questions:
            return {}
        if len({q.name for q in questions}) != len(questions):
            raise ConfigError("Decision 问题名称重复")
        if any(not q.name or not q.instructions or not 2 <= len(q.choices) <= 255 for q in questions):
            raise ConfigError("Decision Choice 问题无效")
        diagnostics = current_diagnostics()
        context = list(reference_context or [])
        if reference_context is not None and not isinstance(state, dict):
            raise ConfigError("Decision 携带上文时 state 必须是对象")
        requested = len(context)
        available = self.preset["context_window_tokens"] - self.preset["context_safety_margin_tokens"]
        while True:
            evidence = {**state, "reference_context": context} if reference_context is not None else state
            body, estimated = (await run_blocking(lambda: self.request_plan(evidence, questions)))
            if estimated <= available or not context:
                break
            context = context[1:]
        if len(context) < requested:
            get_logger(diagnostics.stage if diagnostics and diagnostics.stage else "decision").warning(
                "Decision 上文因上下文窗口限制缩减（Segment %s）：%d → %d",
                segment_id, requested, len(context),
            )
        overview = {"request_kind": "decision", "input": evidence, "segment_id": segment_id,
                    "questions": [asdict(q) for q in questions], "answers": {}}
        request_id = f"decision-{uuid.uuid4().hex}"
        if diagnostics is not None and (self.llm_config is None or estimated > available):
            diagnostics.begin_request(
                request_id=request_id, model=self.preset["model"], messages=[],
                max_attempts=self.retry["http_max_attempts"], request_kind="decision",
                segment_id=segment_id, question_count=len(questions),
                request_body=json.dumps(body, ensure_ascii=False, indent=2), overview=overview,
            )
        try:
            if estimated > available:
                segment = f"（Segment {segment_id}）" if segment_id else ""
                raise FatalExternalError(f"Decision 请求{segment}估算 {estimated} Token，超过可用上下文 {available} Token")
            if self.llm_config is not None:
                from .decision_llm import request_decision
                return await request_decision(self, body, estimated, questions, segment_id,
                                              request_id, requested, len(context), overview)
            if self.http_client is not None:
                return await self._request(self.http_client, body, questions, segment_id, request_id, requested, len(context), overview)
            async with httpx.AsyncClient(proxy=self.preset["proxy_url"] or None) as http:
                return await self._request(http, body, questions, segment_id, request_id, requested, len(context), overview)
        except asyncio.CancelledError:
            if diagnostics is not None:
                diagnostics.fail_request(request_id, "cancelled", interrupted=True)
            raise
        except Exception as exc:
            if diagnostics is not None:
                diagnostics.fail_request(request_id, str(exc))
            raise

    async def _request(self, http: httpx.AsyncClient, body: dict[str, Any],
                       questions: list[DecisionQuestion], segment_id: str | None, request_id: str,
                       context_segments_requested: int, context_segments_used: int,
                       overview: dict[str, Any]) -> dict[str, DecisionAnswer]:
        diagnostics = current_diagnostics()
        try:
            keys = resolve_api_keys(self.preset["credential"])
        except ExternalError as exc:
            raise FatalExternalError(str(exc)) from exc
        key_ids = [hashlib.sha256(key.encode()).hexdigest() for key in keys]
        for attempt in range(self.retry["http_max_attempts"]):
            lease = await self.pool.acquire(key_ids, estimated_tokens=0,
                on_wait_start=(lambda: diagnostics.rate_limit_wait_started(request_id)) if diagnostics is not None else None,
                on_wait_end=(lambda: diagnostics.rate_limit_wait_finished(request_id)) if diagnostics is not None else None)
            if diagnostics is not None:
                diagnostics.request_started(request_id)
            started = time.monotonic()
            record: dict[str, Any] = {"attempt": attempt + 1, "segment_id": segment_id,
                                    "context_segments_requested": context_segments_requested,
                                    "context_segments_used": context_segments_used,
                                    "questions": [q.name for q in questions],
                                    "evidence_digest": hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}
            data = None
            outcome = "succeeded"
            retrying = False
            try:
                response = await http.post(self.preset["url"], json=body,
                                           headers={"Authorization": f"Bearer {keys[lease.key_index]}"},
                                           timeout=self.preset["request_timeout_seconds"])
                record["http_status"] = response.status_code
                if response.status_code == 429 or 500 <= response.status_code <= 599:
                    outcome = "rate_limit_error" if response.status_code == 429 else "http_error"
                    raise httpx.HTTPStatusError("Decision 暂时不可用", request=response.request, response=response)
                if not response.is_success:
                    outcome = "http_error"
                    raise FatalExternalError(f"Decision 请求失败：HTTP {response.status_code}")
                try:
                    data = response.json()
                    usage = data.get("usage") if isinstance(data, dict) else None
                    if isinstance(usage, dict) and type(usage.get("input_tokens")) is int and usage["input_tokens"] >= 0:
                        record["usage"] = {"input_tokens": usage["input_tokens"], "output_tokens": 0, "total_tokens": usage["input_tokens"]}
                        if diagnostics is not None:
                            diagnostics.record_request_usage(
                                request_id, record["usage"], attempt=attempt + 1,
                                complete=True, output_tokens_known=False,
                            )
                    answers = (await run_blocking(lambda: self._parse(data, questions)))
                except (ValueError, TypeError, KeyError):
                    outcome = "response_parse_error"
                    raise
                overview["answers"] = {k: asdict(v) for k, v in answers.items()}
                record.update(model=data["model"], answers={k: asdict(v) for k, v in answers.items()})
                return answers
            except asyncio.CancelledError:
                outcome = "cancelled"
                record["error"] = "cancelled"
                raise
            except (httpx.TransportError, httpx.HTTPStatusError, ValueError, TypeError, KeyError) as exc:
                if isinstance(exc, (ValueError, TypeError, KeyError)) and outcome != "response_parse_error":
                    raise
                if isinstance(exc, httpx.TransportError):
                    outcome = "network_error"
                retrying = attempt + 1 < self.retry["http_max_attempts"]
                record["error"] = type(exc).__name__
                if attempt + 1 >= self.retry["http_max_attempts"]:
                    message = "Decision 响应无效" if outcome == "response_parse_error" else "Decision 请求失败"
                    raise FatalExternalError(f"{message}，重试预算耗尽") from exc
            except ExternalError:
                record["error"] = "decision_error"
                raise
            finally:
                record["elapsed_seconds"] = time.monotonic() - started
                self.records.append(record)
                if self.debug_directory is not None:
                    base = f"{request_id}-A{attempt + 1:03d}"
                    directory = self.debug_directory / "payloads"
                    directory.mkdir(parents=True, exist_ok=True)
                    (await run_blocking(lambda: atomic_write_json(directory / f"{base}.request.json", body)))
                    (await run_blocking(lambda: atomic_write_json(directory / f"{base}.overview.json", overview)))
                    if data is not None:
                        (await run_blocking(lambda: atomic_write_json(directory / f"{base}.response.json", data)))
                    if outcome != "succeeded":
                        (await run_blocking(lambda: atomic_write_json(directory / f"{base}.error.json", {"error": record.get("error", outcome)})))
                    (await run_blocking(lambda: append_jsonl_file(self.debug_directory / "attempts.jsonl", record_header(
                        "request_attempt", self.project_id, record_id=base, run_id=self.run_id,
                        request_id=request_id, stage=self.stage, attempt=attempt + 1,
                        http_status=record.get("http_status"), outcome=outcome,
                        status="completed" if outcome == "succeeded" else "failed", error=record.get("error")))))
                if diagnostics is not None:
                    diagnostics.request_finished(request_id=request_id, attempt=attempt + 1,
                        key_index=lease.key_index + 1, latency_seconds=record["elapsed_seconds"],
                        status=record.get("http_status"), error=outcome not in {"succeeded", "cancelled"}, retrying=retrying, outcome=outcome)
                    if outcome == "succeeded":
                        diagnostics.complete_request(request_id, content=json.dumps(data, ensure_ascii=False, indent=2), reasoning_content=None, overview_answers=overview["answers"])
                    if retrying:
                        diagnostics.retried(request_id)
                await lease.release()
            await asyncio.sleep(min(self.retry.get("max_delay_seconds", 60), self.retry["base_delay_seconds"] * 2 ** attempt))
        raise AssertionError("unreachable")

    def usage_summary(self) -> dict[str, Any] | None:
        from .execution import combine_usage, unavailable_usage
        if not self.records:
            return None
        summary = None
        for record in self.records:
            usage = record.get("usage")
            if self.llm_config is not None:
                summary = combine_usage(summary, usage)
                continue
            if isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0 for k in ("input_tokens", "output_tokens")):
                value = dict(input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
                             total_tokens=usage["input_tokens"] + usage["output_tokens"], available=True, partial=False)
            else:
                value = unavailable_usage()
            summary = combine_usage(summary, value)
        return summary

    def _parse(self, data: Any, questions: list[DecisionQuestion]) -> dict[str, DecisionAnswer]:
        if not isinstance(data, dict) or not isinstance(data.get("model"), str):
            raise ValueError("missing model")
        raw = data["answers"]
        if self.preset["protocol"] == "openai-decisions":
            if not isinstance(raw, list) or len({a["name"] for a in raw}) != len(raw):
                raise ValueError("invalid answers")
            raw = {a["name"]: a for a in raw}
        if not isinstance(raw, dict) or set(raw) != {q.name for q in questions}:
            raise ValueError("question mismatch")
        result = {}
        for q in questions:
            answer = raw[q.name]
            if not isinstance(answer, dict):
                raise ValueError("invalid answer")
            if answer["type"] == "refusal":
                result[q.name] = DecisionAnswer(None, {}, 0, True)
                continue
            if answer["type"] != "choice" or answer["choice"] not in q.choices:
                raise ValueError("invalid choice")
            probabilities = answer["probabilities"]
            if self.preset["protocol"] == "openai-decisions":
                if len({p["value"] for p in probabilities}) != len(probabilities):
                    raise ValueError("duplicate probabilities")
                probabilities = {p["value"]: p["probability"] for p in probabilities}
            if not isinstance(probabilities, dict):
                raise ValueError("invalid probabilities")
            confidence = answer["confidence"]
            if set(probabilities) != set(q.choices):
                raise ValueError("option mismatch")
            values = [confidence, *probabilities.values()]
            if any(type(v) not in {int, float} or not math.isfinite(v) or not 0 <= v <= 1 for v in values):
                raise ValueError("invalid probabilities")
            if not math.isclose(sum(probabilities.values()), 1, abs_tol=0.001):
                raise ValueError("probability sum")
            result[q.name] = DecisionAnswer(answer["choice"], probabilities, confidence)
        return result
