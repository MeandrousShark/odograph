-- Existing public reset requests remain generic. The old admin-shaped call
-- has no actor proof and must not issue a reset after multi-account activation.
CREATE OR REPLACE FUNCTION public.issue_password_reset(
    input_account_id bigint, input_email text, input_initiator text, input_token_digest text
) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    account_row public.accounts%ROWTYPE;
    issued_at timestamptz;
BEGIN
    IF input_initiator IS DISTINCT FROM 'public' OR input_account_id IS NOT NULL
       OR input_email IS NULL OR input_token_digest IS NULL
       OR input_token_digest !~ '^[0-9a-f]{64}$' THEN
        RETURN NULL;
    END IF;
    SELECT * INTO account_row FROM public.accounts
    WHERE email = lower(btrim(input_email)) FOR UPDATE;
    IF NOT FOUND OR NOT account_row.is_enabled OR account_row.email_verified_at IS NULL
       OR account_row.password_hash = '' THEN
        RETURN NULL;
    END IF;
    issued_at := pg_catalog.clock_timestamp();
    IF EXISTS (
        SELECT 1 FROM public.email_challenges
        WHERE account_id = account_row.id AND purpose = 'reset_password'
          AND consumed_at IS NULL AND revoked_at IS NULL
          AND expires_at > issued_at AND created_at > issued_at - interval '10 minutes'
          AND issued_email = account_row.email AND issued_auth_version = account_row.auth_version
    ) THEN
        RETURN NULL;
    END IF;
    IF EXISTS (SELECT 1 FROM public.email_challenges
               WHERE account_id = account_row.id AND purpose = 'reset_password'
                 AND initiator = 'public' AND created_at > issued_at - interval '1 minute')
       OR (SELECT count(*) FROM public.email_challenges
           WHERE account_id = account_row.id AND purpose = 'reset_password'
             AND initiator = 'public' AND created_at > issued_at - interval '24 hours') >= 5 THEN
        RETURN NULL;
    END IF;
    UPDATE public.email_challenges SET revoked_at = issued_at
    WHERE account_id = account_row.id AND purpose = 'reset_password'
      AND consumed_at IS NULL AND revoked_at IS NULL;
    INSERT INTO public.email_challenges
        (account_id, purpose, initiator, target_email, issued_email, issued_auth_version,
         token_digest, created_at, expires_at)
    VALUES (account_row.id, 'reset_password', 'public', account_row.email, account_row.email,
            account_row.auth_version, input_token_digest, issued_at, issued_at + interval '30 minutes');
    RETURN account_row.email;
END
$body$;

-- Lock actor and target in ID order. Authorization and reset issuance are
-- one transaction, so a concurrent disable cannot slip between them.
CREATE FUNCTION public.issue_admin_password_reset(
    input_actor_id bigint, input_actor_version bigint,
    input_target_id bigint, input_token_digest text
) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    actor_row public.accounts%ROWTYPE;
    target_row public.accounts%ROWTYPE;
    issued_at timestamptz;
BEGIN
    IF input_actor_id IS NULL OR input_actor_version IS NULL OR input_target_id IS NULL
       OR input_token_digest IS NULL OR input_token_digest !~ '^[0-9a-f]{64}$' THEN
        RETURN NULL;
    END IF;
    PERFORM 1 FROM public.accounts WHERE id IN (input_actor_id, input_target_id)
    ORDER BY id FOR UPDATE;
    SELECT * INTO actor_row FROM public.accounts WHERE id = input_actor_id;
    SELECT * INTO target_row FROM public.accounts WHERE id = input_target_id;
    IF actor_row.id IS NULL OR NOT actor_row.is_admin OR NOT actor_row.is_enabled
       OR actor_row.auth_version <> input_actor_version
       OR target_row.id IS NULL OR NOT target_row.is_enabled
       OR target_row.email_verified_at IS NULL OR target_row.password_hash = '' THEN
        RETURN NULL;
    END IF;
    issued_at := pg_catalog.clock_timestamp();
    IF EXISTS (SELECT 1 FROM public.email_challenges
               WHERE account_id = target_row.id AND purpose = 'reset_password'
                 AND initiator = 'admin' AND created_at > issued_at - interval '1 minute')
       OR (SELECT count(*) FROM public.email_challenges
           WHERE account_id = target_row.id AND purpose = 'reset_password'
             AND initiator = 'admin' AND created_at > issued_at - interval '24 hours') >= 5 THEN
        RETURN NULL;
    END IF;
    UPDATE public.email_challenges SET revoked_at = issued_at
    WHERE account_id = target_row.id AND purpose = 'reset_password'
      AND consumed_at IS NULL AND revoked_at IS NULL;
    INSERT INTO public.email_challenges
        (account_id, purpose, initiator, target_email, issued_email, issued_auth_version,
         token_digest, created_at, expires_at)
    VALUES (target_row.id, 'reset_password', 'admin', target_row.email, target_row.email,
            target_row.auth_version, input_token_digest, issued_at, issued_at + interval '30 minutes');
    RETURN target_row.email;
END
$body$;

-- These checks hold SHARE locks until the caller's transaction ends. The
-- caller starts the transport task while that transaction is still open.
CREATE FUNCTION public.password_reset_send_usable(
    input_token_digest text, input_actor_id bigint, input_actor_version bigint
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    challenge_row public.email_challenges%ROWTYPE;
    actor_row public.accounts%ROWTYPE;
    target_row public.accounts%ROWTYPE;
BEGIN
    IF input_token_digest IS NULL OR input_token_digest !~ '^[0-9a-f]{64}$' THEN
        RETURN false;
    END IF;
    SELECT * INTO challenge_row FROM public.email_challenges
    WHERE purpose = 'reset_password' AND token_digest = input_token_digest;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    IF (challenge_row.initiator = 'public' AND (input_actor_id IS NOT NULL OR input_actor_version IS NOT NULL))
       OR (challenge_row.initiator = 'admin' AND (input_actor_id IS NULL OR input_actor_version IS NULL)) THEN
        RETURN false;
    END IF;
    PERFORM 1 FROM public.accounts
    WHERE id = challenge_row.account_id OR id = input_actor_id ORDER BY id FOR SHARE;
    SELECT * INTO target_row FROM public.accounts WHERE id = challenge_row.account_id;
    IF input_actor_id IS NOT NULL THEN
        SELECT * INTO actor_row FROM public.accounts WHERE id = input_actor_id;
        IF actor_row.id IS NULL OR NOT actor_row.is_enabled OR NOT actor_row.is_admin
           OR actor_row.auth_version <> input_actor_version THEN
            RETURN false;
        END IF;
    END IF;
    SELECT * INTO challenge_row FROM public.email_challenges
    WHERE purpose = 'reset_password' AND token_digest = input_token_digest;
    RETURN target_row.id IS NOT NULL AND target_row.is_enabled
       AND target_row.email_verified_at IS NOT NULL
       AND challenge_row.id IS NOT NULL AND challenge_row.consumed_at IS NULL
       AND challenge_row.revoked_at IS NULL AND challenge_row.expires_at > pg_catalog.clock_timestamp()
       AND challenge_row.issued_email = target_row.email
       AND challenge_row.target_email = target_row.email
       AND challenge_row.issued_auth_version = target_row.auth_version;
END
$body$;

CREATE FUNCTION public.email_challenge_send_usable(
    input_account_id bigint, input_version bigint, input_purpose text, input_token_digest text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    account_row public.accounts%ROWTYPE;
    challenge_row public.email_challenges%ROWTYPE;
BEGIN
    IF input_purpose NOT IN ('verify_current', 'change_email')
       OR input_token_digest IS NULL OR input_token_digest !~ '^[0-9a-f]{64}$' THEN
        RETURN false;
    END IF;
    SELECT * INTO account_row FROM public.accounts WHERE id = input_account_id FOR SHARE;
    IF NOT FOUND OR NOT account_row.is_enabled OR account_row.auth_version <> input_version THEN
        RETURN false;
    END IF;
    SELECT * INTO challenge_row FROM public.email_challenges
    WHERE account_id = input_account_id AND purpose = input_purpose
      AND token_digest = input_token_digest;
    RETURN challenge_row.id IS NOT NULL AND challenge_row.consumed_at IS NULL
       AND challenge_row.revoked_at IS NULL AND challenge_row.expires_at > pg_catalog.clock_timestamp()
       AND challenge_row.issued_email = account_row.email
       AND challenge_row.issued_auth_version = account_row.auth_version
       AND (input_purpose = 'change_email' OR challenge_row.target_email = account_row.email);
END
$body$;

REVOKE ALL ON FUNCTION public.issue_admin_password_reset(bigint,bigint,bigint,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.password_reset_send_usable(text,bigint,bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.email_challenge_send_usable(bigint,bigint,text,text) FROM PUBLIC;

DO $$
BEGIN
    IF to_regclass('odograph_service.managed_role_state') IS NOT NULL THEN
        ALTER FUNCTION public.issue_password_reset(bigint,text,text,text) OWNER TO odograph_bootstrap;
        ALTER FUNCTION public.issue_admin_password_reset(bigint,bigint,bigint,text) OWNER TO odograph_bootstrap;
        ALTER FUNCTION public.password_reset_send_usable(text,bigint,bigint) OWNER TO odograph_bootstrap;
        ALTER FUNCTION public.email_challenge_send_usable(bigint,bigint,text,text) OWNER TO odograph_bootstrap;
        GRANT EXECUTE ON FUNCTION public.issue_admin_password_reset(bigint,bigint,bigint,text) TO odograph_control;
        GRANT EXECUTE ON FUNCTION public.password_reset_send_usable(text,bigint,bigint) TO odograph_control;
        GRANT EXECUTE ON FUNCTION public.email_challenge_send_usable(bigint,bigint,text,text) TO odograph_control;
    END IF;
END $$;
