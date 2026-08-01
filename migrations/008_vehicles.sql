-- Multiple vehicles: the IRS standard mileage rate
-- deduction is computed per vehicle (each has its own basis/depreciation
-- history in the taxpayer's own records), so trips need to say which vehicle
-- they were driven in even though the detector itself stays vehicle-unaware
-- (DETECTOR_VERSION does not change here — assignment is a UI/tagging
-- concern layered on top, same as places/categories).
--
-- `is_default` marks the vehicle pre-selected for new/unassigned trips; the
-- partial unique index below is what actually enforces "at most one
-- default" (a plain UNIQUE index would also forbid more than one *non*-
-- default row, which isn't the invariant we want).
--
-- `vehicle_id` is ON DELETE SET NULL, not CASCADE: deleting a vehicle
-- detaches its trips (they fall back to unassigned) rather than destroying
-- trip history, since a vehicle a user sold or retired shouldn't take a
-- year's mileage records down with it.
CREATE TABLE vehicles (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name       text NOT NULL,
    make       text,
    model      text,
    plate      text,
    is_default boolean NOT NULL DEFAULT false,
    active     boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX vehicles_one_default_idx ON vehicles (is_default) WHERE is_default;

ALTER TABLE trips ADD COLUMN vehicle_id bigint REFERENCES vehicles(id) ON DELETE SET NULL;

INSERT INTO vehicles (name, is_default) VALUES ('My Car', true);
UPDATE trips SET vehicle_id = (SELECT id FROM vehicles WHERE is_default);
