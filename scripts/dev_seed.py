#!/usr/bin/env python3
"""Seed a DISPOSABLE dev/QA database with synthetic data.

**For throwaway Postgres/PostGIS instances only.** This script wipes and
reseeds every data table it touches (see `_WIPE_TABLES` below) -- never point
it at production or any database whose contents matter. It refuses any
`--database-url` whose host isn't a loopback address, as a cheap guard
against an obvious mistake; that check is not a substitute for pointing this
at a real disposable container.

Detected trips
here are not inserted as finished `trips` rows: this script builds plausible
raw GPS traces (stay -> drive -> stay, ~30-60s fix intervals, mild jitter,
plausible speeds) for one synthetic device and runs the *real* detector
(`app.detector.runner.DetectorRunner`) over them, the same way
`tests/test_runner_db.py` drives it. That gives every detected trip genuine
points, geometry, boundaries, and detector ownership, so the QA site can
exercise the detail map, merge, split, and snapping states end to end -- not
just category/purpose text fields on a bare row.

The trace-generation segment types and math (`Stationary`/`Drive`/`Gap`,
`_offset`/`_travel`) are adapted from `tests/synth.py`'s synthetic-track
builder. This script cannot import `tests/synth.py`
directly (it must run standalone, outside the test suite's import path), so
the minimal generation logic is copied here rather than shared. The one
deliberate difference: fix intervals are drawn per-point from a `[30, 60]`
second window (typical OwnTracks move-triggered cadence) instead of a fixed
cadence, since this script's job is a realistic-looking dataset, not
exact-boundary unit-test determinism.

A fixed RNG seed keeps jitter reproducible; the calendar anchor is always
"now" (so re-running always populates the current week), which is why the
dataset is not byte-for-byte identical run to run even though it is
structurally deterministic.

No OSRM/geocoding/ntfy dependency: `OSRM_URL` is never read here, so every
detected trip's `snap_status` is left at whatever the detector itself
produces (`'pending'`) rather than forced to a terminal state.

Usage:

    .venv/bin/python scripts/dev_seed.py \\
        --database-url postgresql://mileage:pw@127.0.0.1:55432/mileage \\
        --wipe

Both `--database-url` and `--wipe` are required (no default URL, no
`DATABASE_URL` environment fallback) so this can never run against whatever
happens to be configured in the current shell by accident.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import random
import sys
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

# `python scripts/dev_seed.py` puts scripts/ (not the repo root) first on
# sys.path, so the documented invocation from the module docstring could not
# import `app` without this bootstrap.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.dashboard import week_bounds
from app.db import make_pool, run_migrations
from app.account_context import AccountPool, AccountPrincipal, account_id, control_connection
from app.accounts import create_admin, account_exists, get_account_by_email
from app.application_roles import application_role_pools
from app.local_auth import hash_password
from app.detector.core import Params, Point, haversine_m
from app.detector.runner import DetectorRunner
from app.rates import METERS_PER_MILE

SEED = 5150176  # fixed: reproducible jitter across runs (arbitrary constant)
PACIFIC = ZoneInfo("America/Los_Angeles")
DEVICE = "QA-IPHONE"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Delete only this synthetic account's rows, in foreign-key dependency order.
# Rates and tracker identity survive reseeding; their values/history ownership
# are never borrowed from another account.
_WIPE_TABLES = [
    "trip_boundary_overrides", "expenses", "odometer_readings", "points",
    "stays", "trips", "tag_rules", "vehicles", "places", "geocode_cache",
    "raw_messages", "nudge_delivery_windows", "odometer_reminder_windows", "email_deliveries",
]


# --- adapted synthetic-track builder (see module docstring) ----------------

M_PER_DEG_LAT = 111_320.0


@dataclass
class Stationary:
    duration_s: float
    jitter_m: float = 8.0
    silent: bool = False


@dataclass
class Drive:
    km: float
    speed_kmh: float = 45.0
    bearing_deg: float = 90.0
    jitter_m: float = 5.0


@dataclass
class Gap:
    """No emissions; time passes and (optionally) position changes -- used
    both for multi-week "parked, nothing recorded" idle spans between daily
    traces and, mid-drive with a nonzero `move_km`, for a deliberate
    recording-gap trip.
    """
    duration_s: float
    move_km: float = 0.0
    bearing_deg: float = 90.0


def _offset(lat: float, lon: float, east_m: float, north_m: float) -> tuple[float, float]:
    return (
        lat + north_m / M_PER_DEG_LAT,
        lon + east_m / (M_PER_DEG_LAT * math.cos(math.radians(lat))),
    )


def _travel(lat: float, lon: float, dist_m: float, bearing_deg: float) -> tuple[float, float]:
    b = math.radians(bearing_deg)
    return _offset(lat, lon, math.sin(b) * dist_m, math.cos(b) * dist_m)


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Standard initial-bearing (forward azimuth) formula, using the same
    0=north/90=east-clockwise convention as `_travel`'s `bearing_deg` -- lets
    `leg()` below compute an exact course between any two named coordinates
    without ever needing to hand-derive a "reverse" bearing.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlmb = math.radians(lon2 - lon1)
    x = math.sin(dlmb) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlmb)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def leg(a: tuple[float, float], b: tuple[float, float], speed_kmh: float = 45.0) -> Drive:
    """A `Drive` segment from named point `a` to named point `b`, with the
    distance/bearing computed fresh from their coordinates rather than
    tracked through however each point was originally derived -- this is what
    lets every block below just say "drive from X to Y" regardless of
    whether X or Y was defined relative to the other.
    """
    dist_m = haversine_m(a[0], a[1], b[0], b[1])
    bearing = _bearing_deg(a[0], a[1], b[0], b[1])
    return Drive(km=dist_m / 1000.0, speed_kmh=speed_kmh, bearing_deg=bearing)


def build_track(
    segments,
    start: tuple[float, float],
    t0: datetime,
    rng: random.Random,
    min_interval_s: float = 30.0,
    max_interval_s: float = 60.0,
) -> list[Point]:
    lat, lon = start
    points: list[Point] = []

    def emit(t: datetime, plat: float, plon: float, vel_kmh: float, jitter_m: float) -> None:
        jlat, jlon = _offset(plat, plon, rng.gauss(0, jitter_m), rng.gauss(0, jitter_m))
        points.append(Point(t=t, lat=jlat, lon=jlon, accuracy_m=10.0, velocity_kmh=vel_kmh))

    def next_interval() -> float:
        return rng.uniform(min_interval_s, max_interval_s)

    t_last = t0
    emit(t_last, lat, lon, 0.0, 8.0)  # anchor fix at track start

    for seg in segments:
        if isinstance(seg, Stationary):
            end = t_last + timedelta(seconds=seg.duration_s)
            if seg.silent:
                emit(end, lat, lon, 0.0, seg.jitter_m)
            else:
                t = t_last + timedelta(seconds=next_interval())
                while t <= end:
                    emit(t, lat, lon, 0.0, seg.jitter_m)
                    t += timedelta(seconds=next_interval())
                if points[-1].t < end:
                    emit(end, lat, lon, 0.0, seg.jitter_m)
            t_last = end
        elif isinstance(seg, Drive):
            speed_ms = seg.speed_kmh / 3.6
            total_m = seg.km * 1000.0
            duration = total_m / speed_ms if speed_ms else 0.0
            elapsed = next_interval()
            while elapsed <= duration:
                d = speed_ms * elapsed
                plat, plon = _travel(lat, lon, d, seg.bearing_deg)
                emit(t_last + timedelta(seconds=elapsed), plat, plon, seg.speed_kmh, seg.jitter_m)
                elapsed += next_interval()
            end_t = t_last + timedelta(seconds=duration)
            if not points or points[-1].t < end_t:
                plat, plon = _travel(lat, lon, total_m, seg.bearing_deg)
                emit(end_t, plat, plon, seg.speed_kmh, seg.jitter_m)
            lat, lon = _travel(lat, lon, total_m, seg.bearing_deg)
            t_last = end_t
        elif isinstance(seg, Gap):
            lat, lon = _travel(lat, lon, seg.move_km * 1000.0, seg.bearing_deg)
            t_last += timedelta(seconds=seg.duration_s)
        else:
            raise TypeError(f"unknown segment {seg!r}")

    return points


# --- named places (see module docstring: forward-derived, so the trace
# visits these exact coordinates and `leg()` can always route between any
# two of them) -------------------------------------------------------------

HOME = (45.5000, -122.6000)
WORK = _travel(*HOME, 9000, 75)      # ~9km ENE: primary office
CLIENT = _travel(*WORK, 5000, 20)    # ~5km NNE of WORK: client site (kind=work too)
GYM = _travel(*HOME, 2500, 200)      # ~2.5km SSW of home: kind=other, no autotag rule
ERRAND = _travel(*HOME, 18000, 300)  # ~18km NW: unregistered destination
AWAY = _travel(*WORK, 25000, 250)    # ~25km SW of WORK: the "untracked relocation" endpoint
AWAY_2 = _travel(*AWAY, 6000, 80)    # a further, equally unregistered hop


def _block_segments():
    """Every historical/current-week driving day, as `(days_ago, segments,
    detected_trip_count)` tuples (`days_ago=None` means "anchor in the
    current local week", resolved by `build_all_points`).

    Each block starts where the previous one ended (always HOME, except
    block 4 which resolves its own return leg with `leg(AWAY_2, HOME)`), so
    the multi-day/week idle span between blocks always collapses into one
    long stay rather than surfacing as a spurious cross-block "trip" -- the
    detector's stay-merge logic (`_merge_boundary_splits`,
    app/detector/core.py) only tells two nearby-in-space stays apart from one
    long one by physical distance, not by which script call produced the
    points.

    Block 3's HOME->ERRAND leg is deliberately split with a mid-drive `Gap`
    whose speed stays well above `walk_max_speed_ms` (so it isn't mistaken
    for an on-foot stay) but represents an 11-minute recording dropout --
    the one required "recording-gap trip" (`has_gap=True`).

    Block 4's WORK->AWAY `Gap` is the deliberate spatial-gap scenario: it
    produces a real (if silly-looking, hour-long) detected trip bridging
    two real places, which `_discard_missing_trip_bridge` explicitly discards
    via a `trip_boundary_overrides` 'discard' row right after the first
    detection pass -- modeling a user deleting an obviously-bogus GPS artifact and
    leaving behind exactly the kind of spatial gap the missing-trip badge
    (app/missing_trip.py) exists to flag on the very next real trip.
    """
    commute = [Stationary(600), leg(HOME, WORK), Stationary(1800),
               leg(WORK, HOME), Stationary(600)]

    client_day = [
        Stationary(600), leg(HOME, WORK), Stationary(900),
        leg(WORK, CLIENT), Stationary(1800), leg(CLIENT, WORK), Stationary(900),
        leg(WORK, HOME), Stationary(600),
    ]

    errand_full = leg(HOME, ERRAND)
    first_km, gap_km = 8.0, 8.75
    remaining_km = max(errand_full.km - first_km - gap_km, 0.5)
    recording_gap_day = [
        Stationary(600),
        Drive(km=first_km, bearing_deg=errand_full.bearing_deg),
        Gap(duration_s=700, move_km=gap_km, bearing_deg=errand_full.bearing_deg),
        Drive(km=remaining_km, bearing_deg=errand_full.bearing_deg),
        Stationary(900),
        leg(ERRAND, HOME), Stationary(600),
    ]

    missing_trip_day = [
        Stationary(600), leg(HOME, WORK), Stationary(900),
        Gap(duration_s=3600, move_km=25, bearing_deg=250),  # WORK -> AWAY, silent
        Stationary(900),
        leg(AWAY, AWAY_2), Stationary(900),
        leg(AWAY_2, HOME), Stationary(600),
    ]

    current_week_day = [
        Stationary(600), leg(HOME, GYM), Stationary(1200), leg(GYM, HOME), Stationary(600),
        leg(HOME, WORK), Stationary(1800), leg(WORK, HOME), Stationary(600),
    ]

    client_only_day = [
        Stationary(600), leg(HOME, CLIENT), Stationary(1800), leg(CLIENT, HOME), Stationary(600),
    ]

    return [
        (100, commute, 2),
        (78, client_day, 4),
        (55, recording_gap_day, 2),
        (32, missing_trip_day, 4),
        (12, client_only_day, 2),
        (None, current_week_day, 4),  # None: anchored below, in the current week
    ]


def build_all_points(now_utc: datetime) -> list[Point]:
    rng = random.Random(SEED)
    pacific_now = now_utc.astimezone(PACIFIC)
    today = pacific_now.date()

    week_start = week_bounds(today, PACIFIC).start
    current_anchor = max(week_start, pacific_now - timedelta(hours=6))

    points: list[Point] = []
    for days_ago, segments, _count in _block_segments():
        if days_ago is None:
            t0 = current_anchor
        else:
            anchor_date = today - timedelta(days=days_ago)
            t0 = datetime(anchor_date.year, anchor_date.month, anchor_date.day, 8, 0, tzinfo=PACIFIC)
        points.extend(build_track(segments, start=HOME, t0=t0, rng=rng))
    return points


# --- DB helpers --------------------------------------------------------

async def _insert_points(conn, points: list[Point], device_id: int) -> None:
    for p in points:
        await conn.execute(
            "INSERT INTO points (account_id, tracking_device_id, device, recorded_at, received_at, geom, "
            " accuracy_m, velocity_kmh) "
            "VALUES (%s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s)",
            (account_id(conn), device_id, DEVICE, p.t, p.t, p.lon, p.lat, p.accuracy_m, p.velocity_kmh),
        )


async def _wipe(conn) -> None:
    for table in _WIPE_TABLES:
        await conn.execute(f"DELETE FROM {table} WHERE account_id=%s", (account_id(conn),))
    # Re-seed the same two default kind-based rules migration 003 installs
    # once on a fresh DB -- `tag_rules` is in `_WIPE_TABLES` so this script
    # can reseed it identically on every run rather than depending on
    # whichever rows happened to survive from a prior run.
    await conn.execute(
        "INSERT INTO tag_rules (account_id, a_kind, b_kind, category) VALUES "
        "(%s, 'home', 'work', 'personal'), (%s, 'work', 'work', 'business')",
        (account_id(conn), account_id(conn))
    )
    # Force the next run_once() to take the full-reprocess path regardless
    # of what a *previous* run of this script left in detector_state: a
    # stale last_run_at would make the dirty-window query (app/detector/
    # runner.py's _run) skip every point whose received_at predates it,
    # silently dropping this run's older synthetic history.
    await conn.execute(
        "UPDATE detector_state SET last_run_at = NULL, detector_version = 0 WHERE account_id=%s", (account_id(conn),)
    )


async def _seed_reference_data(conn) -> tuple[int, int]:
    await conn.execute(
        "INSERT INTO places (account_id, name, kind, geom, radius_m) VALUES "
        "(%s, 'Home', 'home', ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, 200), "
        "(%s, 'Main Office', 'work', ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, 200), "
        "(%s, 'Client Site', 'work', ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, 200), "
        "(%s, 'Gym', 'other', ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, 150)",
        (
            account_id(conn), HOME[1], HOME[0], account_id(conn), WORK[1], WORK[0],
            account_id(conn), CLIENT[1], CLIENT[0], account_id(conn), GYM[1], GYM[0],
        ),
    )

    cur = await conn.execute(
        "INSERT INTO vehicles (account_id, name, make, model, is_default, active) VALUES "
        "(%s, 'Seed Sedan', 'Honda', 'Accord', true, true) RETURNING id", (account_id(conn),)
    )
    v1 = (await cur.fetchone())[0]
    cur = await conn.execute(
        "INSERT INTO vehicles (account_id, name, make, model, is_default, active) VALUES "
        "(%s, 'Retired Wagon', 'Subaru', 'Outback', false, false) RETURNING id", (account_id(conn),)
    )
    v2 = (await cur.fetchone())[0]
    return v1, v2


async def _seed_mileage_rates(conn, years: set[int]) -> None:
    """migrations/002_mileage_rates.sql and 005_midyear_rates.sql already
    seed 2025/2026; this fills in any *other* year the synthetic trips land
    in (relevant only when this script is run near a year boundary) so the
    dashboard's deduction estimate is never "unavailable" for lack of a
    configured rate. `ON CONFLICT DO NOTHING` leaves the migration's own
    2025/2026 values alone; the placeholder formula for any other year is
    not meant to be historically accurate, only present.
    """
    for year in sorted(years):
        placeholder_rate = round(0.67 + 0.01 * (year - 2024), 4)
        await conn.execute(
            "INSERT INTO mileage_rates (account_id, year, rate_per_mi) VALUES (%s, %s, %s) "
            "ON CONFLICT (account_id, year) DO NOTHING",
            (account_id(conn), year, placeholder_rate),
        )


async def _detect_all(runner: DetectorRunner) -> None:
    ran = await runner.run_once()
    assert ran, "detector run_once() skipped (advisory lock busy) on a fresh DB"


async def _discard_missing_trip_bridge(pool, runner: DetectorRunner, device_id: int) -> None:
    """Find the WORK->AWAY artifact trip block 4's silent relocation `Gap`
    produces and discard it (see `_block_segments`'s docstring) so the
    surviving AWAY->AWAY_2 trip is left with a genuine, undetected-drive-
    shaped spatial gap behind it for the missing-trip badge to flag.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, started_at, ended_at FROM trips WHERE account_id=%s AND tracking_device_id=%s "
            "AND source = 'detected' "
            "AND ST_DWithin(start_geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, 300) "
            "AND ST_DWithin(end_geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, 300)",
            (account_id(conn), device_id, WORK[1], WORK[0], AWAY[1], AWAY[0]),
        )
        row = await cur.fetchone()
        if row is None:
            raise RuntimeError(
                "expected a detected WORK->AWAY bridging trip to discard; "
                "the synthetic trace or detector params changed underneath this script"
            )
        trip_id, started_at, ended_at = row
        await conn.execute(
            "INSERT INTO trip_boundary_overrides (account_id, tracking_device_id, device, kind, range_start, range_end) "
            "VALUES (%s, %s, %s, 'discard', %s, %s) ON CONFLICT DO NOTHING",
            (account_id(conn), device_id, DEVICE, started_at, ended_at),
        )
        await runner.reprocess_device_in(conn, device_id)
        cur = await conn.execute("SELECT 1 FROM trips WHERE account_id=%s AND id=%s", (account_id(conn),trip_id))
        if await cur.fetchone() is not None:
            raise RuntimeError("discard override did not remove the bridging trip")


async def _fetch_device_trips(conn, device_id: int) -> list[dict]:
    cur = await conn.execute(
        "SELECT id, started_at, ended_at, category::text AS category FROM trips "
        "WHERE account_id=%s AND tracking_device_id=%s AND source = 'detected' ORDER BY started_at",
        (account_id(conn),device_id),
    )
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, row)) for row in await cur.fetchall()]


async def _apply_trip_overrides(conn, trips: list[dict], v1: int, v2: int) -> None:
    """Layer human/vehicle detail onto the detector's own output.

    Applied only after detection has fully settled (including the discard
    reprocess above) so nothing here risks being touched by a later
    `_process_device` pass -- which only ever rewrites detector-owned columns
    (`app/detector/runner.py`'s `_write_trip` docstring), never
    category/tag_source/purpose/vehicle_id, but keeping the ordering
    explicit avoids having to reason about that on every future edit here.
    """
    expected = [2, 4, 2, 3, 2, 4]
    if sum(expected) != len(trips):
        raise RuntimeError(
            f"expected {sum(expected)} detected trips across all blocks, got {len(trips)} "
            "-- the synthetic trace no longer matches this script's block-count assumptions"
        )

    def slice_block(i: int) -> list[dict]:
        start = sum(expected[:i])
        return trips[start:start + expected[i]]

    block1, block2, block3, block4, block5, block6 = (slice_block(i) for i in range(6))

    async def vehicle(trip_id: int, vehicle_id: int | None) -> None:
        await conn.execute(
            "UPDATE trips SET vehicle_id = %s WHERE account_id=%s AND id = %s", (vehicle_id, account_id(conn), trip_id)
        )

    for t in block1 + block2 + block5:
        await vehicle(t["id"], v1)
    for t in block6:
        await vehicle(t["id"], v1)

    # Block 3: the recording-gap trip, human-categorized on the (soon-
    # inactive) second vehicle -- exercises a detected trip pointing at a
    # vehicle no longer offered in pickers (app/vehicles.py's list_vehicles
    # docstring).
    await conn.execute(
        "UPDATE trips SET category = 'personal', tag_source = 'human', "
        "purpose = 'Doctor appointment', vehicle_id = %s, updated_at = now() WHERE account_id=%s AND id = %s",
        (v2, account_id(conn), block3[0]["id"]),
    )
    await vehicle(block3[1]["id"], v2)

    # Block 4: HOME->WORK keeps its rule tag; the two AWAY-side trips stay
    # unclassified and unassigned (an untracked-relocation artifact has no
    # sensible default vehicle).
    await vehicle(block4[0]["id"], v1)

    # Block 6: the GYM->HOME leg gets a human purpose/category; HOME->GYM
    # stays unclassified on purpose -- it is this run's current-week
    # attention-strip trip.
    await conn.execute(
        "UPDATE trips SET category = 'personal', tag_source = 'human', "
        "purpose = 'Back from the gym', updated_at = now() WHERE account_id=%s AND id = %s",
        (account_id(conn), block6[1]["id"]),
    )


async def _seed_manual_trips(conn, now_utc: datetime, v1: int) -> None:
    pacific_now = now_utc.astimezone(PACIFIC)
    today = pacific_now.date()

    def at(days_ago: int, hour: int, minute: int = 0) -> datetime:
        d = today - timedelta(days=days_ago)
        return datetime(d.year, d.month, d.day, hour, minute, tzinfo=PACIFIC)

    rows = [
        # (started_at, ended_at, miles, category, purpose, tag_source, vehicle_id)
        (at(50, 14, 0), at(50, 15, 30), 42.0, "business", "Airport pickup", "human", v1),
        (at(20, 9, 0), at(20, 9, 45), 6.0, "unclassified", None, None, None),
        (
            max(week_bounds(today, PACIFIC).start, pacific_now - timedelta(hours=8)),
            max(week_bounds(today, PACIFIC).start, pacific_now - timedelta(hours=7, minutes=15)),
            9.5, "personal", "Grocery run", "human", v1,
        ),
    ]
    for started_at, ended_at, miles, category, purpose, tag_source, vehicle_id in rows:
        await conn.execute(
            "INSERT INTO trips (account_id, device, source, started_at, ended_at, distance_m, "
            " category, purpose, tag_source, vehicle_id) "
            "VALUES (%s, 'manual', 'manual', %s, %s, %s, %s, %s, %s, %s)",
            (
                account_id(conn), started_at, ended_at, miles * METERS_PER_MILE, category,
                purpose, tag_source, vehicle_id,
            ),
        )


async def _seed_expenses_and_odometer(conn, now_utc: datetime, v1: int) -> None:
    """Two historical expenses plus two anchored inside the current local
    week (via `week_bounds`, not a plain `days_ago` offset, since "2 days
    ago" can fall in last week's Sunday when this script runs on a Monday
    or Tuesday) so the dashboard's weekly expense total is always testable.
    """
    pacific_now = now_utc.astimezone(PACIFIC)
    today = pacific_now.date()
    week_start_date = week_bounds(today, PACIFIC).monday

    def day(days_ago: int) -> date:
        return today - timedelta(days=days_ago)

    expenses = [
        (day(90), "fuel", "45.32", "business_use_allocated", "Fill-up before the client trip"),
        (day(60), "maintenance_repairs", "180.00", "business_use_allocated", "Oil change + rotation"),
        (week_start_date, "parking", "12.00", "fully_business", "Client site parking"),
        (min(today, week_start_date + timedelta(days=1)), "tolls", "6.50",
         "business_use_allocated", None),
    ]
    for incurred_on, category, amount, treatment, notes in expenses:
        await conn.execute(
            "INSERT INTO expenses (account_id, vehicle_id, incurred_on, category, amount, treatment, notes) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (account_id(conn), v1, incurred_on, category, amount, treatment, notes),
        )

    await conn.execute(
        "INSERT INTO odometer_readings (account_id, vehicle_id, recorded_at, odometer_m, note) VALUES "
        "(%s, %s, %s, %s, %s), (%s, %s, %s, %s, %s)",
        (
            account_id(conn), v1, datetime(day(95).year, day(95).month, day(95).day, 9, 0, tzinfo=PACIFIC),
            62_000.0 * METERS_PER_MILE, "Quarterly check-in",
            account_id(conn), v1, datetime(day(5).year, day(5).month, day(5).day, 9, 0, tzinfo=PACIFIC),
            62_850.0 * METERS_PER_MILE, "Quarterly check-in",
        ),
    )


# --- CLI / orchestration ------------------------------------------------

def _looks_local(database_url: str) -> bool:
    return (urlsplit(database_url).hostname or "") in _LOCAL_HOSTS


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Seed a DISPOSABLE dev/QA Postgres+PostGIS database with synthetic "
            "trips, places, vehicles, rates, expenses, and an odometer reading. "
            "Wipes and reseeds every table it owns on every run -- never point "
            "this at production or any database whose contents matter."
        ),
        epilog="Refuses any --database-url whose host is not a loopback address.",
    )
    parser.add_argument(
        "--database-url", required=True,
        help="postgresql://... URL for a disposable Postgres/PostGIS instance "
             "(loopback host only; no default, no DATABASE_URL fallback)",
    )
    parser.add_argument(
        "--wipe", action="store_true", required=True,
        help="Required confirmation that this run will wipe and reseed the target database",
    )
    return parser.parse_args(argv)


async def main_async(database_url: str) -> None:
    bootstrap_pool = make_pool(database_url)
    await bootstrap_pool.open(wait=True)
    try:
        await run_migrations(bootstrap_pool)
    finally:
        await bootstrap_pool.close()
    async with application_role_pools(database_url) as pools:
        async with control_connection(pools.control) as conn:
            account = await get_account_by_email(conn, "development@localhost.invalid")
            if account is None:
                if await account_exists(conn):
                    raise RuntimeError("seeding requires the synthetic development account")
                account = await create_admin(conn, "development@localhost.invalid",
                    hash_password(secrets.token_urlsafe(32)), display_timezone=str(PACIFIC))
        pool = AccountPool(pools.runtime, AccountPrincipal(
            account["id"], account["is_enabled"], account["auth_version"]))
        async with pool.connection() as conn:
            await _wipe(conn)
            v1, v2 = await _seed_reference_data(conn)
            cur = await conn.execute("SELECT id FROM tracking_devices WHERE account_id=%s AND label=%s", (account_id(conn),DEVICE))
            devices = await cur.fetchall()
            if len(devices) > 1:
                raise RuntimeError("synthetic tracker label is ambiguous")
            if devices:
                device_id = devices[0][0]
            else:
                cur = await conn.execute("INSERT INTO tracking_devices(account_id,label) VALUES(%s,%s) RETURNING id", (account_id(conn),DEVICE))
                device_id = (await cur.fetchone())[0]
                await conn.execute("INSERT INTO detector_state(account_id,tracking_device_id) VALUES(%s,%s)", (account_id(conn),device_id))

        now_utc = datetime.now(timezone.utc)
        points = build_all_points(now_utc)
        years = {p.t.astimezone(PACIFIC).year for p in points}

        async with pool.connection() as conn:
            await _seed_mileage_rates(conn, years)
            await _insert_points(conn, points, device_id)

        runner = DetectorRunner(pool, Params())
        await _detect_all(runner)
        await _discard_missing_trip_bridge(pool, runner, device_id)

        async with pool.connection() as conn:
            trips = await _fetch_device_trips(conn, device_id)
            await _apply_trip_overrides(conn, trips, v1, v2)
            await _seed_manual_trips(conn, now_utc, v1)
            await _seed_expenses_and_odometer(conn, now_utc, v1)

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT source::text, category::text, count(*), coalesce(sum(point_count), 0) "
                "FROM trips WHERE account_id=%s GROUP BY 1, 2 ORDER BY 1, 2", (account_id(conn),)
            )
            print("trips by source/category (count, total points):")
            for src, cat, count, pts in await cur.fetchall():
                print(f"  {src:>8} / {cat:<12} count={count:<4} points={pts}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if not _looks_local(args.database_url):
        print(
            "refusing --database-url with a non-loopback host; this script is "
            "for disposable local databases only (see module docstring)",
            file=sys.stderr,
        )
        return 1
    asyncio.run(main_async(args.database_url))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
