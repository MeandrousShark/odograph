-- Permit OIDC-only accounts while preserving every valid local password hash.
ALTER TABLE public.accounts ALTER COLUMN password_hash DROP NOT NULL;
UPDATE public.accounts SET password_hash = NULL WHERE password_hash = '';
ALTER TABLE public.accounts ADD CONSTRAINT accounts_password_hash_nonempty
    CHECK (password_hash IS NULL OR password_hash <> '');

CREATE OR REPLACE FUNCTION public.redeem_oidc_member_invitation(
    input_token_digest text, input_issuer text, input_subject text,
    input_provider_email text, input_provider_display_name text, input_display_tz text
) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    invitation_row public.invitations%ROWTYPE;
    issuer_id bigint;
    target_email text;
    member_id bigint;
BEGIN
    IF input_token_digest IS NULL OR input_token_digest !~ '^[0-9a-f]{64}$'
       OR input_issuer IS NULL OR input_issuer = '' OR input_issuer <> pg_catalog.rtrim(input_issuer, '/')
       OR input_subject IS NULL OR input_subject = ''
       OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_timezone_names WHERE name = input_display_tz) THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '22023';
    END IF;
    SELECT issued_by, email INTO issuer_id, target_email FROM public.invitations
    WHERE token_digest = input_token_digest;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '42501';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(901410, pg_catalog.hashtext(target_email));
    PERFORM 1 FROM public.accounts
    WHERE id = issuer_id AND is_admin AND is_enabled FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO invitation_row FROM public.invitations
    WHERE token_digest = input_token_digest FOR UPDATE;
    IF NOT FOUND OR invitation_row.issued_by <> issuer_id
       OR invitation_row.email <> target_email
       OR invitation_row.consumed_at IS NOT NULL OR invitation_row.revoked_at IS NOT NULL
       OR invitation_row.expires_at <= pg_catalog.clock_timestamp() THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '42501';
    END IF;
    INSERT INTO public.accounts (email, password_hash, is_admin)
    VALUES (invitation_row.email, NULL, false)
    RETURNING id INTO member_id;
    INSERT INTO public.oidc_identities
        (account_id, issuer, subject, provider_email, provider_display_name)
    VALUES (member_id, input_issuer, input_subject, input_provider_email, input_provider_display_name);
    INSERT INTO public.account_settings (account_id, display_tz)
    VALUES (member_id, input_display_tz);
    INSERT INTO public.vehicles (account_id, name, is_default)
    VALUES (member_id, 'My Car', true);
    INSERT INTO public.tag_rules (account_id, a_kind, b_kind, category)
    VALUES (member_id, 'home', 'work', 'personal'),
           (member_id, 'work', 'work', 'business');
    INSERT INTO public.mileage_rates (account_id, year, rate_per_mi, rate_h2_per_mi, h2_start_month)
    SELECT member_id, year, rate_per_mi, rate_h2_per_mi, h2_start_month
    FROM public.reference_mileage_rates;
    UPDATE public.invitations SET consumed_at = pg_catalog.clock_timestamp()
    WHERE token_digest = input_token_digest;
    RETURN member_id;
END
$body$;
REVOKE ALL ON FUNCTION public.redeem_oidc_member_invitation(text,text,text,text,text,text) FROM PUBLIC;

DO $$
BEGIN
    IF to_regclass('odograph_service.managed_role_state') IS NOT NULL THEN
        ALTER FUNCTION public.redeem_oidc_member_invitation(text,text,text,text,text,text)
            OWNER TO odograph_bootstrap;
        GRANT SELECT, INSERT, DELETE ON public.oidc_identities TO odograph_bootstrap;
        GRANT USAGE ON SEQUENCE public.oidc_identities_id_seq TO odograph_bootstrap;
        GRANT EXECUTE ON FUNCTION public.redeem_oidc_member_invitation(text,text,text,text,text,text)
            TO odograph_control;
    END IF;
END $$;


CREATE TABLE public.oidc_attempts (
    state_digest text PRIMARY KEY CHECK (state_digest ~ '^[0-9a-f]{64}$'),
    nonce_digest text NOT NULL CHECK (nonce_digest ~ '^[0-9a-f]{64}$'),
    browser_digest text NOT NULL CHECK (browser_digest ~ '^[0-9a-f]{64}$'),
    action text NOT NULL CHECK (action IN ('invite', 'link', 'reauth')),
    account_id bigint REFERENCES public.accounts(id) ON DELETE CASCADE,
    auth_version bigint,
    invitation_digest text CHECK (invitation_digest ~ '^[0-9a-f]{64}$'),
    proof_action text,
    target text NOT NULL,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    UNIQUE (browser_digest, action),
    CHECK (expires_at > created_at),
    CHECK ((action = 'invite') = (invitation_digest IS NOT NULL)),
    CHECK ((action = 'invite') <> (account_id IS NOT NULL)),
    CHECK ((action = 'invite') <> (auth_version IS NOT NULL)),
    CHECK ((action = 'reauth') = (proof_action IS NOT NULL))
);
CREATE TABLE public.oidc_action_proofs (
    browser_digest text NOT NULL CHECK (browser_digest ~ '^[0-9a-f]{64}$'),
    account_id bigint NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
    auth_version bigint NOT NULL,
    action text NOT NULL,
    target text NOT NULL,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL CHECK (expires_at > created_at),
    PRIMARY KEY (browser_digest, action)
);

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
           ('verify_current', 'change_email', 'add_password'))
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
        (input_proof_action = 'add_password' AND input_target <> '')
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

DO $$
BEGIN
    IF to_regclass('odograph_service.managed_role_state') IS NOT NULL THEN
        ALTER TABLE public.oidc_attempts OWNER TO odograph_migrate;
        ALTER TABLE public.oidc_action_proofs OWNER TO odograph_migrate;
        REVOKE ALL ON public.oidc_attempts, public.oidc_action_proofs
            FROM PUBLIC, odograph_control, odograph_runtime, odograph_bootstrap;
        GRANT SELECT, INSERT, UPDATE, DELETE ON public.oidc_attempts, public.oidc_action_proofs
            TO odograph_bootstrap;
        ALTER FUNCTION public.start_oidc_attempt(text,text,text,text,bigint,bigint,text,text,text)
            OWNER TO odograph_bootstrap;
        ALTER FUNCTION public.consume_oidc_attempt(text,text,text,text,bigint,bigint)
            OWNER TO odograph_bootstrap;
        ALTER FUNCTION public.finish_oidc_reauth(text,text,text,bigint,bigint,text,text,timestamptz)
            OWNER TO odograph_bootstrap;
        ALTER FUNCTION public.consume_oidc_action_proof(bigint,bigint,text,text,text)
            OWNER TO odograph_bootstrap;
        GRANT EXECUTE ON FUNCTION public.start_oidc_attempt(text,text,text,text,bigint,bigint,text,text,text),
            public.consume_oidc_attempt(text,text,text,text,bigint,bigint),
            public.finish_oidc_reauth(text,text,text,bigint,bigint,text,text,timestamptz),
            public.consume_oidc_action_proof(bigint,bigint,text,text,text)
            TO odograph_control;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION public.link_oidc_identity(
    input_account_id bigint, input_auth_version bigint, input_issuer text,
    input_subject text, input_provider_email text, input_provider_display_name text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    checked_at timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF input_issuer IS NULL OR input_issuer = '' OR input_issuer <> pg_catalog.rtrim(input_issuer, '/')
       OR input_subject IS NULL OR input_subject = '' THEN
        RETURN false;
    END IF;
    PERFORM 1 FROM public.accounts
    WHERE id = input_account_id AND is_enabled AND auth_version = input_auth_version
      AND password_hash IS NOT NULL FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    INSERT INTO public.oidc_identities
        (account_id, issuer, subject, provider_email, provider_display_name)
    VALUES (input_account_id, input_issuer, input_subject,
            input_provider_email, input_provider_display_name)
    ON CONFLICT DO NOTHING;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    UPDATE public.accounts SET auth_version = auth_version + 1, updated_at = checked_at
    WHERE id = input_account_id;
    UPDATE public.email_challenges SET revoked_at = checked_at
    WHERE account_id = input_account_id AND consumed_at IS NULL AND revoked_at IS NULL;
    DELETE FROM public.oidc_action_proofs WHERE account_id = input_account_id;
    DELETE FROM public.oidc_attempts WHERE account_id = input_account_id;
    RETURN true;
END
$body$;
REVOKE ALL ON FUNCTION public.link_oidc_identity(bigint,bigint,text,text,text,text) FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.unlink_oidc_identity(
    input_account_id bigint, input_auth_version bigint, input_issuer text, input_subject text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    checked_at timestamptz := pg_catalog.clock_timestamp();
BEGIN
    PERFORM 1 FROM public.accounts
    WHERE id = input_account_id AND is_enabled AND auth_version = input_auth_version
      AND password_hash IS NOT NULL FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    DELETE FROM public.oidc_identities
    WHERE account_id = input_account_id AND issuer = input_issuer AND subject = input_subject;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    UPDATE public.accounts SET auth_version = auth_version + 1, updated_at = checked_at
    WHERE id = input_account_id;
    UPDATE public.email_challenges SET revoked_at = checked_at
    WHERE account_id = input_account_id AND consumed_at IS NULL AND revoked_at IS NULL;
    DELETE FROM public.oidc_action_proofs WHERE account_id = input_account_id;
    DELETE FROM public.oidc_attempts WHERE account_id = input_account_id;
    RETURN true;
END
$body$;
REVOKE ALL ON FUNCTION public.unlink_oidc_identity(bigint,bigint,text,text) FROM PUBLIC;

DO $$
BEGIN
    IF to_regclass('odograph_service.managed_role_state') IS NOT NULL THEN
        ALTER FUNCTION public.link_oidc_identity(bigint,bigint,text,text,text,text)
            OWNER TO odograph_bootstrap;
        ALTER FUNCTION public.unlink_oidc_identity(bigint,bigint,text,text)
            OWNER TO odograph_bootstrap;
        GRANT EXECUTE ON FUNCTION public.link_oidc_identity(bigint,bigint,text,text,text,text),
            public.unlink_oidc_identity(bigint,bigint,text,text) TO odograph_control;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION public.replace_account_password(
    input_account_id bigint, input_auth_version bigint, input_password_hash text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    checked_at timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF input_password_hash IS NULL OR input_password_hash = '' THEN
        RETURN false;
    END IF;
    PERFORM 1 FROM public.accounts
    WHERE id = input_account_id AND is_enabled AND auth_version = input_auth_version FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    UPDATE public.accounts SET password_hash = input_password_hash,
        auth_version = auth_version + 1, updated_at = checked_at
    WHERE id = input_account_id;
    UPDATE public.email_challenges SET revoked_at = checked_at
    WHERE account_id = input_account_id AND consumed_at IS NULL AND revoked_at IS NULL;
    DELETE FROM public.oidc_action_proofs WHERE account_id = input_account_id;
    DELETE FROM public.oidc_attempts WHERE account_id = input_account_id;
    RETURN true;
END
$body$;
REVOKE ALL ON FUNCTION public.replace_account_password(bigint,bigint,text) FROM PUBLIC;

DO $$
BEGIN
    IF to_regclass('odograph_service.managed_role_state') IS NOT NULL THEN
        ALTER FUNCTION public.replace_account_password(bigint,bigint,text)
            OWNER TO odograph_bootstrap;
        GRANT EXECUTE ON FUNCTION public.replace_account_password(bigint,bigint,text)
            TO odograph_control;
    END IF;
END $$;
