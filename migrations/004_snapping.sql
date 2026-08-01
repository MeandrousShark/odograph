CREATE TYPE snap_state AS ENUM ('pending', 'ok', 'low_confidence', 'failed');

ALTER TABLE trips
    ADD COLUMN path_snapped      geometry(MultiLineString, 4326),
    ADD COLUMN distance_snapped_m real,
    ADD COLUMN snap_status       snap_state,
    ADD COLUMN snapped_at        timestamptz;

-- Manual trips keep NULL snap_status (no geometry, not snappable) --
-- the WHERE clause is correctness-critical: an unfiltered UPDATE would
-- wrongly force 'pending' onto manual trips too.
UPDATE trips SET snap_status = 'pending' WHERE source = 'detected';

CREATE INDEX trips_snap_pending_idx ON trips (id) WHERE snap_status = 'pending';
