import asyncio
import threading

from app.async_utils import run_blocking


def test_cancel_drains_worker_before_releasing_owner_lock():
    async def scenario():
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        owner = asyncio.Lock()

        def write():
            entered.set()
            assert release.wait(5)
            finished.set()

        async def operation():
            async with owner:
                await run_blocking(write)

        task = asyncio.create_task(operation())
        await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert owner.locked()
        assert not task.done()
        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert finished.is_set()
        assert not owner.locked()

    asyncio.run(scenario())
