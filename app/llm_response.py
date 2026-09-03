from __future__ import annotations
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any
from .errors import (
    ExternalError,
)
from .llm_adapter import LLMResponse

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
    records_by_type: dict[str, tuple[dict[str, Any], ...]] | None = None


class TerminologyResponseMode(str, Enum):
    """The record classes a terminology request is allowed to return."""

    TERMS_ONLY = "terms-only"
    TERMS_AND_FRAGMENT_SUMMARY = "terms+fragment-summary"
    SUMMARY_ONLY = "summary-only"


@dataclass(frozen=True)
class TerminologyResponse:
    """Validated results from one terminology or fragment-summary request.

    ``global_error_codes`` describe the JSONL envelope.  They invalidate every
    requested result class, while a class-specific error only invalidates that
    class.  Valid rows are retained so callers can persist useful partial
    results before retrying the failed class.
    """

    records: tuple[dict[str, Any], ...]
    terms: tuple[dict[str, Any], ...]
    summaries: tuple[dict[str, Any], ...]
    errors: tuple[str, ...]
    error_codes: tuple[str, ...]
    global_errors: tuple[str, ...]
    global_error_codes: tuple[str, ...]
    errors_by_type: dict[str, tuple[str, ...]]
    error_codes_by_type: dict[str, tuple[str, ...]]
    complete: bool
    terms_complete: bool
    summary_complete: bool
    has_valid_end: bool


def response_record_types(
    mode: TerminologyResponseMode | str,
) -> tuple[str, ...]:
    """Return the permitted, ordered record classes for a request mode."""

    try:
        normalized = (
            mode
            if isinstance(mode, TerminologyResponseMode)
            else TerminologyResponseMode(mode)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"不支持的术语响应模式：{mode}") from exc
    if normalized is TerminologyResponseMode.TERMS_ONLY:
        return ("term",)
    if normalized is TerminologyResponseMode.TERMS_AND_FRAGMENT_SUMMARY:
        return ("summary", "term")
    return ("summary",)

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

def parse_jsonl_document(
    content: str,
    *,
    record_type: str | tuple[str, ...],
) -> JSONLDocument:
    """Parse the shared JSONL envelope for one or more allowed record types.

    The single-type call remains the compatibility path used by the existing
    stages.  A tuple is useful for the terminology experiment because it keeps
    the original response order while also exposing rows grouped by type.
    """

    allowed_types = (record_type,) if isinstance(record_type, str) else record_type
    if not allowed_types or any(not isinstance(item, str) or not item for item in allowed_types):
        raise ValueError("record_type 必须是非空字符串或字符串元组")
    if len(set(allowed_types)) != len(allowed_types):
        raise ValueError("record_type 不能含重复类型")
    body = extract_jsonl_content(content)
    records: list[dict[str, Any]] = []
    records_by_type: dict[str, list[dict[str, Any]]] = {
        item: [] for item in allowed_types
    }
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
        if item_type not in allowed_types:
            errors.append(f"第 {line_number} 行包含未知 type")
            error_codes.append("unknown_type")
            continue
        records.append(value)
        records_by_type[item_type].append(value)
    if not seen_end:
        errors.append("响应缺少最终 end 记录")
        error_codes.append("missing_end")
    return JSONLDocument(
        records=tuple(records),
        errors=tuple(errors),
        error_codes=tuple(error_codes),
        complete=seen_end and not errors,
        has_valid_end=seen_end
        and not any(
            code in {"invalid_end", "after_end"}
            for code in error_codes
        ),
        records_by_type={
            item: tuple(values) for item, values in records_by_type.items()
        },
    )


def _term_is_in_sources(term: str, source_texts: tuple[str, ...]) -> bool:
    return any(term in source for source in source_texts)


def _validate_terminology_record(
    record: dict[str, Any],
    *,
    source_texts: tuple[str, ...],
    seen_sources: set[str] | None,
) -> tuple[str | None, dict[str, Any] | None]:
    allowed = {
        "type",
        "source",
        "category",
        "description",
        "preferred_translation",
        "aliases",
    }
    source = record.get("source")
    category = record.get("category")
    if set(record) - allowed:
        return "unknown_field", None
    if not isinstance(source, str) or not source.strip():
        return "invalid_source", None
    source = source.strip()
    if not isinstance(category, str) or not category.strip():
        return "invalid_category", None
    if not _term_is_in_sources(source, source_texts):
        return "source_not_found", None
    if seen_sources is not None and source in seen_sources:
        return "duplicate_term", None
    description = record.get("description")
    preferred = record.get("preferred_translation")
    aliases = record.get("aliases", [])
    if description is not None and not isinstance(description, str):
        return "invalid_description", None
    if preferred is not None and not isinstance(preferred, str):
        return "invalid_preferred_translation", None
    if not isinstance(aliases, list) or not all(
        isinstance(alias, str) and alias.strip() for alias in aliases
    ):
        return "invalid_aliases", None
    if any(not _term_is_in_sources(alias.strip(), source_texts) for alias in aliases):
        return "alias_not_found", None
    if seen_sources is not None:
        seen_sources.add(source)
    return None, {
        "type": "term",
        "source": source,
        "category": category.strip(),
        "description": description.strip() if description else None,
        "preferred_translation": preferred.strip() if preferred else None,
        "aliases": [alias.strip() for alias in aliases],
    }


def _validate_summary_record(
    record: dict[str, Any],
    *,
    source_refs: tuple[str, ...],
) -> tuple[str | None, dict[str, Any] | None]:
    allowed = {"type", "text", "refs"}
    if set(record) - allowed:
        return "unknown_field", None
    text = record.get("text")
    refs = record.get("refs")
    if not isinstance(text, str) or not text.strip():
        return "invalid_text", None
    if not isinstance(refs, list) or not refs or not all(
        isinstance(ref, str) and ref for ref in refs
    ):
        return "invalid_reference", None
    if len(set(refs)) != len(refs):
        return "duplicate_reference", None
    if source_refs and any(ref not in source_refs for ref in refs):
        return "invalid_reference", None
    return None, {"type": "summary", "text": text.strip(), "refs": list(refs)}


def parse_terminology_response(
    content: str,
    *,
    mode: TerminologyResponseMode | str,
    source_refs: tuple[str, ...] | list[str] = (),
    source_texts: tuple[str, ...] | list[str] = (),
) -> TerminologyResponse:
    """Validate one terminology response according to its explicit mode.

    ``source_refs`` are request-local stable references (usually mapped by the
    host to durable ``(file_id, part_id, segment_id)`` ranges).  Term sources
    and aliases are checked against the source text array so context-only text
    cannot create a candidate.
    """

    allowed_types = response_record_types(mode)
    expected_refs = tuple(source_refs)
    source_values = tuple(source_texts)
    document = parse_jsonl_document(content, record_type=allowed_types)
    global_errors = document.errors
    global_codes = document.error_codes
    errors_by_type: dict[str, list[str]] = {item: [] for item in allowed_types}
    error_codes_by_type: dict[str, list[str]] = {
        item: [] for item in allowed_types
    }
    terms: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    # The existing terms-only parser accepts repeated source rows.  The
    # combined experimental response may opt into duplicate detection because
    # it has a class-level completion boundary to report.
    seen_sources: set[str] | None = (
        set() if allowed_types == ("summary", "term") else None
    )
    raw_by_type = document.records_by_type or {}

    if "summary" in allowed_types:
        summary_rows = raw_by_type.get("summary", ())
        if len(summary_rows) > 1:
            errors_by_type["summary"].append("summary 记录只能有一条")
            error_codes_by_type["summary"].append("duplicate_summary")
        if summary_rows:
            error, validated = _validate_summary_record(
                summary_rows[0], source_refs=expected_refs
            )
            if error is None and validated is not None:
                summaries.append(validated)
            else:
                errors_by_type["summary"].append(
                    f"summary 记录字段或引用无效：{error}"
                )
                error_codes_by_type["summary"].append(error or "invalid_summary")
        else:
            errors_by_type["summary"].append("响应缺少 summary 记录")
            error_codes_by_type["summary"].append("missing_summary")

    if "term" in allowed_types:
        for row in raw_by_type.get("term", ()):
            error, validated = _validate_terminology_record(
                row,
                source_texts=source_values,
                seen_sources=seen_sources,
            )
            if error is not None or validated is None:
                errors_by_type["term"].append(
                    f"term 记录字段或来源无效：{error or 'invalid_term'}"
                )
                error_codes_by_type["term"].append(error or "invalid_term")
            else:
                terms.append(validated)

    # A joint response is ordered summary, then terms.  This is a protocol
    # error because callers cannot safely assign a later summary to an earlier
    # batch once records have been persisted.
    if allowed_types == ("summary", "term"):
        saw_term = False
        for row in document.records:
            if row.get("type") == "term":
                saw_term = True
            elif row.get("type") == "summary" and saw_term:
                global_errors = (*global_errors, "summary 必须出现在 term 之前")
                global_codes = (*global_codes, "invalid_order")
                break

    typed_errors = {key: tuple(value) for key, value in errors_by_type.items()}
    typed_codes = {
        key: tuple(value) for key, value in error_codes_by_type.items()
    }
    class_errors = tuple(
        message for values in typed_errors.values() for message in values
    )
    class_codes = tuple(code for values in typed_codes.values() for code in values)
    errors = (*global_errors, *class_errors)
    error_codes = (*global_codes, *class_codes)
    envelope_valid = not global_codes
    terms_complete = "term" not in allowed_types or (
        not error_codes_by_type.get("term")
    )
    summary_complete = "summary" not in allowed_types or (
        not error_codes_by_type.get("summary")
    )
    complete = envelope_valid and terms_complete and summary_complete
    records = tuple([*summaries, *terms])
    return TerminologyResponse(
        records=records,
        terms=tuple(terms),
        summaries=tuple(summaries),
        errors=errors,
        error_codes=error_codes,
        global_errors=tuple(global_errors),
        global_error_codes=tuple(global_codes),
        errors_by_type=typed_errors,
        error_codes_by_type=typed_codes,
        complete=complete,
        terms_complete=terms_complete if envelope_valid else False,
        summary_complete=summary_complete if envelope_valid else False,
        has_valid_end=document.has_valid_end,
    )
