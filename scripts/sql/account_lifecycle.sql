-- Protected account state transitions and security audit access.
CREATE OR REPLACE FUNCTION public.admin_set_account_enabled(
    input_actor_id bigint, input_actor_auth_version bigint,
    input_target_id bigint, input_enable boolean
) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    actor_row public.accounts%ROWTYPE;
    target_row public.accounts%ROWTYPE;
    changed_at timestamptz;
    result text;
BEGIN
    IF input_actor_id IS NULL OR input_actor_auth_version IS NULL
       OR input_target_id IS NULL OR input_enable IS NULL
       OR input_actor_id = input_target_id THEN
        RAISE EXCEPTION 'account transition unavailable' USING ERRCODE = '42501';
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(901412, 1);
    PERFORM 1 FROM public.accounts
    WHERE id IN (input_actor_id, input_target_id) ORDER BY id FOR UPDATE;
    SELECT * INTO actor_row FROM public.accounts WHERE id = input_actor_id;
    SELECT * INTO target_row FROM public.accounts WHERE id = input_target_id;
    IF actor_row.id IS NULL OR NOT actor_row.is_admin OR NOT actor_row.is_enabled
       OR actor_row.auth_version <> input_actor_auth_version
       OR target_row.id IS NULL
       OR (input_enable AND target_row.deletion_deadline IS NOT NULL) THEN
        RAISE EXCEPTION 'account transition unavailable' USING ERRCODE = '42501';
    END IF;
    IF target_row.is_enabled = input_enable THEN
        IF input_enable THEN
            result := 'already_enabled';
        ELSE
            result := 'already_disabled';
        END IF;
        INSERT INTO public.account_security_audit
            (occurred_at, actor_account_id, target_account_id, action, outcome)
        VALUES (pg_catalog.clock_timestamp(), input_actor_id, input_target_id,
                CASE WHEN input_enable THEN 'enable_account' ELSE 'disable_account' END,
                result);
        RETURN result;
    END IF;
    IF NOT input_enable AND target_row.is_admin AND NOT EXISTS (
        SELECT 1 FROM public.accounts a
        WHERE a.id <> input_target_id AND a.is_admin AND a.is_enabled
          AND (NULLIF(a.password_hash, '') IS NOT NULL OR EXISTS (
              SELECT 1 FROM public.oidc_identities oi WHERE oi.account_id = a.id
          ))
    ) THEN
        RAISE EXCEPTION 'account transition unavailable' USING ERRCODE = '42501';
    END IF;

    changed_at := pg_catalog.clock_timestamp();
    IF input_enable THEN
        UPDATE public.accounts SET is_enabled = true, updated_at = changed_at
        WHERE id = input_target_id;
        result := 'enabled';
    ELSE
        UPDATE public.accounts SET is_enabled = false,
            auth_version = auth_version + 1, updated_at = changed_at
        WHERE id = input_target_id;
        UPDATE public.email_challenges SET revoked_at = changed_at
        WHERE account_id = input_target_id
          AND consumed_at IS NULL AND revoked_at IS NULL;
        DELETE FROM public.oidc_action_proofs WHERE account_id = input_target_id;
        DELETE FROM public.oidc_attempts WHERE account_id = input_target_id;
        UPDATE public.invitations SET revoked_at = changed_at
        WHERE issued_by = input_target_id
          AND consumed_at IS NULL AND revoked_at IS NULL;
        UPDATE public.ingest_credentials
        SET revoked_at = changed_at, generation = generation + 1,
            updated_at = changed_at
        WHERE account_id = input_target_id AND revoked_at IS NULL;
        result := 'disabled';
    END IF;
    INSERT INTO public.account_security_audit
        (occurred_at, actor_account_id, target_account_id, action, outcome)
    VALUES (changed_at, input_actor_id, input_target_id,
            CASE WHEN input_enable THEN 'enable_account' ELSE 'disable_account' END,
            result);
    DELETE FROM public.account_security_audit WHERE id IN (
        SELECT id FROM public.account_security_audit
        WHERE occurred_at < changed_at - interval '365 days'
        ORDER BY occurred_at, id LIMIT 100
    );
    RETURN result;
END
$body$;
REVOKE ALL ON FUNCTION public.admin_set_account_enabled(bigint,bigint,bigint,boolean) FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.list_account_security_audit(
    input_actor_id bigint, input_actor_auth_version bigint
) RETURNS TABLE (id bigint, occurred_at timestamptz, actor_account_id bigint,
                 target_account_id bigint, action text, outcome text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
BEGIN
    PERFORM 1 FROM public.accounts a
    WHERE a.id = input_actor_id AND a.is_admin AND a.is_enabled
      AND a.auth_version = input_actor_auth_version FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'audit unavailable' USING ERRCODE = '42501';
    END IF;
    RETURN QUERY SELECT audit.id, audit.occurred_at, audit.actor_account_id,
                        audit.target_account_id, audit.action, audit.outcome
    FROM public.account_security_audit audit
    ORDER BY audit.occurred_at DESC, audit.id DESC LIMIT 50;
END
$body$;
REVOKE ALL ON FUNCTION public.list_account_security_audit(bigint,bigint) FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.prune_account_security_audit()
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    removed integer;
BEGIN
    DELETE FROM public.account_security_audit WHERE account_security_audit.id IN (
        SELECT audit.id FROM public.account_security_audit audit
        WHERE audit.occurred_at < pg_catalog.clock_timestamp() - interval '365 days'
        ORDER BY audit.occurred_at, audit.id LIMIT 1000
    );
    GET DIAGNOSTICS removed = ROW_COUNT;
    RETURN removed;
END
$body$;
REVOKE ALL ON FUNCTION public.prune_account_security_audit() FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.admin_request_account_deletion(
    input_actor_id bigint, input_actor_auth_version bigint,
    input_target_id bigint, input_email text, input_acknowledge boolean
) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    target_row public.accounts%ROWTYPE;
    checked_at timestamptz;
BEGIN
    IF input_acknowledge IS DISTINCT FROM true OR input_email IS NULL
       OR input_actor_id IS NULL OR input_target_id IS NULL
       OR input_actor_id = input_target_id THEN
        RAISE EXCEPTION 'account deletion unavailable' USING ERRCODE = '42501';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(901412, 1);
    PERFORM 1 FROM public.accounts
    WHERE id IN (input_actor_id, input_target_id) ORDER BY id FOR UPDATE;
    SELECT * INTO target_row FROM public.accounts WHERE id = input_target_id;
    IF target_row.id IS NULL OR target_row.email <> input_email
       OR target_row.deletion_deadline IS NOT NULL THEN
        RAISE EXCEPTION 'account deletion unavailable' USING ERRCODE = '42501';
    END IF;
    PERFORM public.admin_set_account_enabled(
        input_actor_id, input_actor_auth_version, input_target_id, false);
    checked_at := pg_catalog.clock_timestamp();
    UPDATE public.accounts SET deletion_deadline = checked_at + interval '30 days',
        updated_at = checked_at WHERE id = input_target_id;
    INSERT INTO public.account_security_audit
        (occurred_at, actor_account_id, target_account_id, action, outcome)
    VALUES (checked_at, input_actor_id, input_target_id, 'request_deletion', 'scheduled');
    RETURN 'scheduled';
END
$body$;
REVOKE ALL ON FUNCTION public.admin_request_account_deletion(bigint,bigint,bigint,text,boolean) FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.admin_cancel_account_deletion(
    input_actor_id bigint, input_actor_auth_version bigint, input_target_id bigint
) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    target_row public.accounts%ROWTYPE;
    checked_at timestamptz;
BEGIN
    IF input_actor_id IS NULL OR input_target_id IS NULL OR input_actor_id = input_target_id THEN
        RAISE EXCEPTION 'account deletion unavailable' USING ERRCODE = '42501';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(901412, 1);
    PERFORM 1 FROM public.accounts
    WHERE id IN (input_actor_id, input_target_id) ORDER BY id FOR UPDATE;
    SELECT * INTO target_row FROM public.accounts WHERE id = input_target_id;
    checked_at := pg_catalog.clock_timestamp();
    IF target_row.id IS NULL OR target_row.deletion_deadline IS NULL
       OR target_row.deletion_deadline <= checked_at THEN
        RAISE EXCEPTION 'account deletion unavailable' USING ERRCODE = '42501';
    END IF;
    UPDATE public.accounts SET deletion_deadline = NULL WHERE id = input_target_id;
    PERFORM public.admin_set_account_enabled(
        input_actor_id, input_actor_auth_version, input_target_id, true);
    INSERT INTO public.account_security_audit
        (occurred_at, actor_account_id, target_account_id, action, outcome)
    VALUES (checked_at, input_actor_id, input_target_id, 'cancel_deletion', 'cancelled');
    RETURN 'cancelled';
END
$body$;
REVOKE ALL ON FUNCTION public.admin_cancel_account_deletion(bigint,bigint,bigint) FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.admin_purge_account(
    input_actor_id bigint, input_actor_auth_version bigint, input_target_id bigint,
    input_email text, input_confirm boolean,
    input_verified_password_hash text, input_browser_digest text
) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    actor_row public.accounts%ROWTYPE;
    target_row public.accounts%ROWTYPE;
    checked_at timestamptz;
BEGIN
    IF input_actor_id IS NULL OR input_actor_auth_version IS NULL
       OR input_target_id IS NULL OR input_actor_id = input_target_id
       OR input_email IS NULL OR input_confirm IS DISTINCT FROM true THEN
        RAISE EXCEPTION 'account purge unavailable' USING ERRCODE = '42501';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(901412, 1);
    IF NOT pg_catalog.pg_try_advisory_xact_lock(-input_target_id) THEN
        RAISE EXCEPTION 'account purge unavailable' USING ERRCODE = '42501';
    END IF;
    -- An admitted unit holds a shared account lock. Never purge through it.
    PERFORM 1 FROM public.accounts
    WHERE id IN (input_actor_id, input_target_id) ORDER BY id FOR UPDATE NOWAIT;
    SELECT * INTO actor_row FROM public.accounts WHERE id = input_actor_id;
    SELECT * INTO target_row FROM public.accounts WHERE id = input_target_id;
    checked_at := pg_catalog.clock_timestamp();
    IF actor_row.id IS NULL OR NOT actor_row.is_admin OR NOT actor_row.is_enabled
       OR actor_row.auth_version <> input_actor_auth_version
       OR target_row.id IS NULL OR target_row.email <> input_email
       OR target_row.is_enabled OR target_row.deletion_deadline IS NULL
       OR target_row.deletion_deadline > checked_at
       OR NOT EXISTS (
           SELECT 1 FROM public.accounts a
           WHERE a.id <> input_target_id AND a.is_admin AND a.is_enabled
             AND (NULLIF(a.password_hash, '') IS NOT NULL OR EXISTS (
                 SELECT 1 FROM public.oidc_identities i WHERE i.account_id = a.id))
       ) THEN
        RAISE EXCEPTION 'account purge unavailable' USING ERRCODE = '42501';
    END IF;
    IF input_verified_password_hash IS NOT NULL THEN
        IF actor_row.password_hash IS NULL
           OR actor_row.password_hash <> input_verified_password_hash THEN
            RAISE EXCEPTION 'account purge unavailable' USING ERRCODE = '42501';
        END IF;
    ELSIF input_browser_digest IS NULL OR NOT EXISTS (
        SELECT 1 FROM public.oidc_action_proofs proof
        WHERE proof.browser_digest = input_browser_digest
          AND proof.account_id = input_actor_id AND proof.action = 'purge_account'
          AND proof.target = input_target_id::text
          AND proof.created_at >= target_row.deletion_deadline - interval '30 days'
    ) OR NOT public.consume_oidc_action_proof(
        input_actor_id, input_actor_auth_version, 'purge_account',
        input_target_id::text, input_browser_digest
    ) THEN
        RAISE EXCEPTION 'account purge unavailable' USING ERRCODE = '42501';
    END IF;

    DELETE FROM public.trip_boundary_overrides WHERE account_id = input_target_id;
    DELETE FROM public.points WHERE account_id = input_target_id;
    DELETE FROM public.expenses WHERE account_id = input_target_id;
    DELETE FROM public.odometer_readings WHERE account_id = input_target_id;
    DELETE FROM public.trips WHERE account_id = input_target_id;
    DELETE FROM public.stays WHERE account_id = input_target_id;
    DELETE FROM public.raw_messages WHERE account_id = input_target_id;
    DELETE FROM public.detector_state WHERE account_id = input_target_id;
    DELETE FROM public.tracking_device_aliases WHERE account_id = input_target_id;
    DELETE FROM public.ingest_credentials WHERE account_id = input_target_id;
    DELETE FROM public.tracking_devices WHERE account_id = input_target_id;
    DELETE FROM public.tag_rules WHERE account_id = input_target_id;
    DELETE FROM public.places WHERE account_id = input_target_id;
    DELETE FROM public.vehicles WHERE account_id = input_target_id;
    DELETE FROM public.mileage_rates WHERE account_id = input_target_id;
    DELETE FROM public.geocode_cache WHERE account_id = input_target_id;
    DELETE FROM public.nudge_delivery_windows WHERE account_id = input_target_id;
    DELETE FROM public.odometer_reminder_windows WHERE account_id = input_target_id;
    DELETE FROM public.email_deliveries WHERE account_id = input_target_id;
    DELETE FROM public.account_settings WHERE account_id = input_target_id;
    DELETE FROM public.email_challenges WHERE account_id = input_target_id;
    DELETE FROM public.oidc_action_proofs WHERE account_id = input_target_id;
    DELETE FROM public.oidc_attempts
    WHERE account_id = input_target_id OR invitation_digest IN (
        SELECT token_digest FROM public.invitations WHERE issued_by = input_target_id);
    DELETE FROM public.invitations WHERE issued_by = input_target_id;
    DELETE FROM public.oidc_identities WHERE account_id = input_target_id;
    UPDATE public.instance_state SET first_account_id = NULL WHERE first_account_id = input_target_id;
    DELETE FROM public.accounts WHERE id = input_target_id;
    INSERT INTO public.account_security_audit
        (occurred_at, actor_account_id, target_account_id, action, outcome)
    VALUES (checked_at, input_actor_id, input_target_id, 'purge_account', 'purged');
    PERFORM public.prune_account_security_audit();
    RETURN 'purged';
END
$body$;
REVOKE ALL ON FUNCTION public.admin_purge_account(bigint,bigint,bigint,text,boolean,text,text) FROM PUBLIC;
