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
