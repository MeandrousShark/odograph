-- Versioned logical stored-value accounting. These are not physical disk bytes.
CREATE TABLE public.account_usage (
    account_id bigint PRIMARY KEY REFERENCES public.accounts(id) ON DELETE CASCADE,
    charge_version smallint NOT NULL DEFAULT 1 CHECK (charge_version = 1),
    actual_bytes bigint NOT NULL DEFAULT 0,
    reserved_bytes bigint NOT NULL DEFAULT 0,
    raw_bytes bigint NOT NULL DEFAULT 0,
    enhancement_bytes bigint NOT NULL DEFAULT 0
);
CREATE TABLE public.device_storage_envelopes (
    account_id bigint NOT NULL,
    tracking_device_id bigint PRIMARY KEY,
    label_bytes bigint NOT NULL DEFAULT 0 CHECK (label_bytes >= 0),
    point_count bigint NOT NULL DEFAULT 0,
    stay_count bigint NOT NULL DEFAULT 0,
    trip_count bigint NOT NULL DEFAULT 0,
    path_vertices bigint NOT NULL DEFAULT 0,
    core_bytes bigint NOT NULL DEFAULT 0,
    FOREIGN KEY (account_id, tracking_device_id)
        REFERENCES public.tracking_devices(account_id, id) ON DELETE CASCADE
);

CREATE FUNCTION public.storage_charge_raw_messages(r public.raw_messages) RETURNS bigint[]
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT ARRAY[128::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).payload::text, 'UTF8')), 0)::bigint, 128::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).payload::text, 'UTF8')), 0)::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint];
$body$;

CREATE FUNCTION public.storage_charge_points(r public.points) RETURNS bigint[]
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT ARRAY[256::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).device::text, 'UTF8')), 0)::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).trigger::text, 'UTF8')), 0)::bigint + COALESCE(pg_catalog.octet_length(public.ST_AsEWKB((r).geom::public.geometry, 'NDR')), 0)::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint];
$body$;

CREATE FUNCTION public.storage_charge_stays(r public.stays) RETURNS bigint[]
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT ARRAY[0::bigint, 0::bigint, 0::bigint, 256::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).device::text, 'UTF8')), 0)::bigint + COALESCE(pg_catalog.octet_length(public.ST_AsEWKB((r).centroid::public.geometry, 'NDR')), 0)::bigint, 1::bigint, 0::bigint, 0::bigint];
$body$;

CREATE FUNCTION public.storage_charge_trips(r public.trips) RETURNS bigint[]
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT ARRAY[CASE WHEN (r).source = 'detected' AND NOT (r).imported THEN COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).notes::text, 'UTF8')), 0)::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).purpose::text, 'UTF8')), 0)::bigint + COALESCE(pg_catalog.octet_length(public.ST_AsEWKB((r).path_snapped::public.geometry, 'NDR')), 0)::bigint ELSE 512::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).device::text, 'UTF8')), 0)::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).notes::text, 'UTF8')), 0)::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).purpose::text, 'UTF8')), 0)::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).start_label::text, 'UTF8')), 0)::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).end_label::text, 'UTF8')), 0)::bigint + COALESCE(pg_catalog.octet_length(public.ST_AsEWKB((r).start_geom::public.geometry, 'NDR')), 0)::bigint + COALESCE(pg_catalog.octet_length(public.ST_AsEWKB((r).end_geom::public.geometry, 'NDR')), 0)::bigint + COALESCE(pg_catalog.octet_length(public.ST_AsEWKB((r).path::public.geometry, 'NDR')), 0)::bigint + COALESCE(pg_catalog.octet_length(public.ST_AsEWKB((r).path_snapped::public.geometry, 'NDR')), 0)::bigint END, 0::bigint, COALESCE(pg_catalog.octet_length(public.ST_AsEWKB((r).path_snapped::public.geometry, 'NDR')), 0)::bigint, CASE WHEN (r).source = 'detected' AND NOT (r).imported THEN 512::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).device::text, 'UTF8')), 0)::bigint + COALESCE(pg_catalog.octet_length(public.ST_AsEWKB((r).start_geom::public.geometry, 'NDR')), 0)::bigint + COALESCE(pg_catalog.octet_length(public.ST_AsEWKB((r).end_geom::public.geometry, 'NDR')), 0)::bigint + COALESCE(pg_catalog.octet_length(public.ST_AsEWKB((r).path::public.geometry, 'NDR')), 0)::bigint ELSE 0::bigint END, 0::bigint, CASE WHEN (r).source = 'detected' AND NOT (r).imported THEN 1::bigint ELSE 0::bigint END, CASE WHEN (r).source = 'detected' AND NOT (r).imported THEN COALESCE(public.ST_NPoints((r).path), 0)::bigint ELSE 0::bigint END];
$body$;

CREATE FUNCTION public.storage_charge_detector_state(r public.detector_state) RETURNS bigint[]
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT ARRAY[128::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint];
$body$;

CREATE FUNCTION public.storage_charge_places(r public.places) RETURNS bigint[]
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT ARRAY[128::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).name::text, 'UTF8')), 0)::bigint + COALESCE(pg_catalog.octet_length(public.ST_AsEWKB((r).geom::public.geometry, 'NDR')), 0)::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint];
$body$;

CREATE FUNCTION public.storage_charge_tag_rules(r public.tag_rules) RETURNS bigint[]
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT ARRAY[128::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint];
$body$;

CREATE FUNCTION public.storage_charge_geocode_cache(r public.geocode_cache) RETURNS bigint[]
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT ARRAY[128::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).address::text, 'UTF8')), 0)::bigint, 0::bigint, 128::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).address::text, 'UTF8')), 0)::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint];
$body$;

CREATE FUNCTION public.storage_charge_trip_boundary_overrides(r public.trip_boundary_overrides) RETURNS bigint[]
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT ARRAY[128::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).device::text, 'UTF8')), 0)::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint];
$body$;

CREATE FUNCTION public.storage_charge_vehicles(r public.vehicles) RETURNS bigint[]
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT ARRAY[128::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).name::text, 'UTF8')), 0)::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).make::text, 'UTF8')), 0)::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).model::text, 'UTF8')), 0)::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).plate::text, 'UTF8')), 0)::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint];
$body$;

CREATE FUNCTION public.storage_charge_mileage_rates(r public.mileage_rates) RETURNS bigint[]
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT ARRAY[128::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint];
$body$;

CREATE FUNCTION public.storage_charge_odometer_readings(r public.odometer_readings) RETURNS bigint[]
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT ARRAY[128::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).note::text, 'UTF8')), 0)::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint];
$body$;

CREATE FUNCTION public.storage_charge_expenses(r public.expenses) RETURNS bigint[]
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT ARRAY[128::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).notes::text, 'UTF8')), 0)::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint];
$body$;

CREATE FUNCTION public.storage_charge_nudge_delivery_windows(r public.nudge_delivery_windows) RETURNS bigint[]
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT ARRAY[128::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint];
$body$;

CREATE FUNCTION public.storage_charge_odometer_reminder_windows(r public.odometer_reminder_windows) RETURNS bigint[]
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT ARRAY[128::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint];
$body$;

CREATE FUNCTION public.storage_charge_email_deliveries(r public.email_deliveries) RETURNS bigint[]
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT ARRAY[128::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).kind::text, 'UTF8')), 0)::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint];
$body$;

CREATE FUNCTION public.storage_charge_account_settings(r public.account_settings) RETURNS bigint[]
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT ARRAY[128::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).display_tz::text, 'UTF8')), 0)::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).ntfy_topic::text, 'UTF8')), 0)::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).email_to::text, 'UTF8')), 0)::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).email_filing_reminder_mmdd::text, 'UTF8')), 0)::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint];
$body$;

CREATE FUNCTION public.storage_charge_tracking_devices(r public.tracking_devices) RETURNS bigint[]
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT ARRAY[128::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).label::text, 'UTF8')), 0)::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint];
$body$;

CREATE FUNCTION public.storage_charge_tracking_device_aliases(r public.tracking_device_aliases) RETURNS bigint[]
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT ARRAY[128::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).original_label::text, 'UTF8')), 0)::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint];
$body$;

CREATE FUNCTION public.storage_charge_ingest_credentials(r public.ingest_credentials) RETURNS bigint[]
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT ARRAY[128::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).public_id::text, 'UTF8')), 0)::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).basic_username::text, 'UTF8')), 0)::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).secret_hash::text, 'UTF8')), 0)::bigint + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to((r).kind::text, 'UTF8')), 0)::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint];
$body$;

CREATE FUNCTION public.storage_account_init() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
BEGIN
    INSERT INTO public.account_usage (account_id, actual_bytes)
    VALUES (NEW.id, 128 + COALESCE(pg_catalog.octet_length(NEW.avatar_bytes), 0)::bigint
        + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to(NEW.avatar_mime, 'UTF8')), 0)::bigint);
    RETURN NULL;
END
$body$;
CREATE FUNCTION public.storage_avatar_change() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
BEGIN
    UPDATE public.account_usage SET actual_bytes = actual_bytes
        + COALESCE(pg_catalog.octet_length(NEW.avatar_bytes), 0)::bigint
        + COALESCE(pg_catalog.octet_length(pg_catalog.convert_to(NEW.avatar_mime, 'UTF8')), 0)::bigint
        - COALESCE(pg_catalog.octet_length(OLD.avatar_bytes), 0)::bigint
        - COALESCE(pg_catalog.octet_length(pg_catalog.convert_to(OLD.avatar_mime, 'UTF8')), 0)::bigint
    WHERE account_id = NEW.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'missing logical storage usage' USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END
$body$;
CREATE FUNCTION public.storage_row_account_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
BEGIN
    IF OLD.account_id <> NEW.account_id THEN
        RAISE EXCEPTION 'personal row account cannot change' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$body$;
CREATE FUNCTION public.storage_apply_statement() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    deltas record;
    previous_reserve bigint;
    next_reserve bigint;
    stream_expression text;
    label_expression text;
    point_expression text;
    stored_query text := '';
BEGIN
    -- Transition relations are PostgreSQL's bound stored OLD/NEW rows. Aggregate
    -- before updating counters so one bulk statement does not build a long HOT
    -- chain on the same account/device row for every input point.
    stream_expression := CASE WHEN TG_TABLE_NAME = 'tracking_devices' THEN 'r.id'
        WHEN TG_TABLE_NAME IN ('points','stays') THEN 'r.tracking_device_id'
        WHEN TG_TABLE_NAME = 'trips' THEN
            'CASE WHEN r.source = ''detected'' AND NOT r.imported THEN r.tracking_device_id END'
        ELSE 'NULL::bigint' END;
    label_expression := CASE WHEN TG_TABLE_NAME = 'tracking_devices'
        THEN 'pg_catalog.octet_length(pg_catalog.convert_to(r.label,''UTF8''))::bigint'
        WHEN TG_TABLE_NAME = 'stays'
        THEN 'pg_catalog.octet_length(pg_catalog.convert_to(r.device,''UTF8''))::bigint'
        WHEN TG_TABLE_NAME = 'trips' THEN
            'CASE WHEN r.source = ''detected'' AND NOT r.imported THEN pg_catalog.octet_length(pg_catalog.convert_to(r.device,''UTF8''))::bigint ELSE 0::bigint END'
        ELSE '0::bigint' END;
    point_expression := CASE WHEN TG_TABLE_NAME = 'points' THEN '1::bigint' ELSE '0::bigint' END;
    IF TG_OP IN ('UPDATE','DELETE') THEN
        stored_query := pg_catalog.format(
            'SELECT r.account_id AS owner_id,%s AS stream_id,%s AS label_bytes,%s AS points,'
            'public.storage_charge_%I(r::public.%I) AS charge,-1::bigint AS direction FROM storage_old_rows r',
            stream_expression,label_expression,point_expression,TG_TABLE_NAME,TG_TABLE_NAME);
    END IF;
    IF TG_OP = 'UPDATE' THEN stored_query := stored_query || ' UNION ALL '; END IF;
    IF TG_OP IN ('INSERT','UPDATE') THEN
        stored_query := stored_query || pg_catalog.format(
            'SELECT r.account_id AS owner_id,%s AS stream_id,%s AS label_bytes,%s AS points,'
            'public.storage_charge_%I(r::public.%I) AS charge,1::bigint AS direction FROM storage_new_rows r',
            stream_expression,label_expression,point_expression,TG_TABLE_NAME,TG_TABLE_NAME);
    END IF;
    FOR deltas IN EXECUTE 'WITH stored AS MATERIALIZED (' || stored_query || ') '
        'SELECT owner_id,stream_id,MAX(label_bytes) AS label_bytes,'
        'SUM(direction * points)::bigint AS points,'
        'SUM(direction * charge[1])::bigint AS actual_bytes,'
        'SUM(direction * charge[2])::bigint AS raw_bytes,'
        'SUM(direction * charge[3])::bigint AS enhancement_bytes,'
        'SUM(direction * charge[4])::bigint AS core_bytes,'
        'SUM(direction * charge[5])::bigint AS stays,'
        'SUM(direction * charge[6])::bigint AS trips,'
        'SUM(direction * charge[7])::bigint AS vertices '
        'FROM stored GROUP BY owner_id,stream_id ORDER BY owner_id,stream_id NULLS FIRST'
    LOOP
        PERFORM 1 FROM public.account_usage WHERE account_id = deltas.owner_id FOR UPDATE;
        IF NOT FOUND THEN
            IF NOT EXISTS (SELECT 1 FROM public.accounts WHERE id = deltas.owner_id) THEN
                CONTINUE;
            END IF;
            RAISE EXCEPTION 'missing logical storage usage' USING ERRCODE = '23514';
        END IF;
        IF deltas.actual_bytes <> 0 OR deltas.raw_bytes <> 0 OR deltas.enhancement_bytes <> 0 THEN
            UPDATE public.account_usage SET actual_bytes = actual_bytes + deltas.actual_bytes,
                raw_bytes = raw_bytes + deltas.raw_bytes,
                enhancement_bytes = enhancement_bytes + deltas.enhancement_bytes
            WHERE account_id = deltas.owner_id;
        END IF;
        IF deltas.stream_id IS NULL OR NOT EXISTS (SELECT 1 FROM public.tracking_devices
            WHERE account_id = deltas.owner_id AND id = deltas.stream_id) THEN
            CONTINUE;
        END IF;
        INSERT INTO public.device_storage_envelopes (account_id,tracking_device_id,label_bytes)
        SELECT deltas.owner_id,deltas.stream_id,pg_catalog.octet_length(pg_catalog.convert_to(d.label,'UTF8'))
        FROM public.tracking_devices d WHERE d.account_id = deltas.owner_id AND d.id = deltas.stream_id
        ON CONFLICT (tracking_device_id) DO NOTHING;
        SELECT point_count * (1024 + 2 * label_bytes) INTO previous_reserve
        FROM public.device_storage_envelopes WHERE tracking_device_id = deltas.stream_id;
        UPDATE public.device_storage_envelopes
        SET label_bytes = GREATEST(label_bytes,deltas.label_bytes),
            point_count = point_count + deltas.points,
            stay_count = stay_count + deltas.stays,
            trip_count = trip_count + deltas.trips,
            path_vertices = path_vertices + deltas.vertices,
            core_bytes = core_bytes + deltas.core_bytes
        WHERE tracking_device_id = deltas.stream_id AND
            (label_bytes < deltas.label_bytes OR deltas.points <> 0 OR deltas.stays <> 0
             OR deltas.trips <> 0 OR deltas.vertices <> 0 OR deltas.core_bytes <> 0)
        RETURNING point_count * (1024 + 2 * label_bytes) INTO next_reserve;
        IF FOUND AND next_reserve <> previous_reserve THEN
            UPDATE public.account_usage SET reserved_bytes = reserved_bytes + next_reserve - previous_reserve
            WHERE account_id = deltas.owner_id;
        END IF;
    END LOOP;
    RETURN NULL;
END
$body$;
CREATE FUNCTION public.storage_check_envelope() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE state public.device_storage_envelopes;
BEGIN
    SELECT * INTO state FROM public.device_storage_envelopes
    WHERE tracking_device_id = COALESCE(NEW.tracking_device_id, OLD.tracking_device_id);
    IF NOT FOUND THEN RETURN NULL; END IF;
    IF state.point_count < 0 OR state.stay_count < 0 OR state.trip_count < 0
       OR state.path_vertices < 0 OR state.core_bytes < 0
       OR state.stay_count > state.point_count
       OR state.trip_count > GREATEST(state.point_count - 1, 0)
       OR state.path_vertices > 2 * state.point_count
       OR state.core_bytes > state.point_count * (1024 + 2 * state.label_bytes) THEN
        RAISE EXCEPTION 'stored detector output exceeds retained point envelope' USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END
$body$;

CREATE FUNCTION public.storage_expected_usage() RETURNS TABLE (
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
CREATE FUNCTION public.storage_expected_envelopes() RETURNS TABLE (
    account_id bigint, tracking_device_id bigint, label_bytes bigint, point_count bigint,
    stay_count bigint, trip_count bigint, path_vertices bigint, core_bytes bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
    WITH outputs AS (
        SELECT p.account_id, p.tracking_device_id, 0::bigint AS label_bytes,
            1::bigint AS point_count, ARRAY[0,0,0,0,0,0,0]::bigint[] AS charge FROM public.points p
        UNION ALL
        SELECT s.account_id, s.tracking_device_id, pg_catalog.octet_length(pg_catalog.convert_to(s.device,'UTF8'))::bigint,
            0::bigint, public.storage_charge_stays(s) FROM public.stays s
        UNION ALL
        SELECT t.account_id, t.tracking_device_id, pg_catalog.octet_length(pg_catalog.convert_to(t.device,'UTF8'))::bigint,
            0::bigint, public.storage_charge_trips(t) FROM public.trips t WHERE t.source = 'detected' AND NOT t.imported
    ), sums AS (
        SELECT o.account_id, o.tracking_device_id, MAX(o.label_bytes) AS label_bytes,
            SUM(o.point_count)::bigint AS point_count, SUM(o.charge[5])::bigint AS stay_count,
            SUM(o.charge[6])::bigint AS trip_count, SUM(o.charge[7])::bigint AS path_vertices,
            SUM(o.charge[4])::bigint AS core_bytes FROM outputs o GROUP BY o.account_id, o.tracking_device_id
    )
    SELECT d.account_id, d.id,
        GREATEST(pg_catalog.octet_length(pg_catalog.convert_to(d.label,'UTF8'))::bigint, COALESCE(s.label_bytes,0), COALESCE(e.label_bytes,0)),
        COALESCE(s.point_count,0), COALESCE(s.stay_count,0), COALESCE(s.trip_count,0), COALESCE(s.path_vertices,0), COALESCE(s.core_bytes,0)
    FROM public.tracking_devices d LEFT JOIN sums s ON s.tracking_device_id = d.id
    LEFT JOIN public.device_storage_envelopes e ON e.tracking_device_id = d.id;
$body$;
CREATE FUNCTION public.storage_usage_consistent() RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT NOT EXISTS (SELECT 1 FROM public.account_usage WHERE charge_version <> 1
        OR actual_bytes < 0 OR reserved_bytes < 0 OR raw_bytes < 0 OR enhancement_bytes < 0
        OR raw_bytes > actual_bytes OR enhancement_bytes > actual_bytes)
    AND NOT EXISTS (SELECT 1 FROM public.device_storage_envelopes e
        WHERE e.point_count < 0 OR e.stay_count < 0 OR e.trip_count < 0 OR e.path_vertices < 0 OR e.core_bytes < 0
        OR e.stay_count > e.point_count OR e.trip_count > GREATEST(e.point_count - 1, 0)
        OR e.path_vertices > 2 * e.point_count
        OR e.core_bytes > e.point_count * (1024 + 2 * e.label_bytes)) AND NOT EXISTS (
        (SELECT account_id,actual_bytes,reserved_bytes,raw_bytes,enhancement_bytes FROM public.account_usage
         EXCEPT SELECT * FROM public.storage_expected_usage())
        UNION ALL
        (SELECT * FROM public.storage_expected_usage()
         EXCEPT SELECT account_id,actual_bytes,reserved_bytes,raw_bytes,enhancement_bytes FROM public.account_usage)
    ) AND NOT EXISTS (
        (SELECT * FROM public.device_storage_envelopes EXCEPT SELECT * FROM public.storage_expected_envelopes())
        UNION ALL
        (SELECT * FROM public.storage_expected_envelopes() EXCEPT SELECT * FROM public.device_storage_envelopes)
    );
$body$;
CREATE FUNCTION public.reconcile_storage_usage() RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
BEGIN
    -- Migration/restore authority runs this under its maintenance lock, never runtime.
    INSERT INTO public.device_storage_envelopes
    SELECT * FROM public.storage_expected_envelopes()
    ON CONFLICT (tracking_device_id) DO UPDATE SET label_bytes = EXCLUDED.label_bytes,
        point_count = EXCLUDED.point_count, stay_count = EXCLUDED.stay_count, trip_count = EXCLUDED.trip_count,
        path_vertices = EXCLUDED.path_vertices, core_bytes = EXCLUDED.core_bytes;
    INSERT INTO public.account_usage(account_id,actual_bytes,reserved_bytes,raw_bytes,enhancement_bytes)
    SELECT * FROM public.storage_expected_usage()
    ON CONFLICT (account_id) DO UPDATE SET actual_bytes = EXCLUDED.actual_bytes,
        reserved_bytes = EXCLUDED.reserved_bytes, raw_bytes = EXCLUDED.raw_bytes,
        enhancement_bytes = EXCLUDED.enhancement_bytes, charge_version = 1;
END
$body$;

ALTER TABLE public.account_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.account_usage FORCE ROW LEVEL SECURITY;

ALTER TABLE public.device_storage_envelopes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.device_storage_envelopes FORCE ROW LEVEL SECURITY;

CREATE FUNCTION public.storage_write_admission() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE owner_id bigint;
BEGIN
    IF session_user = 'odograph_runtime' OR current_setting('role', true) = 'odograph_runtime' THEN
        owner_id := NULLIF(current_setting('app.account_id', true), '')::bigint;
        IF owner_id IS NULL THEN
            -- Forced RLS preserves empty UPDATE/DELETE and denied INSERT.
            RETURN NULL;
        END IF;
        PERFORM 1 FROM public.account_usage WHERE account_id = owner_id FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'missing logical storage usage' USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NULL;
END
$body$;

CREATE TRIGGER storage_write_admission BEFORE INSERT OR UPDATE OR DELETE ON public.raw_messages
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_write_admission();
CREATE TRIGGER storage_row_account_guard AFTER UPDATE ON public.raw_messages
    FOR EACH ROW EXECUTE FUNCTION public.storage_row_account_guard();
CREATE TRIGGER storage_charge_insert AFTER INSERT ON public.raw_messages
    REFERENCING NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_update AFTER UPDATE ON public.raw_messages
    REFERENCING OLD TABLE AS storage_old_rows NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_delete AFTER DELETE ON public.raw_messages
    REFERENCING OLD TABLE AS storage_old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();

CREATE TRIGGER storage_write_admission BEFORE INSERT OR UPDATE OR DELETE ON public.points
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_write_admission();
CREATE TRIGGER storage_row_account_guard AFTER UPDATE ON public.points
    FOR EACH ROW EXECUTE FUNCTION public.storage_row_account_guard();
CREATE TRIGGER storage_charge_insert AFTER INSERT ON public.points
    REFERENCING NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_update AFTER UPDATE ON public.points
    REFERENCING OLD TABLE AS storage_old_rows NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_delete AFTER DELETE ON public.points
    REFERENCING OLD TABLE AS storage_old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();

CREATE TRIGGER storage_write_admission BEFORE INSERT OR UPDATE OR DELETE ON public.stays
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_write_admission();
CREATE TRIGGER storage_row_account_guard AFTER UPDATE ON public.stays
    FOR EACH ROW EXECUTE FUNCTION public.storage_row_account_guard();
CREATE TRIGGER storage_charge_insert AFTER INSERT ON public.stays
    REFERENCING NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_update AFTER UPDATE ON public.stays
    REFERENCING OLD TABLE AS storage_old_rows NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_delete AFTER DELETE ON public.stays
    REFERENCING OLD TABLE AS storage_old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();

CREATE TRIGGER storage_write_admission BEFORE INSERT OR UPDATE OR DELETE ON public.trips
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_write_admission();
CREATE TRIGGER storage_row_account_guard AFTER UPDATE ON public.trips
    FOR EACH ROW EXECUTE FUNCTION public.storage_row_account_guard();
CREATE TRIGGER storage_charge_insert AFTER INSERT ON public.trips
    REFERENCING NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_update AFTER UPDATE ON public.trips
    REFERENCING OLD TABLE AS storage_old_rows NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_delete AFTER DELETE ON public.trips
    REFERENCING OLD TABLE AS storage_old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();

CREATE TRIGGER storage_write_admission BEFORE INSERT OR UPDATE OR DELETE ON public.detector_state
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_write_admission();
CREATE TRIGGER storage_row_account_guard AFTER UPDATE ON public.detector_state
    FOR EACH ROW EXECUTE FUNCTION public.storage_row_account_guard();
CREATE TRIGGER storage_charge_insert AFTER INSERT ON public.detector_state
    REFERENCING NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_update AFTER UPDATE ON public.detector_state
    REFERENCING OLD TABLE AS storage_old_rows NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_delete AFTER DELETE ON public.detector_state
    REFERENCING OLD TABLE AS storage_old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();

CREATE TRIGGER storage_write_admission BEFORE INSERT OR UPDATE OR DELETE ON public.places
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_write_admission();
CREATE TRIGGER storage_row_account_guard AFTER UPDATE ON public.places
    FOR EACH ROW EXECUTE FUNCTION public.storage_row_account_guard();
CREATE TRIGGER storage_charge_insert AFTER INSERT ON public.places
    REFERENCING NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_update AFTER UPDATE ON public.places
    REFERENCING OLD TABLE AS storage_old_rows NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_delete AFTER DELETE ON public.places
    REFERENCING OLD TABLE AS storage_old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();

CREATE TRIGGER storage_write_admission BEFORE INSERT OR UPDATE OR DELETE ON public.tag_rules
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_write_admission();
CREATE TRIGGER storage_row_account_guard AFTER UPDATE ON public.tag_rules
    FOR EACH ROW EXECUTE FUNCTION public.storage_row_account_guard();
CREATE TRIGGER storage_charge_insert AFTER INSERT ON public.tag_rules
    REFERENCING NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_update AFTER UPDATE ON public.tag_rules
    REFERENCING OLD TABLE AS storage_old_rows NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_delete AFTER DELETE ON public.tag_rules
    REFERENCING OLD TABLE AS storage_old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();

CREATE TRIGGER storage_write_admission BEFORE INSERT OR UPDATE OR DELETE ON public.geocode_cache
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_write_admission();
CREATE TRIGGER storage_row_account_guard AFTER UPDATE ON public.geocode_cache
    FOR EACH ROW EXECUTE FUNCTION public.storage_row_account_guard();
CREATE TRIGGER storage_charge_insert AFTER INSERT ON public.geocode_cache
    REFERENCING NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_update AFTER UPDATE ON public.geocode_cache
    REFERENCING OLD TABLE AS storage_old_rows NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_delete AFTER DELETE ON public.geocode_cache
    REFERENCING OLD TABLE AS storage_old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();

CREATE TRIGGER storage_write_admission BEFORE INSERT OR UPDATE OR DELETE ON public.trip_boundary_overrides
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_write_admission();
CREATE TRIGGER storage_row_account_guard AFTER UPDATE ON public.trip_boundary_overrides
    FOR EACH ROW EXECUTE FUNCTION public.storage_row_account_guard();
CREATE TRIGGER storage_charge_insert AFTER INSERT ON public.trip_boundary_overrides
    REFERENCING NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_update AFTER UPDATE ON public.trip_boundary_overrides
    REFERENCING OLD TABLE AS storage_old_rows NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_delete AFTER DELETE ON public.trip_boundary_overrides
    REFERENCING OLD TABLE AS storage_old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();

CREATE TRIGGER storage_write_admission BEFORE INSERT OR UPDATE OR DELETE ON public.vehicles
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_write_admission();
CREATE TRIGGER storage_row_account_guard AFTER UPDATE ON public.vehicles
    FOR EACH ROW EXECUTE FUNCTION public.storage_row_account_guard();
CREATE TRIGGER storage_charge_insert AFTER INSERT ON public.vehicles
    REFERENCING NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_update AFTER UPDATE ON public.vehicles
    REFERENCING OLD TABLE AS storage_old_rows NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_delete AFTER DELETE ON public.vehicles
    REFERENCING OLD TABLE AS storage_old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();

CREATE TRIGGER storage_write_admission BEFORE INSERT OR UPDATE OR DELETE ON public.mileage_rates
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_write_admission();
CREATE TRIGGER storage_row_account_guard AFTER UPDATE ON public.mileage_rates
    FOR EACH ROW EXECUTE FUNCTION public.storage_row_account_guard();
CREATE TRIGGER storage_charge_insert AFTER INSERT ON public.mileage_rates
    REFERENCING NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_update AFTER UPDATE ON public.mileage_rates
    REFERENCING OLD TABLE AS storage_old_rows NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_delete AFTER DELETE ON public.mileage_rates
    REFERENCING OLD TABLE AS storage_old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();

CREATE TRIGGER storage_write_admission BEFORE INSERT OR UPDATE OR DELETE ON public.odometer_readings
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_write_admission();
CREATE TRIGGER storage_row_account_guard AFTER UPDATE ON public.odometer_readings
    FOR EACH ROW EXECUTE FUNCTION public.storage_row_account_guard();
CREATE TRIGGER storage_charge_insert AFTER INSERT ON public.odometer_readings
    REFERENCING NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_update AFTER UPDATE ON public.odometer_readings
    REFERENCING OLD TABLE AS storage_old_rows NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_delete AFTER DELETE ON public.odometer_readings
    REFERENCING OLD TABLE AS storage_old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();

CREATE TRIGGER storage_write_admission BEFORE INSERT OR UPDATE OR DELETE ON public.expenses
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_write_admission();
CREATE TRIGGER storage_row_account_guard AFTER UPDATE ON public.expenses
    FOR EACH ROW EXECUTE FUNCTION public.storage_row_account_guard();
CREATE TRIGGER storage_charge_insert AFTER INSERT ON public.expenses
    REFERENCING NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_update AFTER UPDATE ON public.expenses
    REFERENCING OLD TABLE AS storage_old_rows NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_delete AFTER DELETE ON public.expenses
    REFERENCING OLD TABLE AS storage_old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();

CREATE TRIGGER storage_write_admission BEFORE INSERT OR UPDATE OR DELETE ON public.nudge_delivery_windows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_write_admission();
CREATE TRIGGER storage_row_account_guard AFTER UPDATE ON public.nudge_delivery_windows
    FOR EACH ROW EXECUTE FUNCTION public.storage_row_account_guard();
CREATE TRIGGER storage_charge_insert AFTER INSERT ON public.nudge_delivery_windows
    REFERENCING NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_update AFTER UPDATE ON public.nudge_delivery_windows
    REFERENCING OLD TABLE AS storage_old_rows NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_delete AFTER DELETE ON public.nudge_delivery_windows
    REFERENCING OLD TABLE AS storage_old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();

CREATE TRIGGER storage_write_admission BEFORE INSERT OR UPDATE OR DELETE ON public.odometer_reminder_windows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_write_admission();
CREATE TRIGGER storage_row_account_guard AFTER UPDATE ON public.odometer_reminder_windows
    FOR EACH ROW EXECUTE FUNCTION public.storage_row_account_guard();
CREATE TRIGGER storage_charge_insert AFTER INSERT ON public.odometer_reminder_windows
    REFERENCING NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_update AFTER UPDATE ON public.odometer_reminder_windows
    REFERENCING OLD TABLE AS storage_old_rows NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_delete AFTER DELETE ON public.odometer_reminder_windows
    REFERENCING OLD TABLE AS storage_old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();

CREATE TRIGGER storage_write_admission BEFORE INSERT OR UPDATE OR DELETE ON public.email_deliveries
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_write_admission();
CREATE TRIGGER storage_row_account_guard AFTER UPDATE ON public.email_deliveries
    FOR EACH ROW EXECUTE FUNCTION public.storage_row_account_guard();
CREATE TRIGGER storage_charge_insert AFTER INSERT ON public.email_deliveries
    REFERENCING NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_update AFTER UPDATE ON public.email_deliveries
    REFERENCING OLD TABLE AS storage_old_rows NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_delete AFTER DELETE ON public.email_deliveries
    REFERENCING OLD TABLE AS storage_old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();

CREATE TRIGGER storage_write_admission BEFORE INSERT OR UPDATE OR DELETE ON public.account_settings
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_write_admission();
CREATE TRIGGER storage_row_account_guard AFTER UPDATE ON public.account_settings
    FOR EACH ROW EXECUTE FUNCTION public.storage_row_account_guard();
CREATE TRIGGER storage_charge_insert AFTER INSERT ON public.account_settings
    REFERENCING NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_update AFTER UPDATE ON public.account_settings
    REFERENCING OLD TABLE AS storage_old_rows NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_delete AFTER DELETE ON public.account_settings
    REFERENCING OLD TABLE AS storage_old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();

CREATE TRIGGER storage_write_admission BEFORE INSERT OR UPDATE OR DELETE ON public.tracking_devices
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_write_admission();
CREATE TRIGGER storage_row_account_guard AFTER UPDATE ON public.tracking_devices
    FOR EACH ROW EXECUTE FUNCTION public.storage_row_account_guard();
CREATE TRIGGER storage_charge_insert AFTER INSERT ON public.tracking_devices
    REFERENCING NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_update AFTER UPDATE ON public.tracking_devices
    REFERENCING OLD TABLE AS storage_old_rows NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_delete AFTER DELETE ON public.tracking_devices
    REFERENCING OLD TABLE AS storage_old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();

CREATE TRIGGER storage_write_admission BEFORE INSERT OR UPDATE OR DELETE ON public.tracking_device_aliases
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_write_admission();
CREATE TRIGGER storage_row_account_guard AFTER UPDATE ON public.tracking_device_aliases
    FOR EACH ROW EXECUTE FUNCTION public.storage_row_account_guard();
CREATE TRIGGER storage_charge_insert AFTER INSERT ON public.tracking_device_aliases
    REFERENCING NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_update AFTER UPDATE ON public.tracking_device_aliases
    REFERENCING OLD TABLE AS storage_old_rows NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_delete AFTER DELETE ON public.tracking_device_aliases
    REFERENCING OLD TABLE AS storage_old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();

CREATE TRIGGER storage_write_admission BEFORE INSERT OR UPDATE OR DELETE ON public.ingest_credentials
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_write_admission();
CREATE TRIGGER storage_row_account_guard AFTER UPDATE ON public.ingest_credentials
    FOR EACH ROW EXECUTE FUNCTION public.storage_row_account_guard();
CREATE TRIGGER storage_charge_insert AFTER INSERT ON public.ingest_credentials
    REFERENCING NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_update AFTER UPDATE ON public.ingest_credentials
    REFERENCING OLD TABLE AS storage_old_rows NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_delete AFTER DELETE ON public.ingest_credentials
    REFERENCING OLD TABLE AS storage_old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();

CREATE TRIGGER storage_account_init AFTER INSERT ON public.accounts
    FOR EACH ROW EXECUTE FUNCTION public.storage_account_init();
CREATE TRIGGER storage_avatar_change AFTER UPDATE OF avatar_bytes, avatar_mime ON public.accounts
    FOR EACH ROW EXECUTE FUNCTION public.storage_avatar_change();
CREATE CONSTRAINT TRIGGER storage_envelope_final AFTER INSERT OR UPDATE OR DELETE ON public.device_storage_envelopes
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.storage_check_envelope();

CREATE FUNCTION public.storage_envelope_metadata() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
BEGIN
    UPDATE public.account_usage SET actual_bytes = actual_bytes
        + CASE WHEN TG_OP = 'INSERT' THEN 128 ELSE -128 END
    WHERE account_id = CASE WHEN TG_OP = 'INSERT' THEN NEW.account_id ELSE OLD.account_id END;
    RETURN NULL;
END
$body$;

CREATE TRIGGER storage_envelope_metadata AFTER INSERT OR DELETE ON public.device_storage_envelopes
    FOR EACH ROW EXECUTE FUNCTION public.storage_envelope_metadata();

REVOKE ALL ON FUNCTION public.storage_charge_raw_messages(public.raw_messages) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_charge_points(public.points) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_charge_stays(public.stays) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_charge_trips(public.trips) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_charge_detector_state(public.detector_state) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_charge_places(public.places) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_charge_tag_rules(public.tag_rules) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_charge_geocode_cache(public.geocode_cache) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_charge_trip_boundary_overrides(public.trip_boundary_overrides) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_charge_vehicles(public.vehicles) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_charge_mileage_rates(public.mileage_rates) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_charge_odometer_readings(public.odometer_readings) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_charge_expenses(public.expenses) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_charge_nudge_delivery_windows(public.nudge_delivery_windows) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_charge_odometer_reminder_windows(public.odometer_reminder_windows) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_charge_email_deliveries(public.email_deliveries) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_charge_account_settings(public.account_settings) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_charge_tracking_devices(public.tracking_devices) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_charge_tracking_device_aliases(public.tracking_device_aliases) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_charge_ingest_credentials(public.ingest_credentials) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_account_init() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_avatar_change() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_row_account_guard() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_apply_statement() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_check_envelope() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_expected_usage() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_expected_envelopes() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_usage_consistent() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.reconcile_storage_usage() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_write_admission() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_envelope_metadata() FROM PUBLIC;
-- A fresh database must not claim cluster-wide managed identities before
-- provisioning has checked that they are unused by another database.
DO $permissions$
BEGIN
    IF to_regclass('odograph_service.managed_role_state') IS NOT NULL THEN
        ALTER TABLE public.account_usage OWNER TO odograph_migrate;
        REVOKE ALL ON public.account_usage FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        GRANT SELECT ON public.account_usage TO odograph_runtime;
        GRANT SELECT, DELETE ON public.account_usage TO odograph_bootstrap;
        CREATE POLICY account_isolation ON public.account_usage FOR SELECT TO odograph_runtime
            USING (account_id = NULLIF(current_setting('app.account_id', true), '')::bigint);
        CREATE POLICY migration_writer ON public.account_usage FOR ALL TO odograph_migrate USING (true) WITH CHECK (true);
        CREATE POLICY bootstrap_defaults ON public.account_usage FOR ALL TO odograph_bootstrap USING (true) WITH CHECK (true);
        ALTER TABLE public.device_storage_envelopes OWNER TO odograph_migrate;
        REVOKE ALL ON public.device_storage_envelopes FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        GRANT SELECT ON public.device_storage_envelopes TO odograph_runtime;
        GRANT SELECT, DELETE ON public.device_storage_envelopes TO odograph_bootstrap;
        CREATE POLICY account_isolation ON public.device_storage_envelopes FOR SELECT TO odograph_runtime
            USING (account_id = NULLIF(current_setting('app.account_id', true), '')::bigint);
        CREATE POLICY migration_writer ON public.device_storage_envelopes FOR ALL TO odograph_migrate USING (true) WITH CHECK (true);
        CREATE POLICY bootstrap_defaults ON public.device_storage_envelopes FOR ALL TO odograph_bootstrap USING (true) WITH CHECK (true);
        GRANT SELECT ON public.accounts TO odograph_migrate;
        CREATE POLICY migration_writer ON public.email_challenges FOR ALL TO odograph_migrate USING (true) WITH CHECK (true);
        GRANT SELECT ON public.raw_messages TO odograph_migrate;
        CREATE POLICY migration_writer ON public.raw_messages FOR ALL TO odograph_migrate USING (true) WITH CHECK (true);
        GRANT SELECT ON public.points TO odograph_migrate;
        CREATE POLICY migration_writer ON public.points FOR ALL TO odograph_migrate USING (true) WITH CHECK (true);
        GRANT SELECT ON public.stays TO odograph_migrate;
        CREATE POLICY migration_writer ON public.stays FOR ALL TO odograph_migrate USING (true) WITH CHECK (true);
        GRANT SELECT ON public.trips TO odograph_migrate;
        CREATE POLICY migration_writer ON public.trips FOR ALL TO odograph_migrate USING (true) WITH CHECK (true);
        GRANT SELECT ON public.detector_state TO odograph_migrate;
        CREATE POLICY migration_writer ON public.detector_state FOR ALL TO odograph_migrate USING (true) WITH CHECK (true);
        GRANT SELECT ON public.places TO odograph_migrate;
        CREATE POLICY migration_writer ON public.places FOR ALL TO odograph_migrate USING (true) WITH CHECK (true);
        GRANT SELECT ON public.tag_rules TO odograph_migrate;
        CREATE POLICY migration_writer ON public.tag_rules FOR ALL TO odograph_migrate USING (true) WITH CHECK (true);
        GRANT SELECT ON public.geocode_cache TO odograph_migrate;
        CREATE POLICY migration_writer ON public.geocode_cache FOR ALL TO odograph_migrate USING (true) WITH CHECK (true);
        GRANT SELECT ON public.trip_boundary_overrides TO odograph_migrate;
        CREATE POLICY migration_writer ON public.trip_boundary_overrides FOR ALL TO odograph_migrate USING (true) WITH CHECK (true);
        GRANT SELECT ON public.vehicles TO odograph_migrate;
        CREATE POLICY migration_writer ON public.vehicles FOR ALL TO odograph_migrate USING (true) WITH CHECK (true);
        GRANT SELECT ON public.mileage_rates TO odograph_migrate;
        CREATE POLICY migration_writer ON public.mileage_rates FOR ALL TO odograph_migrate USING (true) WITH CHECK (true);
        GRANT SELECT ON public.odometer_readings TO odograph_migrate;
        CREATE POLICY migration_writer ON public.odometer_readings FOR ALL TO odograph_migrate USING (true) WITH CHECK (true);
        GRANT SELECT ON public.expenses TO odograph_migrate;
        CREATE POLICY migration_writer ON public.expenses FOR ALL TO odograph_migrate USING (true) WITH CHECK (true);
        GRANT SELECT ON public.nudge_delivery_windows TO odograph_migrate;
        CREATE POLICY migration_writer ON public.nudge_delivery_windows FOR ALL TO odograph_migrate USING (true) WITH CHECK (true);
        GRANT SELECT ON public.odometer_reminder_windows TO odograph_migrate;
        CREATE POLICY migration_writer ON public.odometer_reminder_windows FOR ALL TO odograph_migrate USING (true) WITH CHECK (true);
        GRANT SELECT ON public.email_deliveries TO odograph_migrate;
        CREATE POLICY migration_writer ON public.email_deliveries FOR ALL TO odograph_migrate USING (true) WITH CHECK (true);
        GRANT SELECT ON public.account_settings TO odograph_migrate;
        CREATE POLICY migration_writer ON public.account_settings FOR ALL TO odograph_migrate USING (true) WITH CHECK (true);
        GRANT SELECT ON public.tracking_devices TO odograph_migrate;
        CREATE POLICY migration_writer ON public.tracking_devices FOR ALL TO odograph_migrate USING (true) WITH CHECK (true);
        GRANT SELECT ON public.tracking_device_aliases TO odograph_migrate;
        CREATE POLICY migration_writer ON public.tracking_device_aliases FOR ALL TO odograph_migrate USING (true) WITH CHECK (true);
        GRANT SELECT ON public.ingest_credentials TO odograph_migrate;
        CREATE POLICY migration_writer ON public.ingest_credentials FOR ALL TO odograph_migrate USING (true) WITH CHECK (true);
        ALTER FUNCTION public.storage_charge_raw_messages(public.raw_messages) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_charge_raw_messages(public.raw_messages) FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.storage_charge_points(public.points) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_charge_points(public.points) FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.storage_charge_stays(public.stays) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_charge_stays(public.stays) FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.storage_charge_trips(public.trips) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_charge_trips(public.trips) FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.storage_charge_detector_state(public.detector_state) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_charge_detector_state(public.detector_state) FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.storage_charge_places(public.places) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_charge_places(public.places) FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.storage_charge_tag_rules(public.tag_rules) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_charge_tag_rules(public.tag_rules) FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.storage_charge_geocode_cache(public.geocode_cache) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_charge_geocode_cache(public.geocode_cache) FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.storage_charge_trip_boundary_overrides(public.trip_boundary_overrides) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_charge_trip_boundary_overrides(public.trip_boundary_overrides) FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.storage_charge_vehicles(public.vehicles) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_charge_vehicles(public.vehicles) FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.storage_charge_mileage_rates(public.mileage_rates) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_charge_mileage_rates(public.mileage_rates) FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.storage_charge_odometer_readings(public.odometer_readings) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_charge_odometer_readings(public.odometer_readings) FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.storage_charge_expenses(public.expenses) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_charge_expenses(public.expenses) FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.storage_charge_nudge_delivery_windows(public.nudge_delivery_windows) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_charge_nudge_delivery_windows(public.nudge_delivery_windows) FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.storage_charge_odometer_reminder_windows(public.odometer_reminder_windows) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_charge_odometer_reminder_windows(public.odometer_reminder_windows) FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.storage_charge_email_deliveries(public.email_deliveries) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_charge_email_deliveries(public.email_deliveries) FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.storage_charge_account_settings(public.account_settings) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_charge_account_settings(public.account_settings) FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.storage_charge_tracking_devices(public.tracking_devices) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_charge_tracking_devices(public.tracking_devices) FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.storage_charge_tracking_device_aliases(public.tracking_device_aliases) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_charge_tracking_device_aliases(public.tracking_device_aliases) FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.storage_charge_ingest_credentials(public.ingest_credentials) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_charge_ingest_credentials(public.ingest_credentials) FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.storage_account_init() OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_account_init() FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.storage_apply_statement() OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_apply_statement() FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.storage_row_account_guard() OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_row_account_guard() FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.storage_avatar_change() OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_avatar_change() FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.storage_check_envelope() OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_check_envelope() FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.storage_expected_usage() OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_expected_usage() FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.storage_expected_envelopes() OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_expected_envelopes() FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.storage_usage_consistent() OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_usage_consistent() FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.reconcile_storage_usage() OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.reconcile_storage_usage() FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.storage_write_admission() OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_write_admission() FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
        ALTER FUNCTION public.storage_envelope_metadata() OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_envelope_metadata() FROM PUBLIC, odograph_runtime, odograph_control, odograph_bootstrap;
    END IF;
END
$permissions$;

SELECT public.reconcile_storage_usage();
