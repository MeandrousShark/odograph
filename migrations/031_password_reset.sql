-- Password reset reuses protected email challenges with its own purpose and
-- issuance budgets. Public and administrator initiation are budgeted apart
-- from each other and from verification or login-email changes.
ALTER TABLE public.email_challenges DROP CONSTRAINT email_challenges_purpose_check;
ALTER TABLE public.email_challenges ADD CONSTRAINT email_challenges_purpose_check
    CHECK (purpose IN ('verify_current', 'change_email', 'reset_password'));
ALTER TABLE public.email_challenges ADD COLUMN initiator text NOT NULL DEFAULT 'self';
ALTER TABLE public.email_challenges ADD CONSTRAINT email_challenges_initiator_check
    CHECK ((purpose = 'reset_password') = (initiator IN ('public', 'admin'))
           AND initiator IN ('self', 'public', 'admin'));

-- Successor of 030's function: its budget now counts only verification and
-- login-email change challenges, so reset requests cannot exhaust it.
CREATE OR REPLACE FUNCTION public.issue_email_challenge(
    input_account_id bigint, input_auth_version bigint, input_purpose text,
    input_target_email text, input_token_digest text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    account_row public.accounts%ROWTYPE;
    target_email text := lower(btrim(input_target_email));
    issued_at timestamptz;
BEGIN
    IF input_purpose NOT IN ('verify_current', 'change_email')
       OR input_target_email IS NULL OR target_email = ''
       OR input_token_digest IS NULL OR input_token_digest !~ '^[0-9a-f]{64}$' THEN
        RETURN false;
    END IF;
    SELECT * INTO account_row FROM public.accounts WHERE id = input_account_id FOR UPDATE;
    IF NOT FOUND OR NOT account_row.is_enabled OR account_row.auth_version <> input_auth_version
       OR account_row.password_hash = ''
       OR (input_purpose = 'verify_current' AND target_email <> account_row.email)
       OR (input_purpose = 'change_email' AND target_email = account_row.email) THEN
        RETURN false;
    END IF;
    issued_at := pg_catalog.clock_timestamp();
    IF EXISTS (SELECT 1 FROM public.email_challenges
               WHERE account_id = input_account_id AND purpose IN ('verify_current', 'change_email')
                 AND created_at > issued_at - interval '1 minute')
       OR (SELECT count(*) FROM public.email_challenges
           WHERE account_id = input_account_id AND purpose IN ('verify_current', 'change_email')
             AND created_at > issued_at - interval '24 hours') >= 5 THEN
        RETURN false;
    END IF;
    UPDATE public.email_challenges SET revoked_at = issued_at
    WHERE account_id = input_account_id AND purpose = input_purpose
      AND consumed_at IS NULL AND revoked_at IS NULL;
    INSERT INTO public.email_challenges
        (account_id, purpose, target_email, issued_email, issued_auth_version,
         token_digest, created_at, expires_at)
    VALUES (input_account_id, input_purpose, target_email, account_row.email,
            account_row.auth_version, input_token_digest, issued_at, issued_at + interval '30 minutes');
    RETURN true;
END
$body$;

-- Issue one reset for an enabled account with a verified login email. Public
-- initiation names the login email; administrator initiation names the
-- account ID. Returns the bound delivery address, or NULL for every refusal.
CREATE FUNCTION public.issue_password_reset(
    input_account_id bigint, input_email text, input_initiator text, input_token_digest text
) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    account_row public.accounts%ROWTYPE;
    issued_at timestamptz;
BEGIN
    IF input_token_digest IS NULL OR input_token_digest !~ '^[0-9a-f]{64}$'
       OR NOT ((input_initiator = 'public' AND input_account_id IS NULL AND input_email IS NOT NULL)
               OR (input_initiator = 'admin' AND input_account_id IS NOT NULL AND input_email IS NULL)) THEN
        RETURN NULL;
    END IF;
    IF input_initiator = 'public' THEN
        SELECT * INTO account_row FROM public.accounts
        WHERE email = lower(btrim(input_email)) FOR UPDATE;
    ELSE
        SELECT * INTO account_row FROM public.accounts WHERE id = input_account_id FOR UPDATE;
    END IF;
    IF NOT FOUND OR NOT account_row.is_enabled OR account_row.email_verified_at IS NULL
       OR account_row.password_hash = '' THEN
        RETURN NULL;
    END IF;
    issued_at := pg_catalog.clock_timestamp();
    -- A public request cannot cancel a recently delivered reset. Coalescing
    -- issues nothing, supersedes nothing and spends no budget.
    IF input_initiator = 'public' AND EXISTS (
        SELECT 1 FROM public.email_challenges
        WHERE account_id = account_row.id AND purpose = 'reset_password'
          AND consumed_at IS NULL AND revoked_at IS NULL
          AND expires_at > issued_at AND created_at > issued_at - interval '10 minutes'
          AND issued_email = account_row.email AND issued_auth_version = account_row.auth_version) THEN
        RETURN NULL;
    END IF;
    IF EXISTS (SELECT 1 FROM public.email_challenges
               WHERE account_id = account_row.id AND purpose = 'reset_password'
                 AND initiator = input_initiator AND created_at > issued_at - interval '1 minute')
       OR (SELECT count(*) FROM public.email_challenges
           WHERE account_id = account_row.id AND purpose = 'reset_password'
             AND initiator = input_initiator AND created_at > issued_at - interval '24 hours') >= 5 THEN
        RETURN NULL;
    END IF;
    UPDATE public.email_challenges SET revoked_at = issued_at
    WHERE account_id = account_row.id AND purpose = 'reset_password'
      AND consumed_at IS NULL AND revoked_at IS NULL;
    INSERT INTO public.email_challenges
        (account_id, purpose, initiator, target_email, issued_email, issued_auth_version,
         token_digest, created_at, expires_at)
    VALUES (account_row.id, 'reset_password', input_initiator, account_row.email, account_row.email,
            account_row.auth_version, input_token_digest, issued_at, issued_at + interval '30 minutes');
    RETURN account_row.email;
END
$body$;

-- Revoke one issued reset after a known delivery failure.
CREATE FUNCTION public.revoke_password_reset(input_token_digest text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
BEGIN
    UPDATE public.email_challenges SET revoked_at = pg_catalog.clock_timestamp()
    WHERE purpose = 'reset_password' AND token_digest = input_token_digest
      AND consumed_at IS NULL AND revoked_at IS NULL;
END
$body$;

-- Whether a reset is still usable. With lock_account, the account row stays
-- share-locked until the caller's transaction ends, ordering a delivery
-- after or before a concurrent disablement.
CREATE FUNCTION public.password_reset_usable(input_token_digest text, lock_account boolean)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    challenge_row public.email_challenges%ROWTYPE;
    account_row public.accounts%ROWTYPE;
BEGIN
    IF input_token_digest IS NULL OR input_token_digest !~ '^[0-9a-f]{64}$' THEN
        RETURN false;
    END IF;
    SELECT * INTO challenge_row FROM public.email_challenges
    WHERE purpose = 'reset_password' AND token_digest = input_token_digest;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    IF lock_account THEN
        SELECT * INTO account_row FROM public.accounts WHERE id = challenge_row.account_id FOR SHARE;
        SELECT * INTO challenge_row FROM public.email_challenges WHERE id = challenge_row.id;
    ELSE
        SELECT * INTO account_row FROM public.accounts WHERE id = challenge_row.account_id;
    END IF;
    IF account_row.id IS NULL OR challenge_row.id IS NULL THEN
        RETURN false;
    END IF;
    RETURN account_row.is_enabled AND account_row.email_verified_at IS NOT NULL
       AND challenge_row.consumed_at IS NULL AND challenge_row.revoked_at IS NULL
       AND challenge_row.expires_at > pg_catalog.clock_timestamp()
       AND challenge_row.issued_email = account_row.email
       AND challenge_row.target_email = account_row.email
       AND challenge_row.issued_auth_version = account_row.auth_version;
END
$body$;

-- Consume a reset, replace the password, end the account's sessions and
-- revoke its outstanding challenges. Returns the account ID, or NULL.
CREATE FUNCTION public.consume_password_reset(input_token_digest text, input_password_hash text)
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    target_account bigint;
    account_row public.accounts%ROWTYPE;
    challenge_row public.email_challenges%ROWTYPE;
    checked_at timestamptz;
BEGIN
    IF input_token_digest IS NULL OR input_token_digest !~ '^[0-9a-f]{64}$'
       OR input_password_hash IS NULL OR input_password_hash = '' THEN
        RETURN NULL;
    END IF;
    SELECT account_id INTO target_account FROM public.email_challenges
    WHERE purpose = 'reset_password' AND token_digest = input_token_digest;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    -- Account row first, then challenge rows: the order every challenge
    -- operation uses.
    SELECT * INTO account_row FROM public.accounts WHERE id = target_account FOR UPDATE;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    SELECT * INTO challenge_row FROM public.email_challenges
    WHERE purpose = 'reset_password' AND token_digest = input_token_digest FOR UPDATE;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    checked_at := pg_catalog.clock_timestamp();
    IF NOT account_row.is_enabled OR account_row.email_verified_at IS NULL
       OR challenge_row.consumed_at IS NOT NULL OR challenge_row.revoked_at IS NOT NULL
       OR challenge_row.expires_at <= checked_at
       OR challenge_row.issued_email <> account_row.email
       OR challenge_row.target_email <> account_row.email
       OR challenge_row.issued_auth_version <> account_row.auth_version THEN
        RETURN NULL;
    END IF;
    UPDATE public.accounts SET password_hash = input_password_hash,
        auth_version = auth_version + 1, updated_at = checked_at
    WHERE id = account_row.id;
    UPDATE public.email_challenges SET consumed_at = checked_at WHERE id = challenge_row.id;
    UPDATE public.email_challenges SET revoked_at = checked_at
    WHERE account_id = account_row.id AND consumed_at IS NULL AND revoked_at IS NULL;
    RETURN account_row.id;
END
$body$;

-- Trusted host recovery: the same replacement without bearer proof. It never
-- re-enables an account or changes its email, verification or role.
CREATE FUNCTION public.host_reset_password(input_account_id bigint, input_password_hash text)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    account_row public.accounts%ROWTYPE;
    checked_at timestamptz;
BEGIN
    IF input_password_hash IS NULL OR input_password_hash = '' THEN
        RETURN false;
    END IF;
    SELECT * INTO account_row FROM public.accounts WHERE id = input_account_id FOR UPDATE;
    IF NOT FOUND OR NOT account_row.is_enabled THEN
        RETURN false;
    END IF;
    checked_at := pg_catalog.clock_timestamp();
    UPDATE public.accounts SET password_hash = input_password_hash,
        auth_version = auth_version + 1, updated_at = checked_at
    WHERE id = account_row.id;
    UPDATE public.email_challenges SET revoked_at = checked_at
    WHERE account_id = account_row.id AND consumed_at IS NULL AND revoked_at IS NULL;
    RETURN true;
END
$body$;

REVOKE ALL ON FUNCTION public.issue_password_reset(bigint,text,text,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.revoke_password_reset(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.password_reset_usable(text,boolean) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.consume_password_reset(text,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.host_reset_password(bigint,text) FROM PUBLIC;

DO $$
BEGIN
    IF to_regclass('odograph_service.managed_role_state') IS NOT NULL THEN
        ALTER FUNCTION public.issue_password_reset(bigint,text,text,text) OWNER TO odograph_bootstrap;
        ALTER FUNCTION public.revoke_password_reset(text) OWNER TO odograph_bootstrap;
        ALTER FUNCTION public.password_reset_usable(text,boolean) OWNER TO odograph_bootstrap;
        ALTER FUNCTION public.consume_password_reset(text,text) OWNER TO odograph_bootstrap;
        ALTER FUNCTION public.host_reset_password(bigint,text) OWNER TO odograph_bootstrap;
        GRANT EXECUTE ON FUNCTION public.issue_password_reset(bigint,text,text,text) TO odograph_control;
        GRANT EXECUTE ON FUNCTION public.revoke_password_reset(text) TO odograph_control;
        GRANT EXECUTE ON FUNCTION public.password_reset_usable(text,boolean) TO odograph_control;
        GRANT EXECUTE ON FUNCTION public.consume_password_reset(text,text) TO odograph_control;
        GRANT EXECUTE ON FUNCTION public.host_reset_password(bigint,text) TO odograph_control;
    END IF;
END $$;
