-- Funded logical grants, transaction-final ceilings, and retained-raw replay.
CREATE TABLE public.storage_policy (
    id smallint PRIMARY KEY CHECK (id = 1),
    account_default_bytes bigint NOT NULL CHECK (account_default_bytes > 0),
    raw_default_bytes bigint NOT NULL CHECK (raw_default_bytes > 0),
    enhancement_default_bytes bigint NOT NULL CHECK (enhancement_default_bytes > 0),
    instance_budget_bytes bigint NOT NULL CHECK (instance_budget_bytes > 0),
    instance_reserve_bytes bigint NOT NULL CHECK (instance_reserve_bytes > 0),
    CHECK (raw_default_bytes <= account_default_bytes),
    CHECK (enhancement_default_bytes <= account_default_bytes),
    CHECK (instance_reserve_bytes < instance_budget_bytes)
);
INSERT INTO public.storage_policy VALUES
    (1, 2147483648, 268435456, 134217728, 12884901888, 2147483648);

CREATE TABLE public.storage_grants (
    account_id bigint PRIMARY KEY REFERENCES public.accounts(id) ON DELETE CASCADE,
    account_limit_bytes bigint NOT NULL CHECK (account_limit_bytes > 0),
    raw_limit_bytes bigint NOT NULL CHECK (raw_limit_bytes > 0),
    enhancement_limit_bytes bigint NOT NULL CHECK (enhancement_limit_bytes > 0),
    CHECK (raw_limit_bytes <= account_limit_bytes),
    CHECK (enhancement_limit_bytes <= account_limit_bytes)
);
INSERT INTO public.storage_grants
SELECT a.id, p.account_default_bytes, p.raw_default_bytes, p.enhancement_default_bytes
FROM public.accounts a CROSS JOIN public.storage_policy p ORDER BY a.id;
DO $check$
BEGIN
    IF (SELECT SUM(account_limit_bytes) FROM public.storage_grants) >
       (SELECT instance_budget_bytes - instance_reserve_bytes FROM public.storage_policy WHERE id=1) THEN
        RAISE EXCEPTION 'storage capacity exceeded: existing account grants exceed instance budget'
            USING ERRCODE='P0001';
    END IF;
END
$check$;

-- This row is protected state. Its first values for a transaction are retained
-- across all statement-level counter changes until the deferred final check.
CREATE TABLE public.storage_quota_baseline (
    account_id bigint PRIMARY KEY REFERENCES public.accounts(id) ON DELETE CASCADE,
    transaction_id xid8 NOT NULL,
    actual_bytes bigint NOT NULL,
    reserved_bytes bigint NOT NULL,
    raw_bytes bigint NOT NULL,
    enhancement_bytes bigint NOT NULL
);

ALTER TABLE public.raw_messages ADD CONSTRAINT raw_messages_account_id_id_key UNIQUE(account_id,id);
CREATE TABLE public.raw_replay_receipts (
    account_id bigint NOT NULL,
    raw_message_id bigint NOT NULL,
    namespace_kind text NOT NULL CHECK (namespace_kind IN ('device','credential')),
    namespace_key text NOT NULL CHECK (namespace_key <> ''),
    payload_sha256 bytea NOT NULL CHECK (pg_catalog.octet_length(payload_sha256)=32),
    PRIMARY KEY (account_id, raw_message_id),
    FOREIGN KEY (account_id,raw_message_id)
        REFERENCES public.raw_messages(account_id,id) ON DELETE CASCADE
);
CREATE INDEX raw_replay_receipts_lookup_idx ON public.raw_replay_receipts
    (account_id,namespace_kind,namespace_key,payload_sha256);

ALTER TABLE public.geocode_retry ADD COLUMN capacity_paused boolean NOT NULL DEFAULT false;
ALTER TABLE public.geocode_retry ADD COLUMN capacity_needed_bytes bigint NOT NULL DEFAULT 0
    CHECK (capacity_needed_bytes >= 0);
ALTER TABLE public.geocode_discovery ADD COLUMN capacity_paused boolean NOT NULL DEFAULT false;
ALTER TABLE public.trips ADD COLUMN snap_capacity_needed_bytes bigint NOT NULL DEFAULT 0
    CHECK (snap_capacity_needed_bytes >= 0);
CREATE INDEX geocode_retry_capacity_paused_idx ON public.geocode_retry(account_id)
    WHERE capacity_paused;
CREATE INDEX trips_snap_capacity_pending_idx ON public.trips(account_id)
    WHERE snap_capacity_needed_bytes>0;

CREATE FUNCTION public.storage_charge_raw_replay_receipts(r public.raw_replay_receipts)
RETURNS bigint[] LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT ARRAY[96::bigint + pg_catalog.octet_length(pg_catalog.convert_to((r).namespace_kind,'UTF8'))::bigint
        + pg_catalog.octet_length(pg_catalog.convert_to((r).namespace_key,'UTF8'))::bigint,
        96::bigint + pg_catalog.octet_length(pg_catalog.convert_to((r).namespace_kind,'UTF8'))::bigint
        + pg_catalog.octet_length(pg_catalog.convert_to((r).namespace_key,'UTF8'))::bigint,
        0::bigint,0::bigint,0::bigint,0::bigint,0::bigint];
$body$;

ALTER TABLE public.storage_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.storage_grants FORCE ROW LEVEL SECURITY;
ALTER TABLE public.storage_quota_baseline ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.storage_quota_baseline FORCE ROW LEVEL SECURITY;
ALTER TABLE public.raw_replay_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.raw_replay_receipts FORCE ROW LEVEL SECURITY;

CREATE FUNCTION public.storage_grant_account() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE policy public.storage_policy;
BEGIN
    SELECT * INTO STRICT policy FROM public.storage_policy WHERE id=1 FOR UPDATE;
    IF (SELECT COALESCE(SUM(account_limit_bytes),0) FROM public.storage_grants)
            + policy.account_default_bytes > policy.instance_budget_bytes - policy.instance_reserve_bytes THEN
        RAISE EXCEPTION 'storage capacity exceeded: no funded account grant available'
            USING ERRCODE='P0001';
    END IF;
    INSERT INTO public.storage_grants(account_id,account_limit_bytes,raw_limit_bytes,enhancement_limit_bytes)
    VALUES(NEW.id,policy.account_default_bytes,policy.raw_default_bytes,policy.enhancement_default_bytes);
    RETURN NULL;
END
$body$;
CREATE TRIGGER a_storage_grant_account AFTER INSERT ON public.accounts
    FOR EACH ROW EXECUTE FUNCTION public.storage_grant_account();

CREATE FUNCTION public.storage_capture_quota_baseline() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
BEGIN
    INSERT INTO public.storage_quota_baseline
        (account_id,transaction_id,actual_bytes,reserved_bytes,raw_bytes,enhancement_bytes)
    VALUES(NEW.account_id,pg_catalog.pg_current_xact_id(),
        CASE WHEN TG_OP='INSERT' THEN 0 ELSE OLD.actual_bytes END,
        CASE WHEN TG_OP='INSERT' THEN 0 ELSE OLD.reserved_bytes END,
        CASE WHEN TG_OP='INSERT' THEN 0 ELSE OLD.raw_bytes END,
        CASE WHEN TG_OP='INSERT' THEN 0 ELSE OLD.enhancement_bytes END)
    ON CONFLICT(account_id) DO UPDATE SET
        transaction_id=EXCLUDED.transaction_id,actual_bytes=EXCLUDED.actual_bytes,
        reserved_bytes=EXCLUDED.reserved_bytes,raw_bytes=EXCLUDED.raw_bytes,
        enhancement_bytes=EXCLUDED.enhancement_bytes
    WHERE public.storage_quota_baseline.transaction_id <> EXCLUDED.transaction_id;
    RETURN NEW;
END
$body$;
CREATE TRIGGER storage_capture_quota_baseline BEFORE INSERT OR UPDATE ON public.account_usage
    FOR EACH ROW EXECUTE FUNCTION public.storage_capture_quota_baseline();

CREATE FUNCTION public.storage_check_ceiling() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE usage public.account_usage;
DECLARE grant_row public.storage_grants;
DECLARE baseline public.storage_quota_baseline;
BEGIN
    SELECT * INTO usage FROM public.account_usage WHERE account_id=NEW.account_id;
    IF NOT FOUND THEN RETURN NULL; END IF;
    SELECT * INTO STRICT grant_row FROM public.storage_grants WHERE account_id=NEW.account_id;
    SELECT * INTO STRICT baseline FROM public.storage_quota_baseline WHERE account_id=NEW.account_id;
    IF baseline.transaction_id <> pg_catalog.pg_current_xact_id() THEN
        RAISE EXCEPTION 'storage quota baseline missing' USING ERRCODE='23514';
    END IF;
    IF usage.actual_bytes+usage.reserved_bytes > grant_row.account_limit_bytes
        AND usage.actual_bytes+usage.reserved_bytes > baseline.actual_bytes+baseline.reserved_bytes THEN
        RAISE EXCEPTION 'storage capacity exceeded: account allowance' USING ERRCODE='P0001';
    END IF;
    IF usage.raw_bytes > grant_row.raw_limit_bytes AND usage.raw_bytes > baseline.raw_bytes THEN
        RAISE EXCEPTION 'storage capacity exceeded: raw allowance' USING ERRCODE='P0001';
    END IF;
    IF usage.enhancement_bytes > grant_row.enhancement_limit_bytes
        AND usage.enhancement_bytes > baseline.enhancement_bytes THEN
        RAISE EXCEPTION 'storage capacity exceeded: enhancement allowance' USING ERRCODE='P0001';
    END IF;
    RETURN NULL;
END
$body$;
CREATE TRIGGER storage_write_admission BEFORE INSERT OR UPDATE OR DELETE ON public.raw_replay_receipts
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_write_admission();
CREATE TRIGGER storage_row_account_guard AFTER UPDATE ON public.raw_replay_receipts
    FOR EACH ROW EXECUTE FUNCTION public.storage_row_account_guard();
CREATE TRIGGER storage_charge_insert AFTER INSERT ON public.raw_replay_receipts
    REFERENCING NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_update AFTER UPDATE ON public.raw_replay_receipts
    REFERENCING OLD TABLE AS storage_old_rows NEW TABLE AS storage_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();
CREATE TRIGGER storage_charge_delete AFTER DELETE ON public.raw_replay_receipts
    REFERENCING OLD TABLE AS storage_old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement();

CREATE FUNCTION public.storage_find_replay(
    owner_id bigint, replay_kind text, replay_key text, replay_sha256 bytea, payload_json text)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE found_id bigint;
BEGIN
    IF owner_id IS NULL OR owner_id IS DISTINCT FROM
        NULLIF(pg_catalog.current_setting('app.account_id',true),'')::bigint THEN
        RAISE EXCEPTION 'account scope required' USING ERRCODE='42501';
    END IF;
    IF replay_kind NOT IN ('device','credential') OR replay_key IS NULL OR replay_key=''
        OR pg_catalog.octet_length(replay_sha256)<>32 THEN
        RAISE EXCEPTION 'invalid replay namespace' USING ERRCODE='22023';
    END IF;
    PERFORM 1 FROM public.account_usage WHERE account_id=owner_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'missing logical storage usage' USING ERRCODE='23514'; END IF;
    SELECT r.raw_message_id INTO found_id
    FROM public.raw_replay_receipts r JOIN public.raw_messages m
      ON m.account_id=r.account_id AND m.id=r.raw_message_id
    WHERE r.account_id=owner_id AND r.namespace_kind=replay_kind AND r.namespace_key=replay_key
      AND r.payload_sha256=replay_sha256 AND m.payload::text=payload_json::jsonb::text
    ORDER BY r.raw_message_id LIMIT 1;
    RETURN found_id;
END
$body$;

CREATE FUNCTION public.storage_record_replay(
    owner_id bigint, message_id bigint, replay_kind text, replay_key text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE message_row public.raw_messages;
BEGIN
    IF owner_id IS NULL OR owner_id IS DISTINCT FROM
        NULLIF(pg_catalog.current_setting('app.account_id',true),'')::bigint THEN
        RAISE EXCEPTION 'account scope required' USING ERRCODE='42501';
    END IF;
    PERFORM 1 FROM public.account_usage WHERE account_id=owner_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'missing logical storage usage' USING ERRCODE='23514'; END IF;
    SELECT * INTO STRICT message_row FROM public.raw_messages
    WHERE account_id=owner_id AND id=message_id FOR SHARE;
    IF replay_kind='device' THEN
        IF message_row.tracking_device_id IS NULL
           OR replay_key IS DISTINCT FROM message_row.tracking_device_id::text THEN
            RAISE EXCEPTION 'invalid replay namespace' USING ERRCODE='42501';
        END IF;
    ELSIF replay_kind='credential' THEN
        IF message_row.tracking_device_id IS NOT NULL OR NOT EXISTS(
            SELECT 1 FROM public.ingest_credentials c
            WHERE c.account_id=owner_id AND c.public_id=replay_key AND c.kind='legacy'
              AND c.revoked_at IS NULL) THEN
            RAISE EXCEPTION 'invalid replay namespace' USING ERRCODE='42501';
        END IF;
    ELSE
        RAISE EXCEPTION 'invalid replay namespace' USING ERRCODE='42501';
    END IF;
    INSERT INTO public.raw_replay_receipts
        (account_id,raw_message_id,namespace_kind,namespace_key,payload_sha256)
    VALUES(owner_id,message_id,replay_kind,replay_key,
        pg_catalog.sha256(pg_catalog.convert_to(message_row.payload::text,'UTF8')));
END
$body$;

CREATE FUNCTION public.storage_configure_limits(
    account_bytes bigint, raw_bytes bigint, enhancement_bytes bigint,
    budget_bytes bigint, reserve_bytes bigint)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE granted bigint;
BEGIN
    IF session_user NOT IN ('odograph_migrate','postgres') AND NOT EXISTS(
        SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname=session_user AND (r.rolsuper OR r.rolbypassrls)) THEN
        RAISE EXCEPTION 'storage configuration requires migration authority' USING ERRCODE='42501';
    END IF;
    IF account_bytes IS NULL OR raw_bytes IS NULL OR enhancement_bytes IS NULL
       OR budget_bytes IS NULL OR reserve_bytes IS NULL
       OR account_bytes<=0 OR raw_bytes<=0 OR enhancement_bytes<=0
       OR raw_bytes>account_bytes OR enhancement_bytes>account_bytes
       OR reserve_bytes<=0 OR reserve_bytes>=budget_bytes THEN
        RAISE EXCEPTION 'invalid storage limits' USING ERRCODE='22023';
    END IF;
    PERFORM 1 FROM public.storage_policy WHERE id=1 FOR UPDATE;
    SELECT COUNT(*)::bigint*account_bytes INTO granted FROM public.accounts;
    IF granted>budget_bytes-reserve_bytes THEN
        RAISE EXCEPTION 'storage capacity exceeded: account grants exceed instance budget'
            USING ERRCODE='P0001';
    END IF;
    UPDATE public.storage_grants SET account_limit_bytes=account_bytes,
        raw_limit_bytes=raw_bytes, enhancement_limit_bytes=enhancement_bytes;
    UPDATE public.storage_policy SET account_default_bytes=account_bytes,
        raw_default_bytes=raw_bytes,enhancement_default_bytes=enhancement_bytes,
        instance_budget_bytes=budget_bytes,instance_reserve_bytes=reserve_bytes WHERE id=1;
END
$body$;

CREATE FUNCTION public.storage_instance_status() RETURNS TABLE (
    instance_budget_bytes bigint,instance_reserve_bytes bigint,total_grants_bytes bigint,
    account_count bigint,warning_count bigint,blocked_count bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT p.instance_budget_bytes,p.instance_reserve_bytes,
      COALESCE(SUM(g.account_limit_bytes),0)::bigint,COUNT(g.account_id)::bigint,
      COUNT(*) FILTER (WHERE u.actual_bytes+u.reserved_bytes>=g.account_limit_bytes*0.8
        OR u.raw_bytes>=g.raw_limit_bytes*0.8
        OR u.enhancement_bytes>=g.enhancement_limit_bytes*0.8)::bigint,
      COUNT(*) FILTER (WHERE u.actual_bytes+u.reserved_bytes>=g.account_limit_bytes
        OR u.raw_bytes>=g.raw_limit_bytes
        OR u.enhancement_bytes>=g.enhancement_limit_bytes)::bigint
    FROM public.storage_policy p LEFT JOIN public.storage_grants g ON true
    LEFT JOIN public.account_usage u ON u.account_id=g.account_id
    WHERE p.id=1 GROUP BY p.instance_budget_bytes,p.instance_reserve_bytes;
$body$;

CREATE FUNCTION public.geocode_refresh_capacity_pause(owner_id bigint) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE paused boolean;
BEGIN
    IF owner_id IS NULL OR owner_id IS DISTINCT FROM
        NULLIF(pg_catalog.current_setting('app.account_id',true),'')::bigint THEN
        RAISE EXCEPTION 'account scope required' USING ERRCODE='42501';
    END IF;
    SELECT u.actual_bytes+u.reserved_bytes+128>g.account_limit_bytes
       OR u.enhancement_bytes+128>g.enhancement_limit_bytes INTO STRICT paused
    FROM public.account_usage u JOIN public.storage_grants g USING(account_id)
    WHERE u.account_id=owner_id;
    UPDATE public.geocode_discovery SET capacity_paused=paused WHERE account_id=owner_id
      AND capacity_paused IS DISTINCT FROM paused;
    RETURN paused;
END
$body$;

CREATE FUNCTION public.storage_geocode_room(owner_id bigint, additional_bytes bigint)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE usage_row public.account_usage;
DECLARE grant_row public.storage_grants;
BEGIN
    SELECT * INTO STRICT usage_row FROM public.account_usage WHERE account_id=owner_id FOR UPDATE;
    SELECT * INTO STRICT grant_row FROM public.storage_grants WHERE account_id=owner_id;
    RETURN additional_bytes=0 OR
        usage_row.actual_bytes+usage_row.reserved_bytes+additional_bytes<=grant_row.account_limit_bytes;
END
$body$;

CREATE OR REPLACE FUNCTION public.geocode_record_coordinate_turn(owner_id bigint) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
BEGIN
    IF owner_id IS DISTINCT FROM NULLIF(pg_catalog.current_setting('app.account_id',true),'')::bigint OR owner_id IS NULL THEN
        RAISE EXCEPTION 'account scope required' USING ERRCODE='42501';
    END IF;
    PERFORM 1 FROM public.account_usage WHERE account_id=owner_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'account usage missing' USING ERRCODE='23514'; END IF;
    -- The enum text charge grows by one byte from discovery to coordinate.
    -- Preserve the usage lock without forcing optional metadata growth at a ceiling.
    IF public.storage_geocode_room(owner_id,1) THEN
        UPDATE public.geocode_discovery SET last_unit='coordinate' WHERE account_id=owner_id
          AND last_unit<>'coordinate';
    END IF;
END
$body$;

CREATE FUNCTION public.geocode_record_capacity_pause(owner_id bigint) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
BEGIN
    IF owner_id IS DISTINCT FROM NULLIF(pg_catalog.current_setting('app.account_id',true),'')::bigint OR owner_id IS NULL THEN
        RAISE EXCEPTION 'account scope required' USING ERRCODE='42501';
    END IF;
    PERFORM 1 FROM public.account_usage WHERE account_id=owner_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'account usage missing' USING ERRCODE='23514'; END IF;
END
$body$;

CREATE OR REPLACE FUNCTION public.geocode_endpoint_intents() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE changed_sql text;
DECLARE changed record;
DECLARE candidates record;
DECLARE queue_sql text;
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
        UPDATE public.geocode_discovery SET generation=generation+1 WHERE account_id=changed.account_id;
    END LOOP;
    IF TG_TABLE_NAME <> 'trips' OR TG_OP='DELETE' THEN RETURN NULL; END IF;
    queue_sql := 'SELECT DISTINCT n.account_id AS owner_id, '
        || 'ROUND(public.ST_Y(e.geom::public.geometry)::numeric,4) AS lat, '
        || 'ROUND(public.ST_X(e.geom::public.geometry)::numeric,4) AS lon FROM geocode_new_rows n '
        || CASE WHEN TG_OP='UPDATE' THEN 'JOIN geocode_old_rows o USING(id) ' ELSE '' END
        || 'CROSS JOIN LATERAL (VALUES(n.start_geom,n.start_place_id),(n.end_geom,n.end_place_id)) e(geom,place_id) '
        || 'WHERE e.geom IS NOT NULL AND e.place_id IS NULL '
        || CASE WHEN TG_OP='UPDATE' THEN 'AND n.geocode_generation<>o.geocode_generation ' ELSE '' END
        || 'AND (n.tracking_device_id IS NULL OR EXISTS(SELECT 1 FROM public.tracking_devices d '
        || 'WHERE d.account_id=n.account_id AND d.id=n.tracking_device_id AND d.enabled AND d.revoked_at IS NULL)) '
        || 'AND NOT EXISTS(SELECT 1 FROM public.geocode_cache c WHERE c.account_id=n.account_id '
        || 'AND c.lat=ROUND(public.ST_Y(e.geom::public.geometry)::numeric,4) '
        || 'AND c.lon=ROUND(public.ST_X(e.geom::public.geometry)::numeric,4))';
    FOR changed IN EXECUTE 'SELECT DISTINCT owner_id FROM (' || queue_sql || ') q ORDER BY owner_id' LOOP
        EXECUTE 'SELECT COUNT(*) FROM (' || queue_sql || ') q WHERE q.owner_id=$1 '
            || 'AND NOT EXISTS(SELECT 1 FROM public.geocode_retry r WHERE r.account_id=q.owner_id '
            || 'AND r.rounded_lat=q.lat AND r.rounded_lon=q.lon)' INTO candidates USING changed.owner_id;
        IF public.storage_geocode_room(changed.owner_id,candidates.count*128) THEN
            EXECUTE 'INSERT INTO public.geocode_retry(account_id,rounded_lat,rounded_lon) '
                || 'SELECT q.owner_id,q.lat,q.lon FROM (' || queue_sql || ') q WHERE q.owner_id=$1 '
                || 'ORDER BY q.owner_id,q.lat,q.lon ON CONFLICT DO NOTHING' USING changed.owner_id;
        ELSE
            UPDATE public.geocode_discovery SET capacity_paused=true WHERE account_id=changed.owner_id;
        END IF;
    END LOOP;
    RETURN NULL;
END
$body$;

CREATE OR REPLACE FUNCTION public.geocode_discover_page(owner_id bigint) RETURNS TABLE (
    trips_scanned integer, queued_coordinates integer, cursor_trip_id bigint,
    generation bigint, scanned_generation bigint, ready boolean)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE state public.geocode_discovery;
DECLARE page_ids bigint[];
DECLARE page_count integer;
DECLARE candidate_count bigint;
DECLARE queue_count integer;
BEGIN
    IF owner_id IS DISTINCT FROM NULLIF(pg_catalog.current_setting('app.account_id',true),'')::bigint OR owner_id IS NULL THEN
        RAISE EXCEPTION 'account scope required' USING ERRCODE='42501';
    END IF;
    PERFORM 1 FROM public.account_usage WHERE account_id=owner_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'account usage missing' USING ERRCODE='23514'; END IF;
    SELECT d.* INTO STRICT state FROM public.geocode_discovery d WHERE d.account_id=owner_id FOR UPDATE;
    IF state.cursor_trip_id=0 THEN state.round_generation:=state.generation; END IF;
    SELECT pg_catalog.array_agg(p.id ORDER BY p.id),COUNT(*)::integer INTO page_ids,page_count
    FROM (SELECT t.id FROM public.trips t WHERE t.account_id=owner_id AND t.id>state.cursor_trip_id
          ORDER BY t.id LIMIT 500) p;
    WITH candidates AS MATERIALIZED (
        SELECT DISTINCT t.account_id,ROUND(public.ST_Y(e.geom::public.geometry)::numeric,4) lat,
            ROUND(public.ST_X(e.geom::public.geometry)::numeric,4) lon
        FROM public.trips t
        CROSS JOIN LATERAL (VALUES(t.start_geom,t.start_place_id),(t.end_geom,t.end_place_id)) e(geom,place_id)
        WHERE t.account_id=owner_id AND t.id=ANY(page_ids) AND e.geom IS NOT NULL AND e.place_id IS NULL
            AND (t.tracking_device_id IS NULL OR EXISTS(SELECT 1 FROM public.tracking_devices d
                WHERE d.account_id=t.account_id AND d.id=t.tracking_device_id AND d.enabled AND d.revoked_at IS NULL))
            AND NOT EXISTS(SELECT 1 FROM public.geocode_cache c WHERE c.account_id=t.account_id
                AND c.lat=ROUND(public.ST_Y(e.geom::public.geometry)::numeric,4)
                AND c.lon=ROUND(public.ST_X(e.geom::public.geometry)::numeric,4))
    ) SELECT COUNT(*) INTO candidate_count FROM candidates q WHERE NOT EXISTS(
        SELECT 1 FROM public.geocode_retry r WHERE r.account_id=q.account_id
          AND r.rounded_lat=q.lat AND r.rounded_lon=q.lon);
    IF NOT public.storage_geocode_room(owner_id,candidate_count*128) THEN
        UPDATE public.geocode_discovery d SET capacity_paused=true,last_unit='discovery'
        WHERE d.account_id=owner_id;
        RETURN QUERY SELECT 0,0,state.cursor_trip_id,state.generation,state.scanned_generation,false;
        RETURN;
    END IF;
    INSERT INTO public.geocode_retry(account_id,rounded_lat,rounded_lon)
    SELECT DISTINCT t.account_id,ROUND(public.ST_Y(e.geom::public.geometry)::numeric,4),
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
    GET DIAGNOSTICS queue_count=ROW_COUNT;
    IF page_count<500 THEN
        state.cursor_trip_id:=0;
        state.scanned_generation:=state.round_generation;
    ELSE
        state.cursor_trip_id:=page_ids[page_count];
    END IF;
    UPDATE public.geocode_discovery d SET cursor_trip_id=state.cursor_trip_id,
        round_generation=state.round_generation,scanned_generation=state.scanned_generation,
        last_unit='discovery',capacity_paused=false WHERE d.account_id=owner_id;
    RETURN QUERY SELECT page_count,queue_count,state.cursor_trip_id,state.generation,state.scanned_generation,
        state.cursor_trip_id<>0 OR state.generation<>state.scanned_generation;
END
$body$;

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

CREATE OR REPLACE FUNCTION public.reconcile_storage_usage() RETURNS void
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
    -- Repairing counters for existing data does not admit new personal data.
    UPDATE public.storage_quota_baseline b SET
        transaction_id = pg_catalog.pg_current_xact_id(), actual_bytes = u.actual_bytes,
        reserved_bytes = u.reserved_bytes, raw_bytes = u.raw_bytes,
        enhancement_bytes = u.enhancement_bytes
    FROM public.account_usage u WHERE u.account_id = b.account_id;
END
$body$;

REVOKE ALL ON FUNCTION public.reconcile_storage_usage() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_charge_raw_replay_receipts(public.raw_replay_receipts) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_grant_account() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_capture_quota_baseline() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_check_ceiling() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_find_replay(bigint,text,text,bytea,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_record_replay(bigint,bigint,text,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_configure_limits(bigint,bigint,bigint,bigint,bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_instance_status() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_geocode_room(bigint,bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.geocode_refresh_capacity_pause(bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.geocode_record_capacity_pause(bigint) FROM PUBLIC;

DO $permissions$
BEGIN
    IF to_regclass('odograph_service.managed_role_state') IS NOT NULL THEN
        ALTER TABLE public.storage_policy OWNER TO odograph_migrate;
        REVOKE ALL ON public.storage_policy FROM PUBLIC,odograph_runtime,odograph_control,odograph_bootstrap;
        ALTER TABLE public.storage_grants OWNER TO odograph_migrate;
        REVOKE ALL ON public.storage_grants FROM PUBLIC,odograph_runtime,odograph_control,odograph_bootstrap;
        GRANT SELECT ON public.storage_grants TO odograph_runtime;
        CREATE POLICY account_isolation ON public.storage_grants FOR SELECT TO odograph_runtime
            USING (account_id=NULLIF(current_setting('app.account_id',true),'')::bigint);
        CREATE POLICY migration_writer ON public.storage_grants FOR ALL TO odograph_migrate
            USING (true) WITH CHECK (true);
        ALTER TABLE public.storage_quota_baseline OWNER TO odograph_migrate;
        REVOKE ALL ON public.storage_quota_baseline FROM PUBLIC,odograph_runtime,odograph_control,odograph_bootstrap;
        CREATE POLICY migration_writer ON public.storage_quota_baseline FOR ALL TO odograph_migrate
            USING (true) WITH CHECK (true);
        ALTER TABLE public.raw_replay_receipts OWNER TO odograph_migrate;
        REVOKE ALL ON public.raw_replay_receipts FROM PUBLIC,odograph_runtime,odograph_control,odograph_bootstrap;
        CREATE POLICY migration_writer ON public.raw_replay_receipts FOR ALL TO odograph_migrate
            USING (true) WITH CHECK (true);
        ALTER FUNCTION public.storage_charge_raw_replay_receipts(public.raw_replay_receipts) OWNER TO odograph_migrate;
        ALTER FUNCTION public.storage_grant_account() OWNER TO odograph_migrate;
        ALTER FUNCTION public.storage_capture_quota_baseline() OWNER TO odograph_migrate;
        ALTER FUNCTION public.storage_check_ceiling() OWNER TO odograph_migrate;
        ALTER FUNCTION public.storage_find_replay(bigint,text,text,bytea,text) OWNER TO odograph_migrate;
        ALTER FUNCTION public.storage_record_replay(bigint,bigint,text,text) OWNER TO odograph_migrate;
        ALTER FUNCTION public.storage_configure_limits(bigint,bigint,bigint,bigint,bigint) OWNER TO odograph_migrate;
        ALTER FUNCTION public.storage_instance_status() OWNER TO odograph_migrate;
        ALTER FUNCTION public.storage_geocode_room(bigint,bigint) OWNER TO odograph_migrate;
        ALTER FUNCTION public.geocode_refresh_capacity_pause(bigint) OWNER TO odograph_migrate;
        ALTER FUNCTION public.geocode_record_capacity_pause(bigint) OWNER TO odograph_migrate;
        ALTER FUNCTION public.geocode_endpoint_intents() OWNER TO odograph_migrate;
        ALTER FUNCTION public.geocode_discover_page(bigint) OWNER TO odograph_migrate;
        ALTER FUNCTION public.storage_expected_usage() OWNER TO odograph_migrate;
        ALTER FUNCTION public.reconcile_storage_usage() OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.reconcile_storage_usage()
            FROM PUBLIC,odograph_runtime,odograph_control,odograph_bootstrap;
        GRANT EXECUTE ON FUNCTION public.storage_find_replay(bigint,text,text,bytea,text),
            public.storage_record_replay(bigint,bigint,text,text),
            public.geocode_refresh_capacity_pause(bigint),
            public.geocode_record_capacity_pause(bigint) TO odograph_runtime;
        GRANT EXECUTE ON FUNCTION public.storage_instance_status() TO odograph_control;
    END IF;
END
$permissions$;

-- Retained, resolved streams have an exact durable namespace. Historical
-- unresolved raw rows lack the credential identity and cannot be guessed.
INSERT INTO public.raw_replay_receipts
    (account_id,raw_message_id,namespace_kind,namespace_key,payload_sha256)
SELECT r.account_id,r.id,'device',r.tracking_device_id::text,
    pg_catalog.sha256(pg_catalog.convert_to(r.payload::text,'UTF8'))
FROM public.raw_messages r WHERE r.tracking_device_id IS NOT NULL
ORDER BY r.account_id,r.id;
SELECT public.reconcile_storage_usage();

CREATE CONSTRAINT TRIGGER storage_ceiling_final AFTER INSERT OR UPDATE ON public.account_usage
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.storage_check_ceiling();
