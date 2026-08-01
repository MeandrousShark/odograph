CREATE TABLE mileage_rates (
    year         int PRIMARY KEY,
    rate_per_mi  numeric(6,4) NOT NULL CHECK (rate_per_mi > 0),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

INSERT INTO mileage_rates (year, rate_per_mi) VALUES
    (2025, 0.7000),
    (2026, 0.7250);
