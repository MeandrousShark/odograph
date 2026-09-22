"""Cross-workflow locks and portable isolation on real restricted roles.

Prepared-mode cases prove explicit query scoping. The policy-enabled case
activates only its disposable fixture and separately proves database denial.
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from psycopg import errors, sql

from app.account_context import AccountPool, AccountPrincipal, CONTROL_ROLE, RUNTIME_ROLE
from app.accounts import create_admin
from app.application_roles import (
    OWNED_TABLES, application_role_pools, prepare_application_roles, validate_application_contract,
)
from app.db import make_pool
from app.detector.core import Params
from app.detector.runner import DETECTOR_VERSION, DetectorRunner
from app.portable import importer
from app.portable.normalize import normalize_bundle
from app.portable.routes import make_router as portable_router
from app.role_setup import RoleSetupError, role_conninfo
from app.tracking import create_device
from app.ui import make_router as ui_router
from app.ui.merge_split import _merge_trips_core
from conftest import full_schema_reset
from tests.synth import Drive, Stationary, build_track

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")


@asynccontextmanager
async def _fixture():
    owner = make_pool(TEST_DB)
    await owner.open(wait=True)
    try:
        await full_schema_reset(owner)
        state = await prepare_application_roles(TEST_DB)
        async with application_role_pools(TEST_DB) as pools:
            async with pools.control.connection() as conn:
                first = await create_admin(conn, "a@example.invalid", "unused-test-hash")
            # Synthetic second account is test-only. Production keeps both
            # singleton enforcement and the permanent first-account guard.
            async with owner.connection() as conn:
                await conn.execute("DROP INDEX accounts_singleton_idx")
                await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")
                await conn.execute(
                    "INSERT INTO accounts(id,email,password_hash,is_admin) VALUES (84,'b@example.invalid','unused',false)"
                )
                await conn.execute("INSERT INTO account_settings(account_id) VALUES (84)")
                await conn.execute(
                    "INSERT INTO vehicles(account_id,name,is_default) VALUES (84,'My Car',true)"
                )
                await conn.execute(
                    "INSERT INTO tag_rules(account_id,a_kind,b_kind,category) "
                    "SELECT 84,a_kind,b_kind,category FROM tag_rules WHERE account_id=%s",
                    (first["id"],),
                )
            a = AccountPool(pools.runtime, AccountPrincipal(first["id"], True, 1))
            b = AccountPool(pools.runtime, AccountPrincipal(84, True, 1))
            yield owner, pools, state, a, b
    finally:
        await full_schema_reset(owner)
        await owner.close()


def _bundle(label="Imported"):
    bundle = {
        "format": "odograph-portable", "format_version": 3, "schema_version": 26,
        "exported_at": "2026-08-05T00:00:00+00:00",
        "vehicles": [{"$id": 101, "name": f"{label} car", "make": None, "model": None,
                      "plate": None, "is_default": True, "active": True}],
        "places": [{"$id": 102, "name": f"{label} place", "kind": "work",
                    "lat": 47.6, "lon": -122.3, "radius_m": 150}],
        "tag_rules": [{"a_place": 102, "a_kind": None, "b_place": None,
                       "b_kind": "home", "category": "business"}],
        "mileage_rates": [{"year": 2026, "rate_per_mi": 0.725,
                           "rate_h2_per_mi": None, "h2_start_month": None}],
        "trips": [{"$id": 103, "device": "Historical label", "source": "detected",
                   "started_at": "2026-08-01T10:00:00+00:00",
                   "ended_at": "2026-08-01T11:00:00+00:00", "distance_m": 12345,
                   "category": "business", "exclusion": None, "tag_source": "human",
                   "purpose": label, "notes": f"{label} private note", "has_gap": False,
                   "vehicle": 101, "start_place": 102, "end_place": None}],
        "expenses": [{"vehicle": 101, "incurred_on": "2026-08-01", "category": "parking",
                      "amount": "12.34", "treatment": "fully_business", "notes": label, "trip": 103}],
        "odometer_readings": [{"vehicle": 101, "recorded_at": "2026-08-01T12:00:00+00:00",
                               "odometer_m": 100000, "note": label}],
        "settings": {"auto_assign_default_vehicle": True, "display_tz": "Asia/Tokyo"},
    }
    normalized, issues = normalize_bundle(bundle)
    assert issues == []
    return normalized


async def _export(bound):
    endpoint = next(route.endpoint for route in portable_router().routes
                    if route.path == "/settings/export/data")
    response = await endpoint(SimpleNamespace(state=SimpleNamespace(account_pool=bound)), user={})
    assert response.status_code == 200
    return json.loads(response.body)


async def _seed_track(bound, *, two_trips=False):
    segments = [Stationary(900), Drive(km=2), Stationary(1200)]
    if two_trips:
        segments.extend([Drive(km=2), Stationary(900)])
    async with bound.connection() as conn:
        device = (await create_device(conn, "Phone")).tracking_device_id
        for point in build_track(segments):
            await conn.execute(
                "INSERT INTO points(account_id,tracking_device_id,device,recorded_at,received_at,"
                "geom,accuracy_m,velocity_kmh) VALUES (%s,%s,'same',%s,%s,"
                "ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography,%s,%s)",
                (conn.principal.account_id, device, point.t, point.t, point.lon, point.lat,
                 point.accuracy_m, point.velocity_kmh),
            )
    return device


async def _checkpoint(owner, device):
    async with owner.connection() as conn:
        return await (await conn.execute(
            "SELECT last_run_at,detector_version FROM detector_state WHERE tracking_device_id=%s",
            (device,),
        )).fetchone()


async def _wait_blocked(owner, waiter, holder, task):
    async with asyncio.timeout(5):
        while True:
            async with owner.connection() as conn:
                blockers = (await (await conn.execute("SELECT pg_blocking_pids(%s)", (waiter,))).fetchone())[0]
            if holder in blockers:
                return
            if task.done():
                await task
                pytest.fail("mutation finished before the protecting transaction released its lock")
            await asyncio.sleep(0)


class _ObservedPool:
    def __init__(self, bound):
        self.bound = bound
        self.started = asyncio.Event()
        self.pid = None

    @asynccontextmanager
    async def connection(self):
        async with self.bound.connection() as conn:
            self.pid = (await (await conn.execute("SELECT pg_backend_pid()")).fetchone())[0]
            self.started.set()
            yield conn


@pytest.mark.parametrize("rollback", [False, True])
def test_import_holds_shared_lock_from_clean_check_through_writes_and_releases(rollback, monkeypatch):
    async def run():
        async with _fixture() as (owner, _pools, _state, a, b):
            device = await _seed_track(b)
            detector = DetectorRunner(b, Params())
            checked, allow_writes, written, allow_exit = (asyncio.Event() for _ in range(4))
            original_check = importer._check_clean_target
            original_settings = importer._update_settings

            async def check(conn):
                conflicts = await original_check(conn)
                assert conflicts == {}
                checked.set()
                await allow_writes.wait()
                return conflicts

            async def settings(conn, values):
                await original_settings(conn, values)
                written.set()
                await allow_exit.wait()

            monkeypatch.setattr(importer, "_check_clean_target", check)
            monkeypatch.setattr(importer, "_update_settings", settings)

            class InjectedRollback(Exception):
                pass

            async def importing():
                try:
                    async with a.connection() as conn:
                        await importer._apply_import(conn, _bundle())
                        if rollback:
                            raise InjectedRollback
                except InjectedRollback:
                    pass

            task = asyncio.create_task(importing())
            try:
                await asyncio.wait_for(checked.wait(), 5)
                assert await detector.run_once() is False
                assert await _checkpoint(owner, device) == (None, 0)
                allow_writes.set()
                await asyncio.wait_for(written.wait(), 5)
                assert await detector.run_once() is False
                assert await _checkpoint(owner, device) == (None, 0)
                async with owner.connection() as conn:
                    assert (await (await conn.execute("SELECT count(*) FROM trips")).fetchone())[0] == 0
                allow_exit.set()
                await asyncio.wait_for(task, 5)
            finally:
                allow_writes.set()
                allow_exit.set()
                await asyncio.gather(task, return_exceptions=True)
            # A real detector transaction can now acquire the same bigint key.
            assert await detector.run_once() is True
            checkpoint = await _checkpoint(owner, device)
            assert checkpoint[0] is not None and checkpoint[1] == DETECTOR_VERSION
            exported = await _export(a)
            assert len(exported["trips"]) == (0 if rollback else 1)
            assert exported["vehicles"][0]["name"] == ("My Car" if rollback else "Imported car")
    asyncio.run(run())


@pytest.mark.parametrize("mutation", ["batch_delete", "merge"])
def test_batch_and_structural_mutations_wait_for_real_detector_transaction(mutation, monkeypatch):
    async def run():
        async with _fixture() as (owner, _pools, _state, a, b):
            device = await _seed_track(b, two_trips=True)
            detector = DetectorRunner(b, Params())
            assert await detector.run_once()
            async with b.connection() as conn:
                rows = await (await conn.execute(
                    "SELECT id,started_at,ended_at FROM trips WHERE account_id=84 ORDER BY started_at"
                )).fetchall()
                assert len(rows) == 2
                await conn.execute("UPDATE detector_state SET last_run_at=NULL WHERE account_id=84")
            ready, release = asyncio.Event(), asyncio.Event()
            detector_pid = None
            original_run = detector._run

            async def hold(conn, stream):
                nonlocal detector_pid
                await original_run(conn, stream)
                detector_pid = (await (await conn.execute("SELECT pg_backend_pid()")).fetchone())[0]
                ready.set()
                await release.wait()

            monkeypatch.setattr(detector, "_run", hold)
            detector_task = asyncio.create_task(detector.run_once())
            mutation_task = None
            try:
                await asyncio.wait_for(ready.wait(), 5)
                observed = _ObservedPool(b)
                request = SimpleNamespace(
                    state=SimpleNamespace(account_pool=observed, detector_runner=detector),
                    app=SimpleNamespace(state=SimpleNamespace()),
                )
                if mutation == "batch_delete":
                    endpoint = next(route.endpoint for route in ui_router().routes
                                    if route.path == "/trips/batch_delete")
                    mutation_task = asyncio.create_task(endpoint(request, [rows[0][0]], user={}))
                else:
                    mutation_task = asyncio.create_task(_merge_trips_core(request, [row[0] for row in rows]))
                await asyncio.wait_for(observed.started.wait(), 5)
                await _wait_blocked(owner, observed.pid, detector_pid, mutation_task)
                async with owner.connection() as conn:
                    assert (await (await conn.execute("SELECT count(*) FROM trip_boundary_overrides")).fetchone())[0] == 0
                    assert (await (await conn.execute("SELECT count(*) FROM trips")).fetchone())[0] == 2
                assert await _checkpoint(owner, device) == (None, DETECTOR_VERSION)
                release.set()
                assert await asyncio.wait_for(detector_task, 5)
                await asyncio.wait_for(mutation_task, 5)
            finally:
                release.set()
                await asyncio.gather(*(task for task in (detector_task, mutation_task) if task), return_exceptions=True)
            async with b.connection() as conn:
                overrides = await (await conn.execute(
                    "SELECT kind::text,range_start,range_end FROM trip_boundary_overrides WHERE account_id=84"
                )).fetchall()
                remaining = await (await conn.execute(
                    "SELECT id,tracking_device_id FROM trips WHERE account_id=84"
                )).fetchall()
                assert len(remaining) == 1 and remaining[0][1] == device
                if mutation == "batch_delete":
                    assert remaining[0][0] == rows[1][0]
                    assert overrides == [("discard", rows[0][1], rows[0][2])]
                else:
                    assert overrides == [("suppress", rows[0][2], rows[1][1])]
            assert (await _export(a))["trips"] == []
    asyncio.run(run())


@pytest.mark.parametrize("notifications_enabled", [False, True])
def test_prepared_portable_import_export_scope_every_table_and_ignore_bundle_ownership(notifications_enabled):
    async def run():
        async with _fixture() as (owner, pools, _state, a, b):
            async with b.connection() as conn:
                await importer._apply_import(conn, _bundle("B"))
                await create_device(conn, "B private phone")
            before_b = await _export(b)
            async with a.connection() as conn:
                await conn.execute(
                    "UPDATE account_settings SET email_to=%s,email_weekly_nudge=%s,"
                    "email_digest_hour=15,ntfy_topic=%s WHERE account_id=%s",
                    ("destination@example.invalid" if notifications_enabled else "", notifications_enabled,
                     "private-topic" if notifications_enabled else "", a.principal.account_id),
                )
            bundle = _bundle("A")
            # Even a caller bypassing normalization cannot select the owner.
            bundle["account_id"] = 84
            for table in ("vehicles", "places", "tag_rules", "mileage_rates", "trips", "expenses", "odometer_readings"):
                for row in bundle[table]:
                    row["account_id"] = 84
                    row["tracking_device_id"] = 99999
            async with a.connection() as conn:
                counts = await importer._apply_import(conn, bundle)
                assert all(count == 1 for count in counts.values())
                notification_state = await (await conn.execute(
                    "SELECT email_to,email_weekly_nudge,email_digest_hour,ntfy_topic FROM account_settings "
                    "WHERE account_id=%s", (a.principal.account_id,),
                )).fetchone()
                assert notification_state == (
                    "destination@example.invalid" if notifications_enabled else "", notifications_enabled,
                    15, "private-topic" if notifications_enabled else "",
                )
                for table in ("email_deliveries", "nudge_delivery_windows", "odometer_reminder_windows"):
                    assert (await (await conn.execute(
                        f"SELECT count(*) FROM {table} WHERE account_id=%s", (a.principal.account_id,),
                    )).fetchone())[0] == 0
            exported = await _export(a)
            after_b = await _export(b)
            before_b.pop("exported_at")
            after_b.pop("exported_at")
            assert after_b == before_b
            assert exported["vehicles"][0]["name"] == "A car"
            assert exported["places"][0]["name"] == "A place"
            assert exported["trips"][0]["purpose"] == "A"
            assert exported["expenses"][0]["notes"] == "A"
            assert exported["odometer_readings"][0]["note"] == "A"
            assert len(exported["tag_rules"]) == len(exported["mileage_rates"]) == 1
            assert exported["settings"] == {"auto_assign_default_vehicle": True, "display_tz": "Asia/Tokyo"}
            serialized = json.dumps(exported)
            for forbidden in ("account_id", "tracking_device_id", "tracking_devices", "ingest_credentials", "secret_hash", "B private", "email_to", "ntfy_topic", "email_weekly_nudge"):
                assert forbidden not in serialized
            normalized, issues = normalize_bundle(exported)
            assert normalized is not None and issues == []
            async with owner.connection() as conn:
                assert (await (await conn.execute("SELECT count(*) FROM trips WHERE NOT imported OR tracking_device_id IS NOT NULL")).fetchone())[0] == 0
                assert (await (await conn.execute("SELECT bool_or(relrowsecurity OR relforcerowsecurity) FROM pg_class WHERE relname=ANY(%s)", (list(OWNED_TABLES),))).fetchone())[0] is False
            # This broad SQL deliberately sees both accounts while RLS is off:
            # the export/import result above therefore proves query scoping.
            async with pools.runtime.connection() as conn:
                assert (await (await conn.execute("SELECT count(*) FROM trips")).fetchone())[0] == 2
    asyncio.run(run())


@pytest.mark.parametrize("caller", [CONTROL_ROLE, RUNTIME_ROLE])
def test_prepared_validator_rejects_unexpected_executable_definer(caller):
    async def run():
        async with _fixture() as (owner, _pools, state, _a, _b):
            async with owner.connection() as conn:
                await conn.execute(
                    "CREATE FUNCTION public.unexpected_ledger_reader() RETURNS bigint LANGUAGE sql "
                    "SECURITY DEFINER AS $$ SELECT count(*) FROM public.trips $$"
                )
                await conn.execute("REVOKE ALL ON FUNCTION public.unexpected_ledger_reader() FROM PUBLIC")
                await conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION public.unexpected_ledger_reader() TO {}").format(sql.Identifier(caller)))
                with pytest.raises(RoleSetupError):
                    await validate_application_contract(conn, state)
                await conn.execute(sql.SQL("REVOKE EXECUTE ON FUNCTION public.unexpected_ledger_reader() FROM {}").format(sql.Identifier(caller)))
                # An unreachable function grants no new application path.
                await validate_application_contract(conn, state)
    asyncio.run(run())


@pytest.mark.parametrize("retained_work", ["override", "checkpoint"])
def test_portable_refuses_own_retained_work_and_accepts_unused_device(retained_work):
    async def run():
        async with _fixture() as (owner, _pools, _state, a, b):
            async with a.connection() as conn:
                device = (await create_device(conn, "Unused phone")).tracking_device_id
            detector = DetectorRunner(a, Params())
            assert await detector.run_once() is True
            assert await _checkpoint(owner, device) == (None, 0)

            async def retain(conn, stream):
                if retained_work == "override":
                    await conn.execute(
                        "INSERT INTO trip_boundary_overrides(account_id,tracking_device_id,device,kind,range_start,range_end) "
                        "VALUES (%s,%s,'old label','discard','2026-08-01T10:00:00Z','2026-08-01T11:00:00Z')",
                        (conn.principal.account_id, stream),
                    )
                else:
                    await conn.execute(
                        "UPDATE detector_state SET last_run_at=now(),detector_version=%s "
                        "WHERE account_id=%s AND tracking_device_id=%s",
                        (DETECTOR_VERSION, conn.principal.account_id, stream),
                    )

            async with b.connection() as conn:
                other_device = (await create_device(conn, "Other phone")).tracking_device_id
                await retain(conn, other_device)
            # Foreign retained work does not make this account dirty.
            async with a.connection() as conn:
                assert await importer._check_clean_target(conn) == {}
                async with conn.transaction(force_rollback=True):
                    assert (await importer._apply_import(conn, _bundle()))["trips"] == 1
                await retain(conn, device)
            before_a = await _export(a)
            before_b = await _export(b)
            with pytest.raises(importer.PortableImportError) as failure:
                async with a.connection() as conn:
                    await importer._apply_import(conn, _bundle())
            assert failure.value.error == "target_not_clean"
            conflict = "trip_boundary_overrides" if retained_work == "override" else "detector_state"
            assert failure.value.extra["conflicts"][conflict]["count"] == 1
            for bound, before in ((a, before_a), (b, before_b)):
                after = await _export(bound)
                before.pop("exported_at")
                after.pop("exported_at")
                assert after == before
    asyncio.run(run())


def test_idle_sweep_skips_only_pristine_streams_and_keeps_point_free_reconciliation():
    async def run():
        async with _fixture() as (owner, _pools, _state, a, _b):
            async with a.connection() as conn:
                devices = {
                    name: (await create_device(conn, name)).tracking_device_id
                    for name in ("unused", "progressed", "native", "override")
                }
                await conn.execute(
                    "UPDATE detector_state SET last_run_at='2026-08-01T00:00:00Z',detector_version=1 "
                    "WHERE account_id=%s AND tracking_device_id=%s",
                    (a.principal.account_id, devices["progressed"]),
                )
                await conn.execute(
                    "INSERT INTO trips(account_id,tracking_device_id,device,source,started_at,ended_at,distance_m) "
                    "VALUES (%s,%s,'old label','detected','2026-08-01T10:00:00Z','2026-08-01T11:00:00Z',1000)",
                    (a.principal.account_id, devices["native"]),
                )
                await conn.execute(
                    "INSERT INTO trip_boundary_overrides(account_id,tracking_device_id,device,kind,range_start,range_end) "
                    "VALUES (%s,%s,'old label','discard','2026-08-01T10:00:00Z','2026-08-01T11:00:00Z')",
                    (a.principal.account_id, devices["override"]),
                )
            assert await DetectorRunner(a, Params()).run_once() is True
            assert await _checkpoint(owner, devices["unused"]) == (None, 0)
            for name in ("progressed", "native", "override"):
                checkpoint = await _checkpoint(owner, devices[name])
                assert checkpoint[0] is not None and checkpoint[1] == DETECTOR_VERSION
            async with a.connection() as conn:
                assert (await (await conn.execute("SELECT count(*) FROM trips WHERE account_id=%s", (a.principal.account_id,))).fetchone())[0] == 0
                assert (await (await conn.execute("SELECT count(*) FROM trip_boundary_overrides WHERE account_id=%s", (a.principal.account_id,))).fetchone())[0] == 1
    asyncio.run(run())


def test_disposable_enabled_policies_deny_missing_context_and_cross_account_writes():
    async def run():
        async with _fixture() as (owner, _pools, state, a, b):
            # Use a real runtime identity without the prepared-mode startup
            # validator; only this fixture deliberately activates policies.
            runtime = make_pool(role_conninfo(TEST_DB, state, RUNTIME_ROLE))
            await runtime.open(wait=True)
            try:
                async with b.connection() as conn:
                    await importer._apply_import(conn, _bundle("B"))
                async with owner.connection() as conn:
                    foreign_vehicle, foreign_trip = await (await conn.execute(
                        "SELECT vehicle_id,id FROM trips WHERE account_id=84"
                    )).fetchone()
                    for table in OWNED_TABLES:
                        await conn.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(sql.Identifier(table)))
                        await conn.execute(sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(sql.Identifier(table)))
                bound = AccountPool(runtime, a.principal)
                async with runtime.connection() as conn:
                    assert (await (await conn.execute("SELECT count(*) FROM trips")).fetchone())[0] == 0
                    with pytest.raises(errors.InsufficientPrivilege):
                        async with conn.transaction():
                            await conn.execute("INSERT INTO vehicles(account_id,name) VALUES (84,'No context')")
                async with bound.connection() as conn:
                    assert (await (await conn.execute("SELECT count(*) FROM trips")).fetchone())[0] == 0
                    assert (await conn.execute("UPDATE trips SET notes='foreign' WHERE id=%s", (foreign_trip,))).rowcount == 0
                    with pytest.raises(errors.InsufficientPrivilege):
                        async with conn.transaction():
                            await conn.execute("INSERT INTO vehicles(account_id,name) VALUES (84,'Wrong owner')")
                    with pytest.raises(errors.ForeignKeyViolation):
                        async with conn.transaction():
                            await conn.execute(
                                "INSERT INTO expenses(account_id,vehicle_id,incurred_on,category,amount,treatment) "
                                "VALUES (%s,%s,'2026-08-01','parking',12.34,'fully_business')",
                                (a.principal.account_id, foreign_vehicle),
                            )
                    await importer._apply_import(conn, _bundle("A"))
                assert (await _export(bound))["trips"][0]["purpose"] == "A"
                async with runtime.connection() as conn:
                    assert (await (await conn.execute("SELECT NULLIF(current_setting('app.account_id',true),'')")).fetchone())[0] is None
                    assert (await (await conn.execute("SELECT count(*) FROM trips")).fetchone())[0] == 0
                async with owner.connection() as conn:
                    assert (await (await conn.execute("SELECT purpose,notes FROM trips WHERE id=%s", (foreign_trip,))).fetchone()) == ("B", "B private note")
            finally:
                await runtime.close()
    asyncio.run(run())
