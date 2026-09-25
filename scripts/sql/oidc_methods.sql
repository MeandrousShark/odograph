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

CREATE OR REPLACE FUNCTION public.sign_out_account_everywhere(
    input_account_id bigint, input_auth_version bigint
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    checked_at timestamptz := pg_catalog.clock_timestamp();
BEGIN
    PERFORM 1 FROM public.accounts
    WHERE id = input_account_id AND is_enabled AND auth_version = input_auth_version FOR UPDATE;
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
REVOKE ALL ON FUNCTION public.sign_out_account_everywhere(bigint,bigint) FROM PUBLIC;
