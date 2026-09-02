from __future__ import annotations
import asyncio
import csv
import hashlib
import io
import json
import shlex
import sys
import time
import unicodedata
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, NamedTuple
import httpx
from .config import load_project_config
from .documents import (
    DocumentExportJob,
    aozora_match_views,
    aozora_safe_split_positions,
    compact_emphasis_aozora,
    document_adapter_reads_version,
    publish_document_exports,
)
from .errors import (
    ConfigError,
    ContextLengthError,
    ExportError,
    ExternalError,
    FatalExternalError,
    IncompleteError,
    RequestSizeError,
    StorageError,
    UsageError,
)
from .execution import (
    ChunkPlan,
    LLMClient,
    PreviousContextIndex,
    Scope,
    SlidingWindowLimiter,
    build_chunk_plans,
    classify_stage,
    classify_stage_states,
    combine_usage,
    contiguous_groups,
    continue_run,
    create_run,
    dispatch_chunks,
    estimate_messages,
    estimate_single_segment_preflight,
    finalize_run,
    find_running_runs,
    full_prompt,
    iter_chunk_plans,
    load_stage_history,
    localize_request_ids,
    materialize_chunk_stream,
    parse_jsonl_document,
    render_messages,
    save_debug_chunks,
    scope_from_run,
    segment_model_source,
    segment_model_text,
    select_scope,
    stage_fingerprint,
    stage_result_path,
    unavailable_usage,
)
from .i18n import SUPPORTED_LANGUAGES, resolve_language
from .llm_keys import KeyPool
from .logging_utils import get_logger
from .plugins import (
    get_document_adapter,
    normalize_model_text,
)
from .project import (
    PROMPT_LANGUAGES,
    load_segments,
    load_source_files,
    prompt_file,
)
from .sqlite_storage import (
    append_jsonl,
    atomic_write_text,
    latest_stage_states,
    read_json,
    read_jsonl,
    record_exists,
    record_header,
    terminology_scan_state,
    write_json,
)
from .translation_validation import (
    TranslationTermMatch,
    TranslationValidationContext,
    validate_translation_text,
)
from .term_library import TermNormalization, normalize_term

class _PreparedTermMatcher:
    """Pre-index a published terminology library for repeated segment matches."""

    def __init__(self, library: dict[str, Any], spec: TermNormalization) -> None:
        self.spec = spec
        self.terms = list(library.get("terms", []))
        self.by_normalized = {
            str(
                term.get("normalized")
                or normalize_term(str(term.get("source", "")), spec)
            ): term
            for term in self.terms
        }
        self._term_names: list[tuple[str, tuple[str, ...], set[str]]] = []
        self._name_terms: dict[str, set[int]] = {}
        self._prefix_names: dict[str, set[str]] = {}
        self._single_names: set[str] = set()
        for index, term in enumerate(self.terms):
            main_name = normalize_term(str(term.get("source", "")), spec)
            aliases = tuple(
                normalize_term(str(value), spec)
                for value in term.get("aliases", [])
                if value
            )
            conflicted_aliases = {
                normalize_term(str(item.get("alias", "")), spec)
                for item in term.get("conflicts", {}).get("alias_primaries", [])
            }
            self._term_names.append((main_name, aliases, conflicted_aliases))
            for name in {main_name, *aliases}:
                if not name:
                    continue
                self._name_terms.setdefault(name, set()).add(index)
                if len(name) == 1:
                    self._single_names.add(name)
                else:
                    self._prefix_names.setdefault(name[:2], set()).add(name)

        claims: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for term in self.terms:
            for claim in term.get("conflicts", {}).get("group_claims", []):
                key = (
                    str(claim.get("entry", "")),
                    str(claim.get("claimed_by", "")),
                    str(claim.get("alias", "")),
                    str(claim.get("reason", "")),
                )
                claims[key] = dict(claim)
        self._claims = list(claims.values())
        self._claim_components = self._build_claim_components()
        for claim in self._claims:
            alias_name = normalize_term(str(claim.get("alias", "")), spec)
            if not alias_name:
                continue
            if len(alias_name) == 1:
                self._single_names.add(alias_name)
            else:
                self._prefix_names.setdefault(alias_name[:2], set()).add(alias_name)

    def _build_claim_components(
        self,
    ) -> list[tuple[set[str], list[dict[str, Any]]]]:
        components: list[tuple[set[str], list[dict[str, Any]]]] = []
        for claim in self._claims:
            endpoints = {
                str(claim.get("entry", "")),
                str(claim.get("claimed_by", "")),
            }
            matching: list[int] = []
            related_keys = set(endpoints)
            changed = True
            while changed:
                changed = False
                for index, value in enumerate(self._claims):
                    value_endpoints = {
                        str(value.get("entry", "")),
                        str(value.get("claimed_by", "")),
                    }
                    if not value_endpoints & related_keys or index in matching:
                        continue
                    matching.append(index)
                    before = len(related_keys)
                    related_keys.update(value_endpoints)
                    changed = changed or len(related_keys) != before
            related_claims = [self._claims[index] for index in matching]
            if any(existing[0] == related_keys for existing in components):
                continue
            components.append((related_keys, related_claims))
        return components

    def _candidate_names(self, normalized_source: str) -> set[str]:
        candidates = {
            name
            for index in range(len(normalized_source) - 1)
            for name in self._prefix_names.get(normalized_source[index : index + 2], ())
        }
        candidates.update(
            character
            for character in normalized_source
            if character in self._single_names
        )
        return {name for name in candidates if name in normalized_source}

    def _candidate_names_for_source(self, source: str) -> set[str]:
        candidate_names: set[str] = set()
        for view in aozora_match_views(source):
            normalized_view = normalize_term(view, self.spec)
            candidate_names.update(self._candidate_names(normalized_view))
        return candidate_names

    @staticmethod
    def _claim_sort_key(value: dict[str, Any]) -> tuple[str, str, str, str]:
        return (
            str(value.get("entry", "")),
            str(value.get("claimed_by", "")),
            str(value.get("alias", "")),
            str(value.get("reason", "")),
        )

    def _matched_aliases(
        self,
        term: dict[str, Any],
        candidate_names: set[str],
        *,
        excluded_names: set[str] | frozenset[str] = frozenset(),
    ) -> list[str]:
        matched = {
            str(alias)
            for alias in term.get("aliases", [])
            if (normalized := normalize_term(str(alias), self.spec))
            and normalized in candidate_names
            and normalized not in excluded_names
        }
        return sorted(
            matched,
            key=lambda value: (normalize_term(value, self.spec), value),
        )

    def match(self, source: str, limit: int) -> list[dict]:
        candidate_names = self._candidate_names_for_source(source)
        bundles: list[tuple[int, int, int, str, list[dict[str, Any]]]] = []
        disputed: set[str] = set()
        for related_keys, related_claims in self._claim_components:
            active_claims = [
                claim
                for claim in related_claims
                if normalize_term(str(claim.get("alias", "")), self.spec)
                in candidate_names
            ]
            if not active_claims:
                continue
            disputed.update(related_keys)
            related_terms = [
                self.by_normalized[key]
                for key in sorted(related_keys)
                if key in self.by_normalized
            ]
            payload = []
            for term in related_terms:
                term_normalized = str(
                    term.get("normalized")
                    or normalize_term(str(term.get("source", "")), self.spec)
                )
                disputed_names = {
                    normalize_term(str(value.get("alias", "")), self.spec)
                    for value in related_claims
                    if term_normalized
                    in {str(value.get("entry")), str(value.get("claimed_by"))}
                }
                safe_names = [
                    normalize_term(str(term.get("source", "")), self.spec),
                    *(
                        normalize_term(str(value), self.spec)
                        for value in term.get("aliases", [])
                    ),
                ]
                safe_hit = any(
                    name and name not in disputed_names and name in candidate_names
                    for name in safe_names
                )
                item = {
                    key: term.get(key)
                    for key in ("source", "category", "description")
                }
                item["aliases"] = self._matched_aliases(term, candidate_names)
                item["preferred_translation"] = (
                    term.get("preferred_translation") if safe_hit else None
                )
                item["group_claims"] = sorted(
                    related_claims, key=self._claim_sort_key
                )
                payload.append(item)
            bundle_key = "claim:" + ",".join(sorted(related_keys))
            if payload:
                alias_name = normalize_term(
                    str(active_claims[0].get("alias", "")), self.spec
                )
                bundles.append((1, len(alias_name), 0, bundle_key, payload))

        grouped: dict[
            str, list[tuple[bool, int, dict[str, Any], list[str]]]
        ] = {}
        candidate_term_indexes = {
            index
            for name in candidate_names
            for index in self._name_terms.get(name, ())
        }
        for index in sorted(candidate_term_indexes):
            term = self.terms[index]
            normalized = str(
                term.get("normalized")
                or normalize_term(str(term.get("source", "")), self.spec)
            )
            if normalized in disputed:
                continue
            main_name, aliases, conflicted_aliases = self._term_names[index]
            alias_names = [
                name for name in aliases if name not in conflicted_aliases
            ]
            matched_alias_names = {
                name for name in alias_names if name in candidate_names
            }
            matched_aliases = self._matched_aliases(
                term,
                candidate_names,
                excluded_names=conflicted_aliases,
            )
            main_hit = bool(main_name and main_name in candidate_names)
            hits = [
                name
                for name in ([main_name] if main_hit else matched_alias_names)
                if name and name in candidate_names
            ]
            if not hits:
                continue
            primary = str(term.get("group_primary") or normalized)
            grouped.setdefault(primary, []).append(
                (main_hit, max(len(name) for name in hits), term, matched_aliases)
            )

        for primary, hits in grouped.items():
            primary_term = self.by_normalized.get(primary)
            if primary_term is None:
                raise UsageError(f"术语组主不存在：{primary}")
            matched_terms = [value[2] for value in hits]
            matched_aliases_by_normalized = {
                str(
                    value[2].get("normalized")
                    or normalize_term(str(value[2].get("source", "")), self.spec)
                ): value[3]
                for value in hits
            }
            ordered = [primary_term]
            ordered.extend(
                sorted(
                    (
                        term
                        for term in matched_terms
                        if str(
                            term.get("normalized")
                            or normalize_term(str(term.get("source", "")), self.spec)
                        )
                        != primary
                    ),
                    key=lambda term: str(term.get("source", "")),
                )
            )
            payload = []
            for term in ordered:
                item = {
                    key: term.get(key)
                    for key in (
                        "source",
                        "category",
                        "description",
                        "preferred_translation",
                    )
                }
                term_normalized = str(
                    term.get("normalized")
                    or normalize_term(str(term.get("source", "")), self.spec)
                )
                item["aliases"] = matched_aliases_by_normalized.get(
                    term_normalized, []
                )
                if term is not primary_term:
                    item["primary_source"] = primary_term.get("source")
                payload.append(item)
            bundles.append(
                (
                    max(int(value[0]) for value in hits),
                    max(value[1] for value in hits),
                    max(
                        int(bool(value[2].get("preferred_translation")))
                        for value in hits
                    ),
                    str(primary_term.get("source", "")),
                    payload,
                )
            )

        bundles.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
        return [term for bundle in bundles[:limit] for term in bundle[4]]

    def validation_matches(
        self, source: str, payload_terms: list[dict[str, Any]]
    ) -> tuple[TranslationTermMatch, ...]:
        """Return only actual, non-disputed matches represented in the payload."""
        candidate_names = self._candidate_names_for_source(source)
        matches: list[TranslationTermMatch] = []
        seen: set[tuple[str, str, str]] = set()
        for term in payload_terms:
            preferred = term.get("preferred_translation")
            if not isinstance(preferred, str) or not preferred.strip():
                continue
            term_source = str(term.get("source", ""))
            normalized_source = normalize_term(term_source, self.spec)
            if normalized_source and normalized_source in candidate_names:
                key = (term_source, term_source, "source")
                if key not in seen:
                    seen.add(key)
                    matches.append(
                        TranslationTermMatch(
                            source=term_source,
                            matched_text=term_source,
                            match_type="source",
                            preferred_translation=preferred,
                        )
                    )
                continue
            aliases = [
                str(alias)
                for alias in term.get("aliases", [])
                if normalize_term(str(alias), self.spec) in candidate_names
            ]
            for alias in sorted(
                aliases,
                key=lambda value: (normalize_term(value, self.spec), value),
            ):
                key = (term_source, alias, "alias")
                if key in seen:
                    continue
                seen.add(key)
                matches.append(
                    TranslationTermMatch(
                        source=term_source,
                        matched_text=alias,
                        match_type="alias",
                        preferred_translation=preferred,
                    )
                )
        return tuple(matches)

def match_term_validation(
    source: str,
    library: dict[str, Any] | None,
    limit: int,
    spec: TermNormalization,
) -> tuple[TranslationTermMatch, ...]:
    if library is None:
        return ()
    matcher = _PreparedTermMatcher(library, spec)
    payload_terms = matcher.match(source, limit)
    return matcher.validation_matches(source, payload_terms)

class _TermMatchCache:
    def __init__(
        self,
        library: dict[str, Any] | None,
        spec: TermNormalization,
        limit: int,
    ) -> None:
        self.matcher = _PreparedTermMatcher(library, spec) if library else None
        self.spec = spec
        self.limit = limit
        self._cache: dict[tuple[str, str], tuple[dict, ...]] = {}

    def _matches_for_item(self, item: dict[str, Any]) -> tuple[dict, ...]:
        key = (str(item["segment_id"]), str(item["source"]))
        matches = self._cache.get(key)
        if matches is None:
            matches = tuple(
                self.matcher.match(str(item["source"]), self.limit)
                if self.matcher is not None
                else []
            )
            self._cache[key] = matches
        return matches

    def validation_matches_for_item(
        self, item: dict[str, Any]
    ) -> tuple[TranslationTermMatch, ...]:
        if self.matcher is None:
            return ()
        return self.matcher.validation_matches(
            str(item["source"]), list(self._matches_for_item(item))
        )

    def for_items(self, items: list[dict[str, Any]]) -> list[dict]:
        by_source: dict[str, dict] = {}
        for item in items:
            matches = self._matches_for_item(item)
            for term in matches:
                source = str(term["source"])
                current = by_source.get(source)
                if current is None:
                    current = dict(term)
                    aliases = {
                        str(alias)
                        for alias in term.get("aliases", [])
                        if alias
                    }
                    current["aliases"] = sorted(
                        aliases,
                        key=lambda value: (normalize_term(value, self.spec), value),
                    )
                    by_source[source] = current
                    continue
                aliases = {
                    str(alias)
                    for alias in current.get("aliases", [])
                    if alias
                }
                aliases.update(
                    str(alias)
                    for alias in term.get("aliases", [])
                    if alias
                )
                current["aliases"] = sorted(
                    aliases,
                    key=lambda value: (normalize_term(value, self.spec), value),
                )
        return list(by_source.values())

def match_terms(
    source: str,
    library: dict[str, Any] | None,
    limit: int,
    spec: TermNormalization,
) -> list[dict]:
    if library is None:
        return []
    return _PreparedTermMatcher(library, spec).match(source, limit)
