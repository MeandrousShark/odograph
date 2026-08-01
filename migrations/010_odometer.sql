-- Odometer readings + reconciliation, and the quarterly reminder that
-- nudges the user to log one. A reading is an absolute dashboard value at
-- a point in time; app/odometer.py's `reconcile` diffs consecutive
-- readings against the GPS-detected distance in between, surfacing how
-- much real driving the detector didn't capture (informational only — no
-- deduction math changes).
--
-- `double precision`, not `real`. trips.distance_m is `real`, fine for a
-- single trip, but an odometer value (300,000 mi ≈ 4.8e8 m) exceeds
-- `real`'s ~7 significant digits and would quantize to ~±30 m.
-- Reconciliation subtracts two readings, so precision on the absolute
-- value matters; `double precision` keeps the delta exact at this scale.
--
-- `odometer_m` (meters) though entered in miles — same canonical-unit
-- reasoning as `distance_m`; keeps all reconciliation math in one unit.
--
-- `ON DELETE CASCADE` — vehicles are soft-deleted (deactivate) today, so
-- this rarely fires; if a vehicle is ever hard-deleted, its readings go
-- with it rather than dangling (unlike trips.vehicle_id, which is SET NULL
-- — a reading has no meaning detached from the vehicle it measured).
--
-- `UNIQUE (vehicle_id, recorded_at)` — one reading per vehicle per instant.
CREATE TABLE odometer_readings (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vehicle_id  bigint NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
    recorded_at timestamptz NOT NULL,
    odometer_m  double precision NOT NULL CHECK (odometer_m >= 0),
    note        text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (vehicle_id, recorded_at)
);
CREATE INDEX odometer_readings_vehicle_idx ON odometer_readings (vehicle_id, recorded_at);

-- Durable dedup for the quarterly odometer reminder, same role migration
-- 009's nudge_delivery_windows plays for the weekly nudge: a restart or a
-- briefly overlapping replica must not re-send. quarter_starts_at is the
-- local-time quarter boundary (unambiguous as timestamptz across DST).
-- Kept a SEPARATE table rather than adding a `kind` to
-- nudge_delivery_windows so this feature never touches the deployed
-- weekly-nudge path.
CREATE TABLE odometer_reminder_windows (
    quarter_starts_at timestamptz PRIMARY KEY,
    reminded          boolean NOT NULL,   -- false when the quarter was evaluated but nothing was due
    delivered_at      timestamptz NOT NULL DEFAULT now()
);
