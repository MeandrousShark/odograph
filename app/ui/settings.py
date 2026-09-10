from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from psycopg import errors
from psycopg.rows import dict_row

from app.auth import require_csrf, require_user
from app.page import render_page
from app.db import _fetch_schema_version
from app.detector.runner import DETECTOR_VERSION
from app.diagnose import build_report, run_connectivity_checks
from app.odometer import OdometerReading, reconcile
from app.rates import METERS_PER_MILE
from app.trip_queries import DISPLAY_DISTANCE_SQL
from app.validation import parse_finite_number
from app.vehicles import (
    create_vehicle,
    deactivate_vehicle,
    get_auto_assign_default_vehicle,
    list_vehicles,
    set_auto_assign_default_vehicle,
    set_default_vehicle,
    update_vehicle,
)

from app.ui._common import _redirect_back
from app.ui.places import _fetch_boundary_overrides_rows, _fetch_places_rows, _fetch_rules_rows
from app.ui.reports import _env_override_years


async def _fetch_rates_rows(conn) -> list[dict]:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT year, rate_per_mi::float AS rate_per_mi, "
        " rate_h2_per_mi::float AS rate_h2_per_mi, h2_start_month "
        "FROM mileage_rates ORDER BY year DESC"
    )
    rows = await cur.fetchall()
    env_years = _env_override_years()
    for row in rows:
        row["env_override"] = row["year"] in env_years
    return rows


async def _fetch_odometer_context(conn) -> list[dict]:
    """One entry per vehicle (active + inactive, like `_vehicles_table.html`
    itself), each with its readings (newest first) and, once it has >=2,
    the `ReconInterval` table between them. Grouped in Python rather than
    with a per-vehicle query or a window-function join: the vehicle/reading
    counts here are small, and this keeps `app.odometer.reconcile` (the
    part that actually needs to be correct) entirely out of SQL.
    """
    vehicles = await list_vehicles(conn, include_inactive=True)

    reading_cur = conn.cursor(row_factory=dict_row)
    await reading_cur.execute(
        "SELECT id, vehicle_id, recorded_at, odometer_m, note "
        "FROM odometer_readings ORDER BY recorded_at"
    )
    readings_by_vehicle: dict[int, list[dict]] = {}
    for row in await reading_cur.fetchall():
        readings_by_vehicle.setdefault(row["vehicle_id"], []).append(row)

    # Every trip with a vehicle, not just the report year. This view is a
    # running ledger, unlike the annual report's year-scoped coverage.
    trip_cur = conn.cursor(row_factory=dict_row)
    await trip_cur.execute(
        f"SELECT vehicle_id, started_at, {DISPLAY_DISTANCE_SQL} AS display_distance_m "
        "FROM trips WHERE vehicle_id IS NOT NULL "
        "AND exclusion IS DISTINCT FROM 'not_my_vehicle'"
    )
    trips_by_vehicle: dict[int, list[tuple]] = {}
    for row in await trip_cur.fetchall():
        trips_by_vehicle.setdefault(row["vehicle_id"], []).append(
            (row["started_at"], row["display_distance_m"])
        )

    result = []
    for vehicle in vehicles:
        rows = readings_by_vehicle.get(vehicle["id"], [])
        readings = [OdometerReading(r["recorded_at"], r["odometer_m"]) for r in rows]
        recon = reconcile(readings, trips_by_vehicle.get(vehicle["id"], []))
        result.append({
            "vehicle": vehicle,
            "readings": list(reversed(rows)),
            "intervals": recon.intervals,
        })
    return result


async def _render_vehicles_table(request: Request, conn):
    vehicles = await list_vehicles(conn, include_inactive=True)
    return request.app.state.templates.TemplateResponse(
        request, "_vehicles_table.html", {"vehicles": vehicles}
    )


async def _render_odometer_table(request: Request, conn):
    vehicles = await list_vehicles(conn, include_inactive=True)
    odometer = await _fetch_odometer_context(conn)
    return request.app.state.templates.TemplateResponse(
        request, "_odometer_table.html", {"vehicles": vehicles, "odometer": odometer}
    )


async def _fetch_device_fixes(conn) -> list[dict]:
    """One row per device that has ever posted a point, so a misconfigured
    phone (wrong tid, stale credentials, app killed by the OS) is visible on
    the Settings page instead of only showing up once trips stop appearing.
    Grouped aggregate over the `(device, recorded_at)` index -- no per-device
    query loop needed at this scale.
    """
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT device, max(received_at) AS newest_received_at, "
        "max(recorded_at) AS newest_recorded_at, count(*) AS point_count "
        "FROM points GROUP BY device ORDER BY device"
    )
    return await cur.fetchall()


def register(router: APIRouter) -> None:
        @router.get("/settings")
        async def settings_page(request: Request, user: dict = Depends(require_user)):
            async with request.app.state.pool.connection() as conn:
                db_rates = await _fetch_rates_rows(conn)
                # include_inactive=True: unlike the trip-assignment pickers, the
                # settings table is where a deactivated vehicle is managed, so it
                # must stay visible here even though it's dropped elsewhere.
                vehicles = await list_vehicles(conn, include_inactive=True)
                auto_assign_default_vehicle = await get_auto_assign_default_vehicle(conn)
                odometer = await _fetch_odometer_context(conn)
                places = await _fetch_places_rows(conn)
                rules = await _fetch_rules_rows(conn, places)
                boundary_overrides = await _fetch_boundary_overrides_rows(conn)
                device_fixes = await _fetch_device_fixes(conn)
                schema_version = await _fetch_schema_version(conn)
            cfg = request.app.state.config
            # A second, independent report -- built from its own pool borrows,
            # same as every other fetch above -- rather than folding into the
            # small `diagnostics` dict below: that dict's exact shape is a
            # long-standing contract (tests/test_version_identity.py), and the
            # config-presence/worker/migration detail here is new, additive
            # content, not a replacement for it.
            diagnostics_report = await build_report(cfg, request.app.state.pool, request.app.state)
            return await render_page(
                request, "settings.html",
                {
                    "rates": db_rates, "vehicles": vehicles, "odometer": odometer,
                    "auto_assign_default_vehicle": auto_assign_default_vehicle,
                    "places": places, "rules": rules,
                    "boundary_overrides": boundary_overrides,
                    "device_fixes": device_fixes,
                    "user": user, "csrf": request.session.get("csrf", ""),
                    "geocode_enabled": cfg.geocode_provider is not None,
                    "diagnostics": {
                        "app_version": cfg.app_version,
                        "git_revision": cfg.app_git_revision,
                        "schema_version": schema_version,
                        "detector_version": DETECTOR_VERSION,
                    },
                    "diagnostics_report": diagnostics_report,
                    "connectivity": None,
                },
            )

        @router.post("/settings/diagnostics/check", dependencies=[Depends(require_csrf)])
        async def check_diagnostics_connectivity(request: Request, user: dict = Depends(require_user)):
            """The D6 "check now" control: OSRM/geocoder/ntfy/SMTP reachability
            is probed only in direct response to this explicit POST, never on a
            timer or from a plain page load -- see app/diagnose.py's module
            docstring for why a background prober was rejected.
            """
            cfg = request.app.state.config
            connectivity = await run_connectivity_checks(cfg)
            return request.app.state.templates.TemplateResponse(
                request, "_diagnostics_connectivity.html", {"connectivity": connectivity},
            )

        @router.post("/settings/vehicles", dependencies=[Depends(require_csrf)])
        async def add_vehicle(
            request: Request,
            name: str = Form(...),
            make: str = Form(""),
            model: str = Form(""),
            plate: str = Form(""),
            is_default: str = Form(""),
            user: dict = Depends(require_user),
        ):
            name = name.strip()
            if not name:
                raise HTTPException(status_code=400, detail="Name required")
            async with request.app.state.pool.connection() as conn:
                await create_vehicle(
                    conn, name, make.strip() or None, model.strip() or None,
                    plate.strip() or None, is_default=(is_default == "1"),
                )
                return await _render_vehicles_table(request, conn)

        @router.post("/settings/vehicles/{vehicle_id}/update", dependencies=[Depends(require_csrf)])
        async def edit_vehicle(
            request: Request,
            vehicle_id: int,
            name: str = Form(...),
            make: str = Form(""),
            model: str = Form(""),
            plate: str = Form(""),
            user: dict = Depends(require_user),
        ):
            name = name.strip()
            if not name:
                raise HTTPException(status_code=400, detail="Name required")
            async with request.app.state.pool.connection() as conn:
                await update_vehicle(
                    conn, vehicle_id, name, make.strip() or None,
                    model.strip() or None, plate.strip() or None,
                )
                return await _render_vehicles_table(request, conn)

        @router.post("/settings/vehicles/{vehicle_id}/default", dependencies=[Depends(require_csrf)])
        async def make_vehicle_default(
            request: Request, vehicle_id: int, user: dict = Depends(require_user)
        ):
            async with request.app.state.pool.connection() as conn:
                await set_default_vehicle(conn, vehicle_id)
                return await _render_vehicles_table(request, conn)

        @router.post("/settings/vehicles/{vehicle_id}/deactivate", dependencies=[Depends(require_csrf)])
        async def deactivate_vehicle_route(
            request: Request, vehicle_id: int, user: dict = Depends(require_user)
        ):
            async with request.app.state.pool.connection() as conn:
                await deactivate_vehicle(conn, vehicle_id)
                return await _render_vehicles_table(request, conn)

        @router.post("/settings/vehicles/auto_assign", dependencies=[Depends(require_csrf)])
        async def set_auto_assign_default_vehicle_route(
            request: Request,
            auto_assign_default_vehicle: str = Form(""),
            user: dict = Depends(require_user),
        ):
            # An unchecked HTML checkbox submits nothing at all, so absence must
            # read as false -- there is no "unset" value to distinguish from off.
            async with request.app.state.pool.connection() as conn:
                await set_auto_assign_default_vehicle(
                    conn, auto_assign_default_vehicle == "1"
                )
            return _redirect_back(request)

        @router.post("/settings/odometer", dependencies=[Depends(require_csrf)])
        async def add_odometer_reading(
            request: Request,
            vehicle_id: int = Form(...),
            date: str = Form(...),
            time: str = Form("00:00"),
            value: float = Form(...),
            note: str = Form(""),
            user: dict = Depends(require_user),
        ):
            """Same date/time parse path and mi->meters conversion as
            `add_manual_trip`, so a 100 mi entry stores 160934.4 m, one
            canonical-meters convention across the whole app.
            """
            tz = request.app.state.config.display_tz
            try:
                recorded_at = datetime.fromisoformat(f"{date}T{time}").replace(tzinfo=tz)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid date/time")
            # parse_finite_number(minimum=0) alone would newly accept 0, so the
            # strict "> 0" check stays separate from the finite check.
            parsed_value = parse_finite_number(value)
            if parsed_value is None or parsed_value <= 0:
                raise HTTPException(status_code=400, detail="Invalid odometer value")
            odometer_m = parsed_value * METERS_PER_MILE

            async with request.app.state.pool.connection() as conn:
                try:
                    await conn.execute(
                        "INSERT INTO odometer_readings (vehicle_id, recorded_at, odometer_m, note) "
                        "VALUES (%s, %s, %s, %s)",
                        (vehicle_id, recorded_at, odometer_m, note.strip() or None),
                    )
                except errors.ForeignKeyViolation:
                    raise HTTPException(status_code=400, detail="No such vehicle")
                except errors.UniqueViolation:
                    raise HTTPException(status_code=400, detail="A reading already exists at that date/time")
                return await _render_odometer_table(request, conn)

        @router.post("/settings/odometer/{reading_id}/delete", dependencies=[Depends(require_csrf)])
        async def delete_odometer_reading(
            request: Request, reading_id: int, user: dict = Depends(require_user)
        ):
            async with request.app.state.pool.connection() as conn:
                await conn.execute("DELETE FROM odometer_readings WHERE id = %s", (reading_id,))
                return await _render_odometer_table(request, conn)

        @router.post("/settings/rates", dependencies=[Depends(require_csrf)])
        async def upsert_rate(
            request: Request,
            year: int = Form(...),
            rate_per_mi: float = Form(...),
            mid_year: str = Form(""),
            rate_h2_per_mi: str = Form(""),
            h2_start_month: int = Form(7),
            user: dict = Depends(require_user),
        ):
            parsed_rate = parse_finite_number(rate_per_mi)
            if parsed_rate is None or parsed_rate <= 0:
                raise HTTPException(status_code=400, detail="Rate must be positive")
            rate_per_mi = parsed_rate
            # Mid-year change is opt-in per year: when the toggle is off the year
            # keeps a single flat rate (both split columns NULL). When on, a valid
            # second-half rate and month are required (DB CHECK enforces the pair).
            h2_rate = None
            h2_month = None
            if mid_year == "1":
                try:
                    h2_rate = float(rate_h2_per_mi)
                except (TypeError, ValueError):
                    raise HTTPException(
                        status_code=400,
                        detail="Second-half rate is required when mid-year change is on",
                    )
                parsed_h2_rate = parse_finite_number(h2_rate)
                if parsed_h2_rate is None or parsed_h2_rate <= 0:
                    raise HTTPException(status_code=400, detail="Second-half rate must be positive")
                h2_rate = parsed_h2_rate
                if not (1 <= h2_start_month <= 12):
                    raise HTTPException(status_code=400, detail="Invalid mid-year start month")
                h2_month = h2_start_month
            async with request.app.state.pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO mileage_rates (year, rate_per_mi, rate_h2_per_mi, h2_start_month)"
                    " VALUES (%s, %s, %s, %s)"
                    " ON CONFLICT (year) DO UPDATE SET"
                    " rate_per_mi = EXCLUDED.rate_per_mi,"
                    " rate_h2_per_mi = EXCLUDED.rate_h2_per_mi,"
                    " h2_start_month = EXCLUDED.h2_start_month, updated_at = now()",
                    (year, rate_per_mi, h2_rate, h2_month),
                )
                db_rates = await _fetch_rates_rows(conn)
            return request.app.state.templates.TemplateResponse(
                request, "_rates_table.html", {"rates": db_rates}
            )
