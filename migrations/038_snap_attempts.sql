ALTER TABLE public.trips ADD COLUMN snap_attempted_at timestamptz;

CREATE INDEX trips_snap_pending_attempt_idx ON public.trips
    (account_id, (COALESCE(snap_attempted_at, created_at)), id)
    WHERE snap_status = 'pending' AND source = 'detected' AND NOT imported;
