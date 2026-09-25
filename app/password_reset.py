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
from typing import Callable

from app.account_context import control_connection
from app.accounts import get_account, safe_delivery_email
from app.email_challenges import _digest
from app.mailer import Mailer

log = logging.getLogger(__name__)

INITIATOR_PUBLIC = "public"
INITIATOR_ADMIN = "admin"
MAX_QUEUED_REQUESTS = 64
RECOVERY_WORKERS = 2
SECURITY_MAIL_SENDS = 2
MAX_LIMITER_KEYS = 1024


async def issue_password_reset(
    conn, token: str, *, initiator: str, email: str | None = None, account_id: int | None = None,
) -> str | None:
    """Record a reset for `token`; return the verified delivery address."""
    digest = _digest(token)
    if digest is None:
        return None
    cur = await conn.execute(
        "SELECT public.issue_password_reset(%s,%s,%s,%s)", (account_id, email, initiator, digest),
    )
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

    def __init__(self, limit: int = SECURITY_MAIL_SENDS):
        self._slots = asyncio.Semaphore(limit)
        self._tasks: set[asyncio.Task] = set()

    async def send(self, mailer: Mailer, message) -> None:
        await self._slots.acquire()
        try:
            task = asyncio.create_task(mailer.send(message))
        except BaseException:
            self._slots.release()
            raise
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        await asyncio.shield(task)

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

    def submit_admin(self, account_id: int) -> bool:
        return self._submit((INITIATOR_ADMIN, account_id))

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

    async def _process(self, item: tuple[str, object]) -> None:
        initiator, target = item
        token = secrets.token_urlsafe(32)
        async with control_connection(self._pool) as conn:
            address = await issue_password_reset(
                conn, token, initiator=initiator,
                email=target if initiator == INITIATOR_PUBLIC else None,
                account_id=target if initiator == INITIATOR_ADMIN else None,
            )
            if address is not None and not safe_delivery_email(address):
                await revoke_password_reset(conn, token)
                address = None
        if address is None:
            return
        mailer = self._mailer_for(address)
        message = reset_message(mailer, self._link_base, token)
        # The last check before delivery, under the account's row lock.
        async with control_connection(self._pool) as conn:
            async with conn.transaction():
                usable = await password_reset_usable(conn, token, lock_account=True)
        if not usable:
            return
        try:
            await self._admission.send(mailer, message)
        except Exception as exc:
            log.warning("password reset delivery failed (%s)", type(exc).__name__)
            async with control_connection(self._pool) as conn:
                await revoke_password_reset(conn, token)
