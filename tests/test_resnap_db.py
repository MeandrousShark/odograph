"""`python -m app.resnap`: queue split road snaps to be snapped again."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

import httpx
import psycopg
import pytest

import app.resnap as resnap
from app.account_context import account_id
from app.snap import SnapWorker
from conftest import account_pool, add_test_account, reset_account_db, seed_tracking_device
from tests.test_detector_turn_limit_db import OWNER, _run

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL for disposable DB tests")

OTHER = 99
SPLIT = "MULTILINESTRING((-122.33 47.60,-122.32 47.61),(-122.31 47.62,-122.30 47.63))"
SINGLE = "MULTILINESTRING((-122.33 47.60,-122.30 47.63))"


@pytest.fixture
def command_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-long-enough-for-config")
    return monkeypatch


async def _trip(conn, device, geom, *, source="detected", imported=False, status="ok"):
    cur = await conn.execute(
        "INSERT INTO trips(account_id,tracking_device_id,device,source,imported,started_at,ended_at,"
        " distance_m,point_count,detector_version,snap_status,snap_attempted_at,"
        " path_snapped,distance_snapped_m,snapped_at) "
        "VALUES(%s,%s,'phone',%s,%s,now(),now(),1000,10,2,%s::snap_state,"
        " CASE WHEN %s::text IS NULL THEN NULL ELSE now() END,"
        " ST_GeomFromText(%s,4326),CASE WHEN %s::text IS NULL THEN NULL ELSE 900 END,"
        " CASE WHEN %s::text IS NULL THEN NULL ELSE now() END) RETURNING id",
        (account_id(conn), device, source, imported, status, geom, geom, geom, geom))
    return (await cur.fetchone())[0]


async def _points(conn, device, count=60):
    """Retained fixes that the stored trips' storage envelope is checked against."""
    await conn.execute(
        "INSERT INTO points(account_id,tracking_device_id,device,recorded_at,received_at,geom,accuracy_m) "
        "SELECT %s,%s,'phone',now() - make_interval(secs => g),now() - make_interval(secs => g),"
        " ST_SetSRID(ST_MakePoint(-122.33,47.60),4326)::geography,10 FROM generate_series(1,%s) g",
        (account_id(conn), device, count))


async def _fixture(raw):
    """Two devices for the owner, one for another account, mixed trips."""
    pool = await reset_account_db(raw)
    other_pool = await add_test_account(raw, OTHER)
    ids = {}
    async with pool.connection() as conn:
        a = await seed_tracking_device(conn, "a")
        b = await seed_tracking_device(conn, "b")
        await _points(conn, a)
        await _points(conn, b)
        ids["split_a"] = await _trip(conn, a, SPLIT)
        ids["split_a2"] = await _trip(conn, a, SPLIT, status="low_confidence")
        ids["split_b"] = await _trip(conn, b, SPLIT)
        ids["single"] = await _trip(conn, a, SINGLE)
        ids["unsnapped"] = await _trip(conn, a, None, status="pending")
        ids["manual"] = await _trip(conn, a, SPLIT, source="manual")
        ids["imported"] = await _trip(conn, a, SPLIT, imported=True)
    async with other_pool.connection() as conn:
        c = await seed_tracking_device(conn, "c")
        await _points(conn, c)
        ids["foreign"] = await _trip(conn, c, SPLIT)
    return pool, ids, a, b


async def _state(raw):
    async with raw.connection() as conn:
        cur = await conn.execute(
            "SELECT id, snap_status::text, path_snapped IS NULL, distance_snapped_m, snapped_at IS NULL,"
            " snap_attempted_at IS NULL, snap_capacity_needed_bytes FROM trips ORDER BY id")
        return {row[0]: row[1:] for row in await cur.fetchall()}


async def _setup(raw):
    _pool, ids, *_ = await _fixture(raw)
    return ids


def _requeued(row):
    return row == ("pending", True, None, True, True, 0)


def test_requeues_only_split_detected_trips_of_the_named_account(command_env, capsys):
    ids = _run(_setup)
    before = _run(_state)
    assert resnap.main(["--account", str(OWNER)]) == 0
    assert capsys.readouterr().out.strip() == f"account {OWNER}: 3 trips queued for snapping"
    after = _run(_state)
    for name in ("split_a", "split_a2", "split_b"):
        assert _requeued(after[ids[name]]), name
    for name in ("single", "unsnapped", "manual", "imported", "foreign"):
        assert after[ids[name]] == before[ids[name]], name
    # A repeat finds nothing left to do.
    assert resnap.main(["--account", str(OWNER)]) == 0
    assert capsys.readouterr().out.strip() == f"account {OWNER}: no split trips"
    assert _run(_state) == after


def test_device_narrows_the_requeue_and_singular_count(command_env, capsys):
    async def setup(raw):
        _pool, ids, a, b = await _fixture(raw)
        return ids, a, b

    ids, a, b = _run(setup)
    assert resnap.main(["--account", str(OWNER), "--device", str(b)]) == 0
    assert capsys.readouterr().out.strip() == f"account {OWNER}: 1 trip queued for snapping"
    after = _run(_state)
    assert _requeued(after[ids["split_b"]])
    assert not _requeued(after[ids["split_a"]]) and not _requeued(after[ids["split_a2"]])
    assert resnap.main(["--account", str(OWNER), "--device", str(b)]) == 0
    assert capsys.readouterr().out.strip() == f"account {OWNER}: no split trips"
    assert resnap.main(["--account", str(OWNER), "--device", str(a)]) == 0
    assert "2 trips queued" in capsys.readouterr().out


async def _stop_device(raw, device, *, revoke):
    async with raw.connection() as conn:
        if revoke:
            await conn.execute("UPDATE tracking_devices SET revoked_at=now() WHERE id=%s", (device,))
        else:
            await conn.execute("UPDATE tracking_devices SET enabled=false WHERE id=%s", (device,))


@pytest.mark.parametrize("revoke", [False, True])
def test_trips_on_a_disabled_or_revoked_device_are_not_queued(command_env, capsys, revoke):
    async def setup(raw):
        _pool, ids, a, b = await _fixture(raw)
        await _stop_device(raw, b, revoke=revoke)
        return ids

    ids = _run(setup)
    before = _run(_state)
    assert resnap.main(["--account", str(OWNER)]) == 0
    assert capsys.readouterr().out.strip() == f"account {OWNER}: 2 trips queued for snapping"
    after = _run(_state)
    assert _requeued(after[ids["split_a"]]) and _requeued(after[ids["split_a2"]])
    assert after[ids["split_b"]] == before[ids["split_b"]]


@pytest.mark.parametrize("revoke,word", [(False, "disabled"), (True, "revoked")])
def test_naming_a_disabled_or_revoked_device_is_refused(command_env, capsys, revoke, word):
    async def setup(raw):
        _pool, ids, a, b = await _fixture(raw)
        await _stop_device(raw, b, revoke=revoke)
        return b

    b = _run(setup)
    before = _run(_state)
    assert resnap.main(["--account", str(OWNER), "--device", str(b)]) == 1
    captured = capsys.readouterr()
    assert f"That tracking device is {word}; its trips were not queued." in captured.err
    assert "queued for snapping" not in captured.out
    assert _run(_state) == before


def test_refuses_unknown_disabled_account_and_unknown_device(command_env, capsys):
    async def setup(raw):
        await _fixture(raw)
        async with raw.connection() as conn:
            await conn.execute("UPDATE accounts SET is_enabled=false WHERE id=%s", (OTHER,))

    _run(setup)
    before = _run(_state)
    assert resnap.main(["--account", "12345"]) == 1
    assert "No account has that ID" in capsys.readouterr().err
    assert resnap.main(["--account", str(OTHER)]) == 1
    assert "disabled" in capsys.readouterr().err
    assert resnap.main(["--account", str(OWNER), "--device", "777"]) == 1
    assert "No tracking device" in capsys.readouterr().err
    assert _run(_state) == before
    command_env.delenv("DATABASE_URL")
    assert resnap.main(["--account", str(OWNER)]) == 1
    assert "DATABASE_URL" in capsys.readouterr().err


def test_a_device_of_another_account_is_not_found(command_env, capsys):
    async def setup(raw):
        _pool, ids, a, b = await _fixture(raw)
        async with raw.connection() as conn:
            return (await (await conn.execute(
                "SELECT id FROM tracking_devices WHERE account_id=%s", (OTHER,))).fetchone())[0], ids

    foreign_device, ids = _run(setup)
    before = _run(_state)
    assert resnap.main(["--account", str(OWNER), "--device", str(foreign_device)]) == 1
    assert "No tracking device" in capsys.readouterr().err
    assert _run(_state) == before


@pytest.mark.parametrize("message,expected", [
    ("storage capacity exceeded: account allowance", "storage is full for this account"),
    ("unrelated failure with private detail", "queueing failed (RaiseException)"),
])
def test_update_refusals_are_reported_without_detail_and_save_nothing(command_env, capsys, message, expected):
    _run(_setup)
    before = _run(_state)
    real = resnap.external_account_work

    @asynccontextmanager
    async def with_refusing_trigger(control, owner):
        # Installed once the command's pools passed their role-contract check,
        # and removed before the test ends.
        async with real(control, owner):
            with psycopg.connect(TEST_DB, autocommit=True) as admin:
                admin.execute(
                    "CREATE FUNCTION public.refuse_resnap() RETURNS trigger LANGUAGE plpgsql AS "
                    f"$$ BEGIN RAISE EXCEPTION '{message}'; END $$")
                admin.execute("CREATE TRIGGER refuse_resnap BEFORE UPDATE ON trips "
                              "FOR EACH ROW EXECUTE FUNCTION public.refuse_resnap()")
            try:
                yield
            finally:
                with psycopg.connect(TEST_DB, autocommit=True) as admin:
                    admin.execute("DROP TRIGGER IF EXISTS refuse_resnap ON trips")
                    admin.execute("DROP FUNCTION IF EXISTS public.refuse_resnap()")

    command_env.setattr(resnap, "external_account_work", with_refusing_trigger)
    assert resnap.main(["--account", str(OWNER)]) == 1
    captured = capsys.readouterr()
    assert expected in captured.err
    assert "private detail" not in captured.err and TEST_DB not in captured.err
    assert "queued" not in captured.out
    assert _run(_state) == before


def test_unexpected_failure_hides_database_text(command_env, capsys):
    _run(_setup)

    async def broken(*args, **kwargs):
        raise psycopg.errors.InternalError("synthetic failure with private detail")

    command_env.setattr(resnap, "_resnap", broken)
    assert resnap.main(["--account", str(OWNER)]) == 1
    err = capsys.readouterr().err
    assert "resnap failed (InternalError)" in err
    assert "private detail" not in err


def test_snap_worker_picks_up_each_requeued_trip(command_env):
    async def setup(raw):
        pool, ids, a, b = await _fixture(raw)
        async with pool.connection() as conn:
            await conn.execute("UPDATE trips SET snap_status='ok' WHERE snap_status='pending'")
        async with httpx.AsyncClient() as client:
            assert await SnapWorker(pool, client, "http://osrm", 0.5, 250)._next_trip() is None
        return ids

    ids = _run(setup)
    assert resnap.main(["--account", str(OWNER)]) == 0

    async def drain(raw):
        pool = await account_pool(raw, OWNER)
        picked = []
        async with httpx.AsyncClient() as client:
            worker = SnapWorker(pool, client, "http://osrm", 0.5, 250)
            while (row := await worker._next_trip()) is not None:
                trip_id, delay, _age, needed = row
                assert delay == 0 and needed == 0
                picked.append(trip_id)
                async with pool.connection() as conn:
                    await conn.execute("UPDATE trips SET snap_status='ok' WHERE id=%s", (trip_id,))
        return picked

    assert sorted(_run(drain)) == sorted(ids[name] for name in ("split_a", "split_a2", "split_b"))
