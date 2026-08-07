-- A trip that arrived through the portable import (app/portable.py) has no
-- backing rows in this instance's points table -- the source instance's
-- points never travel with it. The detector's reconcile pass treats every
-- source = 'detected' trip it doesn't re-derive from points as stale and
-- deletes it (app/detector/reconcile.py's plan_reconcile), so an imported
-- trip must be permanently excluded from that reconcile set or the first
-- detector run after import destroys the entire imported detected-trip
-- history. The flag is derived at insert time (set true only by the import
-- path; every other writer relies on the default), not carried in the
-- bundle file itself, since "has no points here" is a fact about this
-- instance, not a property of the trip.
ALTER TABLE trips
    ADD COLUMN imported boolean NOT NULL DEFAULT false;
