-- A detected trip cannot be deleted as a bare row: the next detector pass
-- would recreate it from points. A discard override records the trip's time
-- span so every later detector pass omits the corresponding assembled trip.
--
-- PostgreSQL does not permit a newly-added enum value to be used as an enum
-- literal until the ALTER TYPE transaction commits. Keep all same-migration
-- constraint and index expressions generic/text-based so startup can apply
-- this migration atomically through run_migrations().
ALTER TYPE trip_boundary_override_kind ADD VALUE 'discard';

ALTER TABLE trip_boundary_overrides
    DROP CONSTRAINT trip_boundary_overrides_check;

ALTER TABLE trip_boundary_overrides
    ADD CHECK (
        (kind::text IN ('suppress', 'discard')
            AND range_start IS NOT NULL AND range_end IS NOT NULL AND point_id IS NULL)
        OR (kind::text = 'force'
            AND point_id IS NOT NULL AND range_start IS NULL AND range_end IS NULL)
    );

CREATE UNIQUE INDEX trip_boundary_overrides_range_idx
    ON trip_boundary_overrides (device, kind, range_start, range_end)
    WHERE range_start IS NOT NULL;
