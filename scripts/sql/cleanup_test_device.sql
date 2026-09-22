-- Resolve an explicit issued test credential to its account and stable device.
-- Keep these single-bigint keys synchronized with app/db.py.
BEGIN;
SELECT pg_advisory_xact_lock(469920146737);
SELECT pg_advisory_xact_lock(469920146743);
CREATE TEMP TABLE cleanup_target ON COMMIT DROP AS
SELECT d.account_id, d.id AS tracking_device_id
FROM tracking_devices d JOIN ingest_credentials c
  ON c.account_id = d.account_id AND c.tracking_device_id = d.id
WHERE c.basic_username = :'tracking_username' AND c.kind = 'device' AND d.label = 'test'
FOR UPDATE OF d, c;
DO $$
BEGIN
    IF (SELECT count(*) FROM cleanup_target) <> 1 THEN
        RAISE EXCEPTION 'Expected one issued credential for a device named test';
    END IF;
END $$;
DELETE FROM trip_boundary_overrides AS target_row USING cleanup_target AS target
WHERE target_row.account_id = target.account_id AND target_row.tracking_device_id = target.tracking_device_id;
DELETE FROM trips AS target_row USING cleanup_target AS target
WHERE target_row.account_id = target.account_id AND target_row.tracking_device_id = target.tracking_device_id;
DELETE FROM stays AS target_row USING cleanup_target AS target
WHERE target_row.account_id = target.account_id AND target_row.tracking_device_id = target.tracking_device_id;
DELETE FROM points AS target_row USING cleanup_target AS target
WHERE target_row.account_id = target.account_id AND target_row.tracking_device_id = target.tracking_device_id;
DELETE FROM raw_messages AS target_row USING cleanup_target AS target
WHERE target_row.account_id = target.account_id AND target_row.tracking_device_id = target.tracking_device_id;
DELETE FROM detector_state AS target_row USING cleanup_target AS target
WHERE target_row.account_id = target.account_id AND target_row.tracking_device_id = target.tracking_device_id;
DELETE FROM tracking_device_aliases AS target_row USING cleanup_target AS target
WHERE target_row.account_id = target.account_id AND target_row.tracking_device_id = target.tracking_device_id;
DELETE FROM ingest_credentials AS target_row USING cleanup_target AS target
WHERE target_row.account_id = target.account_id AND target_row.tracking_device_id = target.tracking_device_id;
DELETE FROM tracking_devices AS target_row USING cleanup_target AS target
WHERE target_row.account_id = target.account_id AND target_row.id = target.tracking_device_id;
COMMIT;
