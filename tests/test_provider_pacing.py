import asyncio

import pytest

from app.capacity import current_owner
from app.provider_pacing import ProviderPacer

pytestmark = pytest.mark.unit


def test_combined_background_and_ui_starts_are_fifo_and_spaced():
    async def scenario():
        pacer = ProviderPacer(.02)
        starts = []
        first = await pacer.wait_ready()
        assert pacer.try_start(first)
        starts.append(("first", asyncio.get_running_loop().time()))
        background_ready = asyncio.Event()
        prepare_done = asyncio.Event()

        async def background():
            ticket = await pacer.wait_ready()
            # Preparation waits outside the background owner and without a
            # snapshot. Its FIFO reservation remains ahead of the UI caller.
            assert current_owner() is None
            background_ready.set()
            await prepare_done.wait()
            assert pacer.try_start(ticket)
            starts.append(("background", asyncio.get_running_loop().time()))

        async def ui():
            async with pacer.request():
                starts.append(("ui", asyncio.get_running_loop().time()))

        bg = asyncio.create_task(background())
        await asyncio.sleep(0)
        ui_task = asyncio.create_task(ui())
        await background_ready.wait()
        await asyncio.sleep(.025)
        assert [name for name, _ in starts] == ["first"]
        prepare_done.set()
        await asyncio.gather(bg, ui_task)
        assert [name for name, _ in starts] == ["first", "background", "ui"]
        assert all(b - a >= .019 for (_, a), (_, b) in zip(starts, starts[1:]))
        assert not pacer._tickets
    asyncio.run(scenario())


def test_cancellation_after_http_start_keeps_consumed_interval():
    async def scenario():
        pacer = ProviderPacer(.03)
        started = asyncio.Event()
        async def request():
            async with pacer.request():
                started.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(request())
        await started.wait()
        deadline = pacer.next_start
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with pacer.request():
            assert asyncio.get_running_loop().time() >= deadline
    asyncio.run(scenario())


def test_cancelled_waiter_does_not_block_next_fifo_ticket():
    async def scenario():
        pacer = ProviderPacer(0)
        held = await pacer.wait_ready()
        waiting = asyncio.create_task(pacer.wait_ready())
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        held.close()
        async with pacer.request():
            assert not pacer._tickets
    asyncio.run(scenario())
