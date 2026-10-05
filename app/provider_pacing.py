"""Lifespan-owned FIFO pacing without a task per waiting request."""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager


class PacingTicket:
    def __init__(self, pacer):
        self.pacer = pacer
        self.closed = False

    def close(self):
        if not self.closed:
            self.closed = True
            self.pacer._tickets.remove(self)
            self.pacer._changed.set()


class ProviderPacer:
    """Existing serving admission bounds the number of callers/tickets.

    A ready ticket retains its FIFO position through preparation, but consumes
    its interval only immediately before HTTP starts. No owner is needed to
    wait for readiness; callers under background ownership must use try_start.
    """
    def __init__(self, interval_s):
        self.interval_s = max(0.0, interval_s)
        self.next_start = 0.0
        self._tickets = deque()
        self._changed = asyncio.Event()

    async def wait_ready(self, ticket=None):
        if ticket is None or ticket.closed:
            ticket = PacingTicket(self)
            self._tickets.append(ticket)
            self._changed.set()
        try:
            while True:
                remaining = self.next_start - asyncio.get_running_loop().time()
                if self._tickets[0] is ticket and remaining <= 0:
                    return ticket
                self._changed.clear()
                if self._tickets[0] is ticket:
                    try:
                        async with asyncio.timeout(remaining):
                            await self._changed.wait()
                    except TimeoutError:
                        pass
                else:
                    await self._changed.wait()
        except BaseException:
            ticket.close()
            raise

    def try_start(self, ticket):
        now = asyncio.get_running_loop().time()
        if ticket.closed or self._tickets[0] is not ticket or now < self.next_start:
            return False
        self.next_start = now + self.interval_s
        ticket.close()
        return True

    @asynccontextmanager
    async def request(self):
        ticket = await self.wait_ready()
        try:
            if not self.try_start(ticket):
                raise RuntimeError("ready provider ticket lost its FIFO position")
            yield
        finally:
            ticket.close()
