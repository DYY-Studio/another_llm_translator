"""Blocking work without releasing its owner's locks while a worker is running."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial
from typing import Any, TypeVar

T = TypeVar("T")


async def _run_blocking(function: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    task = asyncio.create_task(asyncio.to_thread(partial(function, *args, **kwargs)))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Cancelling an await does not stop a thread. Drain it before the caller
        # releases a project/write lock or publishes the task's terminal state.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


# Planning and response commits belonging to one stage share mutable per-Run
# indexes. Serialize their worker jobs; independent projects remain concurrent.
from contextvars import ContextVar
from functools import wraps

_worker_lock: ContextVar[asyncio.Lock | None] = ContextVar("stage_worker_lock", default=None)


def blocking_scope(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        if _worker_lock.get() is not None:
            return await function(*args, **kwargs)
        token = _worker_lock.set(asyncio.Lock())
        try:
            return await function(*args, **kwargs)
        finally:
            _worker_lock.reset(token)
    return wrapped


async def run_blocking(function: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    lock = _worker_lock.get()
    if lock is None:
        return await _run_blocking(function, *args, **kwargs)
    async with lock:
        return await _run_blocking(function, *args, **kwargs)
