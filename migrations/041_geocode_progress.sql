-- Durable coordinate retries and bounded discovery of eligible trip endpoints.
CREATE TYPE public.geocode_failure_reason AS ENUM ('http', 'transport', 'parse', 'source_changed');
CREATE TYPE public.geocode_work_unit AS ENUM ('discovery', 'coordinate');
CREATE TABLE public.geocode_retry (
    account_id bigint NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
    rounded_lat numeric(8,4) NOT NULL CHECK (rounded_lat BETWEEN -90 AND 90),
    rounded_lon numeric(8,4) NOT NULL CHECK (rounded_lon BETWEEN -180 AND 180),
    attempted_at timestamptz,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    failure_count integer NOT NULL DEFAULT 0 CHECK (failure_count BETWEEN 0 AND 31),
    failure_reason public.geocode_failure_reason,
    PRIMARY KEY (account_id, rounded_lat, rounded_lon)
);
CREATE TABLE public.geocode_discovery (
    account_id bigint PRIMARY KEY REFERENCES public.accounts(id) ON DELETE CASCADE,
    cursor_trip_id bigint NOT NULL DEFAULT 0 CHECK (cursor_trip_id >= 0),
    generation bigint NOT NULL DEFAULT 1 CHECK (generation > 0),
    round_generation bigint NOT NULL DEFAULT 1 CHECK (round_generation > 0),
    scanned_generation bigint NOT NULL DEFAULT 0 CHECK (scanned_generation >= 0),
    last_unit public.geocode_work_unit NOT NULL DEFAULT 'coordinate'
);
ALTER TABLE public.trips ADD COLUMN geocode_generation bigint NOT NULL DEFAULT 1 CHECK (geocode_generation > 0);
ALTER TABLE public.tracking_devices ADD COLUMN geocode_generation bigint NOT NULL DEFAULT 1 CHECK (geocode_generation > 0);
CREATE INDEX geocode_retry_due_idx ON public.geocode_retry
    (account_id, next_attempt_at, attempted_at NULLS FIRST, rounded_lat, rounded_lon);
CREATE INDEX trips_geocode_start_idx ON public.trips
    (account_id, (ROUND(public.ST_Y(start_geom::public.geometry)::numeric, 4)),
     (ROUND(public.ST_X(start_geom::public.geometry)::numeric, 4)), id)
    WHERE start_geom IS NOT NULL AND start_place_id IS NULL;
CREATE INDEX trips_geocode_end_idx ON public.trips
    (account_id, (ROUND(public.ST_Y(end_geom::public.geometry)::numeric, 4)),
     (ROUND(public.ST_X(end_geom::public.geometry)::numeric, 4)), id)
    WHERE end_geom IS NOT NULL AND end_place_id IS NULL;
CREATE INDEX trips_geocode_discovery_idx ON public.trips (account_id, id);

ALTER TABLE public.geocode_retry ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.geocode_retry FORCE ROW LEVEL SECURITY;
CREATE FUNCTION public.storage_charge_geocode_retry(r public.geocode_retry) RETURNS bigint[]
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT ARRAY[128::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).failure_reason::text, 'UTF8')), 0)::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint];
$body$;
CREATE TRIGGER storage_write_admission BEFORE INSERT OR UPDATE OR DELETE ON public.geocode_retry
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_write_admission();
CREATE TRIGGER storage_row_account_guard AFTER UPDATE ON public.geocode_retry
    FOR EACH ROW EXECUTE FUNCTION public.storage_row_account_guard();
CREATE TRIGGER storage_charge_insert AFTER INSERT ON public.geocode_retry
    REFERENCING NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_update AFTER UPDATE ON public.geocode_retry
    REFERENCING OLD TABLE AS storage_old_rows NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_delete AFTER DELETE ON public.geocode_retry
    REFERENCING OLD TABLE AS storage_old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();

ALTER TABLE public.geocode_discovery ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.geocode_discovery FORCE ROW LEVEL SECURITY;
CREATE FUNCTION public.storage_charge_geocode_discovery(r public.geocode_discovery) RETURNS bigint[]
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT ARRAY[128::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).last_unit::text, 'UTF8')), 0)::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint];
$body$;
CREATE TRIGGER storage_write_admission BEFORE INSERT OR UPDATE OR DELETE ON public.geocode_discovery
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_write_admission();
CREATE TRIGGER storage_row_account_guard AFTER UPDATE ON public.geocode_discovery
    FOR EACH ROW EXECUTE FUNCTION public.storage_row_account_guard();
CREATE TRIGGER storage_charge_insert AFTER INSERT ON public.geocode_discovery
    REFERENCING NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_update AFTER UPDATE ON public.geocode_discovery
    REFERENCING OLD TABLE AS storage_old_rows NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_delete AFTER DELETE ON public.geocode_discovery
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

CREATE FUNCTION public.geocode_device_generation() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.geocode_generation := 1;
    ELSIF OLD.enabled IS DISTINCT FROM NEW.enabled OR OLD.revoked_at IS DISTINCT FROM NEW.revoked_at THEN
        NEW.geocode_generation := OLD.geocode_generation + 1;
    ELSE
        NEW.geocode_generation := OLD.geocode_generation;
    END IF;
    RETURN NEW;
END
$body$;
CREATE TRIGGER geocode_device_generation BEFORE INSERT OR UPDATE ON public.tracking_devices
    FOR EACH ROW EXECUTE FUNCTION public.geocode_device_generation();

CREATE FUNCTION public.geocode_trip_generation() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.geocode_generation := 1;
    ELSIF public.ST_AsEWKB(OLD.start_geom::public.geometry, 'NDR') IS DISTINCT FROM public.ST_AsEWKB(NEW.start_geom::public.geometry, 'NDR')
       OR public.ST_AsEWKB(OLD.end_geom::public.geometry, 'NDR') IS DISTINCT FROM public.ST_AsEWKB(NEW.end_geom::public.geometry, 'NDR')
       OR OLD.start_place_id IS DISTINCT FROM NEW.start_place_id
       OR OLD.end_place_id IS DISTINCT FROM NEW.end_place_id
       OR OLD.tracking_device_id IS DISTINCT FROM NEW.tracking_device_id THEN
        NEW.geocode_generation := OLD.geocode_generation + 1;
    ELSE
        NEW.geocode_generation := OLD.geocode_generation;
    END IF;
    RETURN NEW;
END
$body$;
CREATE TRIGGER geocode_trip_generation BEFORE INSERT OR UPDATE ON public.trips
    FOR EACH ROW EXECUTE FUNCTION public.geocode_trip_generation();

CREATE FUNCTION public.geocode_endpoint_intents() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    changed_sql text;
    changed record;
BEGIN
    IF TG_OP = 'DELETE' THEN
        changed_sql := 'SELECT DISTINCT account_id FROM geocode_old_rows ORDER BY account_id';
    ELSIF TG_OP = 'INSERT' THEN
        changed_sql := 'SELECT DISTINCT account_id FROM geocode_new_rows ORDER BY account_id';
    ELSIF TG_TABLE_NAME = 'trips' THEN
        changed_sql := 'SELECT DISTINCT n.account_id FROM geocode_new_rows n JOIN geocode_old_rows o USING(id) '
            || 'WHERE n.geocode_generation <> o.geocode_generation ORDER BY n.account_id';
    ELSE
        changed_sql := 'SELECT DISTINCT n.account_id FROM geocode_new_rows n JOIN geocode_old_rows o USING(id) '
            || 'WHERE n.enabled IS DISTINCT FROM o.enabled OR n.revoked_at IS DISTINCT FROM o.revoked_at '
            || 'OR n.generation IS DISTINCT FROM o.generation ORDER BY n.account_id';
    END IF;
    FOR changed IN EXECUTE changed_sql LOOP
        UPDATE public.geocode_discovery SET generation = generation + 1 WHERE account_id = changed.account_id;
    END LOOP;
    IF TG_TABLE_NAME = 'trips' AND TG_OP <> 'DELETE' THEN
        EXECUTE 'INSERT INTO public.geocode_retry(account_id,rounded_lat,rounded_lon) '
            || 'SELECT DISTINCT n.account_id,ROUND(public.ST_Y(e.geom::public.geometry)::numeric,4), '
            || 'ROUND(public.ST_X(e.geom::public.geometry)::numeric,4) FROM geocode_new_rows n '
            || CASE WHEN TG_OP = 'UPDATE' THEN 'JOIN geocode_old_rows o USING(id) ' ELSE '' END
            || 'CROSS JOIN LATERAL (VALUES(n.start_geom,n.start_place_id),(n.end_geom,n.end_place_id)) e(geom,place_id) '
            || 'WHERE e.geom IS NOT NULL AND e.place_id IS NULL '
            || CASE WHEN TG_OP = 'UPDATE' THEN 'AND n.geocode_generation <> o.geocode_generation ' ELSE '' END
            || 'AND (n.tracking_device_id IS NULL OR EXISTS(SELECT 1 FROM public.tracking_devices d '
            || 'WHERE d.account_id=n.account_id AND d.id=n.tracking_device_id AND d.enabled AND d.revoked_at IS NULL)) '
            || 'AND NOT EXISTS(SELECT 1 FROM public.geocode_cache c WHERE c.account_id=n.account_id '
            || 'AND c.lat=ROUND(public.ST_Y(e.geom::public.geometry)::numeric,4) '
            || 'AND c.lon=ROUND(public.ST_X(e.geom::public.geometry)::numeric,4)) '
            || 'ORDER BY 1,2,3 ON CONFLICT DO NOTHING';
    END IF;
    RETURN NULL;
END
$body$;
CREATE TRIGGER geocode_intents_insert AFTER INSERT ON public.trips
    REFERENCING NEW TABLE AS geocode_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.geocode_endpoint_intents();
CREATE TRIGGER geocode_intents_update AFTER UPDATE ON public.trips
    REFERENCING OLD TABLE AS geocode_old_rows NEW TABLE AS geocode_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.geocode_endpoint_intents();
CREATE TRIGGER geocode_intents_delete AFTER DELETE ON public.trips
    REFERENCING OLD TABLE AS geocode_old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.geocode_endpoint_intents();
CREATE TRIGGER geocode_intents_insert AFTER INSERT ON public.tracking_devices
    REFERENCING NEW TABLE AS geocode_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.geocode_endpoint_intents();
CREATE TRIGGER geocode_intents_update AFTER UPDATE ON public.tracking_devices
    REFERENCING OLD TABLE AS geocode_old_rows NEW TABLE AS geocode_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.geocode_endpoint_intents();
CREATE TRIGGER geocode_intents_delete AFTER DELETE ON public.tracking_devices
    REFERENCING OLD TABLE AS geocode_old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.geocode_endpoint_intents();

CREATE TRIGGER geocode_intents_delete AFTER DELETE ON public.geocode_cache
    REFERENCING OLD TABLE AS geocode_old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.geocode_endpoint_intents();

CREATE FUNCTION public.geocode_account_init() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
BEGIN
    INSERT INTO public.geocode_discovery(account_id) VALUES(NEW.id);
    RETURN NEW;
END
$body$;
CREATE TRIGGER z_geocode_account_init AFTER INSERT ON public.accounts
    FOR EACH ROW EXECUTE FUNCTION public.geocode_account_init();

CREATE FUNCTION public.geocode_representative_source(owner_id bigint,lat numeric,lon numeric) RETURNS TABLE (
    trip_id bigint, trip_geocode_generation bigint, device_id bigint,
    device_generation bigint, device_geocode_generation bigint, endpoint_side text
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
BEGIN
    IF owner_id IS DISTINCT FROM NULLIF(pg_catalog.current_setting('app.account_id',true),'')::bigint OR owner_id IS NULL THEN
        RAISE EXCEPTION 'account scope required' USING ERRCODE='42501';
    END IF;
    RETURN QUERY
    WITH starts AS MATERIALIZED (
        SELECT t.id,t.geocode_generation,t.tracking_device_id,d.generation,d.geocode_generation AS eligibility_generation,'start'::text AS side
        FROM public.trips t LEFT JOIN public.tracking_devices d ON d.account_id=t.account_id AND d.id=t.tracking_device_id
        WHERE t.account_id=owner_id AND t.start_place_id IS NULL AND t.start_geom IS NOT NULL
            AND ROUND(public.ST_Y(t.start_geom::public.geometry)::numeric,4)=lat
            AND ROUND(public.ST_X(t.start_geom::public.geometry)::numeric,4)=lon
            AND (t.tracking_device_id IS NULL OR (d.enabled AND d.revoked_at IS NULL))
        ORDER BY t.id LIMIT 1
    ), ends AS MATERIALIZED (
        SELECT t.id,t.geocode_generation,t.tracking_device_id,d.generation,d.geocode_generation AS eligibility_generation,'end'::text AS side
        FROM public.trips t LEFT JOIN public.tracking_devices d ON d.account_id=t.account_id AND d.id=t.tracking_device_id
        WHERE t.account_id=owner_id AND t.end_place_id IS NULL AND t.end_geom IS NOT NULL
            AND ROUND(public.ST_Y(t.end_geom::public.geometry)::numeric,4)=lat
            AND ROUND(public.ST_X(t.end_geom::public.geometry)::numeric,4)=lon
            AND (t.tracking_device_id IS NULL OR (d.enabled AND d.revoked_at IS NULL))
        ORDER BY t.id LIMIT 1
    )
    SELECT candidate.id,candidate.geocode_generation,candidate.tracking_device_id,candidate.generation,candidate.eligibility_generation,candidate.side
    FROM (SELECT * FROM starts UNION ALL SELECT * FROM ends) candidate ORDER BY candidate.id LIMIT 1;
END
$body$;

CREATE FUNCTION public.geocode_record_coordinate_turn(owner_id bigint) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
BEGIN
    IF owner_id IS DISTINCT FROM NULLIF(pg_catalog.current_setting('app.account_id',true),'')::bigint OR owner_id IS NULL THEN
        RAISE EXCEPTION 'account scope required' USING ERRCODE='42501';
    END IF;
    PERFORM 1 FROM public.account_usage WHERE account_id=owner_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'account usage missing' USING ERRCODE='23514'; END IF;
    UPDATE public.geocode_discovery SET last_unit='coordinate' WHERE account_id=owner_id;
END
$body$;

CREATE FUNCTION public.geocode_discover_page(owner_id bigint) RETURNS TABLE (
    trips_scanned integer, queued_coordinates integer, cursor_trip_id bigint,
    generation bigint, scanned_generation bigint, ready boolean
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    state public.geocode_discovery;
    page_ids bigint[];
    page_count integer;
    queue_count integer;
BEGIN
    IF owner_id IS DISTINCT FROM NULLIF(pg_catalog.current_setting('app.account_id',true),'')::bigint OR owner_id IS NULL THEN
        RAISE EXCEPTION 'account scope required' USING ERRCODE='42501';
    END IF;
    PERFORM 1 FROM public.account_usage WHERE account_id=owner_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'account usage missing' USING ERRCODE='23514'; END IF;
    SELECT d.* INTO STRICT state FROM public.geocode_discovery d WHERE d.account_id=owner_id FOR UPDATE;
    IF state.cursor_trip_id=0 THEN state.round_generation:=state.generation; END IF;
    SELECT pg_catalog.array_agg(p.id ORDER BY p.id), COUNT(*)::integer INTO page_ids,page_count
    FROM (SELECT t.id FROM public.trips t WHERE t.account_id=owner_id AND t.id>state.cursor_trip_id
          ORDER BY t.id LIMIT 500) p;
    INSERT INTO public.geocode_retry(account_id,rounded_lat,rounded_lon)
    SELECT DISTINCT t.account_id, ROUND(public.ST_Y(e.geom::public.geometry)::numeric,4),
        ROUND(public.ST_X(e.geom::public.geometry)::numeric,4)
    FROM public.trips t
    CROSS JOIN LATERAL (VALUES(t.start_geom,t.start_place_id),(t.end_geom,t.end_place_id)) e(geom,place_id)
    WHERE t.account_id=owner_id AND t.id=ANY(page_ids) AND e.geom IS NOT NULL AND e.place_id IS NULL
        AND (t.tracking_device_id IS NULL OR EXISTS(SELECT 1 FROM public.tracking_devices d
            WHERE d.account_id=t.account_id AND d.id=t.tracking_device_id AND d.enabled AND d.revoked_at IS NULL))
        AND NOT EXISTS(SELECT 1 FROM public.geocode_cache c WHERE c.account_id=t.account_id
            AND c.lat=ROUND(public.ST_Y(e.geom::public.geometry)::numeric,4)
            AND c.lon=ROUND(public.ST_X(e.geom::public.geometry)::numeric,4))
    ORDER BY 1,2,3 ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS queue_count = ROW_COUNT;
    IF page_count<500 THEN
        state.cursor_trip_id:=0;
        state.scanned_generation:=state.round_generation;
    ELSE
        state.cursor_trip_id:=page_ids[page_count];
    END IF;
    UPDATE public.geocode_discovery d SET cursor_trip_id=state.cursor_trip_id,
        round_generation=state.round_generation, scanned_generation=state.scanned_generation,last_unit='discovery'
    WHERE d.account_id=owner_id;
    RETURN QUERY SELECT page_count,queue_count,state.cursor_trip_id,state.generation,state.scanned_generation,
        state.cursor_trip_id<>0 OR state.generation<>state.scanned_generation;
END
$body$;
REVOKE ALL ON FUNCTION public.storage_charge_geocode_retry(public.geocode_retry) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_charge_geocode_discovery(public.geocode_discovery) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.geocode_trip_generation() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.geocode_device_generation() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.geocode_endpoint_intents() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.geocode_account_init() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.geocode_representative_source(bigint,numeric,numeric) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.geocode_record_coordinate_turn(bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.geocode_discover_page(bigint) FROM PUBLIC;
DO $permissions$
BEGIN
    IF to_regclass('odograph_service.managed_role_state') IS NOT NULL THEN
        ALTER TABLE public.geocode_retry OWNER TO odograph_migrate;
        REVOKE ALL ON public.geocode_retry FROM PUBLIC,odograph_runtime,odograph_control,odograph_bootstrap;
        GRANT SELECT,INSERT,UPDATE,DELETE ON public.geocode_retry TO odograph_runtime;
        GRANT SELECT,DELETE ON public.geocode_retry TO odograph_bootstrap;
        CREATE POLICY account_isolation ON public.geocode_retry FOR ALL TO odograph_runtime
            USING (account_id = NULLIF(current_setting('app.account_id',true),'')::bigint) WITH CHECK (account_id = NULLIF(current_setting('app.account_id',true),'')::bigint);
        CREATE POLICY migration_writer ON public.geocode_retry FOR ALL TO odograph_migrate USING(true) WITH CHECK(true);
        CREATE POLICY bootstrap_defaults ON public.geocode_retry FOR ALL TO odograph_bootstrap USING(true) WITH CHECK(true);
        ALTER TABLE public.geocode_discovery OWNER TO odograph_migrate;
        REVOKE ALL ON public.geocode_discovery FROM PUBLIC,odograph_runtime,odograph_control,odograph_bootstrap;
        GRANT SELECT ON public.geocode_discovery TO odograph_runtime;
        GRANT SELECT,DELETE ON public.geocode_discovery TO odograph_bootstrap;
        CREATE POLICY account_isolation ON public.geocode_discovery FOR SELECT TO odograph_runtime
            USING (account_id = NULLIF(current_setting('app.account_id',true),'')::bigint);
        CREATE POLICY migration_writer ON public.geocode_discovery FOR ALL TO odograph_migrate USING(true) WITH CHECK(true);
        CREATE POLICY bootstrap_defaults ON public.geocode_discovery FOR ALL TO odograph_bootstrap USING(true) WITH CHECK(true);
        ALTER FUNCTION public.storage_charge_geocode_retry(public.geocode_retry) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_charge_geocode_retry(public.geocode_retry) FROM PUBLIC,odograph_runtime,odograph_control,odograph_bootstrap;
        ALTER FUNCTION public.storage_charge_geocode_discovery(public.geocode_discovery) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_charge_geocode_discovery(public.geocode_discovery) FROM PUBLIC,odograph_runtime,odograph_control,odograph_bootstrap;
        ALTER FUNCTION public.geocode_device_generation() OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.geocode_device_generation() FROM PUBLIC,odograph_runtime,odograph_control,odograph_bootstrap;
        ALTER FUNCTION public.geocode_trip_generation() OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.geocode_trip_generation() FROM PUBLIC,odograph_runtime,odograph_control,odograph_bootstrap;
        ALTER FUNCTION public.geocode_endpoint_intents() OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.geocode_endpoint_intents() FROM PUBLIC,odograph_runtime,odograph_control,odograph_bootstrap;
        ALTER FUNCTION public.geocode_account_init() OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.geocode_account_init() FROM PUBLIC,odograph_runtime,odograph_control,odograph_bootstrap;
        ALTER FUNCTION public.geocode_representative_source(bigint,numeric,numeric) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.geocode_representative_source(bigint,numeric,numeric) FROM PUBLIC,odograph_runtime,odograph_control,odograph_bootstrap;
        ALTER FUNCTION public.geocode_record_coordinate_turn(bigint) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.geocode_record_coordinate_turn(bigint) FROM PUBLIC,odograph_runtime,odograph_control,odograph_bootstrap;
        ALTER FUNCTION public.geocode_discover_page(bigint) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.geocode_discover_page(bigint) FROM PUBLIC,odograph_runtime,odograph_control,odograph_bootstrap;
        GRANT EXECUTE ON FUNCTION public.geocode_representative_source(bigint,numeric,numeric),public.geocode_record_coordinate_turn(bigint),public.geocode_discover_page(bigint) TO odograph_runtime;
    END IF;
END
$permissions$;
INSERT INTO public.geocode_discovery(account_id) SELECT id FROM public.accounts ORDER BY id;
SELECT public.reconcile_storage_usage();
