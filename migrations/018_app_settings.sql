-- Single-row application settings, same CHECK (id = 1) invariant as
-- local_admin in 017_local_admin.sql: "there can only ever be one row" is a
-- database constraint, not a code convention, so a stray second INSERT can
-- never silently create a second, disagreeing settings row.
CREATE TABLE app_settings (
    id                          smallint PRIMARY KEY CHECK (id = 1),
    auto_assign_default_vehicle boolean NOT NULL DEFAULT false,
    updated_at                  timestamptz NOT NULL DEFAULT now()
);

-- Seeded here so no read path has to cope with an empty table. Default
-- false: 008_vehicles.sql already seeds a default vehicle on every existing
-- install, so defaulting this to true would silently change upgrade
-- behavior for operators who never touched the setting.
INSERT INTO app_settings (id) VALUES (1);
