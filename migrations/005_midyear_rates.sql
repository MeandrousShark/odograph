-- Mid-year IRS rate changes (e.g. 2022: 58.5c/mi Jan-Jun, 62.5c/mi Jul-Dec).
-- Each year is either a single flat rate all year (both columns NULL, the
-- default and the pre-existing behavior) or has one mid-year change: a
-- second-half rate that takes effect from the start of h2_start_month.
ALTER TABLE mileage_rates
    ADD COLUMN rate_h2_per_mi numeric(6,4) CHECK (rate_h2_per_mi IS NULL OR rate_h2_per_mi > 0),
    ADD COLUMN h2_start_month smallint     CHECK (h2_start_month IS NULL OR h2_start_month BETWEEN 1 AND 12);

-- The second-half rate and the month it starts must be present together:
-- one without the other is meaningless.
ALTER TABLE mileage_rates
    ADD CONSTRAINT mileage_rates_h2_pair
    CHECK ((rate_h2_per_mi IS NULL) = (h2_start_month IS NULL));
