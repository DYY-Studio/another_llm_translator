from __future__ import annotations

from bisect import bisect_left
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SegmentAlignment:
    """The safe, positional matches between two Segment sequences."""

    preserved_new_to_old: dict[int, int]
    ambiguous_old_indices: tuple[int, ...]
    ambiguous_new_indices: tuple[int, ...]


def _effective_model_source(segment: dict[str, Any]) -> str:
    value = segment.get("model_source")
    return value if isinstance(value, str) else str(segment["source"])


def _key(segment: dict[str, Any]) -> tuple[str, str]:
    return str(segment["source"]), _effective_model_source(segment)


def _longest_increasing_subsequence(
    candidates: Sequence[tuple[int, int]],
) -> list[tuple[int, int]]:
    if not candidates:
        return []
    tails: list[int] = []
    tail_indices: list[int] = []
    previous = [-1] * len(candidates)
    for index, (_, new_index) in enumerate(candidates):
        position = bisect_left(tails, new_index)
        if position:
            previous[index] = tail_indices[position - 1]
        if position == len(tails):
            tails.append(new_index)
            tail_indices.append(index)
        else:
            tails[position] = new_index
            tail_indices[position] = index
    result: list[tuple[int, int]] = []
    index = tail_indices[-1]
    while index >= 0:
        result.append(candidates[index])
        index = previous[index]
    result.reverse()
    return result


def _patience_alignment(
    old: Sequence[tuple[int, tuple[str, str]]],
    new: Sequence[tuple[int, tuple[str, str]]],
    *,
    all_old_counts: Counter[tuple[str, str]] | None = None,
    all_new_counts: Counter[tuple[str, str]] | None = None,
) -> tuple[dict[int, int], set[tuple[str, str]]]:
    all_old_counts = all_old_counts or Counter(key for _, key in old)
    all_new_counts = all_new_counts or Counter(key for _, key in new)
    if not old or not new:
        return {}, set()
    if [item[1] for item in old] == [item[1] for item in new]:
        return (
            {
                new_index: old_index
                for (old_index, _), (new_index, _) in zip(old, new, strict=True)
            },
            set(),
        )

    old_counts = Counter(key for _, key in old)
    new_counts = Counter(key for _, key in new)
    new_unique_positions = {
        key: new_index
        for new_index, key in new
        if new_counts[key] == 1
    }
    candidates = [
        (old_index, new_unique_positions[old_key])
        for old_index, old_key in old
        if old_counts[old_key] == 1 and old_key in new_unique_positions
    ]
    anchors = _longest_increasing_subsequence(candidates)
    if not anchors:
        # A changed region can still contain an exact repeated run.  It is
        # safe to reuse that run only when the key's occurrence count is the
        # same on both sides of this recursive gap; otherwise the occurrences
        # may have shifted and must remain ambiguous.
        old_counts = Counter(key for _, key in old)
        new_counts = Counter(key for _, key in new)
        prefix: list[tuple[int, int]] = []
        old_prefix = 0
        new_prefix = 0
        while old_prefix < len(old) and new_prefix < len(new):
            old_key = old[old_prefix][1]
            if old_key != new[new_prefix][1]:
                break
            if old_counts[old_key] != new_counts[old_key]:
                break
            prefix.append((old[old_prefix][0], new[new_prefix][0]))
            old_prefix += 1
            new_prefix += 1

        old_suffix = len(old)
        new_suffix = len(new)
        suffix: list[tuple[int, int]] = []
        while old_suffix > old_prefix and new_suffix > new_prefix:
            old_key = old[old_suffix - 1][1]
            if old_key != new[new_suffix - 1][1]:
                break
            if old_counts[old_key] != new_counts[old_key]:
                break
            suffix.append((old[old_suffix - 1][0], new[new_suffix - 1][0]))
            old_suffix -= 1
            new_suffix -= 1

        if prefix or suffix:
            middle_matches, middle_ambiguous = _patience_alignment(
                old[old_prefix:old_suffix],
                new[new_prefix:new_suffix],
                all_old_counts=all_old_counts,
                all_new_counts=all_new_counts,
            )
            matches = {new_index: old_index for old_index, new_index in prefix}
            matches.update(middle_matches)
            matches.update(
                {new_index: old_index for old_index, new_index in reversed(suffix)}
            )
            return matches, middle_ambiguous
        ambiguous = {
            key
            for key in old_counts.keys() | new_counts.keys()
            if key in all_old_counts
            and key in all_new_counts
            and max(all_old_counts[key], all_new_counts[key]) > 1
        }
        return {}, ambiguous

    matches: dict[int, int] = {}
    ambiguous: set[tuple[str, str]] = set()
    old_positions = {index: position for position, (index, _) in enumerate(old)}
    new_positions = {index: position for position, (index, _) in enumerate(new)}
    old_cursor = 0
    new_cursor = 0
    for old_anchor, new_anchor in anchors:
        old_anchor_position = old_positions[old_anchor]
        new_anchor_position = new_positions[new_anchor]
        old_gap = old[old_cursor:old_anchor_position]
        new_gap = new[new_cursor:new_anchor_position]
        gap_matches, gap_ambiguous = _patience_alignment(
            old_gap,
            new_gap,
            all_old_counts=all_old_counts,
            all_new_counts=all_new_counts,
        )
        matches.update(gap_matches)
        ambiguous.update(gap_ambiguous)
        matches[new_anchor] = old_anchor
        old_cursor = old_anchor_position + 1
        new_cursor = new_anchor_position + 1
    tail_matches, tail_ambiguous = _patience_alignment(
        old[old_cursor:],
        new[new_cursor:],
        all_old_counts=all_old_counts,
        all_new_counts=all_new_counts,
    )
    matches.update(tail_matches)
    ambiguous.update(tail_ambiguous)
    return matches, ambiguous


def align_segments(
    old_segments: Sequence[dict[str, Any]],
    new_segments: Sequence[dict[str, Any]],
) -> SegmentAlignment:
    """Align exact Segment content independently inside each Part.

    Exact continuous runs are reused, including repeated keys when their
    occurrence count is unchanged in the recursive gap.  Remaining changes
    are aligned by unique patience anchors, so an uncertain duplicate is
    never silently assigned another occurrence's ID.
    Returned indexes refer to the supplied global sequences.
    """

    old_by_part: dict[str, list[tuple[int, tuple[str, str]]]] = defaultdict(list)
    new_by_part: dict[str, list[tuple[int, tuple[str, str]]]] = defaultdict(list)
    for index, segment in enumerate(old_segments):
        old_by_part[str(segment["part_id"])].append((index, _key(segment)))
    for index, segment in enumerate(new_segments):
        new_by_part[str(segment["part_id"])].append((index, _key(segment)))

    preserved: dict[int, int] = {}
    ambiguous_old: set[int] = set()
    ambiguous_new: set[int] = set()
    for part_id in old_by_part.keys() & new_by_part.keys():
        old_items = old_by_part[part_id]
        new_items = new_by_part[part_id]
        matches, ambiguous_keys = _patience_alignment(
            old_items,
            new_items,
            all_old_counts=Counter(key for _, key in old_items),
            all_new_counts=Counter(key for _, key in new_items),
        )
        ambiguous_old.update(
            index for index, key in old_items if key in ambiguous_keys
        )
        ambiguous_new.update(
            index for index, key in new_items if key in ambiguous_keys
        )
        preserved.update({
            new_index: old_index
            for new_index, old_index in matches.items()
            if new_index not in ambiguous_new and old_index not in ambiguous_old
        })

    return SegmentAlignment(
        preserved_new_to_old=preserved,
        ambiguous_old_indices=tuple(sorted(ambiguous_old)),
        ambiguous_new_indices=tuple(sorted(ambiguous_new)),
    )
