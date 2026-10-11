"""`python -m app.reprocess_detector`: unlimited detector work for one account."""
from __future__ import annotations

import os
import threading

import psycopg
import pytest

import app.reprocess_detector as reprocess_detector
from app.db import DETECTOR_ACCOUNT_LOCK_CLASS_ID
from app.detector.core import Params
from app.detector.runner import DetectorDeferrals, DetectorRunner
from conftest import account_pool, add_test_account, reset_account_db, seed_tracking_device
from tests.synth import Drive, Stationary, build_track
from tests.test_detector_turn_limit_db import (
    OWNER, TRACK, _Clock, _counting, _drain, _insert_track, _outputs, _run, _snapshot,
    _stale_version,
)

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL for disposable DB tests")

DETECTOR_ENV = ("MAX_ACCURACY_M", "MAX_SPEED_MS", "STAY_RADIUS_M", "STAY_MIN_DURATION_S",
                "MIN_TRIP_DISTANCE_M", "GAP_FLAG_THRESHOLD_S", "WALK_MAX_SPEED_MS",
                "DETECTOR_TURN_MAX_POINTS")


@pytest.fixture
def command_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    for name in DETECTOR_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _split_track():
    head = build_track(TRACK)
    tail = build_track([Drive(km=3), Stationary(900), Drive(km=2), Stationary(900)],
                       start=(head[-1].lat, head[-1].lon), t0=head[-1].t)[1:]
    return head, tail


async def _incremental_fixture(raw):
    """Checkpointed history plus a dirty suffix whose rewind window is large."""
    pool = await reset_account_db(raw)
    head, tail = _split_track()
    async with pool.connection() as conn:
        device = await seed_tracking_device(conn, "phone")
        await _insert_track(conn, device, head)
    assert await DetectorRunner(pool, Params()).run_once()
    async with pool.connection() as conn:
        await _insert_track(conn, device, tail, dirty=True)
    return pool, device


async def _full_fixture(raw):
    """A version-mismatched checkpoint with no output yet: a full window."""
    pool = await reset_account_db(raw)
    async with pool.connection() as conn:
        device = await seed_tracking_device(conn, "phone")
        await _insert_track(conn, device, build_track(TRACK))
        await _stale_version(conn, device)
    return pool, device


@pytest.mark.parametrize("fixture", [_incremental_fixture, _full_fixture])
def test_command_matches_unlimited_background_run_and_clears_deferral(fixture, command_env, capsys):
    async def background(raw):
        pool, _device = await fixture(raw)
        await _drain(DetectorRunner(pool, Params()))
        async with pool.connection() as conn:
            return await _outputs(conn)

    deferrals = DetectorDeferrals(clock=_Clock())
    Runner = _counting()

    async def deferred(raw):
        pool, device = await fixture(raw)
        async with pool.connection() as conn:
            before = await _snapshot(conn)
        await Runner(pool, Params(), turn_max_points=100, deferrals=deferrals).run_turn()
        assert deferrals.devices() == ((OWNER, device),)
        async with pool.connection() as conn:
            assert await _snapshot(conn) == before
        return device

    expected = _run(background)
    device = _run(deferred)
    assert reprocess_detector.main(["--account", str(OWNER)]) == 0
    out = capsys.readouterr().out
    assert f"device {device}: " in out and "points processed" in out

    async def after(raw):
        pool = await account_pool(raw, OWNER)
        async with pool.connection() as conn:
            outputs = await _outputs(conn)
            # A late fix after the command: the changed checkpoint clears the
            # deferral before its recheck and the small window runs normally.
            # Older receipts move behind the checkpoint's overlap margin first.
            await conn.execute("UPDATE points SET received_at='2020-01-01'")
            last = (await (await conn.execute(
                "SELECT recorded_at FROM points ORDER BY recorded_at DESC LIMIT 1")).fetchone())[0]
            await conn.execute(
                "INSERT INTO points(account_id,tracking_device_id,device,recorded_at,geom,accuracy_m) "
                "SELECT account_id,tracking_device_id,device,%s + interval '15 seconds',geom,accuracy_m "
                "FROM points WHERE recorded_at=%s", (last, last))
        counts = Runner.counts
        outcome = await Runner(pool, Params(), turn_max_points=100, deferrals=deferrals).run_turn()
        assert outcome.batch.completed == 1
        assert Runner.counts == counts + 1
        assert deferrals.devices() == ()
        return outputs

    assert _run(after) == expected


def test_command_runs_only_the_named_device_and_reports_no_pending_work(command_env, capsys):
    async def body(raw):
        pool = await reset_account_db(raw)
        async with pool.connection() as conn:
            named = await seed_tracking_device(conn, "named")
            other = await seed_tracking_device(conn, "other")
            idle = await seed_tracking_device(conn, "idle")
            await _insert_track(conn, named, build_track(TRACK))
            await _insert_track(conn, other, build_track(TRACK))
        return named, other, idle

    named, other, idle = _run(body)
    assert reprocess_detector.main(["--account", str(OWNER), "--device", str(named)]) == 0
    assert "device %d: " % named in capsys.readouterr().out

    async def states(raw):
        async with raw.connection() as conn:
            return dict(await (await conn.execute(
                "SELECT tracking_device_id,last_run_at IS NOT NULL FROM detector_state")).fetchall())

    assert _run(states) == {named: True, other: False, idle: False}
    # An unused device gains no checkpoint history from an explicit request.
    assert reprocess_detector.main(["--account", str(OWNER), "--device", str(idle)]) == 0
    assert capsys.readouterr().out.strip() == f"device {idle}: no pending work"
    assert reprocess_detector.main(["--account", str(OWNER), "--device", str(named)]) == 0
    assert capsys.readouterr().out.strip() == f"device {named}: no pending work"
    assert _run(states) == {named: True, other: False, idle: False}
    assert reprocess_detector.main(["--account", str(OWNER)]) == 0
    assert f"device {other}: " in capsys.readouterr().out
    assert reprocess_detector.main(["--account", str(OWNER)]) == 0
    assert capsys.readouterr().out.strip() == f"account {OWNER}: no pending work"


def test_command_refuses_unknown_or_disabled_accounts_and_unknown_devices(command_env, capsys):
    async def body(raw):
        await reset_account_db(raw)
        await add_test_account(raw, 99)
        async with raw.connection() as conn:
            await conn.execute("UPDATE accounts SET is_enabled=false WHERE id=99")

    _run(body)
    assert reprocess_detector.main(["--account", "12345"]) == 1
    assert "No account has that ID" in capsys.readouterr().err
    assert reprocess_detector.main(["--account", "99"]) == 1
    assert "disabled" in capsys.readouterr().err
    assert reprocess_detector.main(["--account", str(OWNER), "--device", "777"]) == 1
    assert "No enabled tracking device" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exit_info:
        reprocess_detector.main(["--account", "abc"])
    assert exit_info.value.code == 2
    capsys.readouterr()
    command_env.delenv("DATABASE_URL")
    assert reprocess_detector.main(["--account", str(OWNER)]) == 1
    assert "DATABASE_URL" in capsys.readouterr().err


def test_command_failure_exits_nonzero_and_keeps_the_device_checkpoint(command_env, capsys):
    async def body(raw):
        pool = await reset_account_db(raw)
        async with pool.connection() as conn:
            device = await seed_tracking_device(conn, "phone")
            await _insert_track(conn, device, build_track(TRACK))
        return device

    device = _run(body)

    async def broken(self, conn, device, dirty_from, full):
        await conn.execute("UPDATE detector_state SET detector_version=99")
        raise RuntimeError("synthetic failure with private detail")

    command_env.setattr(DetectorRunner, "_process_device", broken)
    assert reprocess_detector.main(["--account", str(OWNER)]) == 1
    err = capsys.readouterr().err
    assert f"device {device}: reprocess failed (RuntimeError)" in err
    assert "private detail" not in err and TEST_DB not in err

    async def state(raw):
        async with raw.connection() as conn:
            return await (await conn.execute(
                "SELECT last_run_at,detector_version FROM detector_state")).fetchone()

    assert tuple(_run(state)) == (None, 0)


def test_command_waits_for_the_account_detector_lock_without_a_lock_timeout(command_env, capsys):
    async def body(raw):
        pool = await reset_account_db(raw)
        async with pool.connection() as conn:
            device = await seed_tracking_device(conn, "phone")
            await _insert_track(conn, device, build_track(TRACK))
        return device

    device = _run(body)
    holder = psycopg.connect(TEST_DB)
    try:
        holder.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s::text))",
                       (DETECTOR_ACCOUNT_LOCK_CLASS_ID, OWNER))
        result = {}
        thread = threading.Thread(
            target=lambda: result.setdefault("code", reprocess_detector.main(["--account", str(OWNER)])))
        thread.start()
        # Longer than the serving 1s lock timeout, so a bounded wait would fail.
        thread.join(2.5)
        assert thread.is_alive()
        holder.rollback()
        thread.join(30)
        assert result == {"code": 0}
    finally:
        holder.close()
    assert f"device {device}: " in capsys.readouterr().out
