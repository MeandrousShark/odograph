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
BEGIN
    SELECT first_account_id INTO established_id
    FROM public.instance_state WHERE id = 1 FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'instance bootstrap state is missing';
    END IF;
    IF established_id IS NOT NULL OR EXISTS (SELECT 1 FROM public.accounts) THEN
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
