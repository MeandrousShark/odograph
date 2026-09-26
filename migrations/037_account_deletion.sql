ALTER TABLE public.accounts ADD COLUMN deletion_deadline timestamptz;
ALTER TABLE public.accounts ADD CONSTRAINT accounts_deletion_disabled
    CHECK (deletion_deadline IS NULL OR NOT is_enabled);
ALTER TABLE public.instance_state DROP CONSTRAINT instance_state_check;
ALTER TABLE public.instance_state ADD CONSTRAINT instance_state_bootstrap_marker
    CHECK (first_account_id IS NULL OR bootstrap_completed_at IS NOT NULL);

-- Installed under the NOLOGIN bootstrap identity by application role setup.
-- Explicit schema qualification prevents a caller-controlled search path from
-- replacing any privileged object. PUBLIC receives no execution privilege.
CREATE OR REPLACE FUNCTION public.bootstrap_first_account(
    input_email text, input_password_hash text, input_display_tz text DEFAULT 'UTC'
) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    owner_id bigint;
    established_id bigint;
    completed_at timestamptz;
BEGIN
    SELECT first_account_id, bootstrap_completed_at INTO established_id, completed_at
    FROM public.instance_state WHERE id = 1 FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'instance bootstrap state is missing';
    END IF;
    IF completed_at IS NOT NULL OR established_id IS NOT NULL OR EXISTS (SELECT 1 FROM public.accounts) THEN
        RAISE EXCEPTION 'first-account setup is already complete' USING ERRCODE = '23505';
    END IF;
    IF input_email IS NULL OR btrim(input_email) = ''
        OR input_password_hash IS NULL OR input_password_hash = '' THEN
        RAISE EXCEPTION 'account credentials are required' USING ERRCODE = '22023';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_timezone_names WHERE name = input_display_tz) THEN
        RAISE EXCEPTION 'display time zone is invalid' USING ERRCODE = '22023';
    END IF;
    INSERT INTO public.accounts (email, password_hash, is_admin)
    VALUES (lower(btrim(input_email)), input_password_hash, true)
    RETURNING id INTO owner_id;
    INSERT INTO public.account_settings (account_id, display_tz)
    VALUES (owner_id, input_display_tz);
    INSERT INTO public.vehicles (account_id, name, is_default)
    VALUES (owner_id, 'My Car', true);
    INSERT INTO public.tag_rules (account_id, a_kind, b_kind, category)
    VALUES (owner_id, 'home', 'work', 'personal'),
           (owner_id, 'work', 'work', 'business');
    INSERT INTO public.mileage_rates (account_id, year, rate_per_mi, rate_h2_per_mi, h2_start_month)
    SELECT owner_id, year, rate_per_mi, rate_h2_per_mi, h2_start_month
    FROM public.reference_mileage_rates;
    UPDATE public.instance_state SET first_account_id = owner_id, bootstrap_completed_at = now()
    WHERE id = 1;
    RETURN owner_id;
END $$;
REVOKE ALL ON FUNCTION public.bootstrap_first_account(text, text, text) FROM PUBLIC;

-- Pending OIDC browser transactions contain only digests and bounded metadata.
CREATE OR REPLACE FUNCTION public.start_oidc_attempt(
    input_action text, input_state_digest text, input_nonce_digest text,
    input_browser_digest text, input_account_id bigint, input_auth_version bigint,
    input_invitation_digest text, input_proof_action text, input_target text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    checked_at timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF input_action NOT IN ('invite', 'link', 'reauth')
       OR input_state_digest IS NULL OR input_state_digest !~ '^[0-9a-f]{64}$'
       OR input_nonce_digest IS NULL OR input_nonce_digest !~ '^[0-9a-f]{64}$'
       OR input_browser_digest IS NULL OR input_browser_digest !~ '^[0-9a-f]{64}$'
       OR (input_action = 'invite') <> (input_invitation_digest IS NOT NULL)
       OR (input_action = 'invite' AND input_invitation_digest !~ '^[0-9a-f]{64}$')
       OR (input_action = 'invite') = (input_account_id IS NOT NULL)
       OR (input_action = 'invite') = (input_auth_version IS NOT NULL)
       OR (input_action = 'reauth') <> (input_proof_action IS NOT NULL)
       OR (input_action = 'reauth' AND input_proof_action NOT IN
           ('verify_current', 'change_email', 'add_password', 'purge_account'))
       OR input_target IS NULL OR length(input_target) > 512 THEN
        RETURN false;
    END IF;
    IF input_action = 'invite' THEN
        IF NOT EXISTS (
            SELECT 1 FROM public.invitations i JOIN public.accounts issuer ON issuer.id = i.issued_by
            WHERE i.token_digest = input_invitation_digest AND i.consumed_at IS NULL
              AND i.revoked_at IS NULL AND i.expires_at > checked_at
              AND issuer.is_admin AND issuer.is_enabled
              AND NOT EXISTS (SELECT 1 FROM public.accounts a WHERE a.email = i.email)
        ) THEN
            RETURN false;
        END IF;
    ELSIF NOT EXISTS (
        SELECT 1 FROM public.accounts a WHERE a.id = input_account_id
          AND a.is_enabled AND a.auth_version = input_auth_version
    ) THEN
        RETURN false;
    END IF;
    IF input_action = 'reauth' AND (
        (input_proof_action = 'purge_account' AND NOT EXISTS (
            SELECT 1 FROM public.accounts actor, public.accounts target
            WHERE actor.id = input_account_id AND actor.is_admin AND actor.is_enabled
              AND target.id <> actor.id AND target.id::text = input_target
              AND NOT target.is_enabled AND target.deletion_deadline <= checked_at))
        OR (input_proof_action = 'add_password' AND input_target <> '')
        OR (input_proof_action = 'verify_current' AND NOT EXISTS (
            SELECT 1 FROM public.accounts WHERE id = input_account_id AND email = input_target))
        OR (input_proof_action = 'change_email' AND EXISTS (
            SELECT 1 FROM public.accounts WHERE id = input_account_id AND email = input_target))
    ) THEN
        RETURN false;
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(901411, 1);
    DELETE FROM public.oidc_attempts WHERE expires_at <= checked_at;
    DELETE FROM public.oidc_action_proofs WHERE expires_at <= checked_at;
    IF input_action = 'reauth' THEN
        DELETE FROM public.oidc_action_proofs
        WHERE browser_digest = input_browser_digest AND action = input_proof_action;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.oidc_attempts
                   WHERE browser_digest = input_browser_digest AND action = input_action)
       AND (SELECT count(*) FROM public.oidc_attempts) >= 1024 THEN
        RETURN false;
    END IF;
    DELETE FROM public.oidc_attempts
    WHERE browser_digest = input_browser_digest AND action = input_action;
    INSERT INTO public.oidc_attempts
        (action, state_digest, nonce_digest, browser_digest, account_id, auth_version,
         invitation_digest, proof_action, target, created_at, expires_at)
    VALUES (input_action, input_state_digest, input_nonce_digest, input_browser_digest,
            input_account_id, input_auth_version, input_invitation_digest, input_proof_action,
            input_target, checked_at, checked_at + interval '10 minutes');
    RETURN true;
END
$body$;
REVOKE ALL ON FUNCTION public.start_oidc_attempt(text,text,text,text,bigint,bigint,text,text,text) FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.consume_oidc_attempt(
    input_action text, input_state_digest text, input_nonce_digest text,
    input_browser_digest text, input_account_id bigint, input_auth_version bigint
) RETURNS TABLE (
    invitation_digest text, account_id bigint, auth_version bigint,
    proof_action text, target text, started_at timestamptz
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
BEGIN
    RETURN QUERY
    DELETE FROM public.oidc_attempts a
    WHERE a.action = input_action AND a.state_digest = input_state_digest
      AND a.nonce_digest = input_nonce_digest AND a.browser_digest = input_browser_digest
      AND a.account_id IS NOT DISTINCT FROM input_account_id
      AND a.auth_version IS NOT DISTINCT FROM input_auth_version
      AND a.expires_at > pg_catalog.clock_timestamp()
    RETURNING a.invitation_digest, a.account_id, a.auth_version,
              a.proof_action, a.target, a.created_at;
END
$body$;
REVOKE ALL ON FUNCTION public.consume_oidc_attempt(text,text,text,text,bigint,bigint) FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.finish_oidc_reauth(
    input_state_digest text, input_nonce_digest text, input_browser_digest text,
    input_account_id bigint, input_auth_version bigint, input_issuer text,
    input_subject text, input_auth_time timestamptz
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    attempt public.oidc_attempts%ROWTYPE;
    checked_at timestamptz := pg_catalog.clock_timestamp();
BEGIN
    PERFORM 1 FROM public.accounts
    WHERE id = input_account_id AND is_enabled AND auth_version = input_auth_version FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    DELETE FROM public.oidc_attempts a
    WHERE a.action = 'reauth' AND a.state_digest = input_state_digest
      AND a.nonce_digest = input_nonce_digest AND a.browser_digest = input_browser_digest
      AND a.account_id = input_account_id AND a.auth_version = input_auth_version
      AND a.expires_at > checked_at
    RETURNING a.* INTO attempt;
    IF NOT FOUND OR input_auth_time IS NULL
       OR input_auth_time < attempt.created_at - interval '60 seconds'
       OR input_auth_time > checked_at + interval '60 seconds' THEN
        RETURN false;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.oidc_identities
        WHERE account_id = input_account_id AND issuer = input_issuer AND subject = input_subject
    ) THEN
        RETURN false;
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(901411, 1);
    DELETE FROM public.oidc_action_proofs WHERE expires_at <= checked_at;
    IF NOT EXISTS (SELECT 1 FROM public.oidc_action_proofs
                   WHERE browser_digest = input_browser_digest
                     AND action = attempt.proof_action)
       AND (SELECT count(*) FROM public.oidc_action_proofs) >= 1024 THEN
        RETURN false;
    END IF;
    INSERT INTO public.oidc_action_proofs
        (browser_digest, account_id, auth_version, action, target, created_at, expires_at)
    VALUES (input_browser_digest, input_account_id, input_auth_version,
            attempt.proof_action, attempt.target, checked_at, attempt.expires_at)
    ON CONFLICT (browser_digest, action) DO UPDATE SET
        account_id = EXCLUDED.account_id, auth_version = EXCLUDED.auth_version,
        target = EXCLUDED.target, created_at = EXCLUDED.created_at,
        expires_at = EXCLUDED.expires_at;
    RETURN true;
END
$body$;
REVOKE ALL ON FUNCTION public.finish_oidc_reauth(text,text,text,bigint,bigint,text,text,timestamptz) FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.consume_oidc_action_proof(
    input_account_id bigint, input_auth_version bigint, input_action text,
    input_target text, input_browser_digest text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    removed_count integer;
BEGIN
    PERFORM 1 FROM public.accounts
    WHERE id = input_account_id AND is_enabled AND auth_version = input_auth_version FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    DELETE FROM public.oidc_action_proofs
    WHERE browser_digest = input_browser_digest AND account_id = input_account_id
      AND auth_version = input_auth_version AND action = input_action AND target = input_target
      AND expires_at > pg_catalog.clock_timestamp();
    GET DIAGNOSTICS removed_count = ROW_COUNT;
    RETURN removed_count = 1;
END
$body$;
REVOKE ALL ON FUNCTION public.consume_oidc_action_proof(bigint,bigint,text,text,text) FROM PUBLIC;

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

DO $$
DECLARE table_name text; function_name text;
BEGIN
    IF to_regclass('odograph_service.managed_role_state') IS NOT NULL THEN
        FOREACH table_name IN ARRAY ARRAY[
            'raw_messages','points','stays','trips','detector_state','places',
            'tag_rules','geocode_cache','trip_boundary_overrides','vehicles',
            'mileage_rates','odometer_readings','expenses','nudge_delivery_windows',
            'odometer_reminder_windows','email_deliveries','account_settings',
            'tracking_devices','tracking_device_aliases','ingest_credentials','email_challenges'
        ] LOOP
            EXECUTE format('GRANT SELECT, DELETE ON public.%I TO odograph_bootstrap', table_name);
            IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_policy
                          WHERE polrelid = format('public.%I', table_name)::regclass
                            AND polname = 'bootstrap_defaults') THEN
                EXECUTE format('CREATE POLICY bootstrap_defaults ON public.%I FOR ALL TO odograph_bootstrap USING (true) WITH CHECK (true)', table_name);
            END IF;
        END LOOP;
        GRANT DELETE ON public.accounts, public.invitations TO odograph_bootstrap;
        ALTER FUNCTION public.bootstrap_first_account(text,text,text) OWNER TO odograph_bootstrap;
        GRANT EXECUTE ON FUNCTION public.bootstrap_first_account(text,text,text) TO odograph_control;
        ALTER FUNCTION public.start_oidc_attempt(text,text,text,text,bigint,bigint,text,text,text) OWNER TO odograph_bootstrap;
        GRANT EXECUTE ON FUNCTION public.start_oidc_attempt(text,text,text,text,bigint,bigint,text,text,text) TO odograph_control;
        ALTER FUNCTION public.consume_oidc_attempt(text,text,text,text,bigint,bigint) OWNER TO odograph_bootstrap;
        GRANT EXECUTE ON FUNCTION public.consume_oidc_attempt(text,text,text,text,bigint,bigint) TO odograph_control;
        ALTER FUNCTION public.finish_oidc_reauth(text,text,text,bigint,bigint,text,text,timestamptz) OWNER TO odograph_bootstrap;
        GRANT EXECUTE ON FUNCTION public.finish_oidc_reauth(text,text,text,bigint,bigint,text,text,timestamptz) TO odograph_control;
        ALTER FUNCTION public.consume_oidc_action_proof(bigint,bigint,text,text,text) OWNER TO odograph_bootstrap;
        GRANT EXECUTE ON FUNCTION public.consume_oidc_action_proof(bigint,bigint,text,text,text) TO odograph_control;
        ALTER FUNCTION public.admin_set_account_enabled(bigint,bigint,bigint,boolean) OWNER TO odograph_bootstrap;
        GRANT EXECUTE ON FUNCTION public.admin_set_account_enabled(bigint,bigint,bigint,boolean) TO odograph_control;
        ALTER FUNCTION public.list_account_security_audit(bigint,bigint) OWNER TO odograph_bootstrap;
        GRANT EXECUTE ON FUNCTION public.list_account_security_audit(bigint,bigint) TO odograph_control;
        ALTER FUNCTION public.prune_account_security_audit() OWNER TO odograph_bootstrap;
        GRANT EXECUTE ON FUNCTION public.prune_account_security_audit() TO odograph_control;
        ALTER FUNCTION public.admin_request_account_deletion(bigint,bigint,bigint,text,boolean) OWNER TO odograph_bootstrap;
        GRANT EXECUTE ON FUNCTION public.admin_request_account_deletion(bigint,bigint,bigint,text,boolean) TO odograph_control;
        ALTER FUNCTION public.admin_cancel_account_deletion(bigint,bigint,bigint) OWNER TO odograph_bootstrap;
        GRANT EXECUTE ON FUNCTION public.admin_cancel_account_deletion(bigint,bigint,bigint) TO odograph_control;
        ALTER FUNCTION public.admin_purge_account(bigint,bigint,bigint,text,boolean,text,text) OWNER TO odograph_bootstrap;
        GRANT EXECUTE ON FUNCTION public.admin_purge_account(bigint,bigint,bigint,text,boolean,text,text) TO odograph_control;
    END IF;
END $$;
