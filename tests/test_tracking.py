"""Authentication precedes body reads, including the pre-account intake gate."""
from __future__ import annotations

import asyncio
import base64
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app import ingest
from app.tracking import IssuedCredential, TrackingUnavailable


def _app():
    app = FastAPI()
    app.state.config = SimpleNamespace(
        ingest_username="legacy", ingest_password="test-only-secret", ingest_max_body_bytes=1000,
    )
    app.state.control_pool = None
    app.state.ingest_limiter = ingest.FailedAuthLimiter(10, 60)
    app.include_router(ingest.make_router())
    return app


def test_unknown_credential_fails_before_body_read(monkeypatch):
    async def reject(*args, **kwargs):
        return None
    monkeypatch.setattr(ingest, "authenticate_ingest", reject)

    async def run():
        read = False

        async def body():
            nonlocal read
            read = True
            yield b'{"_type":"location"}'

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app()), base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/ingest", content=body(),
                headers={"Authorization": "Basic " + base64.b64encode("öther:päss".encode()).decode()},
            )
        assert response.status_code == 401
        assert not read
    asyncio.run(run())


def test_pre_account_sender_receives_retryable_response_without_body_read(monkeypatch):
    async def unavailable(*args, **kwargs):
        raise TrackingUnavailable("Complete account setup")
    monkeypatch.setattr(ingest, "authenticate_ingest", unavailable)

    async def run():
        read = False

        async def body():
            nonlocal read
            read = True
            yield b'{"_type":"location"}'

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app()), base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/ingest", content=body(), auth=("legacy", "test-only-secret"),
            )
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "60"
        assert not read
    asyncio.run(run())


def test_malformed_basic_auth_never_looks_up_credentials(monkeypatch):
    async def unexpected(*args, **kwargs):
        raise AssertionError("malformed authorization reached the database")
    monkeypatch.setattr(ingest, "authenticate_ingest", unexpected)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app()), base_url="http://testserver",
        ) as client:
            for value in ("", "Bearer bad", "Basic ***", "Basic " + base64.b64encode(b"no-colon").decode()):
                response = await client.post("/ingest", content=b"{}", headers={"Authorization": value})
                assert response.status_code == 401
    asyncio.run(run())


def test_issued_secret_is_not_in_debug_representation():
    issued = IssuedCredential("public", "username", "do-not-log-this-secret", 42)
    assert issued.secret not in repr(issued)


def test_blocked_ip_skips_verification_and_body_then_valid_retry_succeeds(monkeypatch):
    calls = []

    async def verify(*args, **kwargs):
        calls.append(args)
        return object()

    monkeypatch.setattr(ingest, "authenticate_ingest", verify)

    async def run():
        now = [0.0]
        app = _app()
        limiter = ingest.FailedAuthLimiter(1, 60, clock=lambda: now[0])
        app.state.ingest_limiter = limiter
        limiter.record_failure("127.0.0.1")
        read = False

        async def body():
            nonlocal read
            read = True
            yield b"private body"

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            response = await client.post("/ingest", content=body(), auth=("valid", "password"))
            assert response.status_code == 429
            assert int(response.headers["Retry-After"]) > 0
            assert calls == [] and not read
            now[0] = 61
            response = await client.post("/ingest", content=b"", auth=("valid", "password"))
            assert response.status_code == 200
            assert len(calls) == 1
            assert not limiter.blocked("127.0.0.1")
    asyncio.run(run())


def test_auth_saturation_and_request_cancellation_hold_slots_until_threads_finish(monkeypatch):
    async def run():
        app = _app()
        limiter = app.state.ingest_limiter
        loop = asyncio.get_running_loop()
        entered = {name: asyncio.Event() for name in ("first", "second")}
        release = {name: threading.Event() for name in entered}
        calls = []
        body_reads = []

        def thread_work(username):
            loop.call_soon_threadsafe(entered[username].set)
            assert release[username].wait(5), "test did not release verification thread"

        async def verify(_pool, username, _password, **kwargs):
            calls.append(username)
            if username in release:
                await asyncio.to_thread(thread_work, username)
            return object()

        async def body():
            body_reads.append(True)
            yield b"private body"

        monkeypatch.setattr(ingest, "authenticate_ingest", verify)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            first = asyncio.create_task(client.post("/ingest", content=b"", auth=("first", "password")))
            second = asyncio.create_task(client.post("/ingest", content=b"", auth=("second", "password")))
            try:
                await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 5)
                active = set(limiter._auth_tasks)
                assert len(active) == 2
                response = await client.post("/ingest", content=body(), auth=("third", "password"))
                assert response.status_code == 503 and response.headers["Retry-After"] == "1"
                assert calls == ["first", "second"] and body_reads == []

                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
                assert limiter._auth_tasks == active
                response = await client.post("/ingest", content=body(), auth=("third", "password"))
                assert response.status_code == 503
                assert calls == ["first", "second"] and body_reads == []

                release["first"].set()
                done, pending = await asyncio.wait(active, timeout=5, return_when=asyncio.FIRST_COMPLETED)
                assert len(done) == len(pending) == 1
                assert not second.done()
                response = await client.post("/ingest", content=b"", auth=("legitimate-retry", "password"))
                assert response.status_code == 200
                assert calls == ["first", "second", "legitimate-retry"]
                assert not limiter.blocked("127.0.0.1")
                release["second"].set()
                assert (await asyncio.wait_for(second, 5)).status_code == 200
            finally:
                for event in release.values():
                    event.set()
                await asyncio.gather(first, second, return_exceptions=True)
                await asyncio.gather(*limiter._auth_tasks, return_exceptions=True)
        assert not limiter._auth_tasks
    asyncio.run(run())


def test_cancelled_authentication_still_records_completed_failure(monkeypatch):
    async def run():
        now = [0.0]
        app = _app()
        limiter = ingest.FailedAuthLimiter(1, 60, clock=lambda: now[0], max_concurrent_auth=1)
        app.state.ingest_limiter = limiter
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        calls = []

        def thread_work():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "test did not release verification thread"

        async def verify(_pool, username, _password, **kwargs):
            calls.append(username)
            if username == "bad":
                await asyncio.to_thread(thread_work)
                return None
            return object()

        monkeypatch.setattr(ingest, "authenticate_ingest", verify)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            request = asyncio.create_task(client.post("/ingest", content=b"", auth=("bad", "password")))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                active = set(limiter._auth_tasks)
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
                release.set()
                await asyncio.wait_for(asyncio.gather(*active), 5)
                assert limiter.blocked("127.0.0.1")
                response = await client.post("/ingest", content=b"", auth=("valid", "password"))
                assert response.status_code == 429 and calls == ["bad"]
                now[0] = 61
                response = await client.post("/ingest", content=b"", auth=("valid", "password"))
                assert response.status_code == 200 and calls == ["bad", "valid"]
            finally:
                release.set()
                await asyncio.gather(request, return_exceptions=True)
                await asyncio.gather(*limiter._auth_tasks, return_exceptions=True)
    asyncio.run(run())
