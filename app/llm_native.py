"""Provider response protocols: text, termination, errors and exact token counts."""
from __future__ import annotations

from typing import Any

from .errors import ExternalError
from .llm_adapter import LLMResponse, StreamErrorDetails, Usage

FORMATS = frozenset({"chat-completions", "responses", "anthropic", "gemini"})
FINISH_REASONS = {
    "chat-completions": (["length"], ["content_filter", "refusal"]),
    "responses": (["max_output_tokens"], ["content_filter", "refusal"]),
    "anthropic": (["max_tokens", "model_context_window_exceeded"], ["refusal"]),
    "gemini": (["MAX_TOKENS"], ["SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY", "OTHER"]),
}


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExternalError("LLM 原生响应字段必须是 JSON 对象")
    return value


def _string(value: Any) -> str:
    if not isinstance(value, str):
        raise ExternalError("LLM 响应正文不是字符串")
    return value


def _blocks(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ExternalError("LLM 原生响应内容必须是数组")
    return [_object(item) for item in value]


def _candidate(data: dict[str, Any]) -> dict[str, Any]:
    candidates = data.get("candidates", [])
    values = _blocks(candidates)
    return values[0] if values else {}


def finish_reason(fmt: str, data: dict[str, Any], *, stream: bool = False) -> str | None:
    if fmt == "chat-completions":
        choices = _blocks(data.get("choices", []))
        choice = choices[0] if choices else {}
        message = _object(choice.get("delta" if stream else "message", {}))
        refusal = message.get("refusal")
        if refusal:
            _string(refusal)
            return "refusal"
        reason = choice.get("finish_reason")
    elif fmt == "responses":
        if stream and data.get("type") in {"response.refusal.delta", "response.refusal.done"}:
            return "refusal"
        response = _object(data.get("response", {})) if stream else data
        for item in _blocks(response.get("output", [])):
            if item.get("type") == "message" and any(part.get("type") == "refusal" for part in _blocks(item.get("content", []))):
                return "refusal"
        reason = _object(response.get("incomplete_details") or {}).get("reason")
    elif fmt == "anthropic":
        reason = _object(data.get("delta", {})).get("stop_reason") if stream else data.get("stop_reason")
    else:
        reason = _object(data.get("promptFeedback", {})).get("blockReason") or _candidate(data).get("finishReason")
    return _string(reason) if reason is not None else None


def content_deltas(fmt: str, data: dict[str, Any]) -> list[str]:
    if fmt == "chat-completions":
        choices = _blocks(data.get("choices", []))
        delta = _object(choices[0].get("delta", {})) if choices else {}
        value = delta.get("content")
        return [_string(value)] if value is not None else []
    if fmt == "responses":
        return [_string(data.get("delta"))] if data.get("type") == "response.output_text.delta" else []
    if fmt == "anthropic":
        delta = _object(data.get("delta", {}))
        return [_string(delta.get("text"))] if data.get("type") == "content_block_delta" and delta.get("type") == "text_delta" else []
    content = _object(_candidate(data).get("content", {}))
    return [_string(part["text"]) for part in _blocks(content.get("parts", [])) if "text" in part and not part.get("thought", False)]


def reasoning_deltas(fmt: str, data: dict[str, Any]) -> list[str]:
    if fmt != "chat-completions":
        return []
    choices = _blocks(data.get("choices", []))
    delta = _object(choices[0].get("delta", {})) if choices else {}
    return [_string(delta[key]) for key in ("reasoning_content", "reasoning") if key in delta and delta[key] is not None]


def stream_error(fmt: str, data: dict[str, Any]) -> StreamErrorDetails | None:
    detail = data.get("error")
    if fmt == "responses":
        response = _object(data.get("response", {})) if data.get("type") == "response.incomplete" else data
        if response.get("status") == "incomplete" or data.get("type") == "response.incomplete":
            reason = _object(response.get("incomplete_details") or {}).get("reason")
            if reason not in {"max_output_tokens", "content_filter"}:
                return StreamErrorDetails(f"Responses 未完成：{reason or '未提供原因'}", None)
    if fmt == "responses" and data.get("type") == "response.failed":
        detail = _object(data.get("response", {})).get("error") or {"message": "response.failed"}
    elif fmt == "responses" and data.get("type") == "error":
        detail = data
    if detail is None:
        return None
    detail = _object(detail)
    message = _string(detail.get("message", "LLM 供应商报告错误"))
    status = detail.get("status")
    if status is not None and (type(status) is not int or not 100 <= status <= 599):
        raise ExternalError("LLM 流式错误状态不是有效 HTTP 状态码")
    return StreamErrorDetails(message, status)


def terminal(fmt: str, data: dict[str, Any]) -> bool:
    if fmt == "responses":
        return data.get("type") in {"response.completed", "response.incomplete"}
    if fmt == "anthropic":
        return data.get("type") == "message_stop"
    return False  # Chat uses [DONE]; Gemini reads usage through the natural EOF.


def parse_response(fmt: str, response: Any) -> LLMResponse:
    data = _object(response)
    failure = stream_error(fmt, data)
    if fmt == "responses" and data.get("status") == "failed":
        failure = failure or StreamErrorDetails("response.failed", None)
    if failure:
        raise ExternalError(failure.message)
    reason = finish_reason(fmt, data)
    reasoning = None
    if fmt == "chat-completions":
        choices = _blocks(data.get("choices"))
        if not choices:
            raise ExternalError("LLM 响应没有 choices")
        message = _object(choices[0].get("message"))
        if "content" not in message and reason != "refusal":
            raise ExternalError("LLM 响应缺少正文路径：message.content")
        text = _string(message["content"]) if message.get("content") is not None else ""
        for key in ("reasoning_content", "reasoning"):
            if key in message:
                reasoning = _string(message[key]) if message[key] is not None else None
                break
    elif fmt == "responses":
        parts = []
        for item in _blocks(data.get("output")):
            if item.get("type") == "message":
                parts.extend(_string(part["text"]) for part in _blocks(item.get("content")) if part.get("type") == "output_text")
        text = "".join(parts)
    elif fmt == "anthropic":
        blocks = data.get("content", []) if reason in sum(FINISH_REASONS[fmt], []) else data.get("content")
        text = "".join(_string(part["text"]) for part in _blocks(blocks) if part.get("type") == "text")
    else:
        candidate = _candidate(data)
        if not candidate and reason is None:
            raise ExternalError("LLM 响应没有 candidates")
        content = _object(candidate.get("content", {}))
        text = "".join(_string(part["text"]) for part in _blocks(content.get("parts", [])) if "text" in part and not part.get("thought", False))
    return LLMResponse(text, reasoning, reason)


def usage_values(fmt: str, data: dict[str, Any], *, stream: bool = False) -> dict[str, int]:
    if fmt == "anthropic" and stream and data.get("type") == "message_start":
        usage = _object(_object(data.get("message", {})).get("usage", {}))
    elif fmt == "responses" and stream:
        usage = _object(data.get("response", {})).get("usage")
    else:
        usage = data.get("usageMetadata" if fmt == "gemini" else "usage")
    if usage is None:
        return {}
    usage = _object(usage)
    if fmt == "anthropic":
        values = {}
        if "input_tokens" in usage:
            fields = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
            counts = [usage.get(field, 0) for field in fields]
            if all(type(count) is int and count >= 0 for count in counts):
                values["input_tokens"] = sum(counts)
        if type(usage.get("output_tokens")) is int and usage["output_tokens"] >= 0:
            values["output_tokens"] = usage["output_tokens"]
        return complete_usage(fmt, values)
    fields = ("prompt_tokens", "completion_tokens", "total_tokens") if fmt == "chat-completions" else ("promptTokenCount", "candidatesTokenCount", "totalTokenCount") if fmt == "gemini" else ("input_tokens", "output_tokens", "total_tokens")
    return {metric: usage[field] for metric, field in zip(("input_tokens", "output_tokens", "total_tokens"), fields) if type(usage.get(field)) is int and usage[field] >= 0}


def complete_usage(fmt: str, values: dict[str, int]) -> dict[str, int]:
    if fmt == "anthropic" and {"input_tokens", "output_tokens"} <= values.keys():
        return {**values, "total_tokens": values["input_tokens"] + values["output_tokens"]}
    return values


def extract_usage(fmt: str, response: Any) -> Usage | None:
    values = usage_values(fmt, _object(response))
    if not {"input_tokens", "output_tokens", "total_tokens"} <= values.keys():
        return None
    return Usage(**values)
