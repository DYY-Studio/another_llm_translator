from __future__ import annotations
from typing import Any
from .documents import (
    aozora_match_views,
)
from .errors import (
    UsageError,
)
from .translation_validation import (
    TranslationTermMatch,
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
                            category=term.get("category"),
                            description=term.get("description"),
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
                        category=term.get("category"),
                        description=term.get("description"),
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
        *, segments: list[dict[str, Any]], warnings: list[str] | None = None,
        sourced_policy: str = "all", unsourced_policy: str = "all", project=None,
    ) -> None:
        self.matcher = _PreparedTermMatcher(library, spec) if library else None
        self.project = project
        self.library = library
        self._resolved_entities: set[str] = set()
        self.spec = spec
        self.limit = limit
        self.sourced_policy = sourced_policy
        self.unsourced_policy = unsourced_policy
        self.positions = {str(s["segment_id"]): i for i, s in enumerate(segments)}
        self.warnings = warnings if warnings is not None else []
        self._cache: dict[tuple[str, str], tuple[dict, ...]] = {}
        self._entity_sources: dict[str, list[dict[str, Any]]] = {}

    def _matches_for_item(self, item: dict[str, Any]) -> tuple[dict, ...]:
        key = (str(item["segment_id"]), str(item["source"]))
        matches = self._cache.get(key)
        if matches is None:
            if self.matcher is not None:
                names = self.matcher._candidate_names_for_source(str(item["source"]))
                for index in {i for name in names for i in self.matcher._name_terms.get(name, ())}:
                    term = self.matcher.terms[index]
                    if self.project is not None and "knowledge_status" not in term:
                        from .term_knowledge import entity_view
                        view = entity_view(self.project, self.library, str(term["normalized"]), positions=self.positions)
                        root = view["entity_key"]
                        if root not in self._resolved_entities:
                            if view["status"] != "stale":
                                origin = {"description_items": view["items"]}
                                self._entity_sources[root] = self._prepare_entity(origin)
                            for member in view["members"]:
                                target = self.matcher.by_normalized.get(member)
                                if target is not None:
                                    target.update(entity_key=root, knowledge_status=view["status"],
                                        description="；".join(item["text"] for item in view["items"]))
                            self._resolved_entities.add(root)
                    if term.get("knowledge_status") == "stale":
                        raise UsageError(f"实体说明已过期，请更新后再执行：{term['source']}")
                    if term.get("knowledge_status") == "pending":
                        warning = f"实体说明待聚合，当前只使用已应用说明：{term['source']}"
                        if warning not in self.warnings:
                            self.warnings.append(warning)
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

    def _prepare_entity(self, origin: dict[str, Any]) -> list[dict[str, Any]]:
        from .term_knowledge import compact_source_ranges
        result = []
        for entry in origin.get("description_items", []):
            unique = {(r["segment_id"], r["start"], r["end"]): r
                      for refs in entry["sources"] if refs is not None for r in refs}
            ranges = sorted(unique.values(), key=lambda r: (self.positions[r["segment_id"]], r["start"], r["end"]))
            intervals = compact_source_ranges(ranges, self.positions) or []
            # Intervals can overlap when an item cites multiple scan requests.
            merged: list[tuple[tuple[int, int], tuple[int, int]]] = []
            for interval in intervals:
                start = (interval["first"]["position"], interval["first"]["start"])
                end = (interval["last"]["position"], interval["last"]["end"])
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], end))
                else:
                    merged.append((start, end))
            result.append({"text": entry["text"], "unsourced": any(refs is None for refs in entry["sources"]),
                           "starts": tuple(start for start, _ in merged), "ends": tuple(end for _, end in merged)})
        return result

    def _relative_items(self, entity: str, origin: dict[str, Any], first: tuple[int, int], last: tuple[int, int]) -> list[dict[str, Any]]:
        from bisect import bisect_right
        prepared = self._entity_sources.get(entity)
        if prepared is None:
            prepared = self._prepare_entity(origin)
            self._entity_sources[entity] = prepared
        relations = []
        for entry in prepared:
            starts, ends = entry["starts"], entry["ends"]
            before = starts[0] if starts and starts[0] < first else None
            index = bisect_right(ends, first)
            within = index < len(starts) and starts[index] < last
            index = bisect_right(ends, last)
            after = max(starts[index], last) if index < len(starts) else None
            relations.append((before, within, after))
        before_ranks = {position: rank for rank, position in enumerate(sorted({before for before, _, _ in relations if before is not None}), 1)}
        after_ranks = {position: rank for rank, position in enumerate(sorted({after for _, _, after in relations if after is not None}), 1)}
        items = [{"text": entry["text"], "unsourced": entry["unsourced"],
                 "source_relation": [*([f"before {before_ranks[before]}"] if before is not None else []),
                                     *(["within"] if within else []),
                                     *([f"after {after_ranks[after]}"] if after is not None else [])]}
                for entry, (before, within, after) in zip(prepared, relations, strict=True)]
        sourced = [i for i, (before, within, after) in enumerate(relations)
                   if before is not None or within or after is not None]
        if self.sourced_policy == "none":
            sourced = []
        elif self.sourced_policy == "prefer_previous":
            previous = [i for i in sourced if relations[i][0] is not None or relations[i][1]]
            sourced = previous or [i for i in sourced if "after 1" in items[i]["source_relation"]]
        selected = set(sourced)
        if self.unsourced_policy == "all" or (self.unsourced_policy == "when_no_sourced" and not sourced):
            selected.update(i for i, item in enumerate(items) if not item["source_relation"])
        return [item for i, item in enumerate(items) if i in selected]

    def for_items(self, items: list[dict[str, Any]]) -> list[dict]:
        by_source: dict[str, dict] = {}
        entities: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for item in items:
            for term in self._matches_for_item(item):
                source = str(term["source"])
                current = by_source.get(source)
                if current is None:
                    current = dict(term)
                    current.pop("description", None)
                    current["aliases"] = set()
                    by_source[source] = current
                    if self.matcher is not None and "primary_source" not in term:
                        normalized = normalize_term(source, self.spec)
                        origin = self.matcher.by_normalized[normalized]
                        entity = str(origin.get("entity_key") or origin.get("group_primary") or normalized)
                        entities.setdefault(entity, (current, origin))
                current["aliases"].update(str(alias) for alias in term.get("aliases", []) if alias)
        if entities and (self.sourced_policy != "none" or self.unsourced_policy != "none"):
            bounds = []
            for item in items:
                original_id = str(item.get("_original_segment_id") or item["segment_id"])
                if original_id not in self.positions:
                    raise UsageError("请求缺少可追溯的当前源文位置")
                start = int(item.get("_source_offset", 0))
                bounds.append(((self.positions[original_id], start), (self.positions[original_id], start + len(str(item["source"])))))
            first, last = min(start for start, _ in bounds), max(end for _, end in bounds)
            for entity, (current, origin) in entities.items():
                descriptions = self._relative_items(entity, origin, first, last)
                if descriptions:
                    current["entity_knowledge"] = {"entity": entity, "items": descriptions}
        for current in by_source.values():
            current["aliases"] = sorted(current["aliases"], key=lambda value: (normalize_term(value, self.spec), value))
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
