-- migration-class: rewrite
-- Administrator storage limit editor. An enabled administrator sets a member
-- grant to follow the instance default, carry a custom account limit, or have
-- no limit. Raw and enhancement sub-limits follow the policy defaults, clamped
-- to a custom account limit. Administrator grants stay unlimited. Lowering a
-- limit keeps existing data; the ceiling then refuses growth. Startup now
-- also applies the policy sub-limits to custom grants, so the first start
-- after this migration can rewrite their raw and enhancement limits. The
-- smallest custom limit is 10737418 bytes, floor(0.01 GiB);
-- app/storage_limits.py derives the same value.
CREATE FUNCTION public.storage_set_account_grant(
    input_actor_id bigint, input_actor_auth_version bigint, input_target_id bigint,
    input_mode text, input_account_bytes bigint)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE policy public.storage_policy;
DECLARE target public.accounts;
DECLARE current_grant public.storage_grants;
DECLARE next_account bigint;
DECLARE next_raw bigint;
DECLARE next_enhancement bigint;
BEGIN
    IF input_actor_id IS NULL OR input_actor_auth_version IS NULL OR input_target_id IS NULL THEN
        RAISE EXCEPTION 'storage limit change unavailable' USING ERRCODE='42501';
    END IF;
    PERFORM 1 FROM public.accounts
    WHERE id IN (input_actor_id, input_target_id) ORDER BY id FOR SHARE;
    PERFORM 1 FROM public.accounts a
    WHERE a.id=input_actor_id AND a.is_admin AND a.is_enabled
      AND a.auth_version=input_actor_auth_version;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'storage limit change unavailable' USING ERRCODE='42501';
    END IF;
    SELECT * INTO target FROM public.accounts WHERE id=input_target_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'storage limit change unavailable' USING ERRCODE='42501';
    END IF;
    IF input_mode IS NULL OR input_mode NOT IN ('default','custom','unlimited')
       OR (input_mode='custom' AND (input_account_bytes IS NULL OR input_account_bytes<10737418))
       OR (input_mode<>'custom' AND input_account_bytes IS NOT NULL) THEN
        RAISE EXCEPTION 'invalid storage limit' USING ERRCODE='22023';
    END IF;
    IF target.is_admin THEN
        IF input_mode<>'unlimited' THEN
            RAISE EXCEPTION 'administrator storage is always unlimited' USING ERRCODE='22023';
        END IF;
        RETURN 'unchanged';
    END IF;
    SELECT * INTO STRICT policy FROM public.storage_policy WHERE id=1 FOR UPDATE;
    PERFORM 1 FROM public.account_usage WHERE account_id=input_target_id FOR UPDATE;
    SELECT * INTO STRICT current_grant FROM public.storage_grants WHERE account_id=input_target_id;
    IF input_mode='default' THEN
        next_account := policy.account_default_bytes;
        next_raw := policy.raw_default_bytes;
        next_enhancement := policy.enhancement_default_bytes;
    ELSIF input_mode='custom' THEN
        next_account := input_account_bytes;
        -- LEAST ignores NULL, so a NULL default must stay NULL explicitly.
        next_raw := CASE WHEN policy.raw_default_bytes IS NULL THEN NULL
            ELSE LEAST(policy.raw_default_bytes, input_account_bytes) END;
        next_enhancement := CASE WHEN policy.enhancement_default_bytes IS NULL THEN NULL
            ELSE LEAST(policy.enhancement_default_bytes, input_account_bytes) END;
    END IF;
    IF current_grant.limit_mode=input_mode
       AND current_grant.account_limit_bytes IS NOT DISTINCT FROM next_account
       AND current_grant.raw_limit_bytes IS NOT DISTINCT FROM next_raw
       AND current_grant.enhancement_limit_bytes IS NOT DISTINCT FROM next_enhancement THEN
        RETURN 'unchanged';
    END IF;
    IF next_account IS NOT NULL
       AND (SELECT COALESCE(SUM(g.account_limit_bytes),0) FROM public.storage_grants g
            WHERE g.account_id<>input_target_id) + next_account
           > policy.instance_budget_bytes - policy.instance_reserve_bytes THEN
        RAISE EXCEPTION 'storage capacity exceeded: account grants exceed instance budget'
            USING ERRCODE='P0001';
    END IF;
    UPDATE public.storage_grants SET limit_mode=input_mode, account_limit_bytes=next_account,
        raw_limit_bytes=next_raw, enhancement_limit_bytes=next_enhancement
    WHERE account_id=input_target_id;
    RETURN 'changed';
END
$body$;

CREATE FUNCTION public.storage_list_account_grants(
    input_actor_id bigint, input_actor_auth_version bigint)
RETURNS TABLE (grant_account_id bigint, grant_is_admin boolean, grant_limit_mode text,
    grant_account_limit_bytes bigint, grant_default_limit_bytes bigint, used_bytes bigint,
    fundable_bytes bigint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
BEGIN
    PERFORM 1 FROM public.accounts a
    WHERE a.id=input_actor_id AND a.is_admin AND a.is_enabled
      AND a.auth_version=input_actor_auth_version FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'storage limits unavailable' USING ERRCODE='42501';
    END IF;
    -- fundable_bytes is the largest finite account limit the budget could
    -- fund for that account, given every other account's grant.
    RETURN QUERY SELECT g.account_id, a.is_admin, g.limit_mode, g.account_limit_bytes,
        p.account_default_bytes,
        COALESCE(u.actual_bytes+u.reserved_bytes,0)::bigint,
        GREATEST(p.instance_budget_bytes-p.instance_reserve_bytes
            -(COALESCE(SUM(g.account_limit_bytes) OVER (),0)-COALESCE(g.account_limit_bytes,0)),0)::bigint
    FROM public.storage_grants g JOIN public.accounts a ON a.id=g.account_id
    LEFT JOIN public.account_usage u ON u.account_id=g.account_id
    CROSS JOIN public.storage_policy p WHERE p.id=1
    ORDER BY g.account_id;
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
    -- Custom grants keep their account limit; sub-limits follow the policy,
    -- clamped as in storage_set_account_grant.
    UPDATE public.storage_grants SET
        raw_limit_bytes=CASE WHEN raw_bytes IS NULL THEN NULL
            ELSE LEAST(raw_bytes, account_limit_bytes) END,
        enhancement_limit_bytes=CASE WHEN enhancement_bytes IS NULL THEN NULL
            ELSE LEAST(enhancement_bytes, account_limit_bytes) END
    WHERE limit_mode='custom';
    UPDATE public.storage_policy SET account_default_bytes=account_bytes,
        raw_default_bytes=raw_bytes,enhancement_default_bytes=enhancement_bytes,
        instance_budget_bytes=budget_bytes,instance_reserve_bytes=reserve_bytes WHERE id=1;
END
$body$;

REVOKE ALL ON FUNCTION public.storage_set_account_grant(bigint,bigint,bigint,text,bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_list_account_grants(bigint,bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_configure_limits(bigint,bigint,bigint,bigint,bigint) FROM PUBLIC;

DO $permissions$
BEGIN
    IF to_regclass('odograph_service.managed_role_state') IS NOT NULL THEN
        ALTER FUNCTION public.storage_set_account_grant(bigint,bigint,bigint,text,bigint) OWNER TO odograph_migrate;
        ALTER FUNCTION public.storage_list_account_grants(bigint,bigint) OWNER TO odograph_migrate;
        ALTER FUNCTION public.storage_configure_limits(bigint,bigint,bigint,bigint,bigint) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.storage_configure_limits(bigint,bigint,bigint,bigint,bigint)
            FROM PUBLIC,odograph_runtime,odograph_control,odograph_bootstrap;
        REVOKE ALL ON FUNCTION public.storage_set_account_grant(bigint,bigint,bigint,text,bigint),
            public.storage_list_account_grants(bigint,bigint)
            FROM PUBLIC,odograph_runtime,odograph_bootstrap;
        GRANT EXECUTE ON FUNCTION public.storage_set_account_grant(bigint,bigint,bigint,text,bigint),
            public.storage_list_account_grants(bigint,bigint) TO odograph_control;
    END IF;
END
$permissions$;
