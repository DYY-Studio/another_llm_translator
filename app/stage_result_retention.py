"""Retain current segment outcomes and their exact result dependencies."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Iterable

from .errors import StorageError

STAGES = frozenset({"translation", "proofreading", "proofreading_applied", "polishing", "polishing_applied"})
_COLUMNS = "sequence,record_id,stage,segment_id,status,json_extract(payload_json,'$.base_result_id') AS base,json_extract(payload_json,'$.suggestion_result_id') AS suggestion"


def _batches(values: Iterable[str], size: int = 400):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _parents(row: sqlite3.Row) -> tuple[str, ...]:
    values = (row["base"], row["suggestion"])
    if any(value is not None and (not isinstance(value, str) or not value) for value in values):
        raise StorageError(f"阶段结果引用无效：{row['record_id']}")
    return tuple(value for value in values if value is not None)


def _roots(connection: sqlite3.Connection, rows: dict[str, sqlite3.Row]) -> tuple[set[str], dict[str, set[str]]]:
    groups = defaultdict(list)
    for row in rows.values():
        groups[(row["stage"], row["segment_id"])].append(row)
    active = set()
    for batch in _batches({key[1] for key in groups if key[1] is not None}):
        active.update(row[0] for row in connection.execute(
            f"SELECT segment_id FROM segments WHERE segment_id IN ({','.join('?' for _ in batch)})", batch))
    roots = set()
    barriers = {}
    for (_, sid), group in groups.items():
        if sid not in active:
            continue
        latest = max(group, key=lambda row: row["sequence"])
        roots.add(latest["record_id"])
        reset = max((row for row in group if row["status"] == "reset"), key=lambda row: row["sequence"], default=None)
        completed = max((row for row in group if row["status"] == "completed"), key=lambda row: row["sequence"], default=None)
        if completed is not None and (reset is None or completed["sequence"] > reset["sequence"]):
            roots.add(completed["record_id"])
        elif reset is not None and completed is not None:
            barriers[reset["record_id"]] = {row["record_id"] for row in group
                                              if row["status"] == "completed"}
    return roots, barriers


def _closure(connection: sqlite3.Connection, rows: dict[str, sqlite3.Row], roots: set[str],
             barriers: dict[str, set[str]]) -> set[str]:
    retained = set()
    pending = roots
    while pending:
        missing = pending - rows.keys()
        for batch in _batches(missing):
            rows.update((row["record_id"], row) for row in connection.execute(
                f"SELECT {_COLUMNS} FROM stage_results WHERE record_id IN ({','.join('?' for _ in batch)})", batch))
        absent = missing - rows.keys()
        if absent:
            raise StorageError(f"必要阶段结果引用不存在：{next(iter(absent))}")
        retained.update(pending)
        pending = ({parent for key in pending for parent in _parents(rows[key])}
                   | {key for key, completed in barriers.items() if completed & retained}) - retained
    return retained


def obsolete_stage_results(connection: sqlite3.Connection) -> set[str]:
    """Compute the full closure only for explicit project maintenance."""
    rows = {row["record_id"]: row for row in connection.execute(
        f"SELECT {_COLUMNS} FROM stage_results WHERE stage IN ({','.join('?' for _ in STAGES)})", tuple(STAGES))}
    candidates = set(rows)
    roots, barriers = _roots(connection, rows)
    retained = _closure(connection, rows, roots, barriers)
    return candidates - retained


def _delete(connection: sqlite3.Connection, ids: set[str]) -> None:
    connection.executemany("DELETE FROM stage_results WHERE record_id = ?", ((key,) for key in ids))


def prune_stage_results(connection: sqlite3.Connection, pairs: Iterable[tuple[str, str | None]]) -> int:
    """Prune affected groups, then revisit parents that lost their last child."""
    pairs = {(stage, sid) for stage, sid in pairs if stage in STAGES}
    deleted = 0
    while pairs:
        rows = {}
        by_stage = defaultdict(set)
        for stage, sid in pairs:
            by_stage[stage].add(sid)
        for stage, ids in by_stage.items():
            for batch in _batches(ids):
                rows.update((row["record_id"], row) for row in connection.execute(
                    f"SELECT {_COLUMNS} FROM stage_results WHERE stage = ? AND segment_id IN ({','.join('?' for _ in batch)})", [stage, *batch]))
        candidates = set(rows)
        roots, barriers = _roots(connection, rows)
        retained = _closure(connection, rows, roots, barriers)
        # A child outside these groups protects its exact parent, regardless of age.
        for batch in _batches(candidates - retained):
            placeholders = ','.join('?' for _ in batch)
            for row in connection.execute(
                f"SELECT {_COLUMNS} FROM stage_results WHERE json_extract(payload_json,'$.base_result_id') IN ({placeholders}) OR json_extract(payload_json,'$.suggestion_result_id') IN ({placeholders})", batch + batch):
                if row["record_id"] not in candidates:
                    roots.update(parent for parent in _parents(row) if parent in candidates)
        if roots - retained:
            retained = _closure(connection, rows, roots, barriers)
        obsolete = candidates - retained
        if not obsolete:
            break
        parents = {parent for key in obsolete for parent in _parents(rows[key])} - obsolete
        _delete(connection, obsolete)
        deleted += len(obsolete)
        pairs = set()
        for batch in _batches(parents):
            pairs.update((row[0], row[1]) for row in connection.execute(
                f"SELECT stage,segment_id FROM stage_results WHERE record_id IN ({','.join('?' for _ in batch)})", batch))
    return deleted


def maintain_stage_results(connection: sqlite3.Connection) -> int:
    obsolete = obsolete_stage_results(connection)
    _delete(connection, obsolete)
    return len(obsolete)
