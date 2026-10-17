-- migration-class: rewrite
-- Optional storage limits. A NULL limit means no limit for that measure. A
-- grant follows the instance default ('default'), carries its own account
-- limit ('custom'), or has no limits ('unlimited'). Administrators are always
-- unlimited. The existing positive and subset checks still apply when both
-- sides are set; startup applies the configured policy to 'default' grants.
ALTER TABLE public.storage_policy
    ALTER COLUMN account_default_bytes DROP NOT NULL,
    ALTER COLUMN raw_default_bytes DROP NOT NULL,
    ALTER COLUMN enhancement_default_bytes DROP NOT NULL,
    ADD CONSTRAINT storage_policy_sublimit_check CHECK (account_default_bytes IS NOT NULL
        OR (raw_default_bytes IS NULL AND enhancement_default_bytes IS NULL));

ALTER TABLE public.storage_grants
    ALTER COLUMN account_limit_bytes DROP NOT NULL,
    ALTER COLUMN raw_limit_bytes DROP NOT NULL,
    ALTER COLUMN enhancement_limit_bytes DROP NOT NULL,
    ADD COLUMN limit_mode text NOT NULL DEFAULT 'default',
    ADD CONSTRAINT storage_grants_limit_mode_check
        CHECK (limit_mode IN ('default','custom','unlimited')),
    ADD CONSTRAINT storage_grants_sublimit_check CHECK (account_limit_bytes IS NOT NULL
        OR (raw_limit_bytes IS NULL AND enhancement_limit_bytes IS NULL)),
    ADD CONSTRAINT storage_grants_mode_limit_check CHECK (
        (limit_mode <> 'unlimited' OR account_limit_bytes IS NULL)
        AND (limit_mode <> 'custom' OR account_limit_bytes IS NOT NULL));
UPDATE public.storage_grants g SET limit_mode='unlimited',account_limit_bytes=NULL,
    raw_limit_bytes=NULL,enhancement_limit_bytes=NULL
FROM public.accounts a WHERE a.id=g.account_id AND a.is_admin;
ALTER TABLE public.storage_grants ALTER COLUMN limit_mode DROP DEFAULT;

CREATE OR REPLACE FUNCTION public.storage_grant_account() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE policy public.storage_policy;
BEGIN
    SELECT * INTO STRICT policy FROM public.storage_policy WHERE id=1 FOR UPDATE;
    IF NEW.is_admin THEN
        INSERT INTO public.storage_grants
            (account_id,limit_mode,account_limit_bytes,raw_limit_bytes,enhancement_limit_bytes)
        VALUES(NEW.id,'unlimited',NULL,NULL,NULL);
        RETURN NULL;
    END IF;
    IF policy.account_default_bytes IS NOT NULL
       AND (SELECT COALESCE(SUM(account_limit_bytes),0) FROM public.storage_grants)
            + policy.account_default_bytes > policy.instance_budget_bytes - policy.instance_reserve_bytes THEN
        RAISE EXCEPTION 'storage capacity exceeded: no funded account grant available'
            USING ERRCODE='P0001';
    END IF;
    INSERT INTO public.storage_grants
        (account_id,limit_mode,account_limit_bytes,raw_limit_bytes,enhancement_limit_bytes)
    VALUES(NEW.id,'default',policy.account_default_bytes,policy.raw_default_bytes,
        policy.enhancement_default_bytes);
    RETURN NULL;
END
$body$;

CREATE OR REPLACE FUNCTION public.storage_check_ceiling() RETURNS trigger
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
    IF grant_row.account_limit_bytes IS NOT NULL
        AND usage.actual_bytes+usage.reserved_bytes > grant_row.account_limit_bytes
        AND usage.actual_bytes+usage.reserved_bytes > baseline.actual_bytes+baseline.reserved_bytes THEN
        RAISE EXCEPTION 'storage capacity exceeded: account allowance' USING ERRCODE='P0001';
    END IF;
    IF grant_row.raw_limit_bytes IS NOT NULL
        AND usage.raw_bytes > grant_row.raw_limit_bytes AND usage.raw_bytes > baseline.raw_bytes THEN
        RAISE EXCEPTION 'storage capacity exceeded: raw allowance' USING ERRCODE='P0001';
    END IF;
    IF grant_row.enhancement_limit_bytes IS NOT NULL
        AND usage.enhancement_bytes > grant_row.enhancement_limit_bytes
        AND usage.enhancement_bytes > baseline.enhancement_bytes THEN
        RAISE EXCEPTION 'storage capacity exceeded: enhancement allowance' USING ERRCODE='P0001';
    END IF;
    RETURN NULL;
END
$body$;

CREATE OR REPLACE FUNCTION public.storage_configure_limits(
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
    IF budget_bytes IS NULL OR reserve_bytes IS NULL
       OR reserve_bytes<=0 OR reserve_bytes>=budget_bytes
       OR account_bytes<=0 OR raw_bytes<=0 OR enhancement_bytes<=0
       OR raw_bytes>account_bytes OR enhancement_bytes>account_bytes
       OR (account_bytes IS NULL AND (raw_bytes IS NOT NULL OR enhancement_bytes IS NOT NULL)) THEN
        RAISE EXCEPTION 'invalid storage limits' USING ERRCODE='22023';
    END IF;
    PERFORM 1 FROM public.storage_policy WHERE id=1 FOR UPDATE;
    SELECT COALESCE(SUM(account_limit_bytes) FILTER (WHERE limit_mode<>'default'),0)
        + COALESCE(COUNT(*) FILTER (WHERE limit_mode='default')*account_bytes,0)
        INTO granted FROM public.storage_grants;
    IF granted>budget_bytes-reserve_bytes THEN
        RAISE EXCEPTION 'storage capacity exceeded: account grants exceed instance budget'
            USING ERRCODE='P0001';
    END IF;
    UPDATE public.storage_grants SET account_limit_bytes=account_bytes,
        raw_limit_bytes=raw_bytes, enhancement_limit_bytes=enhancement_bytes
    WHERE limit_mode='default';
    UPDATE public.storage_policy SET account_default_bytes=account_bytes,
        raw_default_bytes=raw_bytes,enhancement_default_bytes=enhancement_bytes,
        instance_budget_bytes=budget_bytes,instance_reserve_bytes=reserve_bytes WHERE id=1;
END
$body$;

CREATE OR REPLACE FUNCTION public.storage_instance_status() RETURNS TABLE (
    instance_budget_bytes bigint,instance_reserve_bytes bigint,total_grants_bytes bigint,
    account_count bigint,warning_count bigint,blocked_count bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT p.instance_budget_bytes,p.instance_reserve_bytes,
      COALESCE(SUM(g.account_limit_bytes),0)::bigint,COUNT(g.account_limit_bytes)::bigint,
      COUNT(*) FILTER (WHERE (g.account_limit_bytes IS NOT NULL
          AND u.actual_bytes+u.reserved_bytes>=g.account_limit_bytes*0.8)
        OR (g.raw_limit_bytes IS NOT NULL AND u.raw_bytes>=g.raw_limit_bytes*0.8)
        OR (g.enhancement_limit_bytes IS NOT NULL
          AND u.enhancement_bytes>=g.enhancement_limit_bytes*0.8))::bigint,
      COUNT(*) FILTER (WHERE (g.account_limit_bytes IS NOT NULL
          AND u.actual_bytes+u.reserved_bytes>=g.account_limit_bytes)
        OR (g.raw_limit_bytes IS NOT NULL AND u.raw_bytes>=g.raw_limit_bytes)
        OR (g.enhancement_limit_bytes IS NOT NULL
          AND u.enhancement_bytes>=g.enhancement_limit_bytes))::bigint
    FROM public.storage_policy p LEFT JOIN public.storage_grants g ON true
    LEFT JOIN public.account_usage u ON u.account_id=g.account_id
    WHERE p.id=1 GROUP BY p.instance_budget_bytes,p.instance_reserve_bytes;
$body$;

CREATE OR REPLACE FUNCTION public.geocode_refresh_capacity_pause(owner_id bigint) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE paused boolean;
BEGIN
    IF owner_id IS NULL OR owner_id IS DISTINCT FROM
        NULLIF(pg_catalog.current_setting('app.account_id',true),'')::bigint THEN
        RAISE EXCEPTION 'account scope required' USING ERRCODE='42501';
    END IF;
    SELECT COALESCE(u.actual_bytes+u.reserved_bytes+128>g.account_limit_bytes,false)
       OR COALESCE(u.enhancement_bytes+128>g.enhancement_limit_bytes,false) INTO STRICT paused
    FROM public.account_usage u JOIN public.storage_grants g USING(account_id)
    WHERE u.account_id=owner_id;
    UPDATE public.geocode_discovery SET capacity_paused=paused WHERE account_id=owner_id
      AND capacity_paused IS DISTINCT FROM paused;
    RETURN paused;
END
$body$;

CREATE OR REPLACE FUNCTION public.storage_geocode_room(owner_id bigint, additional_bytes bigint)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE usage_row public.account_usage;
DECLARE grant_row public.storage_grants;
BEGIN
    SELECT * INTO STRICT usage_row FROM public.account_usage WHERE account_id=owner_id FOR UPDATE;
    SELECT * INTO STRICT grant_row FROM public.storage_grants WHERE account_id=owner_id;
    RETURN additional_bytes=0 OR grant_row.account_limit_bytes IS NULL OR
        usage_row.actual_bytes+usage_row.reserved_bytes+additional_bytes<=grant_row.account_limit_bytes;
END
$body$;

REVOKE ALL ON FUNCTION public.storage_grant_account() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_check_ceiling() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_configure_limits(bigint,bigint,bigint,bigint,bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_instance_status() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_geocode_room(bigint,bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.geocode_refresh_capacity_pause(bigint) FROM PUBLIC;

DO $permissions$
BEGIN
    IF to_regclass('odograph_service.managed_role_state') IS NOT NULL THEN
        ALTER FUNCTION public.storage_grant_account() OWNER TO odograph_migrate;
        ALTER FUNCTION public.storage_check_ceiling() OWNER TO odograph_migrate;
        ALTER FUNCTION public.storage_configure_limits(bigint,bigint,bigint,bigint,bigint) OWNER TO odograph_migrate;
        ALTER FUNCTION public.storage_instance_status() OWNER TO odograph_migrate;
        ALTER FUNCTION public.storage_geocode_room(bigint,bigint) OWNER TO odograph_migrate;
        ALTER FUNCTION public.geocode_refresh_capacity_pause(bigint) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_grant_account(),public.storage_check_ceiling(),
            public.storage_configure_limits(bigint,bigint,bigint,bigint,bigint),
            public.storage_geocode_room(bigint,bigint)
            FROM PUBLIC,odograph_runtime,odograph_control,odograph_bootstrap;
        GRANT EXECUTE ON FUNCTION public.geocode_refresh_capacity_pause(bigint) TO odograph_runtime;
        GRANT EXECUTE ON FUNCTION public.storage_instance_status() TO odograph_control;
    END IF;
END
$permissions$;
