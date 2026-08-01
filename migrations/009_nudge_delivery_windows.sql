-- Weekly unclassified-trip nudges need a durable completion marker rather
-- than in-memory "last sent" state: a container restart, or two replicas
-- briefly overlapping during a deploy, must not turn one Sunday reminder
-- into two. `window_ends_at` identifies the scheduled local-time boundary;
-- its timestamptz value remains unambiguous across DST changes.
CREATE TABLE nudge_delivery_windows (
    window_ends_at timestamptz PRIMARY KEY,
    trip_count     integer NOT NULL CHECK (trip_count >= 0),
    delivered_at   timestamptz NOT NULL DEFAULT now()
);
