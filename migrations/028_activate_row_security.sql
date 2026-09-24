-- Enforce the account policies prepared by 026 and role setup. FORCE also
-- applies them to the owning role; only a superuser or BYPASSRLS migration
-- role, which startup requires, still sees every account's rows.
DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'raw_messages', 'points', 'stays', 'trips', 'detector_state', 'places',
        'tag_rules', 'geocode_cache', 'trip_boundary_overrides', 'vehicles',
        'mileage_rates', 'odometer_readings', 'expenses', 'nudge_delivery_windows',
        'odometer_reminder_windows', 'email_deliveries', 'account_settings',
        'tracking_devices', 'tracking_device_aliases', 'ingest_credentials'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
    END LOOP;
END $$;

ALTER TABLE instance_state DROP CONSTRAINT instance_state_security_contract_version_check;
UPDATE instance_state SET security_contract_version = 'ownership-activated-v1';
ALTER TABLE instance_state
    ALTER COLUMN security_contract_version SET DEFAULT 'ownership-activated-v1',
    ADD CONSTRAINT instance_state_security_contract_version_check
        CHECK (security_contract_version = 'ownership-activated-v1');

-- Role setup creates this state after migrations on a new installation, so
-- it exists here only when upgrading a provisioned one.
DO $$
BEGIN
    IF to_regclass('odograph_service.managed_role_state') IS NOT NULL THEN
        UPDATE odograph_service.managed_role_state
        SET contract_version = 'ownership-activated-v1'
        WHERE contract_version = 'ownership-prepared-v1';
    END IF;
    IF to_regclass('odograph_service.recovery_metadata') IS NOT NULL THEN
        UPDATE odograph_service.recovery_metadata
        SET contract_version = 'ownership-activated-v1'
        WHERE contract_version = 'ownership-prepared-v1';
    END IF;
END $$;
