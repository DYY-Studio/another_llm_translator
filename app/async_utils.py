"""Blocking work without releasing its owner's locks while a worker is running."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial
from typing import Any, TypeVar

T = TypeVar("T")


async def run_blocking(function: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
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
