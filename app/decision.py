"""Host-managed HTTP Choice decisions."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from .credentials import resolve_api_keys
from .errors import ConfigError, ExternalError, FatalExternalError
from .llm_keys import KeyPool


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
                     segment_id: str | None = None) -> dict[str, DecisionAnswer]: ...


def decision_preset_path(root: Path, preset_id: str) -> Path:
    if not isinstance(preset_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", preset_id):
        raise ConfigError("Decision Preset ID 格式无效")
    return root / "decision_presets" / f"{preset_id}.json"


def validate_decision_preset(value: dict[str, Any]) -> dict[str, Any]:
    required = {"preset_id", "protocol", "url", "model", "credential", "proxy_url",
                "request_timeout_seconds", "requests_per_minute", "max_parallel",
                "context_window_tokens", "context_safety_margin_tokens"}
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
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not parsed.path or parsed.path == "/"):
        raise ConfigError("Decision URL 必须含最终 Path，不能含凭据、查询参数或 fragment")
    if not isinstance(value["model"], str) or not value["model"].strip():
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
            or not isinstance(credential["kind"], str) or credential["kind"] not in {"environment", "keychain"}
            or not isinstance(credential["name"], str) or not credential["name"].strip()):
        raise ConfigError("Decision 凭据引用无效")
    for key, minimum in (("requests_per_minute", 0), ("max_parallel", 1),
                         ("context_window_tokens", 1), ("context_safety_margin_tokens", 0)):
        if type(value[key]) is not int or value[key] < minimum:
            raise ConfigError(f"Decision {key} 无效")
    if value["context_safety_margin_tokens"] >= value["context_window_tokens"]:
        raise ConfigError("Decision 安全余量必须小于上下文窗口")
    timeout = value["request_timeout_seconds"]
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout <= 0:
        raise ConfigError("Decision 超时必须是有限正数")
    return value


def load_decision_preset(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"无法读取 Decision Preset：{path.name}") from exc
    if not isinstance(value, dict):
        raise ConfigError("Decision Preset 必须是对象")
    return validate_decision_preset(value)


class DecisionClient:
    def __init__(self, preset: dict[str, Any], *, http_client: httpx.AsyncClient | None = None,
                 retry: dict[str, Any] | None = None) -> None:
        self.preset = preset
        self.http_client = http_client
        self.retry = retry or {"http_max_attempts": 1, "base_delay_seconds": 0}
        self.records: list[dict[str, Any]] = []
        self.pool = KeyPool(preset["requests_per_minute"], 0,
                            preset["max_parallel"], preset["max_parallel"])

    async def choose(self, state: Any, questions: list[DecisionQuestion], *, segment_id: str | None = None) -> dict[str, DecisionAnswer]:
        if not questions:
            return {}
        if len({q.name for q in questions}) != len(questions):
            raise ConfigError("Decision 问题名称重复")
        if any(not q.name or not q.instructions or not 2 <= len(q.choices) <= 255 for q in questions):
            raise ConfigError("Decision Choice 问题无效")
        if self.preset["protocol"] == "typesafe":
            body = dict(model=self.preset["model"], state=state,
                        questions={q.name: dict(type="choice", instructions=q.instructions, criteria=q.choices) for q in questions})
        else:
            body = dict(model=self.preset["model"], input=json.dumps(state, ensure_ascii=False),
                        questions=[dict(type="choice", name=q.name, instructions=q.instructions,
                                        choices=[dict(value=k, description=v) for k, v in q.choices.items()]) for q in questions])
        from .execution import estimate_tokens
        estimated = estimate_tokens(json.dumps(body, ensure_ascii=False, separators=(",", ":")))
        available = self.preset["context_window_tokens"] - self.preset["context_safety_margin_tokens"]
        if estimated > available:
            segment = f"（Segment {segment_id}）" if segment_id else ""
            raise FatalExternalError(f"Decision 请求{segment}估算 {estimated} Token，超过可用上下文 {available} Token")
        if self.http_client is not None:
            return await self._request(self.http_client, body, questions, segment_id)
        async with httpx.AsyncClient(proxy=self.preset["proxy_url"] or None) as http:
            return await self._request(http, body, questions, segment_id)

    async def _request(self, http: httpx.AsyncClient, body: dict[str, Any],
                       questions: list[DecisionQuestion], segment_id: str | None) -> dict[str, DecisionAnswer]:
        try:
            keys = resolve_api_keys(self.preset["credential"])
        except ExternalError as exc:
            raise FatalExternalError(str(exc)) from exc
        key_ids = [hashlib.sha256(key.encode()).hexdigest() for key in keys]
        for attempt in range(self.retry["http_max_attempts"]):
            lease = await self.pool.acquire(key_ids, estimated_tokens=0)
            started = time.monotonic()
            record: dict[str, Any] = {"attempt": attempt + 1, "segment_id": segment_id,
                                    "questions": [q.name for q in questions],
                                    "evidence_digest": hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}
            try:
                response = await http.post(self.preset["url"], json=body,
                                           headers={"Authorization": f"Bearer {keys[lease.key_index]}"},
                                           timeout=self.preset["request_timeout_seconds"])
                record["http_status"] = response.status_code
                if response.status_code == 429 or 500 <= response.status_code <= 599:
                    raise httpx.HTTPStatusError("Decision 暂时不可用", request=response.request, response=response)
                if not response.is_success:
                    raise FatalExternalError(f"Decision 请求失败：HTTP {response.status_code}")
                try:
                    data = response.json()
                    answers = self._parse(data, questions)
                except (ValueError, TypeError, KeyError) as exc:
                    raise FatalExternalError("Decision 响应无效") from exc
                record.update(model=data["model"], answers={k: asdict(v) for k, v in answers.items()}, usage=data.get("usage"))
                return answers
            except asyncio.CancelledError:
                record["error"] = "cancelled"
                raise
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                record["error"] = type(exc).__name__
                if attempt + 1 >= self.retry["http_max_attempts"]:
                    raise FatalExternalError("Decision 请求失败，重试预算耗尽") from exc
            except ExternalError:
                record["error"] = "decision_error"
                raise
            finally:
                record["elapsed_seconds"] = time.monotonic() - started
                self.records.append(record)
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
