-- An optional trip attribution keeps vehicle-wide expense records intact while
-- allowing a per-trip ledger view. Deleting a trip must never delete money.
ALTER TABLE expenses ADD COLUMN trip_id bigint REFERENCES trips(id) ON DELETE SET NULL;
CREATE INDEX expenses_trip_id_idx ON expenses (trip_id);
