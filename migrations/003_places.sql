CREATE TYPE place_kind AS ENUM ('home', 'work', 'other');

CREATE TABLE places (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name       text NOT NULL UNIQUE,
    kind       place_kind NOT NULL DEFAULT 'other',
    geom       geography(Point, 4326) NOT NULL,
    radius_m   real NOT NULL DEFAULT 150 CHECK (radius_m > 0),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX places_geom_idx ON places USING gist (geom);

-- Each rule side matches by specific place, by kind, or any (both NULL).
-- Direction-agnostic. At least one side must be constrained.
CREATE TABLE tag_rules (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    a_place    bigint REFERENCES places(id) ON DELETE CASCADE,
    a_kind     place_kind,
    b_place    bigint REFERENCES places(id) ON DELETE CASCADE,
    b_kind     place_kind,
    category   trip_category NOT NULL CHECK (category <> 'unclassified'),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (a_place IS NULL OR a_kind IS NULL),
    CHECK (b_place IS NULL OR b_kind IS NULL),
    CHECK (a_place IS NOT NULL OR a_kind IS NOT NULL OR b_place IS NOT NULL OR b_kind IS NOT NULL)
);

-- Seeded defaults per user decision (kind-based; work<->work covers all
-- work-site pairs since the user has multiple work sites):
INSERT INTO tag_rules (a_kind, b_kind, category) VALUES
    ('home', 'work', 'personal'),   -- IRS: commuting is not deductible
    ('work', 'work', 'business');

CREATE TYPE tag_origin AS ENUM ('human', 'rule');
ALTER TABLE trips
    ADD COLUMN start_place_id bigint REFERENCES places(id) ON DELETE SET NULL,
    ADD COLUMN end_place_id   bigint REFERENCES places(id) ON DELETE SET NULL,
    ADD COLUMN tag_source     tag_origin;
UPDATE trips SET tag_source = 'human' WHERE category <> 'unclassified';
