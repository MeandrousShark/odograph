"""Disposable loopback SMTP fixture exercising the production transport."""
from __future__ import annotations

import asyncio
from email import policy
from email.parser import BytesParser


class LocalRelay:
    """Stall the first session at greeting or QUIT, then accept later sends."""

    def __init__(self, stall="quit"):
        self.stall = stall
        self.entered = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.messages = []
        self.sessions = 0
        self._tasks = set()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._session, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_exc):
        self.server.close()
        await self.server.wait_closed()
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def _session(self, reader, writer):
        task = asyncio.current_task()
        self._tasks.add(task)
        self.sessions += 1
        first = self.sessions == 1
        try:
            if first and self.stall == "greeting":
                self.entered.set()
                assert await reader.read() == b""
                self.disconnected.set()
                return
            writer.write(b"220 loopback fixture\r\n")
            await writer.drain()
            while command := await reader.readline():
                verb = command.split(None, 1)[0].upper()
                if verb in (b"EHLO", b"HELO"):
                    response = b"250 loopback\r\n"
                elif verb in (b"MAIL", b"RCPT", b"RSET", b"NOOP"):
                    response = b"250 OK\r\n"
                elif verb == b"DATA":
                    writer.write(b"354 end with dot\r\n")
                    await writer.drain()
                    lines = []
                    while (line := await reader.readline()) != b".\r\n":
                        if not line:
                            return
                        lines.append(line[1:] if line.startswith(b"..") else line)
                    self.messages.append(BytesParser(policy=policy.default).parsebytes(b"".join(lines)))
                    response = b"250 accepted\r\n"
                elif verb == b"QUIT":
                    if self.stall == "all_quit" or first and self.stall == "quit":
                        self.entered.set()
                        assert await reader.read() == b""
                        self.disconnected.set()
                        return
                    writer.write(b"221 bye\r\n")
                    await writer.drain()
                    return
                else:
                    raise AssertionError(f"unexpected SMTP command {verb!r}")
                writer.write(response)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            self._tasks.discard(task)
