CREATE FUNCTION public.sign_out_account_everywhere(
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

DO $$
BEGIN
    IF to_regclass('odograph_service.managed_role_state') IS NOT NULL THEN
        ALTER FUNCTION public.sign_out_account_everywhere(bigint,bigint)
            OWNER TO odograph_bootstrap;
        GRANT EXECUTE ON FUNCTION public.sign_out_account_everywhere(bigint,bigint)
            TO odograph_control;
    END IF;
END $$;
