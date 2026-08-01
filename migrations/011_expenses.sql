-- Actual-expense ledger. Money uses an exact decimal because
-- binary floating point can change cent totals across a year or between the
-- HTML and workbook renderers. Vehicles are deliberately RESTRICTed on hard
-- delete: an expense cannot be understood without its vehicle, and the normal
-- retirement path is the existing soft-deactivation workflow.
CREATE TYPE expense_category AS ENUM (
    'fuel', 'maintenance_repairs', 'tires', 'insurance',
    'registration_taxes', 'lease_payments', 'depreciation',
    'parking', 'tolls', 'other'
);

CREATE TYPE expense_treatment AS ENUM ('business_use_allocated', 'fully_business');

CREATE TABLE expenses (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vehicle_id  bigint NOT NULL REFERENCES vehicles(id) ON DELETE RESTRICT,
    incurred_on date NOT NULL,
    category    expense_category NOT NULL,
    amount      numeric(12,2) NOT NULL CHECK (amount > 0),
    treatment   expense_treatment NOT NULL,
    notes       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX expenses_vehicle_date_idx ON expenses (vehicle_id, incurred_on DESC, id DESC);
