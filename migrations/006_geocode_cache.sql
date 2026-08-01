-- Reverse-geocode cache, keyed by rounded coordinate rather than by
-- trip/place FK — deliberately decoupled so it works for both trip
-- endpoints and (later, if useful) place search results.
--
-- numeric(8,4) precision (~11m at this latitude) must stay in sync with
-- GEOCODE_PRECISION in app/geocode.py (SQL and Python can't share a literal
-- constant directly).
--
-- A NULL address is a cached "we tried, nothing usable" (e.g. a coordinate
-- in the middle of a lake, or a transient API error) — its existence is the
-- "don't retry every sweep" signal, same role fetched_at plays here as
-- snapped_at does for trips.snap_status.
CREATE TABLE geocode_cache (
    lat        numeric(8,4) NOT NULL,
    lon        numeric(8,4) NOT NULL,
    address    text,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (lat, lon)
);
