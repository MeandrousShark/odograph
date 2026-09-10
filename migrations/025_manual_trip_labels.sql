-- A trip endpoint's custom label names that specific trip's endpoint, not a
-- reusable place. Nullable and independently optional per endpoint, the
-- same shape as every other manual-only trip detail already on this table.
ALTER TABLE trips
    ADD COLUMN start_label text,
    ADD COLUMN end_label text;

-- Only a manual trip can carry a label: a detected trip's endpoints come
-- from its own GPS points, and a label naming them would just be a second,
-- possibly stale name for a location the trip already describes precisely.
ALTER TABLE trips
    ADD CONSTRAINT trips_start_label_manual_only
        CHECK (start_label IS NULL OR source = 'manual'),
    ADD CONSTRAINT trips_end_label_manual_only
        CHECK (end_label IS NULL OR source = 'manual');

-- A label only exists to name an endpoint that has nothing else naming it.
-- A saved place already has a stable name (start_place_id/end_place_id),
-- and a routed or map-picked endpoint already has coordinates to reverse-
-- geocode (start_geom/end_geom). Requiring both to be NULL keeps a label
-- from becoming a second, possibly contradictory name for an endpoint that
-- already has one.
ALTER TABLE trips
    ADD CONSTRAINT trips_start_label_requires_no_route
        CHECK (start_label IS NULL OR (start_place_id IS NULL AND start_geom IS NULL)),
    ADD CONSTRAINT trips_end_label_requires_no_route
        CHECK (end_label IS NULL OR (end_place_id IS NULL AND end_geom IS NULL));

-- btrim, the same already-trimmed-value convention 020_accounts.sql's
-- `email = lower(btrim(email))` uses: the application is the only writer
-- that needs to run the full trim, and this constraint's job is only to
-- catch a direct write that skipped it.
ALTER TABLE trips
    ADD CONSTRAINT trips_start_label_trimmed_nonblank
        CHECK (start_label IS NULL OR (start_label = btrim(start_label) AND start_label <> '')),
    ADD CONSTRAINT trips_end_label_trimmed_nonblank
        CHECK (end_label IS NULL OR (end_label = btrim(end_label) AND end_label <> ''));

-- char_length, not octet_length: the cap is on displayed characters, so a
-- name written in a multi-byte script isn't penalized for its encoded size.
ALTER TABLE trips
    ADD CONSTRAINT trips_start_label_length
        CHECK (start_label IS NULL OR char_length(start_label) <= 100),
    ADD CONSTRAINT trips_end_label_length
        CHECK (end_label IS NULL OR char_length(end_label) <= 100);
