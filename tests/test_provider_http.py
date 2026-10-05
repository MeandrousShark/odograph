import asyncio
import gzip
import threading

import httpx
import pytest

from app.provider_http import bounded_json, bounded_request, ProviderResponseTooLarge
from app.account_context import AccountPrincipal
from app.capacity import AdmissionManager

pytestmark = pytest.mark.unit


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks = chunks
        self.delay = delay
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self):
        self.closed = True


def test_absolute_deadline_ends_a_stream_that_keeps_making_progress():
    async def scenario():
        stream = Stream([b"x"] * 100, .01)
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=stream))) as client:
            with pytest.raises(httpx.TimeoutException):
                await bounded_request(client, "GET", "https://provider.example", max_bytes=200,
                                      deadline_s=.045)
        assert stream.closed
    asyncio.run(scenario())


@pytest.mark.parametrize("compressed", [False, True])
def test_cap_counts_yielded_decompressed_bytes_and_closes_without_truncating(compressed):
    async def scenario():
        payload = b"x" * 10000
        stream = Stream([gzip.compress(payload) if compressed else payload])
        headers = {"Content-Encoding": "gzip"} if compressed else {}
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, headers=headers, stream=stream))) as client:
            with pytest.raises(ProviderResponseTooLarge):
                await bounded_request(client, "GET", "https://provider.example", max_bytes=1000)
        assert stream.closed
    asyncio.run(scenario())


def test_network_cancellation_closes_stream():
    async def scenario():
        stream = Stream([b"x"] * 100, .1)
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=stream))) as client:
            task = asyncio.create_task(bounded_request(client, "GET", "https://provider.example",
                                                       max_bytes=200))
            await asyncio.sleep(.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert stream.closed
    asyncio.run(scenario())


def test_valid_response_at_limit_and_selected_status_body_parsing():
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(400, content=b'{"error":true}'))) as client:
            assert await bounded_json(client, "GET", "https://provider.example", max_bytes=14,
                                      allowed_statuses=(400,)) == {"error": True}
            with pytest.raises(httpx.HTTPStatusError):
                await bounded_request(client, "GET", "https://provider.example", max_bytes=14)
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(503, content=b'{"error":true}'))) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await bounded_json(client, "GET", "https://provider.example", max_bytes=14,
                                   allowed_statuses=(400,))
    asyncio.run(scenario())


def test_cancelled_json_parse_retains_serving_owner_and_lease_until_thread_returns(monkeypatch):
    started = threading.Event()
    finish = threading.Event()
    def parse(content):
        started.set()
        finish.wait(5)
        return {}
    monkeypatch.setattr("app.provider_http.json.loads", parse)

    async def scenario():
        manager = AdmissionManager()
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"{}"))) as client:
            async def request():
                async with manager.operation("background", AccountPrincipal(1, True, 1),
                                              registration="provider-test"):
                    async with manager.lease((1,)):
                        await bounded_json(client, "GET", "https://provider.example", max_bytes=2)
            task = asyncio.create_task(request())
            try:
                while not started.is_set():
                    await asyncio.sleep(.001)
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
                assert manager.snapshot()["background"]["active"] == 1
                assert manager.snapshot()["leases"] == 1
            finally:
                finish.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert manager.snapshot()["background"]["active"] == 0
            assert manager.snapshot()["leases"] == 0
    asyncio.run(scenario())
