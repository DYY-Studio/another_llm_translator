"""Translate one in-memory Segment without project results or a Run."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from .async_utils import run_blocking
from .config import load_project_config
from .errors import ContextLengthError, IncompleteError, UsageError
from .execution import combine_usage
from .llm_client import LLMClient, empty_response_split_scope
from .llm_keys import KeyPool
from .logging_utils import get_logger
from .sqlite_storage import read_project_meta_read_only
from .stage_runtime import (
    _localized_request_loop, _prompt_factory, _prompt_language,
    _replace_with_runtime_parts, _restore_leading_whitespace,
    _split_oversized_preflight, _split_segment_source, _split_source_once,
)
from .stage_translation import (
    _has_hard_validation_findings, _map_local_translation_response,
    translation_payload, translation_validation_resources,
)
from .term_library import load_terms, term_normalization
from .term_matching import _TermMatchCache
from .translation_validation import TranslationValidationContext, validate_translation_response


async def translate_text(
    project: Path, source: str, *,
    config: dict[str, Any] | None = None,
    limiter: KeyPool | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    if not source.strip():
        raise UsageError("翻译文本不能为空")
    if config is None:
        config = await run_blocking(lambda: load_project_config(project, stage="translation"))
    metadata = await run_blocking(read_project_meta_read_only, project)
    # These identities exist only within this request, never in project storage.
    segment = dict(segment_id="TEXT", file_id="TEXT", part_id="TEXT", source=source,
                   line_index=0, is_empty=False)
    warnings: list[str] = []
    library = await run_blocking(lambda: load_terms(project, resolve_knowledge=False))
    if library is None:
        warnings.append("没有已发布术语库，本次翻译未使用项目术语")
    cache = _TermMatchCache(library, term_normalization(config),
                           int(config["terminology"]["max_terms_per_segment"]),
                           project=project, segments=[segment], warnings=warnings, positionless=True)
    language = await run_blocking(_prompt_language, project, "translation", None)
    prompt = await run_blocking(lambda: _prompt_factory(project, "translation", language)(()))
    decisions, decision, validators = translation_validation_resources(
        config, project_id=metadata["project_id"], http_client=http_client,
    )
    options = config["validation"]["translation"]

    def payload_builder(items: list[dict[str, Any]]) -> dict[str, Any]:
        return translation_payload(config["project"]["target_language"], items, cache.for_items(items),
                                   reference_context=[], summary_context=[], summary_context_relation=None)

    if limiter is None:
        execution = config["execution"]
        limiter = KeyPool(execution["requests_per_minute"], execution["input_tokens_per_minute"],
                          execution["max_parallel"], execution["max_parallel_per_key"])
    findings: list[dict[str, Any]] = []
    hard_repairs = 0
    advisory_repaired = False
    candidate = ""
    async with LLMClient(config, limiter, run_dir=None, project_id=metadata["project_id"],
                         run_id="", stage="translation", client=http_client) as llm:
        while True:
            preflight = await run_blocking(lambda: _split_oversized_preflight(
                [segment], config=config, prompt=prompt, payload_builder=payload_builder,
                fail_planning=lambda _exc: None,
                make_probe=lambda item, text: _split_segment_source(item, "TEXT-PROBE", text),
                split_part=lambda text: list(_split_source_once(text)), accept_part=_split_segment_source,
            ))
            if preflight.preflight_failed:
                raise IncompleteError("输入超过模型限制，项目设置已关闭超长 Segment 拆分")
            by_id = {str(item["segment_id"]): item for item in [segment, *preflight.request_segments]}
            results: dict[str, str] = {}

            async def accept_response(group: list[dict[str, Any]], values: dict, **_: Any) -> None:
                results.update({key: str(value[1]) for key, value in values.items()})

            async def save_error(ids: list[str], request_id: str, message: str) -> None:
                raise IncompleteError(message)

            async def generate(items: list[dict[str, Any]], parent: str | None = None) -> None:
                repair = {str(item["segment_id"]): {"findings": findings} for item in items} if findings else None
                try:
                    exhausted = await _localized_request_loop(
                        items, payload_builder=payload_builder, prompt=prompt, prompt_language=language,
                        config=config, llm=llm, stage="translation", accept=None,
                        accept_response=accept_response, save_error=save_error,
                        parse=_map_local_translation_response, by_id=by_id, segments=[segment],
                        prompt_partition_key=lambda _item: None, logger=get_logger("translation"),
                        repair_candidates=repair, initial_parent_request_id=parent,
                    )
                except ContextLengthError as exc:
                    if not config["chunking"]["allow_split_oversized_segment"]:
                        raise
                    with empty_response_split_scope(exc):
                        # Requests contain one slice of the same logical Segment.
                        parts = _replace_with_runtime_parts(items[0], part_original=preflight.part_original,
                                                            original_parts=preflight.original_parts, by_id=by_id)
                        for part in parts:
                            await generate([part], exc.request_id)
                    return
                if exhausted:
                    raise IncompleteError("翻译响应格式修正次数耗尽")

            for item in preflight.request_segments:
                await generate([item])
            expected = preflight.original_parts.get("TEXT", ["TEXT"])
            if any(key not in results for key in expected):
                raise IncompleteError("译文不完整")
            candidate = "".join(results[key] for key in expected)
            context = TranslationValidationContext(
                source=source, translation=candidate, segment_id="TEXT",
                terms=await run_blocking(cache.validation_matches_for_item, segment), decision=decision,
                decision_prompts={key: value["content"] for key, value in config.get("_decision_prompt_definitions", {}).items()},
                decision_confidence_threshold=options["decision_confidence_threshold"],
                decision_warning_threshold=options["decision_warning_threshold"],
            )
            findings = (await validate_translation_response((context,), validators)).get("TEXT", [])
            if not findings:
                break
            if _has_hard_validation_findings(findings):
                if hard_repairs < options["max_retry_attempts"]:
                    hard_repairs += 1
                    continue
                if options["exhausted_mode"] != "warning":
                    raise IncompleteError("翻译文字校验修复次数耗尽")
            elif not advisory_repaired and any(item.get("repairable", True) for item in findings):
                advisory_repaired = True
                continue
            warnings.append("译文存在校验警告，请核对译文")
            break
    warnings.extend(value for value in llm.warnings if value not in warnings)
    usage = llm.usage_summary()
    for client in decisions.values():
        usage = combine_usage(usage, client.usage_summary())
    return dict(text=_restore_leading_whitespace(source, candidate), usage=usage,
                warnings=warnings, validation_findings=findings)
