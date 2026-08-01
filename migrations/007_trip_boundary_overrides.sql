-- Durable, replayable merge/split instructions. The dirty-window
-- reprocess (app/detector/runner.py) fully regenerates stays+trips from raw
-- points on every run via the pure detect(), so a one-off edit to
-- trips/points would just be overwritten (or duplicated) the next time a
-- reprocess touches that time range. These rows are fed into detect() as an
-- extra parameter on every run instead, so a merge/split survives
-- reprocessing without touching DETECTOR_VERSION.
--
-- 'suppress' drops a real stay that used to separate two trips (a merge);
-- 'force' pins one specific points.id to act as a stay boundary even though
-- no real stay was detected there (a split). point_id references points
-- directly (never deleted) rather than stays (deleted/reinserted every
-- reprocess run), so it stays a stable anchor across runs.
CREATE TYPE trip_boundary_override_kind AS ENUM ('suppress', 'force');

CREATE TABLE trip_boundary_overrides (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device      text NOT NULL,
    kind        trip_boundary_override_kind NOT NULL,
    range_start timestamptz,   -- suppress only: the stay's [started_at, ended_at]
    range_end   timestamptz,
    point_id    bigint REFERENCES points(id) ON DELETE RESTRICT,  -- force only
    created_at  timestamptz NOT NULL DEFAULT now(),
    CHECK (
        (kind = 'suppress' AND range_start IS NOT NULL AND range_end IS NOT NULL AND point_id IS NULL)
        OR (kind = 'force' AND point_id IS NOT NULL AND range_start IS NULL AND range_end IS NULL)
    )
);
CREATE INDEX trip_boundary_overrides_device_idx ON trip_boundary_overrides (device);
CREATE UNIQUE INDEX trip_boundary_overrides_force_point_idx
    ON trip_boundary_overrides (point_id) WHERE kind = 'force';
CREATE UNIQUE INDEX trip_boundary_overrides_suppress_range_idx
    ON trip_boundary_overrides (device, range_start, range_end) WHERE kind = 'suppress';
