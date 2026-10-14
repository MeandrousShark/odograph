-- Per-trip list warning acknowledgements. A row stores the condition's
-- fingerprint when it was acknowledged; the application ignores a row whose
-- fingerprint no longer matches the trip, so a changed condition shows again.
-- No reference to the predecessor trip: a stale predecessor ID simply fails
-- the match.
CREATE TYPE public.trip_warning_kind AS ENUM ('recording_gap', 'missing_trip');
CREATE TABLE public.trip_warning_acks (
    account_id bigint NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
    trip_id bigint NOT NULL,
    kind public.trip_warning_kind NOT NULL,
    trip_started_at timestamptz NOT NULL,
    trip_ended_at timestamptz NOT NULL,
    prev_trip_id bigint,
    prev_ended_at timestamptz,
    acknowledged_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, trip_id, kind),
    CONSTRAINT trip_warning_acks_owned_trip_fkey FOREIGN KEY (account_id, trip_id)
        REFERENCES public.trips(account_id, id) ON DELETE CASCADE,
    CONSTRAINT trip_warning_acks_predecessor_check CHECK (
        (kind = 'recording_gap' AND prev_trip_id IS NULL AND prev_ended_at IS NULL)
        OR (kind = 'missing_trip' AND prev_trip_id IS NOT NULL AND prev_ended_at IS NOT NULL))
);

ALTER TABLE public.trip_warning_acks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.trip_warning_acks FORCE ROW LEVEL SECURITY;
CREATE FUNCTION public.storage_charge_trip_warning_acks(r public.trip_warning_acks) RETURNS bigint[]
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT ARRAY[128::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).kind::text, 'UTF8')), 0)::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint];
$body$;
CREATE TRIGGER storage_write_admission BEFORE INSERT OR UPDATE OR DELETE ON public.trip_warning_acks
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_write_admission();
CREATE TRIGGER storage_row_account_guard AFTER UPDATE ON public.trip_warning_acks
    FOR EACH ROW EXECUTE FUNCTION public.storage_row_account_guard();
CREATE TRIGGER storage_charge_insert AFTER INSERT ON public.trip_warning_acks
    REFERENCING NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_update AFTER UPDATE ON public.trip_warning_acks
    REFERENCING OLD TABLE AS storage_old_rows NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_delete AFTER DELETE ON public.trip_warning_acks
    REFERENCING OLD TABLE AS storage_old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();

CREATE OR REPLACE FUNCTION public.storage_expected_usage() RETURNS TABLE (
    account_id bigint, actual_bytes bigint, reserved_bytes bigint, raw_bytes bigint, enhancement_bytes bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
    WITH charged AS (
        SELECT t.account_id, public.storage_charge_raw_messages(t) AS charge FROM public.raw_messages t
        UNION ALL
SELECT t.account_id, public.storage_charge_points(t) AS charge FROM public.points t
        UNION ALL
SELECT t.account_id, public.storage_charge_stays(t) AS charge FROM public.stays t
        UNION ALL
SELECT t.account_id, public.storage_charge_trips(t) AS charge FROM public.trips t
        UNION ALL
SELECT t.account_id, public.storage_charge_detector_state(t) AS charge FROM public.detector_state t
        UNION ALL
SELECT t.account_id, public.storage_charge_places(t) AS charge FROM public.places t
        UNION ALL
SELECT t.account_id, public.storage_charge_tag_rules(t) AS charge FROM public.tag_rules t
        UNION ALL
SELECT t.account_id, public.storage_charge_geocode_cache(t) AS charge FROM public.geocode_cache t
        UNION ALL
SELECT t.account_id, public.storage_charge_trip_boundary_overrides(t) AS charge FROM public.trip_boundary_overrides t
        UNION ALL
SELECT t.account_id, public.storage_charge_vehicles(t) AS charge FROM public.vehicles t
        UNION ALL
SELECT t.account_id, public.storage_charge_mileage_rates(t) AS charge FROM public.mileage_rates t
        UNION ALL
SELECT t.account_id, public.storage_charge_odometer_readings(t) AS charge FROM public.odometer_readings t
        UNION ALL
SELECT t.account_id, public.storage_charge_expenses(t) AS charge FROM public.expenses t
        UNION ALL
SELECT t.account_id, public.storage_charge_nudge_delivery_windows(t) AS charge FROM public.nudge_delivery_windows t
        UNION ALL
SELECT t.account_id, public.storage_charge_odometer_reminder_windows(t) AS charge FROM public.odometer_reminder_windows t
        UNION ALL
SELECT t.account_id, public.storage_charge_email_deliveries(t) AS charge FROM public.email_deliveries t
        UNION ALL
SELECT t.account_id, public.storage_charge_account_settings(t) AS charge FROM public.account_settings t
        UNION ALL
SELECT t.account_id, public.storage_charge_tracking_devices(t) AS charge FROM public.tracking_devices t
        UNION ALL
SELECT t.account_id, public.storage_charge_tracking_device_aliases(t) AS charge FROM public.tracking_device_aliases t
        UNION ALL
SELECT t.account_id, public.storage_charge_ingest_credentials(t) AS charge FROM public.ingest_credentials t
        UNION ALL
        SELECT r.account_id, public.storage_charge_geocode_retry(r) FROM public.geocode_retry r
        UNION ALL
        SELECT r.account_id, public.storage_charge_geocode_discovery(r) FROM public.geocode_discovery r
        UNION ALL
        SELECT r.account_id, public.storage_charge_raw_replay_receipts(r) FROM public.raw_replay_receipts r
        UNION ALL
        SELECT r.account_id, public.storage_charge_trip_warning_acks(r) FROM public.trip_warning_acks r
    ), sums AS (
        SELECT c.account_id, SUM(c.charge[1])::bigint AS actual_bytes,
            SUM(c.charge[2])::bigint AS raw_bytes, SUM(c.charge[3])::bigint AS enhancement_bytes
        FROM charged c GROUP BY c.account_id
    ), reserves AS (
        SELECT e.account_id, SUM(e.point_count * (1024 + 2 * e.label_bytes))::bigint AS reserved_bytes
        FROM public.device_storage_envelopes e GROUP BY e.account_id
    )
    SELECT a.id, 128 + (SELECT COUNT(*) * 128 FROM public.device_storage_envelopes x WHERE x.account_id = a.id) + COALESCE(s.actual_bytes, 0)
        + COALESCE(pg_catalog.octet_length(a.avatar_bytes), 0)::bigint
        + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to(a.avatar_mime, 'UTF8')), 0)::bigint,
        COALESCE(e.reserved_bytes, 0), COALESCE(s.raw_bytes, 0), COALESCE(s.enhancement_bytes, 0)
    FROM public.accounts a LEFT JOIN sums s ON s.account_id = a.id
    LEFT JOIN reserves e ON e.account_id = a.id;
$body$;

REVOKE ALL ON FUNCTION public.storage_charge_trip_warning_acks(public.trip_warning_acks) FROM PUBLIC;
DO $permissions$
BEGIN
    IF to_regclass('odograph_service.managed_role_state') IS NOT NULL THEN
        ALTER TABLE public.trip_warning_acks OWNER TO odograph_migrate;
        REVOKE ALL ON public.trip_warning_acks FROM PUBLIC,odograph_runtime,odograph_control,odograph_bootstrap;
        GRANT SELECT,INSERT,UPDATE,DELETE ON public.trip_warning_acks TO odograph_runtime;
        GRANT SELECT,DELETE ON public.trip_warning_acks TO odograph_bootstrap;
        CREATE POLICY account_isolation ON public.trip_warning_acks FOR ALL TO odograph_runtime
            USING (account_id = NULLIF(current_setting('app.account_id',true),'')::bigint) WITH CHECK (account_id = NULLIF(current_setting('app.account_id',true),'')::bigint);
        CREATE POLICY migration_writer ON public.trip_warning_acks FOR ALL TO odograph_migrate USING(true) WITH CHECK(true);
        CREATE POLICY bootstrap_defaults ON public.trip_warning_acks FOR ALL TO odograph_bootstrap USING(true) WITH CHECK(true);
        ALTER FUNCTION public.storage_charge_trip_warning_acks(public.trip_warning_acks) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_charge_trip_warning_acks(public.trip_warning_acks) FROM PUBLIC,odograph_runtime,odograph_control,odograph_bootstrap;
        ALTER FUNCTION public.storage_expected_usage() OWNER TO odograph_migrate;
    END IF;
END
$permissions$;
SELECT public.reconcile_storage_usage();
