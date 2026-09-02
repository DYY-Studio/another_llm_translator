from __future__ import annotations
import asyncio
import hashlib
import json
import math
import random
import re
import shutil
import sys
import time
import uuid
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TypeVar
import httpx
from .config import load_project_config, load_run_config
from .credentials import resolve_api_keys
from .diagnostics import current_diagnostics
from .documents import aozora_to_model_ruby
from .errors import (
    ConfigError,
    ContextLengthError,
    ExternalError,
    FatalExternalError,
    RequestSizeError,
    StorageError,
    UsageError,
)
from .i18n import SUPPORTED_LANGUAGES
from .llm_adapter import JSONLLMAdapter, LLMResponse, Usage
from .llm_keys import KeyPool, NoAvailableKey
from .llm_preset import endpoint_url
from .logging_utils import get_logger
from .sqlite_storage import (
    append_jsonl,
    append_jsonl_file,
    atomic_write_json,
    read_json,
    read_jsonl,
    record_header,
    utc_now,
    write_json,
)
from .term_decision_protocol import terminology_decision_protocol

_SUPPORTED_FENCE_LABELS = {"", "jsonl", "ndjson", "json"}
_FENCE_RE = re.compile(
    r"```[ \t]*(?P<label>[^\r\n`]*)\r?\n(?P<body>.*?)```",
    re.DOTALL,
)
_THOUGHT_BLOCK_TAGS = (
    ("<think>", "</think>"),
    ("<thinking>", "</thinking>"),
    ("<thought>", "</thought>"),
    ("<analysis>", "</analysis>"),
)



@dataclass(frozen=True)
class JSONLDocument:
    records: tuple[dict[str, Any], ...]
    errors: tuple[str, ...]
    error_codes: tuple[str, ...]
    complete: bool
    has_valid_end: bool

def normalize_llm_response(response: LLMResponse) -> LLMResponse:
    embedded = _extract_embedded_reasoning(response.content)
    if response.reasoning_content and embedded.reasoning_content:
        raise ExternalError("LLM 响应同时包含结构化和 content 内嵌思考正文")
    return LLMResponse(
        content=embedded.content,
        reasoning_content=(response.reasoning_content or embedded.reasoning_content),
    )

def _extract_embedded_reasoning(content: str) -> LLMResponse:
    normalized = content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    stripped = normalized.lstrip()
    for opening, closing in _THOUGHT_BLOCK_TAGS:
        if not stripped.startswith(opening):
            continue
        closing_at = stripped.find(closing, len(opening))
        if closing_at < 0:
            return LLMResponse(stripped.strip(), None)
        thought = stripped[len(opening) : closing_at]
        remainder = stripped[closing_at + len(closing) :].lstrip()
        if any(tag in thought for pair in _THOUGHT_BLOCK_TAGS for tag in pair) or any(
            remainder.startswith(tag) for pair in _THOUGHT_BLOCK_TAGS for tag in pair
        ):
            return LLMResponse(stripped.strip(), None)
        return LLMResponse(remainder, thought)
    return LLMResponse(stripped.strip(), None)

def extract_jsonl_content(content: str) -> str:
    normalized = _extract_embedded_reasoning(content).content
    for match in _FENCE_RE.finditer(normalized):
        label = match.group("label").strip().casefold()
        body = match.group("body").strip()
        if label in _SUPPORTED_FENCE_LABELS and body:
            outside = normalized[: match.start()] + normalized[match.end() :]
            if any(tag in outside for pair in _THOUGHT_BLOCK_TAGS for tag in pair):
                return normalized.strip()
            return body
    return normalized.strip()

def parse_jsonl_document(content: str, *, record_type: str) -> JSONLDocument:
    body = extract_jsonl_content(content)
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    error_codes: list[str] = []
    seen_end = False
    for line_number, raw_line in enumerate(body.split("\n"), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if seen_end:
            errors.append(f"第 {line_number} 行位于 end 之后")
            error_codes.append("after_end")
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"第 {line_number} 行不是合法 JSON 对象")
            error_codes.append("invalid_json")
            continue
        if not isinstance(value, dict):
            errors.append(f"第 {line_number} 行必须是 JSON 对象")
            error_codes.append("non_object")
            continue
        item_type = value.get("type")
        if item_type == "end":
            if set(value) != {"type"}:
                errors.append(f"第 {line_number} 行 end 记录含有额外字段")
                error_codes.append("invalid_end")
            seen_end = True
            continue
        if item_type != record_type:
            errors.append(f"第 {line_number} 行包含未知 type")
            error_codes.append("unknown_type")
            continue
        records.append(value)
    if not seen_end:
        errors.append("响应缺少最终 end 记录")
        error_codes.append("missing_end")
    return JSONLDocument(
        records=tuple(records),
        errors=tuple(errors),
        error_codes=tuple(error_codes),
        complete=seen_end and not errors,
        has_valid_end=seen_end
        and not any(code in {"invalid_end", "after_end"} for code in error_codes),
    )
