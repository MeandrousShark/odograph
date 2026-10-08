"""Verified-channel password reset and trusted host recovery.

A public request never waits for account lookup or delivery: the route
counts the attempt, hands a normalized identifier to a bounded process-local
queue and returns the same acknowledgement for every outcome. Workers check
eligibility and persistent budgets in the database, then deliver only to the
account's stored verified address. Plaintext tokens exist only in a worker's
memory while it sends; there is no durable mail queue, so a lost or
interrupted delivery needs a later request.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager, AsyncExitStack
from typing import AsyncContextManager, Callable

from app.account_context import control_connection
from app.accounts import get_account, get_account_by_email, safe_delivery_email
from app.account_work import external_account_work
from app.email_challenges import _digest
from app.mailer import Mailer
from app.capacity import AdmissionManager, CapacityBusy
from app.preparation import PreparationOperation
from app.security_mail_preparation import (
    SecurityMailConstructionError, SecurityMailSpec, prepare_security_mail,
)

log = logging.getLogger(__name__)

INITIATOR_PUBLIC = "public"
INITIATOR_ADMIN = "admin"
MAX_QUEUED_REQUESTS = 64
RECOVERY_WORKERS = 2
SECURITY_MAIL_SENDS = 2
MAX_LIMITER_KEYS = 1024


async def issue_password_reset(
    conn, token: str, *, initiator: str, email: str | None = None, account_id: int | None = None,
    actor_id: int | None = None, actor_auth_version: int | None = None,
) -> str | None:
    """Record a reset for `token`; return the verified delivery address."""
    digest = _digest(token)
    if digest is None:
        return None
    if initiator == INITIATOR_PUBLIC and actor_id is None and actor_auth_version is None:
        cur = await conn.execute(
            "SELECT public.issue_password_reset(%s,%s,%s,%s)", (account_id, email, initiator, digest),
        )
    elif (initiator == INITIATOR_ADMIN and email is None and account_id is not None
          and actor_id is not None and actor_auth_version is not None):
        cur = await conn.execute(
            "SELECT public.issue_admin_password_reset(%s,%s,%s,%s)",
            (actor_id, actor_auth_version, account_id, digest),
        )
    else:
        return None
    return (await cur.fetchone())[0]


async def revoke_password_reset(conn, token: str) -> None:
    digest = _digest(token)
    if digest is not None:
        await conn.execute("SELECT public.revoke_password_reset(%s)", (digest,))


async def password_reset_usable(conn, token: str, *, lock_account: bool = False) -> bool:
    digest = _digest(token)
    if digest is None:
        return False
    cur = await conn.execute("SELECT public.password_reset_usable(%s,%s)", (digest, lock_account))
    return bool((await cur.fetchone())[0])


async def password_reset_send_usable(
    conn, token: str, *, actor_id: int | None = None, actor_auth_version: int | None = None,
) -> bool:
    digest = _digest(token)
    if digest is None:
        return False
    cur = await conn.execute(
        "SELECT public.password_reset_send_usable(%s,%s,%s)",
        (digest, actor_id, actor_auth_version),
    )
    return bool((await cur.fetchone())[0])


async def consume_password_reset(conn, token: str, password_hash: str) -> int | None:
    digest = _digest(token)
    if digest is None:
        return None
    cur = await conn.execute("SELECT public.consume_password_reset(%s,%s)", (digest, password_hash))
    return (await cur.fetchone())[0]


async def host_reset_password(conn, account_id: int, password_hash: str) -> dict | None:
    """Explicit-target host recovery; the caller holds host authority.

    Needs no bearer proof, so only the host CLI (app/manage_account.py) may
    call it. Never reach it from request handling; a test enforces this.
    """
    cur = await conn.execute("SELECT public.host_reset_password(%s,%s)", (account_id, password_hash))
    if not (await cur.fetchone())[0]:
        return None
    return await get_account(conn, account_id)


class AttemptLimiter:
    """Count every attempt per key in a sliding window, with bounded keys.

    At capacity a new key is refused rather than evicting a live limit, so a
    flood of fresh keys fails closed instead of resetting active limits.
    """

    def __init__(self, name: str, max_attempts: int, window_s: float, *,
                 max_keys: int = MAX_LIMITER_KEYS, clock=time.monotonic):
        self.name = name
        self.max_attempts = max(1, max_attempts)
        self.window_s = window_s
        self.max_keys = max_keys
        self._clock = clock
        self._attempts: dict[str, deque] = {}
        self._saturation_logged_at: float | None = None

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_s
        for key in list(self._attempts):
            attempts = self._attempts[key]
            while attempts and attempts[0] <= cutoff:
                attempts.popleft()
            if not attempts:
                del self._attempts[key]

    def allow(self, key: str) -> bool:
        now = self._clock()
        self._prune(now)
        attempts = self._attempts.get(key)
        if attempts is None:
            if len(self._attempts) >= self.max_keys:
                if self._saturation_logged_at is None or now - self._saturation_logged_at >= self.window_s:
                    self._saturation_logged_at = now
                    log.warning("%s attempt limiter is at capacity; refusing new keys", self.name)
                return False
            attempts = self._attempts[key] = deque(maxlen=self.max_attempts)
        if len(attempts) >= self.max_attempts:
            return False
        attempts.append(now)
        return True


class SecurityMailAdmission:
    """Process-wide bound on concurrent security-mail sends.

    A send keeps its slot until the transport thread really finishes, even
    when the waiting caller is cancelled.
    """

    def __init__(self, limit: int = SECURITY_MAIL_SENDS, *, capacity=None, spool_root=None):
        if type(limit) is not int or not 1 <= limit <= SECURITY_MAIL_SENDS:
            raise ValueError("security mail supports at most two sends")
        self.capacity = capacity if capacity is not None else AdmissionManager()
        self.spool_root = spool_root
        self._slots = asyncio.Semaphore(limit)
        self._tasks: set[asyncio.Task] = set()

    async def send(
        self, mailer: Mailer, message, *, wait: bool = False,
        admit: Callable[[], AsyncContextManager[bool]] | None = None,
        lease: Callable[[], AsyncContextManager] | None = None,
    ) -> bool:
        if not wait and self._slots.locked():
            return False
        await self._slots.acquire()
        task = asyncio.create_task(self._send(mailer, message, admit, lease))
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        return await asyncio.shield(task)

    async def _send(self, mailer, message, admit, lease) -> bool:
        async with AsyncExitStack() as contexts:
            try:
                await contexts.enter_async_context(self.capacity.operation("mail"))
            except CapacityBusy:
                return False
            if isinstance(message, SecurityMailSpec):
                async with PreparationOperation(spool_root=self.spool_root) as operation:
                    async def prepared_send():
                        async with AsyncExitStack() as lifecycle:
                            if lease is not None:
                                await lifecycle.enter_async_context(lease())
                            try:
                                prepared = await prepare_security_mail(operation, mailer, message)
                                return await self._authorized_send(
                                    lambda: mailer.send_prepared(prepared,
                                        before_transport=operation.finish_preparation), admit)
                            except BaseException as exc:
                                await operation.backend_failure(exc)
                                raise
                            finally:
                                # Actual cleanup precedes release of the lifecycle lease.
                                await operation.close()
                    return await operation.perform(prepared_send)
            if lease is not None:
                await contexts.enter_async_context(lease())
            return await self._authorized_send(lambda: mailer.send(message), admit)

    async def _authorized_send(self, send, admit):
        if admit is None:
            await send()
        else:
            transport = None
            try:
                async with admit() as allowed:
                    if not allowed:
                        return False
                    transport = asyncio.create_task(send())
            except BaseException:
                if transport is not None:
                    await self._drain_transport(transport)
                raise
            await transport
        return True

    @staticmethod
    async def _drain_transport(task: asyncio.Task) -> None:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()

    def _finished(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        self._slots.release()
        if not task.cancelled():
            task.exception()

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)


def reset_message(mailer: Mailer, link_base: str, token: str):
    link = f"{link_base}/reset-password#token={token}"
    return mailer.compose(
        "Reset your Odograph password",
        "Someone asked to reset the password for your Odograph account.\n\n"
        f"To choose a new password, open this link:\n{link}\n\n"
        f"If the link does not fill the form, enter this code manually: {token}\n\n"
        "The code expires in 30 minutes and works once. Resetting your password signs "
        "you out of Odograph everywhere. It does not change your sign-in provider or "
        "tracking devices. If you did not ask for this, ignore this email.",
    )


class RecoveryQueue:
    """Bounded queue and workers for reset issuance and delivery."""

    def __init__(self, pool, link_base: str, mailer_for: Callable[[str], Mailer],
                 admission: SecurityMailAdmission, *, max_pending: int = MAX_QUEUED_REQUESTS,
                 workers: int = RECOVERY_WORKERS):
        if type(max_pending) is not int or not 1 <= max_pending <= MAX_QUEUED_REQUESTS:
            raise ValueError("recovery queue supports at most 64 pending requests")
        if type(workers) is not int or not 1 <= workers <= RECOVERY_WORKERS:
            raise ValueError("recovery queue supports at most two workers")
        self._pool = pool
        self._link_base = link_base
        self._mailer_for = mailer_for
        self._admission = admission
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_pending)
        self._pending: set[tuple[str, object]] = set()
        self._worker_count = workers
        self._workers: list[asyncio.Task] = []
        self._closed = True

    async def start(self) -> None:
        self._closed = False
        self._workers = [asyncio.create_task(self._work(), name=f"password-reset-{index}")
                         for index in range(self._worker_count)]

    async def stop(self) -> None:
        self._closed = True
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []
        await self._admission.drain()

    def submit_public(self, email: str) -> bool:
        return self._submit((INITIATOR_PUBLIC, email))

    def submit_admin(self, actor_id: int, actor_auth_version: int, account_id: int) -> bool:
        return self._submit((INITIATOR_ADMIN, actor_id, actor_auth_version, account_id))

    def _submit(self, item: tuple[str, object]) -> bool:
        if self._closed:
            return False
        if item in self._pending:
            return True
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            return False
        self._pending.add(item)
        return True

    async def _work(self) -> None:
        while True:
            item = await self._queue.get()
            self._pending.discard(item)
            task = asyncio.create_task(self._process(item))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # Finish the in-flight request, including revocation after a
                # failed send, before shutdown continues.
                await asyncio.gather(task, return_exceptions=True)
                raise
            except Exception as exc:
                log.warning("password reset request failed (%s)", type(exc).__name__)

    async def _process(self, item: tuple) -> None:
        initiator = item[0]
        target = item[1] if initiator == INITIATOR_PUBLIC else item[3]
        actor_id = item[1] if initiator == INITIATOR_ADMIN else None
        actor_version = item[2] if initiator == INITIATOR_ADMIN else None
        token = secrets.token_urlsafe(32)
        async with control_connection(self._pool, lane="identity") as conn:
            address = await issue_password_reset(
                conn, token, initiator=initiator,
                email=target if initiator == INITIATOR_PUBLIC else None,
                account_id=target if initiator == INITIATOR_ADMIN else None,
                actor_id=actor_id, actor_auth_version=actor_version,
            )
            target_account = await get_account_by_email(conn, address) if address else None
            if address is not None and not safe_delivery_email(address):
                await revoke_password_reset(conn, token)
                address = None
        if address is None or target_account is None:
            return
        mailer = self._mailer_for(address)
        message = SecurityMailSpec('reset', self._link_base, token)
        @asynccontextmanager
        async def admit():
            async with control_connection(self._pool, lane="mail") as conn:
                async with conn.transaction():
                    yield await password_reset_send_usable(
                        conn, token, actor_id=actor_id, actor_auth_version=actor_version,
                    )
        try:
            admitted = await self._admission.send(
                mailer, message, wait=True, admit=admit,
                lease=lambda: external_account_work(self._pool, target_account["id"], *([actor_id] if actor_id else [])),
            )
            if not admitted:
                async with control_connection(self._pool, lane="identity") as conn:
                    await revoke_password_reset(conn, token)
        except SecurityMailConstructionError:
            raise
        except Exception as exc:
            log.warning("password reset delivery failed (%s)", type(exc).__name__)
            async with control_connection(self._pool, lane="identity") as conn:
                await revoke_password_reset(conn, token)
