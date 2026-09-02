from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field

from .errors import ConfigError


class NoAvailableKey(RuntimeError):
    """All keys supplied to a lease request were excluded."""


@dataclass
class _KeyState:
    records: deque[tuple[float, int]] = field(default_factory=deque)
    last_admitted_at: float | None = None
    active: int = 0
    cooldown_until: float = 0.0
    idle_since: float | None = None


@dataclass
class KeyLease:
    _pool: KeyPool
    key_id: str
    key_index: int
    _released: bool = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._pool.release(self)


class KeyPool:
    """Atomically schedule independent key windows under two concurrency caps."""

    def __init__(
        self,
        requests_per_minute: int,
        input_tokens_per_minute: int,
        max_parallel: int,
        max_parallel_per_key: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retention_seconds: float = 60.0,
    ) -> None:
        self.requests_per_minute = int(requests_per_minute)
        self.input_tokens_per_minute = int(input_tokens_per_minute)
        self.max_parallel = int(max_parallel)
        self.max_parallel_per_key = int(max_parallel_per_key)
        if self.requests_per_minute < 0 or self.input_tokens_per_minute < 0:
            raise ConfigError("Key 限流配置不能是负数")
        if self.max_parallel <= 0 or self.max_parallel_per_key <= 0:
            raise ConfigError("Key 并发配置必须是正整数")
        self.clock = clock
        self.sleeper = sleeper
        self.retention_seconds = retention_seconds
        self.states: dict[str, _KeyState] = {}
        self.lock = asyncio.Lock()
        self.active = 0
        self.last_key_id: str | None = None

    @staticmethod
    def _validate_key_ids(key_ids: Iterable[str]) -> tuple[str, ...]:
        values = tuple(key_ids)
        if not values:
            raise ConfigError("至少需要一个 API Key")
        if any(not isinstance(value, str) or not value for value in values):
            raise ConfigError("API Key 身份不能为空")
        if len(values) != len(set(values)):
            raise ConfigError("API Key 身份不能重复")
        return values

    def _prune_records(self, state: _KeyState, now: float) -> None:
        while state.records and now - state.records[0][0] >= 60:
            state.records.popleft()

    def _wait_for_rate(
        self,
        state: _KeyState,
        estimated_tokens: int,
        now: float,
    ) -> float:
        self._prune_records(state, now)
        wait = max(0.0, state.cooldown_until - now)
        if (
            self.requests_per_minute > 0
            and state.last_admitted_at is not None
        ):
            wait = max(
                wait,
                state.last_admitted_at + 60 / self.requests_per_minute - now,
            )
        request_full = (
            self.requests_per_minute > 0
            and len(state.records) >= self.requests_per_minute
        )
        token_full = self.input_tokens_per_minute > 0 and (
            sum(tokens for _, tokens in state.records) + estimated_tokens
            > self.input_tokens_per_minute
        )
        if request_full or token_full:
            wait = max(wait, 60 - (now - state.records[0][0]))
        return max(0.0, wait)

    def _rotation_rank(self, key_ids: tuple[str, ...], index: int) -> int:
        if self.last_key_id not in key_ids:
            return index
        start = (key_ids.index(self.last_key_id) + 1) % len(key_ids)
        return (index - start) % len(key_ids)

    async def acquire(
        self,
        key_ids: Iterable[str],
        *,
        estimated_tokens: int,
        excluded: frozenset[int] = frozenset(),
        on_wait_start: Callable[[], None] | None = None,
        on_wait_end: Callable[[], None] | None = None,
    ) -> KeyLease:
        values = self._validate_key_ids(key_ids)
        if (
            self.input_tokens_per_minute > 0
            and estimated_tokens > self.input_tokens_per_minute
        ):
            raise ConfigError("单请求预测 Token 超过 ITPM")
        if all(index in excluded for index in range(len(values))):
            raise NoAvailableKey
        waited = False
        try:
            while True:
                async with self.lock:
                    now = self.clock()
                    for key_id in values:
                        self.states.setdefault(key_id, _KeyState())
                    candidates: list[tuple[float, int, str]] = []
                    for index, key_id in enumerate(values):
                        if index in excluded:
                            continue
                        state = self.states[key_id]
                        if state.active >= self.max_parallel_per_key:
                            continue
                        delay = self._wait_for_rate(state, estimated_tokens, now)
                        candidates.append((delay, self._rotation_rank(values, index), key_id))
                    if self.active < self.max_parallel and candidates:
                        delay, _, key_id = min(candidates)
                        if delay <= 0:
                            index = values.index(key_id)
                            state = self.states[key_id]
                            state.records.append((now, estimated_tokens))
                            state.last_admitted_at = now
                            state.active += 1
                            state.idle_since = None
                            self.active += 1
                            self.last_key_id = key_id
                            return KeyLease(self, key_id, index)
                    if not candidates or self.active >= self.max_parallel:
                        delay = 0.01
                    else:
                        delay = min(item[0] for item in candidates)
                if not waited:
                    waited = True
                    if on_wait_start is not None:
                        on_wait_start()
                delay = max(0.01, delay)
                await self.sleeper(delay)
        finally:
            if waited and on_wait_end is not None:
                on_wait_end()

    async def release(self, lease: KeyLease) -> None:
        async with self.lock:
            state = self.states.get(lease.key_id)
            if state is None:
                return
            state.active = max(0, state.active - 1)
            self.active = max(0, self.active - 1)
            if state.active == 0:
                state.idle_since = self.clock()
            self._prune_idle(self.clock())

    async def cool_down(self, key_id: str, delay: float) -> None:
        if delay < 0:
            raise ConfigError("Key 冷却时间不能是负数")
        async with self.lock:
            state = self.states.setdefault(key_id, _KeyState())
            state.cooldown_until = max(state.cooldown_until, self.clock() + delay)

    def _prune_idle(self, now: float) -> None:
        expired = [
            key_id
            for key_id, state in self.states.items()
            if (
                state.active == 0
                and not state.records
                and state.idle_since is not None
                and now - state.idle_since >= self.retention_seconds
                and state.cooldown_until <= now
            )
        ]
        for key_id in expired:
            self.states.pop(key_id, None)

    def has_unexpired_cooldown(self, now: float | None = None) -> bool:
        """Return whether a key cooldown still needs this pool retained."""
        current = self.clock() if now is None else now
        return any(state.cooldown_until > current for state in self.states.values())
