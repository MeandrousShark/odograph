CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TYPE trip_category AS ENUM ('unclassified', 'business', 'personal');
CREATE TYPE trip_source   AS ENUM ('detected', 'manual');

CREATE TABLE raw_messages (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    received_at timestamptz NOT NULL DEFAULT now(),
    payload     jsonb NOT NULL
);

CREATE TABLE trips (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device           text NOT NULL,
    source           trip_source NOT NULL DEFAULT 'detected',
    started_at       timestamptz NOT NULL,
    ended_at         timestamptz NOT NULL,
    start_geom       geography(Point, 4326),
    end_geom         geography(Point, 4326),
    distance_m       real NOT NULL,
    point_count      int NOT NULL DEFAULT 0,
    path             geometry(LineString, 4326),
    has_gap          boolean NOT NULL DEFAULT false,
    category         trip_category NOT NULL DEFAULT 'unclassified',
    notes            text,
    detector_version int NOT NULL DEFAULT 0,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX trips_started_at_idx ON trips (started_at DESC);

CREATE TABLE points (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device       text NOT NULL,
    recorded_at  timestamptz NOT NULL,
    received_at  timestamptz NOT NULL DEFAULT now(),
    geom         geography(Point, 4326) NOT NULL,
    accuracy_m   real,
    velocity_kmh real,
    altitude_m   real,
    battery_pct  smallint,
    trigger      text,
    trip_id      bigint REFERENCES trips(id) ON DELETE SET NULL,
    UNIQUE (device, recorded_at)
);
CREATE INDEX points_recorded_at_idx ON points (device, recorded_at);
CREATE INDEX points_received_at_idx ON points (received_at);
CREATE INDEX points_geom_idx ON points USING gist (geom);
CREATE INDEX points_trip_id_idx ON points (trip_id);

CREATE TABLE stays (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device      text NOT NULL,
    started_at  timestamptz NOT NULL,
    ended_at    timestamptz NOT NULL,
    centroid    geography(Point, 4326) NOT NULL,
    point_count int NOT NULL
);
CREATE INDEX stays_ended_at_idx ON stays (device, ended_at DESC);

CREATE TABLE detector_state (
    id               int PRIMARY KEY CHECK (id = 1),
    last_run_at      timestamptz,
    detector_version int NOT NULL DEFAULT 0
);
INSERT INTO detector_state (id) VALUES (1);
