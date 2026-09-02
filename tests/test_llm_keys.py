from __future__ import annotations

import asyncio

import pytest

from app.llm_keys import KeyPool


@pytest.mark.asyncio
async def test_key_pool_allows_each_key_to_use_its_own_limits() -> None:
    pool = KeyPool(
        requests_per_minute=1,
        input_tokens_per_minute=10,
        max_parallel=2,
        max_parallel_per_key=1,
    )
    first = await pool.acquire(("key-1", "key-2"), estimated_tokens=10)
    second = await pool.acquire(("key-1", "key-2"), estimated_tokens=10)
    assert {first.key_index, second.key_index} == {0, 1}
    await first.release()
    await second.release()


@pytest.mark.asyncio
async def test_key_pool_enforces_preset_concurrency_across_keys() -> None:
    pool = KeyPool(
        requests_per_minute=0,
        input_tokens_per_minute=0,
        max_parallel=1,
        max_parallel_per_key=1,
    )
    first = await pool.acquire(("key-1", "key-2"), estimated_tokens=10)
    waiting = asyncio.create_task(
        pool.acquire(("key-1", "key-2"), estimated_tokens=10)
    )
    await asyncio.sleep(0)
    assert not waiting.done()
    await first.release()
    second = await asyncio.wait_for(waiting, timeout=1)
    await second.release()


@pytest.mark.asyncio
async def test_key_pool_rotates_equal_candidates() -> None:
    pool = KeyPool(
        requests_per_minute=0,
        input_tokens_per_minute=0,
        max_parallel=1,
        max_parallel_per_key=1,
    )
    indexes: list[int] = []
    for _ in range(3):
        lease = await pool.acquire(("key-1", "key-2"), estimated_tokens=1)
        indexes.append(lease.key_index)
        await lease.release()
    assert indexes == [0, 1, 0]
