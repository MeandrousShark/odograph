"""Bounded serving admission and ownership of connections and blocking work."""
from __future__ import annotations

import asyncio
import contextvars
import math
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial


class CapacityBusy(RuntimeError):
    """A configured serving budget or its finite waiting time is exhausted."""


class CapacityContractError(RuntimeError):
    """Serving work attempted to use a resource without its admitted owner."""


@dataclass(frozen=True, slots=True)
class Lane:
    limit: int
    pending: int = 0
    wait: float = 0


@dataclass(slots=True)
class _Lifetime:
    runtime: bool = False
    control: bool = False
    lease: bool = False
    lease_connection: bool = False
    threads: set = field(default_factory=set)
    cancelled: bool = False
    released: bool = False


@dataclass(frozen=True, slots=True, eq=False)
class OperationOwner:
    manager: AdmissionManager
    lane: str
    principal: object = None
    registration: str | None = None
    _lifetime: _Lifetime = field(default_factory=_Lifetime, repr=False, compare=False)

    @property
    def cancelled(self):
        return self._lifetime.cancelled


@dataclass(slots=True)
class _Borrow:
    manager: AdmissionManager
    role: str
    owner: OperationOwner
    task: asyncio.Task
    connected: bool = False


@dataclass(slots=True)
class _Ticket:
    owner: OperationOwner
    future: asyncio.Future
    deadline: float


_owner = contextvars.ContextVar("capacity_owner", default=None)
_borrow = contextvars.ContextVar("capacity_borrow", default=None)


def current_owner() -> OperationOwner | None:
    return _owner.get()


def _value(config, name, default):
    return getattr(config, "capacity_" + name, default)


def validate_capacity_config(config):
    """Reject configurations that increase the reviewed resource envelope."""
    caps = {name: _value(config, name + "_slots", default) for name, default in (
        ("ingest", 2), ("routine", 2), ("foreground", 1), ("background", 1),
        ("ingest_identity", 1), ("identity", 2), ("lifecycle", 1), ("mail", 2),
        ("auth_ingest", 2), ("auth_interactive", 1))}
    if any(type(value) is not int or value <= 0 for value in caps.values()):
        raise ValueError("capacity slots must be positive integers")
    if sum(caps[name] for name in ("ingest", "routine", "foreground", "background")) > 6:
        raise ValueError("runtime capacity reservations exceed six connections")
    if sum(caps[name] for name in ("ingest_identity", "identity", "lifecycle", "mail")) > 6:
        raise ValueError("control capacity reservations exceed six connections")
    if any(caps[name] > maximum for name, maximum in (
        ("foreground", 1), ("background", 1), ("ingest_identity", 1), ("mail", 2),
        ("auth_ingest", 2), ("auth_interactive", 1))):
        raise ValueError("capacity exceeds the reviewed operation or authentication bound")
    for name, default, maximum in (
            ("ingest", 4, 4), ("routine", 4, 4), ("foreground", 4, 4),
            ("identity", 4, 4), ("auth_ingest", 8, 8),
            ("ingest_identity", 1, 1)):
        pending = _value(config, name + "_pending", default)
        if type(pending) is not int or not 0 <= pending <= maximum:
            raise ValueError(f"capacity {name} pending limit must be an integer from zero to {maximum}")
    for name, default in (("ingest_wait_s", .25), ("routine_wait_s", 1.),
            ("foreground_wait_s", 2.), ("identity_wait_s", 1.),
            ("auth_ingest_wait_s", 1.), ("ingest_identity_wait_s", .25),
            ("auth_body_timeout_s", 15.), ("ingest_body_timeout_s", 15.),
            ("import_body_timeout_s", 60.), ("response_timeout_s", 60.), ("routine_sql_timeout_s", 5.),
            ("operation_sql_timeout_s", 15.), ("lock_timeout_s", 1.)):
        value = _value(config, name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError("capacity waits and deadlines must be positive and finite")
        if name.endswith("sql_timeout_s") or name == "lock_timeout_s":
            if value > 2147483.647:
                raise ValueError("capacity SQL deadlines exceed the supported database timeout")
        maximum = {"auth_ingest_wait_s": 1., "ingest_identity_wait_s": .25}.get(name)
        if maximum is not None and value > maximum:
            raise ValueError("capacity authentication waits exceed the reviewed limit")
    for name, default, maximum in (("auth_form_max_bytes", 65536, 65536),
            ("basic_header_max_bytes", 8192, 8192),
            ("multipart_overhead_bytes", 65536, 65536),
            ("multipart_max_fields", 16, 16), ("multipart_max_files", 1, 1)):
        value = _value(config, name, default)
        if type(value) is not int or not 0 < value <= maximum:
            raise ValueError("capacity input bounds exceed the reviewed envelope")


class AdmissionManager:
    def __init__(self, config=None):
        validate_capacity_config(config)
        self.config = config
        self.lanes = {}
        for name, limit, pending, wait in (
                ("ingest", 2, 4, .25), ("routine", 2, 4, 1.),
                ("foreground", 1, 4, 2.), ("background", 1, 0, 0),
                ("ingest_identity", 1, 1, .25), ("identity", 2, 4, 1.),
                ("lifecycle", 1, 0, 0), ("mail", 2, 0, 0),
                ("auth_ingest", 2, 8, 1.), ("auth_interactive", 1, 0, 0)):
            self.lanes[name] = Lane(_value(config, name + "_slots", limit),
                _value(config, name + "_pending", pending) if pending else 0,
                _value(config, name + "_wait_s", wait) if wait else 0)
        self._active = {name: set() for name in self.lanes}
        self._pending = {name: deque() for name in self.lanes}
        self._closed = False
        self._drained = asyncio.Event()
        self._drained.set()
        self._leases = set()

    def manage_pool(self, pool, role):
        if role not in ("runtime", "control"):
            raise ValueError("managed pool role must be runtime or control")
        return ManagedPool(pool, self, role)

    def _key(self, owner):
        return getattr(owner.principal, "account_id", None)

    def _promote(self, lane):
        active, pending = self._active[lane], self._pending[lane]
        now = asyncio.get_running_loop().time()
        for ticket in tuple(pending):
            if ticket.future.done() or ticket.deadline <= now or self._closed:
                pending.remove(ticket)
                if not ticket.future.done():
                    ticket.future.set_exception(CapacityBusy("capacity wait expired"))
        while pending and len(active) < self.lanes[lane].limit:
            ticket = pending.popleft()
            if ticket.future.done():
                continue
            if ticket.deadline <= now or self._closed:
                ticket.future.set_exception(CapacityBusy("capacity wait expired"))
                continue
            active.add(ticket.owner)
            ticket.future.set_result(None)

    async def _acquire(self, owner):
        lane = owner.lane
        if self._closed:
            raise CapacityBusy("serving admission is shutting down")
        if lane not in self.lanes:
            raise CapacityContractError("unknown capacity lane")
        parent = current_owner()
        if parent is not None and parent.manager is self and parent.lane == lane:
            raise CapacityContractError("nested operation admission")
        from app.account_context import AccountPrincipal
        if owner.principal is not None and not isinstance(owner.principal, AccountPrincipal):
            raise CapacityContractError("capacity identity requires an immutable validated principal")
        if lane in ("ingest", "routine", "foreground", "background"):
            if owner.principal is None or not owner.principal.enabled:
                raise CapacityContractError("account admission requires a validated enabled principal")
        self._promote(lane)
        active, pending, spec = self._active[lane], self._pending[lane], self.lanes[lane]
        key = self._key(owner)
        if key is not None and any(self._key(other) == key for other in active):
            raise CapacityBusy("account already owns capacity")
        if key is not None and any(self._key(ticket.owner) == key for ticket in pending):
            raise CapacityBusy("account already awaits capacity")
        if not pending and len(active) < spec.limit:
            active.add(owner)
            self._drained.clear()
            return
        if len(pending) >= spec.pending or not spec.wait:
            raise CapacityBusy("capacity is full")
        future = asyncio.get_running_loop().create_future()
        ticket = _Ticket(owner, future, asyncio.get_running_loop().time() + spec.wait)
        pending.append(ticket)
        self._drained.clear()
        try:
            await asyncio.wait_for(asyncio.shield(future), spec.wait)
        except BaseException:
            if ticket in pending:
                pending.remove(ticket)
            active.discard(owner)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()
            self._promote(lane)
            self._update_drained()
            raise

    def _update_drained(self):
        if not any(self._active.values()) and not any(self._pending.values()):
            self._drained.set()

    @asynccontextmanager
    async def operation(self, lane, principal=None, registration=None):
        owner = OperationOwner(self, lane, principal, registration)
        try:
            await self._acquire(owner)
        except TimeoutError:
            raise CapacityBusy("capacity wait expired") from None
        token = _owner.set(owner)
        try:
            yield owner
        except asyncio.CancelledError:
            owner._lifetime.cancelled = True
            raise
        finally:
            # Threads retain the owner even when its caller has been cancelled.
            cancelled = await _drain(owner._lifetime.threads)
            _owner.reset(token)
            owner._lifetime.released = True
            self._active[lane].discard(owner)
            self._promote(lane)
            self._update_drained()
            if cancelled:
                owner._lifetime.cancelled = True
                raise asyncio.CancelledError

    @asynccontextmanager
    async def runtime_borrow(self, principal):
        if _borrow.get() is not None:
            raise CapacityContractError("nested database borrow")
        owner = current_owner()
        if owner is not None and owner.manager is self and owner.lane in ("ingest", "foreground", "background"):
            if owner.principal != principal or owner._lifetime.released:
                raise CapacityContractError("runtime borrow does not match its operation principal")
            async with self._borrow_for(owner, "runtime"):
                yield owner
        else:
            async with self.operation("routine", principal) as routine:
                async with self._borrow_for(routine, "runtime"):
                    yield routine

    @asynccontextmanager
    async def control_borrow(self, lane="identity"):
        if _borrow.get() is not None:
            raise CapacityContractError("nested database borrow")
        owner = current_owner()
        if owner is not None and owner.manager is self and owner.lane == "mail" and lane == "identity":
            lane = "mail"
        if lane not in ("identity", "ingest_identity", "lifecycle", "mail"):
            raise CapacityContractError("unknown control reservation")
        if owner is not None and owner.manager is self and owner.lane == lane:
            async with self._borrow_for(owner, "control"):
                yield owner
        else:
            async with self.operation(lane) as control:
                async with self._borrow_for(control, "control"):
                    yield control

    @asynccontextmanager
    async def _borrow_for(self, owner, role):
        if getattr(owner._lifetime, role):
            raise CapacityContractError("nested connection reservation")
        setattr(owner._lifetime, role, True)
        token = _borrow.set(_Borrow(self, role, owner, asyncio.current_task()))
        try:
            yield owner
        finally:
            _borrow.reset(token)
            setattr(owner._lifetime, role, False)

    @asynccontextmanager
    async def lease(self, account_ids):
        owner = current_owner()
        if owner is None or owner.manager is not self or owner.lane not in ("foreground", "background", "mail"):
            raise CapacityContractError("external work requires a foreground, background or mail owner")
        if owner._lifetime.lease or len(self._leases) >= 4:
            raise CapacityContractError("nested or excess lifecycle lease")
        if owner.principal is not None and owner.principal.account_id not in account_ids:
            raise CapacityContractError("lifecycle lease does not cover its owner")
        owner._lifetime.lease = True
        self._leases.add(owner)
        try:
            yield owner
        finally:
            cancelled = await _drain(owner._lifetime.threads)
            owner._lifetime.lease = False
            self._leases.discard(owner)
            if cancelled:
                owner._lifetime.cancelled = True
                raise asyncio.CancelledError

    async def shutdown(self):
        self._closed = True
        for pending in self._pending.values():
            while pending:
                ticket = pending.popleft()
                if not ticket.future.done():
                    ticket.future.set_exception(CapacityBusy("serving admission is shutting down"))
        self._update_drained()
        await self._drained.wait()

    def snapshot(self):
        return {name: {"active": len(self._active[name]), "pending": len(self._pending[name])}
                for name in self.lanes} | {"leases": len(self._leases)}


class ManagedPool:
    """A serving pool whose raw borrows require an admitted helper."""
    def __init__(self, pool, manager, role):
        self._pool = pool
        self.capacity = manager
        self.role = role

    def __getattr__(self, name):
        if name in ("getconn", "putconn"):
            raise CapacityContractError("raw serving borrows require an admitted helper")
        return getattr(self._pool, name)

    @asynccontextmanager
    async def connection(self, **kwargs):
        borrow = _borrow.get()
        if (borrow is None or borrow.manager is not self.capacity or borrow.role != self.role
                or borrow.owner._lifetime.released or borrow.task is not asyncio.current_task()):
            raise CapacityContractError("serving connection has no reservation owner")
        if borrow.connected:
            raise CapacityContractError("nested raw serving connection")
        borrow.connected = True
        try:
            async with self._pool.connection(**kwargs) as conn:
                yield conn
        finally:
            borrow.connected = False


async def _drain(futures):
    cancelled = False
    while futures:
        future = next(iter(futures))
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
            continue
        except BaseException:
            pass
        finally:
            if future.done():
                futures.discard(future)
    return cancelled


async def owned_thread(function, *args, **kwargs):
    """Keep admission and any surrounding lease until the actual thread returns."""
    owner = current_owner()
    if owner is None or owner._lifetime.released:
        raise CapacityContractError("blocking serving work requires an operation owner")
    if owner._lifetime.threads:
        raise CapacityContractError("an operation already owns blocking work")
    context = contextvars.copy_context()
    future = asyncio.get_running_loop().run_in_executor(None, context.run, partial(function, *args, **kwargs))
    owner._lifetime.threads.add(future)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        owner._lifetime.cancelled = True
        await _drain({future})
        raise
    finally:
        owner._lifetime.threads.discard(future)


async def await_completion(task):
    """Drain a protected asynchronous finalizer before its caller drops admission."""
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await _drain({task})
        raise
